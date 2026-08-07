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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import httpx

from robbie.config import Config, Secrets
from robbie.db import Db

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# in the notice key of any verdict that was allowed without a meter behind it
UNREADABLE = "unreadable"
USAGE_TTL_S = 60  # a tick asks once per reviewable PR, and the endpoint rate-limits
USAGE_STALE_S = 900  # how old a reading may be before a failed read gives up on it


@dataclass(frozen=True)
class _Reading:
    at: float = 0.0  # monotonic; 0 means never read
    pct: float = 0.0
    note: str = ""
    quiet_until: float = 0.0
    why: str = "not read yet"

    @property
    def usable(self) -> bool:
        """On the reading, not the window: re-deriving it after taking one could
        pair a stale verdict with a fresh number."""
        return bool(self.at) and time.monotonic() - self.at < USAGE_STALE_S


class _Window:
    """The last thing known about one meter, kept across calls.

    The gate is asked once per reviewable PR, and a fetch per ask earns a 429 —
    after which the meter is unreadable and reviews run unguarded. So a reading is
    held briefly, a failure is held the same way, and a failed read keeps using the
    last number while it is worth anything.

    Rebound in one assignment, never mutated field by field: the dashboard asks
    from several threads, and a torn read pairs a fresh percentage with a stale
    reset time.
    """

    def __init__(self) -> None:
        self.now = _Reading()

    def refresh(self, fetch: Callable[[], tuple[float, str]]) -> None:
        was = self.now
        now = time.monotonic()
        # `was.at` of 0 means never read, not read at monotonic 0 — and monotonic
        # counts from boot, so on a host in its first minute of uptime dropping
        # this guard reads "fresh enough", never fetches, and leaves every review
        # in that window unmeasured.
        if (was.at and now - was.at < USAGE_TTL_S) or now < was.quiet_until:
            return
        try:
            pct, note = fetch()
        except Exception as ex:  # noqa: BLE001 — any failure is "unknown", handled below
            self.now = replace(was, quiet_until=now + USAGE_TTL_S, why=str(ex))
            logger.warning("usage unreadable: %s", ex)
            return
        self.now = _Reading(at=now, pct=pct, note=note)


_plan = _Window()  # the account's five-hour window, backend=oauth
_endpoint = _Window()  # REVIEW_BASE_URL's own limits


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    detail: str
    # set once per pause window so the operator is told, but only once
    notice_key: str | None = None


def check(
    cfg: Config, secrets: Secrets, db: Db, inflight: int = 0, *, via_endpoint: bool = False
) -> Verdict:
    """May another review start, given `inflight` of them already running?

    Every meter reads what has already been *spent*, and a container halfway through
    a review has spent nothing yet and will spend plenty — so each one in flight,
    plus the one asking, holds back a reserve.

    Which meter follows where the run will be billed, not `backend`.
    """
    if via_endpoint:
        return _check_endpoint(cfg, secrets, inflight)
    if cfg.backend == "api":
        return _check_api(cfg, db, inflight)
    return _check_oauth(cfg, secrets, inflight)


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


def _check_oauth(cfg: Config, secrets: Secrets, inflight: int) -> Verdict:
    """ponytail: undocumented endpoint, so it can change under us. A read failure
    must be reported as unknown, never as "plenty left".

    The read blocks, which is why every async caller runs `check` in a thread.
    """
    _plan.refresh(lambda: _fetch_plan(secrets))
    reading = _plan.now
    if not reading.usable:
        return Verdict(
            True,
            f"usage unreadable ({reading.why}); running unguarded",
            # dated, like every other pause key: a constant one is announced once
            # in the life of the database and every later outage is silent
            notice_key=f"budget:unreadable:{datetime.now(UTC):%Y-%m-%d}",
        )
    pct, held = reading.pct, (inflight + 1) * cfg.budget.reserve_pct
    if pct + held <= cfg.budget.stop_pct:
        return Verdict(True, f"5h window at {pct:.0f}% (+{held:.0f}% held back)")
    return Verdict(
        False,
        f"5h window at {pct:.0f}% +{held:.0f}% held for {inflight} running + 1 "
        f"(cutoff {cfg.budget.stop_pct}%); resumes around {reading.note}",
        # resets_at jitters by ~1s between calls, so key on the rounded minute
        notice_key=f"budget:{_minute_key(reading.note)}",
    )


def _check_endpoint(cfg: Config, secrets: Secrets, inflight: int) -> Verdict:
    """The review endpoint's own limits, which the account's meters know nothing of.

    Gated on `limits.*.usage` and not on `activity.cost`, which only fills in after
    a review has been paid for. The worse of session and weekly wins.
    """
    _endpoint.refresh(lambda: _fetch_endpoint(secrets))
    reading = _endpoint.now
    if not reading.usable:
        return Verdict(
            True,
            f"endpoint usage unreadable ({reading.why}); running unguarded",
            notice_key=f"budget:endpoint-unreadable:{datetime.now(UTC):%Y-%m-%d}",
        )
    pct, held = reading.pct, (inflight + 1) * cfg.budget.endpoint_reserve_pct
    if pct + held <= cfg.budget.endpoint_stop_pct:
        return Verdict(True, f"endpoint at {pct:.1f}% ({reading.note}, +{held:.0f}% held)")
    return Verdict(
        False,
        f"endpoint limit reached: {pct:.1f}% +{held:.0f}% held for {inflight} running + 1 "
        f"(cutoff {cfg.budget.endpoint_stop_pct}%; {reading.note})",
        notice_key=f"budget:endpoint:{datetime.now(UTC):%Y-%m-%dT%H}",
    )


def _fetch_plan(secrets: Secrets) -> tuple[float, str]:
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
