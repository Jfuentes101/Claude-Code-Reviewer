"""Spend gate. Reviews stop before they eat the budget, and resume by themselves.

Two backends, one question ("may I start another review?"):

- **api**: sum today's recorded cost from SQLite against `budget.daily_usd`. The
  reviewer reports `total_cost_usd` per run, so this is measured, not estimated.
- **oauth**: read the plan's 5-hour window and pause at `budget.stop_pct`, so
  robbie can't eat a human's own headroom on a shared plan.

An unreadable budget is never treated as "unlimited": it warns once and keeps
going, so a broken metrics endpoint degrades loudly instead of silently.

Nothing here calls a provider except `poll`. It runs on its own clock in the
daemon and leaves the numbers in SQLite; the gate, the panel and any second
process read that row. The meters rate-limit, and robbie is more than one
process, so how often they are asked cannot be left to how often they are read.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from robbie.config import Config, Secrets
from robbie.db import Db, now_ms

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# in the notice key of any verdict that was allowed without a meter behind it
UNREADABLE = "unreadable"
USAGE_STALE_S = 900  # how old the poller's number may be before the gate drops it

PLAN = "plan"  # the account's five-hour window, backend=oauth
ENDPOINT = "endpoint"  # REVIEW_BASE_URL's own limits


def poll(cfg: Config, secrets: Secrets, db: Db) -> None:
    """Read every meter this deployment has and store what came back.

    The only caller of a provider in the whole process tree. Blocking, so async
    callers hand it to a thread.

    A failure keeps the last number: `USAGE_STALE_S` over the poll interval is how
    many consecutive failures the gate tolerates before it says it is blind. That
    matters most for the account's meter, which rate-limits and answers 429 to a
    burst it would have served one at a time.
    """
    for name, fetch in (
        (PLAN, lambda: _fetch_plan(secrets)),
        (ENDPOINT, lambda: _fetch_endpoint(secrets)),
    ):
        if not _wanted(name, cfg, secrets):
            continue
        try:
            pct, note = fetch()
        except Exception as ex:  # noqa: BLE001 — any failure is "unknown", handled below
            db.meter_failed(name, str(ex))
            logger.warning("%s usage unreadable: %s", name, ex)
        else:
            db.write_meter(name, pct=pct, note=note)
            logger.debug("%s meter at %.1f%% (%s)", name, pct, note)


def _wanted(name: str, cfg: Config, secrets: Secrets) -> bool:
    if name == PLAN:
        return cfg.backend == "oauth" and secrets.claude_credentials is not None
    return bool(secrets.review_base_url)


def _stored(db: Db, name: str) -> tuple[float, str] | str:
    """The poller's number for `name`, or a sentence saying why there isn't one."""
    row = db.read_meter(name)
    if row is None or not row["read_at"]:
        return (row["error"] if row and row["error"] else "not polled yet")
    age_s = (now_ms() - int(row["read_at"])) / 1000
    if age_s > USAGE_STALE_S:
        return row["error"] or f"last reading is {age_s / 60:.0f}m old"
    return float(row["pct"]), str(row["note"])


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    detail: str
    # set once per pause window so the operator is told, but only once
    notice_key: str | None = None


def check(cfg: Config, db: Db, inflight: int = 0, *, via_endpoint: bool = False) -> Verdict:
    """May another review start, given `inflight` of them already running?

    Every meter reads what has already been *spent*, and a container halfway through
    a review has spent nothing yet and will spend plenty — so each one in flight,
    plus the one asking, holds back a reserve.

    Which meter follows where the run will be billed, not `backend`. No credentials
    and no network: whatever `poll` last stored is the answer.
    """
    if via_endpoint:
        return _check_endpoint(cfg, db, inflight)
    if cfg.backend == "api":
        return _check_api(cfg, db, inflight)
    return _check_oauth(cfg, db, inflight)


def _check_api(cfg: Config, db: Db, inflight: int) -> Verdict:
    spent = db.spend_since(midnight_ms(), cfg.endpoint_models)
    limit = cfg.budget.daily_usd
    held = (inflight + 1) * cfg.budget.reserve_usd
    if spent + held <= limit:
        return Verdict(True, f"${spent:.2f} of ${limit:.2f} spent today")
    return Verdict(
        False,
        f"daily budget reached: ${spent:.2f} of ${limit:.2f}"
        + (f", ${held:.2f} held for {inflight} running + 1" if inflight or held else ""),
        notice_key=f"budget:{datetime.now(UTC):%Y-%m-%d}",
    )


def _check_oauth(cfg: Config, db: Db, inflight: int) -> Verdict:
    """ponytail: undocumented endpoint, so it can change under us. A read failure
    must be reported as unknown, never as "plenty left".
    """
    reading = _stored(db, PLAN)
    if isinstance(reading, str):
        return Verdict(
            True,
            f"usage unreadable ({reading}); running unguarded",
            # dated, like every other pause key: a constant one is announced once
            # in the life of the database and every later outage is silent
            notice_key=f"budget:unreadable:{datetime.now(UTC):%Y-%m-%d}",
        )
    (pct, resets_at), held = reading, (inflight + 1) * cfg.budget.reserve_pct
    if pct + held <= cfg.budget.stop_pct:
        return Verdict(True, f"5h window at {pct:.0f}% (+{held:.0f}% held back)")
    return Verdict(
        False,
        f"5h window at {pct:.0f}% +{held:.0f}% held for {inflight} running + 1 "
        f"(cutoff {cfg.budget.stop_pct}%); resumes around {resets_at}",
        # resets_at jitters by ~1s between calls, so key on the rounded minute
        notice_key=f"budget:{_minute_key(resets_at)}",
    )


def _check_endpoint(cfg: Config, db: Db, inflight: int) -> Verdict:
    """The review endpoint's own limits, which the account's meters know nothing of.

    Gated on `limits.*.usage` and not on `activity.cost`, which only fills in after
    a review has been paid for. The worse of session and weekly wins.
    """
    reading = _stored(db, ENDPOINT)
    if isinstance(reading, str):
        return Verdict(
            True,
            f"endpoint usage unreadable ({reading}); running unguarded",
            notice_key=f"budget:endpoint-unreadable:{datetime.now(UTC):%Y-%m-%d}",
        )
    (pct, note), held = reading, (inflight + 1) * cfg.budget.endpoint_reserve_pct
    if pct + held <= cfg.budget.endpoint_stop_pct:
        return Verdict(True, f"endpoint at {pct:.1f}% ({note}, +{held:.0f}% held)")
    return Verdict(
        False,
        f"endpoint limit reached: {pct:.1f}% +{held:.0f}% held for {inflight} running + 1 "
        f"(cutoff {cfg.budget.endpoint_stop_pct}%; {note})",
        notice_key=f"budget:endpoint:{datetime.now(UTC):%Y-%m-%dT%H}",
    )


def _oauth(secrets: Secrets) -> dict:
    return json.loads(secrets.claude_credentials.read_text())["claudeAiOauth"]  # type: ignore[union-attr]


def _read_usage(token: str) -> tuple[float, str]:
    resp = httpx.get(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["five_hour"]["utilization"]), str(data["five_hour"]["resets_at"])


def _fetch_plan(secrets: Secrets) -> tuple[float, str]:
    """Read the account's five-hour window.

    Nothing here refreshes the access token — the host's own CLI rewrites the file when it
    rotates one, and re-reading is how that arrives. The rotation is therefore a live race:
    read the file, the CLI replaces it, and the token in hand is already dead. That 401 is
    not a broken meter, and treating it as one unguards the gate for a whole quiet window
    every few hours. So re-read once, and only give up if the file really has not moved on.
    """
    oauth = _oauth(secrets)
    try:
        return _read_usage(oauth["accessToken"])
    except httpx.HTTPStatusError as ex:
        if ex.response.status_code != 401:
            raise
        fresh = _oauth(secrets)
        if fresh["accessToken"] != oauth["accessToken"]:
            return _read_usage(fresh["accessToken"])
        stale_for = time.time() - float(fresh.get("expiresAt", 0)) / 1000
        if stale_for > 0:
            raise RuntimeError(
                f"the account's access token expired {stale_for / 60:.0f} min ago and nothing "
                "here refreshes it; run `claude` on the host to renew it"
            ) from ex
        raise


def _fetch_endpoint(secrets: Secrets) -> tuple[float, str]:
    resp = httpx.get(
        f"{(secrets.review_base_url or '').rstrip('/')}/api/usage",
        headers={
            "Authorization": "Bearer "
            + (secrets.review_api_token.get_secret_value() if secrets.review_api_token else "")
        },
        timeout=15,
    )
    resp.raise_for_status()
    limits = resp.json().get("limits") or {}
    shares = {
        name: float((limits.get(name) or {}).get("usage") or 0.0)
        for name in ("session", "weekly")
    }
    worst = max(shares.values(), default=0.0)
    if worst > 1.0:
        # a share is meant to be 0..1; anything else means this field is not that
        logger.warning("endpoint usage %r is not a 0-1 share; the gate may be wrong", shares)
    note = " · ".join(f"{name} {share * 100:.1f}%" for name, share in shares.items())
    return worst * 100, note


def midnight_ms() -> int:
    now = datetime.now(UTC)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _minute_key(iso: str) -> str:
    try:
        return f"{datetime.fromisoformat(iso.replace('Z', '+00:00')):%Y-%m-%dT%H:%M}"
    except ValueError:
        return str(int(time.time() // 60))
