"""The spend gate, and the reserve that makes it true for a fleet.

Both backends can only read what has already been *spent*. A container halfway
through a review has spent nothing yet, so a gate that reads the raw number lets
every free slot start at the same safe reading and blow through the cutoff
together. Each review in flight, plus the one asking, holds back a reserve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robbie import budget
from robbie.config import BudgetConfig, Config, RepoConfig, ReviewModel, Secrets, SlackConfig
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


@pytest.fixture(autouse=True)
def _no_reading_carried_between_tests(monkeypatch):
    monkeypatch.setattr(budget, "_plan", budget._Window())
    monkeypatch.setattr(budget, "_endpoint", budget._Window())


def secrets(tmp_path: Path) -> Secrets:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    return Secrets(
        gh_token="w", slack_bot_token="s", reviewer_gh_token="r", claude_credentials=creds
    )


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def usage(monkeypatch, pct: float, *, session: float = 0.0, weekly: float = 0.0) -> None:
    """Both meters answer, dispatched on the URL like the real ones are."""
    plan = {"five_hour": {"utilization": pct, "resets_at": "2026-08-04T19:10:00Z"}}
    endpoint = {"limits": {"session": {"usage": session}, "weekly": {"usage": weekly}}}
    monkeypatch.setattr(
        budget.httpx, "get",
        lambda url, **k: FakeResponse(endpoint if "/api/usage" in url else plan),
    )


def endpoint_cfg(tmp_path, **kw):
    conf = cfg(tmp_path, **kw)
    conf.review_models = [
        ReviewModel(model="glm-5.2:cloud", via="endpoint", weight=2),
        ReviewModel(model="sonnet", weight=1),
    ]
    return conf


# ----- oauth: percent of the five-hour window ----------------------------


def test_the_token_never_reaches_a_command_line(tmp_path, db, monkeypatch):
    """It used to ride in a curl argv, which /proc hands to anyone on the host."""
    seen: dict = {}

    def fake_get(url, **kw):
        seen.update(url=url, headers=kw.get("headers", {}))
        return FakeResponse({"five_hour": {"utilization": 10, "resets_at": "x"}})

    monkeypatch.setattr(budget.httpx, "get", fake_get)
    assert budget.check(cfg(tmp_path), secrets(tmp_path), db).allowed
    assert seen["url"] == budget.USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer t"


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
    """A cold start with nothing to go on is the only case that goes unguarded."""
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    v = budget.check(cfg(tmp_path), secrets(tmp_path), db)
    assert v.allowed and v.notice_key == "budget:unreadable"


def test_the_window_is_read_once_for_a_whole_tick(tmp_path, db, monkeypatch):
    """The gate is asked once per reviewable PR, and the endpoint 429s on a burst.

    Six reads two seconds apart earned one on the real endpoint, and the fallback
    for an unreadable window is to run without the guard at all.
    """
    reads = []
    payload = {"five_hour": {"utilization": 10, "resets_at": "x"}}

    def counting(*a, **k):
        reads.append(1)
        return FakeResponse(payload)

    monkeypatch.setattr(budget.httpx, "get", counting)
    conf = cfg(tmp_path)
    for _ in range(6):
        assert budget.check(conf, secrets(tmp_path), db).allowed
    assert len(reads) == 1


def test_a_rate_limited_endpoint_is_not_asked_again_for_every_pr(tmp_path, db, monkeypatch):
    """Retrying a 429 once per reviewable PR is how it stays a 429.

    Observed live: seven requests in one tick, every one of them rate-limited, and
    every one of them logged as running without the guard.
    """
    reads = []

    def boom(*a, **k):
        reads.append(1)
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(budget.httpx, "get", boom)
    conf = cfg(tmp_path)
    for _ in range(6):
        assert budget.check(conf, secrets(tmp_path), db).notice_key == "budget:unreadable"
    assert len(reads) == 1


def test_a_failed_read_holds_to_the_last_number_rather_than_unguarding(tmp_path, db, monkeypatch):
    usage(monkeypatch, 85)
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    assert not budget.check(conf, secrets(tmp_path), db).allowed

    monkeypatch.setattr(budget, "USAGE_TTL_S", 0)  # force a fresh read, which now fails
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    v = budget.check(conf, secrets(tmp_path), db)
    assert not v.allowed, "85% is still the best thing known about the window"
    assert v.notice_key != "budget:unreadable"


def test_a_reading_too_old_to_trust_gives_up_on_it(tmp_path, db, monkeypatch):
    usage(monkeypatch, 85)
    assert not budget.check(cfg(tmp_path, stop_pct=90), secrets(tmp_path), db).allowed

    monkeypatch.setattr(budget, "USAGE_TTL_S", 0)
    monkeypatch.setattr(budget, "USAGE_STALE_S", 0)
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    assert budget.check(cfg(tmp_path), secrets(tmp_path), db).notice_key == "budget:unreadable"


# ----- the review endpoint's own limits ----------------------------------


def test_the_endpoint_arm_is_not_held_against_the_account_window(tmp_path, db, monkeypatch):
    """The bug this fixes refused a real review for a reason that did not apply.

    The account window sat at 65% with a 70% cutoff, so the gate said no — to a run
    that was about to be billed by a third party and would not touch the plan.
    """
    usage(monkeypatch, 100, session=0.01)
    conf = endpoint_cfg(tmp_path, stop_pct=70)
    assert not budget.check(conf, secrets(tmp_path), db).allowed, "the account is full"
    assert budget.check(conf, secrets(tmp_path), db, via_endpoint=True).allowed


def test_the_endpoint_arm_stops_on_its_own_limit(tmp_path, db, monkeypatch):
    usage(monkeypatch, 0, session=0.90)
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80)
    v = budget.check(conf, secrets(tmp_path), db, via_endpoint=True)
    assert not v.allowed
    assert "session 90.0%" in v.detail


def test_the_worse_of_session_and_weekly_is_what_stops_it(tmp_path, db, monkeypatch):
    """Either one running out stops reviews, so the gate cannot read only one."""
    usage(monkeypatch, 0, session=0.10, weekly=0.95)
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80)
    assert not budget.check(conf, secrets(tmp_path), db, via_endpoint=True).allowed


def test_each_endpoint_review_in_flight_holds_back_its_share(tmp_path, db, monkeypatch):
    usage(monkeypatch, 0, session=0.70)
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80, endpoint_reserve_pct=5)
    ask = lambda n: budget.check(  # noqa: E731
        conf, secrets(tmp_path), db, inflight=n, via_endpoint=True
    ).allowed
    assert ask(1), "70 + 10 held is exactly the cutoff"
    assert not ask(2), "70 + 15 is past it"


def test_an_unreadable_endpoint_says_so_rather_than_guessing(tmp_path, db, monkeypatch):
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    v = budget.check(endpoint_cfg(tmp_path), secrets(tmp_path), db, via_endpoint=True)
    assert v.allowed and v.notice_key == "budget:endpoint-unreadable"


# ----- api: dollars ------------------------------------------------------


def test_dollars_reserve_the_same_way(tmp_path, db):
    conf = cfg(tmp_path, backend="api", daily_usd=20.0, reserve_usd=5.0)
    assert budget.check(conf, secrets(tmp_path), db, inflight=3).allowed, "$20 covers four"
    assert not budget.check(conf, secrets(tmp_path), db, inflight=4).allowed, "not five"
