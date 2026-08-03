"""Concurrency, without docker: does the semaphore cap in-flight containers,
and does one bad review leave the others alone.

The container side of the same question lives in test_docker_fleet.py, which
needs a docker daemon and is opt-in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from robbie import orchestrator as orch_mod
from robbie import publish as publish_mod
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.contract import Blocks
from robbie.db import Db
from robbie.github import PrMeta
from robbie.orchestrator import Orchestrator
from robbie.publish import PublishResult
from robbie.runner import ReviewRun

REQ = "2026-01-01T00:00:00Z"


class FakeSlack:
    def __init__(self) -> None:
        self.owner: list[str] = []

    async def dm_owner(self, text: str) -> bool:
        self.owner.append(text)
        return True

    async def post(self, channel: str, text: str) -> bool:
        return True

    async def dm_author(self, login: str, text: str) -> str:
        return "sent"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    repo = RepoConfig(slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"))
    cfg = Config(
        slack=SlackConfig(owner_id="U0"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5), max_concurrent_reviews=2,
    )
    db = Db(tmp_path / "robbie.db")
    o = Orchestrator(
        cfg,
        Secrets(gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="k"),
        db, FakeSlack(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(publish_mod, "clear_needs_work", _async(PublishResult(True, "cleared")))
    monkeypatch.setattr(orch_mod, "review_still_requested", _async(True))
    monkeypatch.setattr(o, "_token_login", _async("robbie-bot"))
    yield o
    db.close()


def _async(value):
    async def _call(*a, **kw):
        return value
    return _call


def pr(number: int) -> PrMeta:
    return PrMeta(
        number=number, title=f"pr {number}", url=f"https://x/{number}", author="dev",
        head_sha=f"sha{number:04d}", changed_files=1, labels=(), checks=(),
    )


def _ok_run(pr_number: int) -> ReviewRun:
    return ReviewRun(
        ok=True,
        blocks=Blocks(verdict="ok", github="", inline="[]", slack=f"briefing for {pr_number}"),
        cost_usd=0.10, duration_s=1.0, transcript=Path(f"/tmp/{pr_number}.md"),
    )


class Fleet:
    """Stands in for run_review and records how many ran at once."""

    def __init__(self, hold: float = 0.05) -> None:
        self.hold = hold
        self.live = 0
        self.peak = 0
        self.order: list[int] = []
        self.fail_on: set[int] = set()

    async def __call__(self, cfg, secrets, repo, meta, *, prompt):
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.order.append(meta.number)
        try:
            await asyncio.sleep(self.hold)
            if meta.number in self.fail_on:
                return ReviewRun(ok=False, error="boom", duration_s=self.hold)
            return _ok_run(meta.number)
        finally:
            self.live -= 1


async def _run_all(orch, fleet, numbers):
    return await asyncio.gather(
        *(orch._review(orch.cfg.repos[0], pr(n), f"key-{n}", REQ) for n in numbers)
    )


async def test_the_semaphore_caps_containers_in_flight(orch, monkeypatch):
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(6))
    assert fleet.peak == 2, f"cap is 2, saw {fleet.peak} at once"


async def test_raising_the_cap_raises_actual_parallelism(orch, monkeypatch):
    orch._sem = asyncio.Semaphore(5)
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(6))
    assert fleet.peak == 5


async def test_a_cap_of_one_serializes(orch, monkeypatch):
    orch._sem = asyncio.Semaphore(1)
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(4))
    assert fleet.peak == 1


async def test_everything_queued_eventually_runs(orch, monkeypatch):
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(6))
    assert sorted(fleet.order) == list(range(6)), "nothing may be dropped by the cap"


async def test_results_are_not_crossed_between_concurrent_reviews(orch, monkeypatch):
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    outcomes = await _run_all(orch, fleet, range(6))
    assert [o.pr for o in outcomes] == list(range(6))
    for n in range(6):
        row = orch.db.get_review(f"key-{n}")
        assert row is not None and row.pr == n and row.head_sha == f"sha{n:04d}"


async def test_one_failing_container_does_not_take_the_others_down(orch, monkeypatch):
    fleet = Fleet()
    fleet.fail_on = {1, 4}
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    outcomes = await _run_all(orch, fleet, range(6))
    failed = {o.pr for o in outcomes if o.action == "failed"}
    assert failed == {1, 4}
    assert {o.pr for o in outcomes if o.action == "review"} == {0, 2, 3, 5}


async def test_a_failed_review_stays_retryable_while_its_neighbours_are_judged(orch, monkeypatch):
    fleet = Fleet()
    fleet.fail_on = {1}
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(3))
    assert not orch.db.sha_was_judged("acme/app", 1, "sha0001")
    assert orch.db.sha_was_judged("acme/app", 0, "sha0000")
    assert orch.db.sha_was_judged("acme/app", 2, "sha0002")


async def test_cost_sums_across_the_fleet(orch, monkeypatch):
    fleet = Fleet()
    monkeypatch.setattr(orch_mod, "run_review", fleet)
    await _run_all(orch, fleet, range(4))
    assert orch.db.spend_since(0) == pytest.approx(0.40)


async def test_the_cap_comes_from_config(tmp_path):
    repo = RepoConfig(slug="a/b", reviewer_login="r", bare=Path("/x"))
    cfg = Config(
        slack=SlackConfig(owner_id="U0"), repos=[repo], state_dir=tmp_path,
        max_concurrent_reviews=7,
    )
    db = Db(tmp_path / "d.db")
    try:
        o = Orchestrator(
            cfg,
            Secrets(gh_token="w", slack_bot_token="s", reviewer_gh_token="r",
                    anthropic_api_key="k"),
            db, FakeSlack(),  # type: ignore[arg-type]
        )
        assert o._sem._value == 7
    finally:
        db.close()
