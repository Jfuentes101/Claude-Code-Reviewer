"""The spend gate, and the reserve that makes it true for a fleet.

Both backends can only read what has already been *spent*. A container halfway
through a review has spent nothing yet, so a gate that reads the raw number lets
every free slot start at the same safe reading and blow through the cutoff
together. Each review in flight, plus the one asking, holds back a reserve.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from robbie import budget
from robbie.config import BudgetConfig, Config, RepoConfig, Secrets, SlackConfig
from robbie.db import Db


def cfg(tmp_path: Path, backend: str = "oauth", **kw) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"),
        repos=[RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path)],
        state_dir=tmp_path,
        backend=backend,
        budget=BudgetConfig(**kw),
    )


@pytest.fixture
def db(tmp_path) -> Db:
    return Db(tmp_path / "robbie.db")


def secrets(tmp_path: Path) -> Secrets:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    return Secrets(
        gh_token="w", slack_bot_token="s", reviewer_gh_token="r", claude_credentials=creds
    )


def usage(monkeypatch, pct: float) -> None:
    payload = json.dumps({"five_hour": {"utilization": pct, "resets_at": "2026-08-04T19:10:00Z"}})
    monkeypatch.setattr(
        budget.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=payload, stderr=""),
    )


# ----- oauth: percent of the five-hour window ----------------------------


def test_room_for_one_review_is_room_enough(tmp_path, db, monkeypatch):
    usage(monkeypatch, 70)
    v = budget.check(cfg(tmp_path, stop_pct=90, reserve_pct=8), secrets(tmp_path), db)
    assert v.allowed


def test_a_reading_under_the_cutoff_still_refuses_without_room_to_finish(
    tmp_path, db, monkeypatch
):
    """85% passes the old test and lands at 93%. That is the bug the reserve fixes."""
    usage(monkeypatch, 85)
    v = budget.check(cfg(tmp_path, stop_pct=90, reserve_pct=8), secrets(tmp_path), db)
    assert not v.allowed
    assert "+8% held" in v.detail


def test_each_running_review_holds_back_its_own_share(tmp_path, db, monkeypatch):
    usage(monkeypatch, 70)
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    ask = lambda n: budget.check(conf, secrets(tmp_path), db, inflight=n).allowed  # noqa: E731
    assert ask(1), "one running plus this one is 16%, and 86% clears the cutoff"
    assert not ask(2), "two running plus this one is 24%, and 94% does not"


def test_five_agents_cannot_all_start_on_the_same_safe_reading(tmp_path, db, monkeypatch):
    usage(monkeypatch, 50)
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    admitted = 0
    while budget.check(conf, secrets(tmp_path), db, inflight=admitted).allowed:
        admitted += 1
    assert admitted == 5, "50% + 5 reviews at 8% each is the whole cutoff"


def test_an_unreadable_window_still_runs_but_says_so(tmp_path, db, monkeypatch):
    monkeypatch.setattr(budget.subprocess, "run", lambda *a, **k: 1 / 0)
    v = budget.check(cfg(tmp_path), secrets(tmp_path), db)
    assert v.allowed and v.notice_key == "budget:unreadable"


# ----- api: dollars ------------------------------------------------------


def test_dollars_reserve_the_same_way(tmp_path, db):
    conf = cfg(tmp_path, backend="api", daily_usd=20.0, reserve_usd=5.0)
    assert budget.check(conf, secrets(tmp_path), db, inflight=3).allowed, "$20 covers four"
    assert not budget.check(conf, secrets(tmp_path), db, inflight=4).allowed, "not five"
