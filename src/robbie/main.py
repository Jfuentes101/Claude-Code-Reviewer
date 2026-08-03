"""CLI and process lifecycle.

    robbie poll                     the daemon: a tick every poll_interval_s
    robbie poll --once              a single tick, for cron-style deployments
    robbie once --repo R --pr N     force one review, ignoring queue and gates
    robbie status                   what's reviewed, held, or waiting
    robbie digest [--days 7]        post the stuck-in-review digest

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

from robbie import config as configmod
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

    one = sub.add_parser("once", help="review a single PR now")
    one.add_argument("--repo", required=True)
    one.add_argument("--pr", required=True, type=int)

    sub.add_parser("status", help="show the queue")

    dig = sub.add_parser("digest", help="post the stuck-in-review digest")
    dig.add_argument("--days", type=int, default=None)
    return ap


async def _run(args: argparse.Namespace) -> int:
    cfg = configmod.load(args.config)
    secrets = configmod.load_secrets(cfg)
    db = Db(cfg.db_path)
    slack = Slack(
        token=secrets.slack_bot_token,
        owner_id=cfg.slack.owner_id,
        users_file=cfg.slack.users_file,
        dry_run=args.dry_run or args.no_publish,
    )
    orch = Orchestrator(
        cfg, secrets, db, slack, dry_run=args.dry_run, no_publish=args.no_publish
    )

    try:
        if args.command == "digest":
            listed = await post_digest(cfg, slack, days=args.days)
            logger.info("digest done: %d PR(s) listed", listed)
            return 0

        if args.command == "status":
            for row in await orch.status():
                print(row)
            return 0

        if args.command == "once":
            outcome = await orch.review_one(args.repo, args.pr)
            logger.info("%s", outcome)
            return 0 if outcome.action != "failed" else 1

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
        try:
            for outcome in await orch.poll_once():
                logger.info("%s", outcome)
        except Exception:  # noqa: BLE001 — a bad tick must not end the daemon
            logger.exception("tick failed; continuing")
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
