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
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from robbie.config import Config, Secrets
from robbie.db import Db

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    detail: str
    # set once per pause window so the operator is told, but only once
    notice_key: str | None = None


def check(cfg: Config, secrets: Secrets, db: Db) -> Verdict:
    if cfg.backend == "api":
        return _check_api(cfg, db)
    return _check_oauth(cfg, secrets)


def _check_api(cfg: Config, db: Db) -> Verdict:
    spent = db.spend_since(_midnight_ms())
    limit = cfg.budget.daily_usd
    if spent < limit:
        return Verdict(True, f"${spent:.2f} of ${limit:.2f} spent today")
    return Verdict(
        False,
        f"daily budget reached: ${spent:.2f} of ${limit:.2f}",
        notice_key=f"budget:{datetime.now(UTC):%Y-%m-%d}",
    )


def _check_oauth(cfg: Config, secrets: Secrets) -> Verdict:
    """ponytail: undocumented endpoint, so it can change under us. A read failure
    must be reported as unknown, never as "plenty left"."""
    try:
        creds = json.loads(secrets.claude_credentials.read_text())  # type: ignore[union-attr]
        token = creds["claudeAiOauth"]["accessToken"]
        raw = subprocess.run(
            ["curl", "-sS", "--max-time", "15",
             "-H", f"Authorization: Bearer {token}",
             "-H", "anthropic-beta: oauth-2025-04-20", USAGE_URL],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout
        data = json.loads(raw)
        pct = float(data["five_hour"]["utilization"])
        resets = str(data["five_hour"]["resets_at"])
    except Exception as ex:  # noqa: BLE001 — any failure is "unknown", handled below
        logger.warning("plan usage unreadable, running without the guard: %s", ex)
        return Verdict(True, f"usage unreadable ({ex}); running unguarded",
                       notice_key="budget:unreadable")
    if pct < cfg.budget.stop_pct:
        return Verdict(True, f"5h window at {pct:.0f}%")
    return Verdict(
        False,
        f"5h window at {pct:.0f}% (cutoff {cfg.budget.stop_pct}%); resumes around {resets}",
        # resets_at jitters by ~1s between calls, so key on the rounded minute
        notice_key=f"budget:{_minute_key(resets)}",
    )


def _midnight_ms() -> int:
    now = datetime.now(UTC)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _minute_key(iso: str) -> str:
    try:
        return f"{datetime.fromisoformat(iso.replace('Z', '+00:00')):%Y-%m-%dT%H:%M}"
    except ValueError:
        return str(int(time.time() // 60))
