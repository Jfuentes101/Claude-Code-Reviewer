"""Spend gate. Reviews stop before they eat the budget, and resume by themselves.

Two backends, one question ("may I start another review?"):

- **api**: sum today's recorded cost from SQLite against `budget.daily_usd`. The
  reviewer reports `total_cost_usd` per run, so this is measured, not estimated.
- **oauth**: read the plan's 5-hour window and pause at `budget.stop_pct`, so
  robbie can't eat a human's own headroom on a shared plan.

An unreadable budget is never treated as "unlimited": it warns once and keeps
going, so a broken metrics endpoint degrades loudly instead of silently.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from robbie.config import Config, Secrets
from robbie.db import Db

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TTL_S = 60  # a tick asks once per reviewable PR, and the endpoint rate-limits
USAGE_STALE_S = 900  # how old a reading may be before a failed read gives up on it


@dataclass
class _Window:
    """The last thing known about the plan's five-hour window.

    Kept across calls because the gate is asked once per reviewable PR: reading
    the endpoint each time earned a 429, and an unreadable window runs unguarded.
    A failure is remembered too — retrying a rate limit per PR is how it stays one.
    """

    at: float = 0.0  # monotonic; 0 means never read
    pct: float = 0.0
    resets: str = ""
    quiet_until: float = 0.0
    why: str = "not read yet"


_window = _Window()


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    detail: str
    # set once per pause window so the operator is told, but only once
    notice_key: str | None = None


def check(cfg: Config, secrets: Secrets, db: Db, inflight: int = 0) -> Verdict:
    """May another review start, given `inflight` of them already running?

    Both backends read what has already been *spent*, which is the wrong quantity
    on its own: a container that is halfway through a review has spent nothing
    yet and will spend plenty. So each review in flight, plus the one asking,
    holds back a reserve. Without it a fleet all reads the same safe number at
    once and starts together — the failure the cutoff exists to prevent.
    """
    if cfg.backend == "api":
        return _check_api(cfg, db, inflight)
    return _check_oauth(cfg, secrets, inflight)


def _check_api(cfg: Config, db: Db, inflight: int) -> Verdict:
    spent = db.spend_since(_midnight_ms())
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


def _check_oauth(cfg: Config, secrets: Secrets, inflight: int) -> Verdict:
    """ponytail: undocumented endpoint, so it can change under us. A read failure
    must be reported as unknown, never as "plenty left".

    ponytail: a blocking read on the event loop, once a minute at most against
    ticks of ten minutes. Make it async if a tick ever waits on it.
    """
    now = time.monotonic()
    if now - _window.at >= USAGE_TTL_S and now >= _window.quiet_until:
        try:
            _window.pct, _window.resets = _fetch_usage(secrets)
            _window.at, _window.why = now, ""
        except Exception as ex:  # noqa: BLE001 — any failure is "unknown", handled below
            _window.quiet_until, _window.why = now + USAGE_TTL_S, str(ex)
            logger.warning("plan usage unreadable: %s", ex)

    if not _window.at or now - _window.at >= USAGE_STALE_S:
        return Verdict(
            True,
            f"usage unreadable ({_window.why}); running unguarded",
            notice_key="budget:unreadable",
        )
    pct, held = _window.pct, (inflight + 1) * cfg.budget.reserve_pct
    if pct + held <= cfg.budget.stop_pct:
        return Verdict(True, f"5h window at {pct:.0f}% (+{held:.0f}% held back)")
    return Verdict(
        False,
        f"5h window at {pct:.0f}% +{held:.0f}% held for {inflight} running + 1 "
        f"(cutoff {cfg.budget.stop_pct}%); resumes around {_window.resets}",
        # resets_at jitters by ~1s between calls, so key on the rounded minute
        notice_key=f"budget:{_minute_key(_window.resets)}",
    )


def _fetch_usage(secrets: Secrets) -> tuple[float, str]:
    creds = json.loads(secrets.claude_credentials.read_text())  # type: ignore[union-attr]
    resp = httpx.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['claudeAiOauth']['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["five_hour"]["utilization"]), str(data["five_hour"]["resets_at"])


def _midnight_ms() -> int:
    now = datetime.now(UTC)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _minute_key(iso: str) -> str:
    try:
        return f"{datetime.fromisoformat(iso.replace('Z', '+00:00')):%Y-%m-%dT%H:%M}"
    except ValueError:
        return str(int(time.time() // 60))
