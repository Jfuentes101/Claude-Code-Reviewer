"""The pipeline's state transitions, with the container and GitHub faked out.

What these protect: a failed run must come back next tick, and a published one
must not — including when publishing itself failed, because retrying a permanent
publish error would burn a full review every tick.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robbie import orchestrator as orch_mod
from robbie import publish as publish_mod
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.contract import Blocks
from robbie.db import Db
from robbie.gates import Decision, dedup_key
from robbie.github import PrMeta
from robbie.orchestrator import Orchestrator
from robbie.publish import PublishResult
from robbie.runner import ReviewRun

KEY = dedup_key("acme/app", 7, "abc1234567", "2026-01-01T00:00:00Z")
REQ = "2026-01-01T00:00:00Z"


class FakeSlack:
    def __init__(self) -> None:
        self.owner: list[str] = []
        self.channels: list[tuple[str, str]] = []
        self.authors: list[str] = []
        self.author_state = "sent"

    async def dm_owner(self, text: str) -> bool:
        self.owner.append(text)
        return True

    async def post(self, channel: str, text: str) -> bool:
        self.channels.append((channel, text))
        return True

    async def dm_author(self, login: str, text: str) -> str:
        self.authors.append(login)
        return self.author_state


@pytest.fixture
def repo() -> RepoConfig:
    return RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
        slack_channel="C0CHAN",
    )


@pytest.fixture
def cfg(tmp_path, repo) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0OWNER"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5),
    )


@pytest.fixture
def orch(cfg, tmp_path, monkeypatch):
    db = Db(tmp_path / "robbie.db")
    slack = FakeSlack()
    secrets = Secrets(
        gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="sk"
    )
    o = Orchestrator(cfg, secrets, db, slack)  # type: ignore[arg-type]
    # nothing in these tests may reach GitHub
    monkeypatch.setattr(orch_mod, "review_still_requested", _async(True))
    yield o
    db.close()


def _async(value):
    async def _call(*a, **kw):
        return value
    return _call


def pr(**kw) -> PrMeta:
    base = dict(
        number=7, title="Add widgets", url="https://x/7", author="dev",
        head_sha="abc1234567", changed_files=2, labels=(), checks=(),
    )
    return PrMeta(**{**base, **kw})


def stub_run(monkeypatch, run: ReviewRun) -> None:
    monkeypatch.setattr(orch_mod, "run_review", _async(run))


def ok_run(verdict: str, *, inline: str = "[]", slack: str = "briefing") -> ReviewRun:
    return ReviewRun(
        ok=True,
        blocks=Blocks(verdict=verdict, github="summary", inline=inline, slack=slack),
        cost_usd=0.42, tokens_in=1000, tokens_out=200, duration_s=12.0,
        transcript=Path("/tmp/t.md"),
    )


# ----- holds ---------------------------------------------------------------


async def test_a_recorded_hold_dms_once_and_is_not_looked_at_again(orch, repo):
    decision = Decision("hold", "nothing new pushed", dm="heads up", record=True)
    await orch._act(repo, pr(), KEY, REQ, decision)
    await orch._act(repo, pr(), KEY, REQ, decision)
    assert orch.slack.owner == ["heads up"], "one DM per key, not one per tick"
    assert orch.db.get_review(KEY).state == "held"


async def test_a_retryable_hold_leaves_no_row(orch, repo):
    await orch._act(repo, pr(), KEY, REQ, Decision("hold", "comments open", dm="x", record=False))
    assert orch.db.get_review(KEY) is None, "the next tick has to look again"
    assert orch.slack.owner == ["x"]


async def test_a_silent_hold_never_dms(orch, repo):
    await orch._act(repo, pr(), KEY, REQ, Decision("hold", "label on", record=False))
    assert orch.slack.owner == []


# ----- a failed run --------------------------------------------------------


async def test_a_failed_run_is_retryable_and_reported(orch, repo, monkeypatch):
    stub_run(monkeypatch, ReviewRun(ok=False, error="container exited 1", duration_s=3.0))
    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "failed"
    assert orch.db.get_review(KEY).state == "failed"
    assert not orch.db.sha_was_judged("acme/app", 7, "abc1234567")
    assert "the run failed" in orch.slack.owner[0]


async def test_a_run_without_a_verdict_posts_nothing_and_stops_retrying(orch, repo, monkeypatch):
    stub_run(monkeypatch, ReviewRun(ok=True, blocks=Blocks(None, "", "", ""), duration_s=1.0))
    called = []
    monkeypatch.setattr(publish_mod, "publish_review", lambda *a, **k: called.append(1))
    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "failed"
    assert called == [], "no verdict means nothing gets posted"
    assert orch.db.get_review(KEY).state == "held"
    assert orch.db.sha_was_judged("acme/app", 7, "abc1234567"), "a 30-min run is not retried blind"


# ----- verdicts ------------------------------------------------------------


async def test_ok_clears_the_label_posts_nothing_and_briefs_the_owner(orch, repo, monkeypatch):
    cleared = []
    monkeypatch.setattr(
        publish_mod, "clear_needs_work",
        lambda *a, **k: _mark(cleared, PublishResult(True, "cleared")),
    )
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "nope")))
    stub_run(monkeypatch, ok_run("ok"))

    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "review"
    assert cleared, "an ok verdict releases the brake"
    assert orch.db.get_review(KEY).verdict == "ok"
    assert orch.slack.owner and "briefing" in orch.slack.owner[0]
    assert orch.slack.channels == [], "nothing was posted, so nothing to announce"


async def test_needs_work_publishes_notifies_and_does_not_brief_the_owner(
    orch, repo, monkeypatch
):
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "did it")))
    stub_run(monkeypatch, ok_run(
        "needs-work", inline='[{"path":"a.rb","line":1,"severity":"Must-fix"}]'
    ))

    await orch._review(repo, pr(), KEY, REQ)
    assert orch.db.get_review(KEY).state == "published"
    assert orch.slack.channels[0][0] == "C0CHAN"
    assert "1 thing to fix" in orch.slack.channels[0][1]
    assert orch.slack.authors == ["dev"]
    assert orch.slack.owner == [], "the PR left their queue; no briefing needed"


async def test_comment_publishes_and_still_briefs_the_owner(orch, repo, monkeypatch):
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "did it")))
    stub_run(monkeypatch, ok_run("comment"))

    await orch._review(repo, pr(), KEY, REQ)
    assert orch.db.get_review(KEY).verdict == "comment"
    assert orch.slack.authors == ["dev"]
    assert orch.slack.owner, "the review request is untouched, so they still look at it"


async def test_an_unmapped_author_warns_the_owner_once(orch, repo, monkeypatch):
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "did it")))
    stub_run(monkeypatch, ok_run("needs-work"))
    orch.slack.author_state = "unmapped"

    await orch._review(repo, pr(), KEY, REQ)
    await orch._review(repo, pr(), "another-key", REQ)
    assert sum("no Slack id" in m for m in orch.slack.owner) == 1


async def test_nothing_is_announced_when_the_publish_was_a_no_op(orch, repo, monkeypatch):
    monkeypatch.setattr(
        publish_mod, "publish_review", _async(PublishResult(False, "already posted"))
    )
    stub_run(monkeypatch, ok_run("needs-work"))
    await orch._review(repo, pr(), KEY, REQ)
    assert orch.slack.channels == []
    assert orch.slack.authors == []


async def test_a_publish_crash_still_counts_as_judged(orch, repo, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("422 unprocessable")

    monkeypatch.setattr(publish_mod, "publish_review", boom)
    stub_run(monkeypatch, ok_run("needs-work"))

    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "failed"
    assert orch.db.get_review(KEY).state == "published", (
        "retrying a permanent publish failure would burn a full review every tick"
    )
    assert "post by hand" in orch.slack.owner[0]


async def test_cost_and_tokens_are_recorded(orch, repo, monkeypatch):
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "did it")))
    stub_run(monkeypatch, ok_run("comment"))
    await orch._review(repo, pr(), KEY, REQ)
    assert orch.db.spend_since(0) == pytest.approx(0.42)


# ----- the budget gate ----------------------------------------------------


async def test_the_budget_gate_stops_reviews_and_warns_once(orch, repo, monkeypatch):
    monkeypatch.setattr(orch.cfg.budget, "daily_usd", 0.0)
    reviewed = []
    monkeypatch.setattr(orch, "_review", lambda *a, **k: reviewed.append(1))

    for _ in range(3):
        outcome = await orch._act(repo, pr(), KEY, REQ, Decision("review"))
    assert outcome.action == "budget"
    assert reviewed == [], "no container is spawned while the budget is closed"
    assert sum("Holding off" in m for m in orch.slack.owner) == 1


# ----- dry run -----------------------------------------------------------


async def test_dry_run_writes_nothing(orch, repo, monkeypatch):
    orch.dry_run = True
    stub_run(monkeypatch, ok_run("needs-work"))
    await orch._act(repo, pr(), KEY, REQ, Decision("hold", "x", dm=None, record=True))
    await orch._act(repo, pr(), KEY, REQ, Decision("review"))
    assert orch.db.get_review(KEY) is None


def _mark(sink, value):
    sink.append(1)

    async def _noop():
        return value

    return _noop()
