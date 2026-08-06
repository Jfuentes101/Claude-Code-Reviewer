"""CLI and process lifecycle.

    robbie poll                     the daemon: a tick every poll_interval_s
    robbie poll --once              a single tick, for cron-style deployments
    robbie once --repo R --pr N     force one review, ignoring queue and gates
    robbie status                   what's reviewed, held, or waiting
    robbie digest [--days 7]        post the stuck-in-review digest
    robbie dashboard [--port 4020]  read-only metrics panel, off unless asked for

SIGTERM finishes the tick in flight rather than killing a review halfway
through, so `docker compose down` needs a stop_grace_period longer than
docker.timeout_s (the compose file sets one).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time

from robbie import config as configmod
from robbie import dashboard
from robbie.db import Db
from robbie.digest import post_digest
from robbie.orchestrator import Orchestrator
from robbie.slack import Slack

logger = logging.getLogger("robbie")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("ROBBIE_LOG_FORMAT", "console") == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("ROBBIE_LOG_LEVEL", "INFO").upper())


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="robbie")
    ap.add_argument("--config", default=None, help="path to robbie.yaml")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, write nothing")
    ap.add_argument(
        "--no-publish", action="store_true",
        help="run the review for real, then write nothing to GitHub or Slack",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="run the review loop")
    poll.add_argument("--once", action="store_true", help="one tick, then exit")

    one = sub.add_parser("once", help="review specific PRs now, ignoring the gates")
    one.add_argument("--repo", required=True)
    # several in one process so they share the concurrency cap
    one.add_argument("--pr", required=True, type=int, nargs="+")
    one.add_argument(
        "--model", default=None,
        help="review with this model instead of the account default, through "
             "REVIEW_BASE_URL when set (for comparing models on one harness)",
    )

    sub.add_parser("status", help="show the queue")

    th = sub.add_parser("threads", help="act on replies to my own review threads")
    th.add_argument("--repo", default=None)
    th.add_argument("--pr", type=int, nargs="+", default=[], help="only these PRs")

    dig = sub.add_parser("digest", help="post the stuck-in-review digest")
    dig.add_argument("--days", type=int, default=None)

    dash = sub.add_parser("dashboard", help="serve the read-only metrics panel")
    dash.add_argument("--port", type=int, default=4020)
    # localhost by default: the panel has no auth and shows diffs, titles and spend
    dash.add_argument("--host", default="127.0.0.1")
    return ap


def why_no_model(
    cfg: configmod.Config, secrets: configmod.Secrets, model: str | None
) -> str | None:
    """Why `--model` cannot run, if it cannot.

    Only an arm the config routes to REVIEW_BASE_URL needs that endpoint's own auth.
    A tag nobody configured runs on the account's own backend, where the account's
    own credentials are the right ones and an unknown name fails cleanly.
    """
    if not model or not cfg.named_model(model).via_endpoint:
        return None
    if secrets.review_base_url and secrets.review_api_token:
        return None
    return (
        f"--model {model} is a `via: endpoint` arm in this config, so it needs "
        "REVIEW_BASE_URL and REVIEW_API_TOKEN in the env"
    )


async def _run(args: argparse.Namespace) -> int:
    cfg = configmod.load(args.config)
    secrets = configmod.load_secrets(cfg)

    # before the Db: the panel reads through its own read-only connections, and
    # opening a writable one here would migrate the schema and hold it open for
    # the life of a process that serves forever
    if args.command == "dashboard":
        dashboard.serve(cfg, secrets, host=args.host, port=args.port)
        return 0

    db = Db(cfg.db_path)
    slack = Slack(
        token=secrets.slack_bot_token,
        owner_id=cfg.slack.owner_id,
        users_file=cfg.slack.users_file,
        approved_ids=tuple(cfg.slack.approved_ids),
        dry_run=args.dry_run or args.no_publish,
    )
    model = getattr(args, "model", None)
    if (why := why_no_model(cfg, secrets, model)) is not None:
        raise SystemExit(why)
    orch = Orchestrator(
        cfg, secrets, db, slack, dry_run=args.dry_run, no_publish=args.no_publish,
        model=model,
    )

    try:
        if args.command == "digest":
            listed = await post_digest(cfg, slack, days=args.days)
            logger.info("digest done: %d PR(s) listed", listed)
            return 0

        if args.command == "threads":
            outcomes = await orch.answer_threads(args.repo, tuple(args.pr))
            for outcome in outcomes:
                logger.info("%s", outcome)
            if not outcomes:
                logger.info("no threads of mine are waiting on me")
            return 0

        if args.command == "status":
            for row in await orch.status():
                print(row)
            return 0

        if args.command == "once":
            outcomes = await asyncio.gather(
                *(orch.review_one(args.repo, pr) for pr in args.pr),
                return_exceptions=True,
            )
            failed = 0
            for pr, outcome in zip(args.pr, outcomes, strict=True):
                if isinstance(outcome, Exception):
                    logger.error("%s#%s crashed: %s", args.repo, pr, outcome)
                    failed += 1
                    continue
                logger.info("%s", outcome)
                failed += outcome.action == "failed"
            return 1 if failed else 0

        orphaned = db.reap_running()
        if orphaned:
            logger.warning("marked %d orphaned running review(s) failed at boot", orphaned)

        if args.once:
            for outcome in await orch.poll_once():
                logger.info("%s", outcome)
            return 0

        return await _loop(cfg, orch)
    finally:
        db.close()


async def _loop(cfg: configmod.Config, orch: Orchestrator) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    logger.info(
        "robbie up: %d repo(s), backend=%s, %d concurrent review(s), tick %ds",
        len(cfg.repos), cfg.backend, cfg.max_concurrent_reviews, cfg.poll_interval_s,
    )
    while not stop.is_set():
        started = time.monotonic()
        try:
            for outcome in await orch.poll_once():
                logger.info("%s", outcome)
        except Exception:  # noqa: BLE001 — a bad tick must not end the daemon
            logger.exception("tick failed; continuing")
        # Ticks cannot overlap — the wait below starts after this one returns — so
        # outgrowing the interval costs drift rather than a pile-up, and nothing
        # else would ever say so. It is the signal that the queue has outgrown
        # polling and wants webhooks.
        elapsed = time.monotonic() - started
        if elapsed > cfg.poll_interval_s:
            logger.warning(
                "tick took %.0fs, longer than the %ds interval; reviews are running "
                "behind the queue", elapsed, cfg.poll_interval_s,
            )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval_s)
    logger.info("shutdown signal received; stopping after this tick")
    return 0


def main() -> None:
    setup_logging()
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
