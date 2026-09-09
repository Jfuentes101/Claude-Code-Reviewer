"""The spend gate, and the reserve that makes it true for a fleet.

Both backends can only read what has already been *spent*. A container halfway
through a review has spent nothing yet, so a gate that reads the raw number lets
every free slot start at the same safe reading and blow through the cutoff
together. Each review in flight, plus the one asking, holds back a reserve.

`poll` is the only thing here that talks to a provider. The gate reads what it
stored, which is why these tests poll first and then ask.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
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


def secrets(tmp_path: Path) -> Secrets:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    return Secrets(
        gh_token="w", slack_bot_token="s", reviewer_gh_token="r", claude_credentials=creds,
        review_base_url="https://endpoint.example", review_api_token="k",
    )


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def answers(monkeypatch, pct: float, *, session: float = 0.0, weekly: float = 0.0) -> None:
    """Both meters answer, dispatched on the URL like the real ones are."""
    plan = {"five_hour": {"utilization": pct, "resets_at": "2026-08-04T19:10:00Z"}}
    endpoint = {"limits": {"session": {"usage": session}, "weekly": {"usage": weekly}}}
    monkeypatch.setattr(
        budget.httpx, "get",
        lambda url, **k: FakeResponse(endpoint if "/api/usage" in url else plan),
    )


def meter(monkeypatch, conf, db, tmp_path, pct: float, **kw) -> None:
    """What the daemon's meter loop would have left in the database."""
    answers(monkeypatch, pct, **kw)
    budget.poll(conf, secrets(tmp_path), db)


def endpoint_cfg(tmp_path, **kw):
    conf = cfg(tmp_path, **kw)
    conf.review_models = [
        ReviewModel(model="glm-5.2:cloud", via="endpoint", weight=2),
        ReviewModel(model="sonnet", weight=1),
    ]
    return conf


# ----- one poller, many readers ------------------------------------------


def test_the_gate_never_calls_a_provider(tmp_path, db, monkeypatch):
    """The panel refreshes itself every 30s in its own container, the daemon asks
    once per reviewable PR, and the account's meter answers a burst with a 429 —
    after which nothing is measuring the reviews that keep starting."""
    conf = cfg(tmp_path)
    meter(monkeypatch, conf, db, tmp_path, 10)

    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: pytest.fail("asked a provider"))
    for _ in range(6):
        assert budget.check(conf, db).allowed


def test_a_meter_nobody_has_polled_runs_unguarded_and_says_so(tmp_path, db):
    v = budget.check(cfg(tmp_path), db)
    assert v.allowed
    assert "not polled yet" in v.detail


def test_a_failed_poll_keeps_the_last_number(tmp_path, db, monkeypatch):
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    meter(monkeypatch, conf, db, tmp_path, 85)
    assert not budget.check(conf, db).allowed

    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    budget.poll(conf, secrets(tmp_path), db)

    v = budget.check(conf, db)
    assert not v.allowed, "85% is still the best thing known about the window"
    assert not v.notice_key.startswith("budget:unreadable")


def test_the_api_backend_polls_no_plan_meter(tmp_path, db, monkeypatch):
    """Its budget is the dollars in SQLite; asking the account would be a request
    spent on a number nothing reads."""
    asked = []
    monkeypatch.setattr(
        budget.httpx, "get",
        lambda url, **k: asked.append(url) or FakeResponse({"limits": {}}),
    )
    budget.poll(cfg(tmp_path, backend="api"), secrets(tmp_path), db)
    assert budget.USAGE_URL not in asked
    assert db.read_meter(budget.PLAN) is None


# ----- oauth: percent of the five-hour window ----------------------------


def test_the_token_never_reaches_a_command_line(tmp_path, db, monkeypatch):
    """It used to ride in a curl argv, which /proc hands to anyone on the host."""
    seen: dict = {}

    def fake_get(url, **kw):
        if url == budget.USAGE_URL:
            seen.update(url=url, headers=kw.get("headers", {}))
        return FakeResponse({"five_hour": {"utilization": 10, "resets_at": "x"}})

    monkeypatch.setattr(budget.httpx, "get", fake_get)
    budget.poll(cfg(tmp_path), secrets(tmp_path), db)
    assert budget.check(cfg(tmp_path), db).allowed
    assert seen["url"] == budget.USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer t"


def test_room_for_one_review_is_room_enough(tmp_path, db, monkeypatch):
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    meter(monkeypatch, conf, db, tmp_path, 70)
    assert budget.check(conf, db).allowed


def test_a_reading_under_the_cutoff_still_refuses_without_room_to_finish(
    tmp_path, db, monkeypatch
):
    """85% passes the old test and lands at 93%. That is the bug the reserve fixes."""
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    meter(monkeypatch, conf, db, tmp_path, 85)
    v = budget.check(conf, db)
    assert not v.allowed
    assert "+8% held" in v.detail


def test_each_running_review_holds_back_its_own_share(tmp_path, db, monkeypatch):
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    meter(monkeypatch, conf, db, tmp_path, 70)
    ask = lambda n: budget.check(conf, db, inflight=n).allowed  # noqa: E731
    assert ask(1), "one running plus this one is 16%, and 86% clears the cutoff"
    assert not ask(2), "two running plus this one is 24%, and 94% does not"


def test_five_agents_cannot_all_start_on_the_same_safe_reading(tmp_path, db, monkeypatch):
    conf = cfg(tmp_path, stop_pct=90, reserve_pct=8)
    meter(monkeypatch, conf, db, tmp_path, 50)
    admitted = 0
    while budget.check(conf, db, inflight=admitted).allowed:
        admitted += 1
    assert admitted == 5, "50% + 5 reviews at 8% each is the whole cutoff"


def test_an_unreadable_window_still_runs_but_says_so(tmp_path, db, monkeypatch):
    """A cold start with nothing to go on is the only case that goes unguarded.

    Dated like every other pause key: a constant one is announced once in the life
    of the database, which makes every outage after the first one silent — and a
    silent one runs without a spend guard at all.
    """
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    budget.poll(cfg(tmp_path), secrets(tmp_path), db)
    v = budget.check(cfg(tmp_path), db)
    assert v.allowed
    assert v.notice_key == f"budget:unreadable:{datetime.now(UTC):%Y-%m-%d}"
    assert "ZeroDivisionError" in v.detail or "division" in v.detail


def test_a_reading_too_old_to_trust_gives_up_on_it(tmp_path, db, monkeypatch):
    """A poller that has been failing for long enough is a poller that is down, and
    an old number is not a reading. Seven failed polls at the default interval."""
    conf = cfg(tmp_path, stop_pct=90)
    meter(monkeypatch, conf, db, tmp_path, 85)
    assert not budget.check(conf, db).allowed

    monkeypatch.setattr(budget, "USAGE_STALE_S", 0)
    v = budget.check(conf, db)
    assert v.allowed and v.notice_key.startswith("budget:unreadable")
    assert "old" in v.detail


# ----- the review endpoint's own limits ----------------------------------


def test_the_endpoint_arm_is_not_held_against_the_account_window(tmp_path, db, monkeypatch):
    """The bug this fixes refused a real review for a reason that did not apply.

    The account window sat at 65% with a 70% cutoff, so the gate said no — to a run
    that was about to be billed by a third party and would not touch the plan.
    """
    conf = endpoint_cfg(tmp_path, stop_pct=70)
    meter(monkeypatch, conf, db, tmp_path, 100, session=0.01)
    assert not budget.check(conf, db).allowed, "the account is full"
    assert budget.check(conf, db, via_endpoint=True).allowed


def test_the_endpoint_arm_stops_on_its_own_limit(tmp_path, db, monkeypatch):
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80)
    meter(monkeypatch, conf, db, tmp_path, 0, session=0.90)
    v = budget.check(conf, db, via_endpoint=True)
    assert not v.allowed
    assert "session 90.0%" in v.detail


def test_the_worse_of_session_and_weekly_is_what_stops_it(tmp_path, db, monkeypatch):
    """Either one running out stops reviews, so the gate cannot read only one."""
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80)
    meter(monkeypatch, conf, db, tmp_path, 0, session=0.10, weekly=0.95)
    assert not budget.check(conf, db, via_endpoint=True).allowed


def test_each_endpoint_review_in_flight_holds_back_its_share(tmp_path, db, monkeypatch):
    conf = endpoint_cfg(tmp_path, endpoint_stop_pct=80, endpoint_reserve_pct=5)
    meter(monkeypatch, conf, db, tmp_path, 0, session=0.70)
    ask = lambda n: budget.check(conf, db, inflight=n, via_endpoint=True).allowed  # noqa: E731
    assert ask(1), "70 + 10 held is exactly the cutoff"
    assert not ask(2), "70 + 15 is past it"


def test_an_unreadable_endpoint_says_so_rather_than_guessing(tmp_path, db, monkeypatch):
    conf = endpoint_cfg(tmp_path)
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: 1 / 0)
    budget.poll(conf, secrets(tmp_path), db)
    v = budget.check(conf, db, via_endpoint=True)
    assert v.allowed and v.notice_key.startswith("budget:endpoint-unreadable")


def test_one_meter_failing_does_not_cost_the_other_its_reading(tmp_path, db, monkeypatch):
    """They are different providers; the account's is the one that rate-limits."""
    conf = endpoint_cfg(tmp_path, stop_pct=90, endpoint_stop_pct=80)
    plan = {"five_hour": {"utilization": 10, "resets_at": "x"}}

    def half_broken(url, **kw):
        if "/api/usage" in url:
            raise RuntimeError("429 Too Many Requests")
        return FakeResponse(plan)

    monkeypatch.setattr(budget.httpx, "get", half_broken)
    budget.poll(conf, secrets(tmp_path), db)

    assert budget.check(conf, db).allowed
    assert "10%" in budget.check(conf, db).detail
    assert "429" in budget.check(conf, db, via_endpoint=True).detail


# ----- api: dollars ------------------------------------------------------


def test_a_third_party_run_is_not_charged_to_the_daily_dollars(tmp_path, db):
    """Its `cost_usd` is the CLI's own price table, not a bill the account got.

    Counting it would close the daily budget on money nobody spent — and the
    numbers are not small: $5.07 recorded for one review that cost the account $0.
    """
    conf = endpoint_cfg(tmp_path, backend="api", daily_usd=20.0, reserve_usd=5.0)
    for key, model, cost in (("a", "glm-5.2:cloud", 18.0), ("b", None, 1.0)):
        db.start_review(key=key, repo="acme/app", pr=7, head_sha=key, requested_at="t")
        db.finish_review(key, state="published", cost_usd=cost, model=model)
    assert db.spend_since(0) == pytest.approx(19.0), "everything, for the record"
    assert db.spend_since(0, conf.endpoint_models) == pytest.approx(1.0)
    assert budget.check(conf, db).allowed, "$1 of $20 is spent, not $19"


def test_dollars_reserve_the_same_way(tmp_path, db):
    conf = cfg(tmp_path, backend="api", daily_usd=20.0, reserve_usd=5.0)
    assert budget.check(conf, db, inflight=3).allowed, "$20 covers four"
    assert not budget.check(conf, db, inflight=4).allowed, "not five"


# ----- TOKEN ROTATION (the poller's read, `_fetch_plan`) -----
def _rotating_creds(tmp_path: Path, expires_in_min: float = 60.0) -> Secrets:
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old",
                    "expiresAt": (time.time() + expires_in_min * 60) * 1000,
                }
            }
        )
    )
    return Secrets(
        gh_token="w", slack_bot_token="s", reviewer_gh_token="r", claude_credentials=creds
    )


def _unauthorized() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", budget.USAGE_URL)
    return httpx.HTTPStatusError(
        "401", request=request, response=httpx.Response(401, request=request)
    )


def test_a_token_rotated_mid_read_is_retried_rather_than_failing(tmp_path, monkeypatch):
    """The CLI rewrites the file when it rotates a token; the 401 in hand is a
    race, not a broken meter — re-read once and carry on."""
    sec = _rotating_creds(tmp_path)

    def fake_get(url, **kw):
        if kw["headers"]["Authorization"] == "Bearer old":
            sec.claude_credentials.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "new", "expiresAt": 9e12}})
            )
            raise _unauthorized()
        return FakeResponse({"five_hour": {"utilization": 12, "resets_at": "x"}})

    monkeypatch.setattr(budget.httpx, "get", fake_get)
    pct, _ = budget._fetch_plan(sec)
    assert pct == 12


def test_a_token_that_is_simply_expired_says_so_instead_of_a_401_url(tmp_path, monkeypatch):
    """A bare httpx 401 names the endpoint and nothing an operator can act on."""
    sec = _rotating_creds(tmp_path, expires_in_min=-90)

    def fake_get(url, **kw):
        raise _unauthorized()

    monkeypatch.setattr(budget.httpx, "get", fake_get)
    with pytest.raises(RuntimeError, match="run `claude` on the host"):
        budget._fetch_plan(sec)
