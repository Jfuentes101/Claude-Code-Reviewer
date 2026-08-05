"""The pipeline's state transitions, with the container and GitHub faked out.

What these protect: a failed run must come back next tick, and a published one
must not — including when publishing itself failed, because retrying a permanent
publish error would burn a full review every tick.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robbie import orchestrator as orch_mod
from robbie import publish as publish_mod
from robbie.budget import Verdict
from robbie.config import Config, DockerConfig, RepoConfig, ReviewModel, Secrets, SlackConfig
from robbie.contract import Blocks
from robbie.db import Db
from robbie.gates import Decision, dedup_key
from robbie.github import GhError, PrMeta, Thread
from robbie.orchestrator import Orchestrator
from robbie.publish import PublishResult
from robbie.runner import ReviewRun

KEY = dedup_key("acme/app", 7, "abc1234567", "2026-01-01T00:00:00Z")
REQ = "2026-01-01T00:00:00Z"


class FakeSlack:
    def __init__(self) -> None:
        self.owner: list[str] = []
        self.reviewers: list[str] = []
        self.channels: list[tuple[str, str]] = []
        self.authors: list[str] = []
        self.author_state = "sent"

    async def dm_owner(self, text: str) -> bool:
        self.owner.append(text)
        return True

    async def dm_reviewers(self, text: str) -> bool:
        self.reviewers.append(text)
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


def ok_run(verdict: str, *, inline: str = "[]") -> ReviewRun:
    return ReviewRun(
        ok=True,
        blocks=Blocks(verdict=verdict, github="summary", inline=inline),
        cost_usd=0.42, tokens_in=1000, tokens_out=200, duration_s=12.0,
        transcript=Path("/tmp/t.md"),
    )


async def test_the_gate_cache_does_not_cross_repos(orch, cfg, monkeypatch):
    """Two repos can each have a PR #7, and the conversation is not shared."""
    other = RepoConfig(slug="acme/other", reviewer_login="rev", bare=Path("/srv/m/o.git"))
    cfg.repos.append(other)

    async def threads(slug, number, reviewer):
        return [Thread(
            path=f"{slug}#{number}", line=1, resolved=False, outdated=False,
            mine="a finding", replies=(),
        )]

    monkeypatch.setattr(orch_mod, "my_threads", threads)
    monkeypatch.setattr(orch_mod, "pr_meta", _async(pr()))
    monkeypatch.setattr(orch_mod, "last_review_request", _async(REQ))

    await orch._gate(cfg.repos[0], 7)
    prior = await orch._prior_threads(other, pr())
    assert [t.path for t in prior] == ["acme/other#7"]


# ----- which arm reviews, and what covers for it ---------------------------


def _arms(cfg) -> None:
    cfg.review_models = [
        ReviewModel(model="glm-5.2:cloud", via="endpoint", weight=2),
        ReviewModel(model="sonnet", weight=1),
    ]


def _meters(monkeypatch, *, account: bool, endpoint: bool) -> list[bool]:
    """Answer each arm's gate independently, and record which was asked."""
    asked: list[bool] = []

    def gate(cfg, secrets, db, inflight=0, *, via_endpoint=False):
        asked.append(via_endpoint)
        ok = endpoint if via_endpoint else account
        return Verdict(ok, "endpoint" if via_endpoint else "account")

    monkeypatch.setattr(orch_mod.budget, "check", gate)
    return asked


async def test_a_spent_account_falls_back_to_the_endpoint(orch, cfg, monkeypatch):
    """Today's live case: the plan window full while the endpoint sits at 0.1%."""
    _arms(cfg)
    _meters(monkeypatch, account=False, endpoint=True)
    key = next(k for k in (f"k{n}" for n in range(50)) if not cfg.choose_model(k).via_endpoint)
    choice, verdict = orch._admit(key)
    assert verdict.allowed
    assert (choice.model, choice.via_endpoint) == ("glm-5.2:cloud", True)


async def test_a_spent_endpoint_falls_back_to_the_account(orch, cfg, monkeypatch):
    _arms(cfg)
    _meters(monkeypatch, account=True, endpoint=False)
    key = next(k for k in (f"k{n}" for n in range(50)) if cfg.choose_model(k).via_endpoint)
    choice, verdict = orch._admit(key)
    assert verdict.allowed
    assert (choice.model, choice.via_endpoint) == ("sonnet", False)


async def test_both_spent_refuses_with_the_reason_of_the_arm_it_wanted(orch, cfg, monkeypatch):
    _arms(cfg)
    _meters(monkeypatch, account=False, endpoint=False)
    choice, verdict = orch._admit("k1")
    assert not verdict.allowed
    assert verdict.detail == ("endpoint" if choice.via_endpoint else "account")


async def test_a_room_to_spare_arm_is_never_asked(orch, cfg, monkeypatch):
    _arms(cfg)
    asked = _meters(monkeypatch, account=True, endpoint=True)
    orch._admit("k1")
    assert len(asked) == 1, "the second meter is only read when the first says no"


async def test_nothing_configured_has_nothing_to_fall_back_to(orch, monkeypatch):
    _meters(monkeypatch, account=False, endpoint=True)
    choice, verdict = orch._admit("k1")
    assert not verdict.allowed
    assert choice.model is None, "the account's own model is the only arm there is"


async def test_a_model_named_on_the_cli_is_never_substituted(orch, cfg, monkeypatch):
    """An explicit --model is a request, not a routing preference."""
    _arms(cfg)
    _meters(monkeypatch, account=True, endpoint=False)
    orch.model = "glm-5.2:cloud"
    choice, verdict = orch._admit("k1")
    assert not verdict.allowed
    assert choice.model == "glm-5.2:cloud"


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


async def test_a_dry_run_does_not_spend_the_one_shot_dm(orch, repo):
    """--dry-run is how a deployment is proved before it reviews anything.

    Recording the notice there would make the operator DM for every held PR
    vanish from the next real tick, which is the opposite of writing nothing.
    """
    decision = Decision("hold", "nothing new pushed", dm="they asked again")
    orch.dry_run = True
    await orch._act(repo, pr(), KEY, REQ, decision)
    assert orch.slack.owner == ["they asked again"], "a dry run still says what it would send"

    orch.dry_run = False
    await orch._act(repo, pr(), KEY, REQ, decision)
    assert orch.slack.owner == ["they asked again"] * 2, "the real tick still owes the DM"

    await orch._act(repo, pr(), KEY, REQ, decision)
    assert len(orch.slack.owner) == 2, "and owes it once, which is what the key is for"


# ----- a failed run --------------------------------------------------------


async def test_a_failed_run_is_retryable_and_reported(orch, repo, monkeypatch):
    stub_run(monkeypatch, ReviewRun(ok=False, error="container exited 1", duration_s=3.0))
    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "failed"
    assert orch.db.get_review(KEY).state == "failed"
    assert not orch.db.sha_was_judged("acme/app", 7, "abc1234567")
    assert "the run failed" in orch.slack.owner[0]


async def test_a_run_without_a_verdict_posts_nothing_and_stops_retrying(orch, repo, monkeypatch):
    stub_run(monkeypatch, ReviewRun(ok=True, blocks=Blocks(None, "", ""), duration_s=1.0))
    called = []
    monkeypatch.setattr(publish_mod, "publish_review", lambda *a, **k: called.append(1))
    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "failed"
    assert called == [], "no verdict means nothing gets posted"
    assert orch.db.get_review(KEY).state == "held"
    assert orch.db.sha_was_judged("acme/app", 7, "abc1234567"), "a 30-min run is not retried blind"


# ----- verdicts ------------------------------------------------------------


async def test_ok_clears_the_label_asks_for_ci_and_says_so(orch, repo, monkeypatch):
    cleared, ci = [], []
    monkeypatch.setattr(
        publish_mod, "clear_needs_work",
        lambda *a, **k: _mark(cleared, PublishResult(True, "cleared")),
    )
    monkeypatch.setattr(
        publish_mod, "request_ci",
        lambda *a, **k: _mark(ci, PublishResult(True, "asked CI to run (run-ci)")),
    )
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "nope")))
    stub_run(monkeypatch, ok_run("ok"))

    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert outcome.action == "review"
    assert cleared, "an ok verdict releases the brake"
    assert ci, "an approval is what pays for a build now that push does not"
    assert orch.db.get_review(KEY).verdict == "ok"
    assert orch.slack.reviewers == [
        "✅ <https://x/7|*#7*> Add widgets — nothing to fix, asked CI to run (run-ci)."
    ], "an ok is invisible on the PR, so one line has to say it happened"
    assert orch.slack.owner == [], "not an operator alert; it goes to whoever reviews"
    assert orch.slack.channels == [], "no review was posted, so nothing to announce"


async def test_ci_is_asked_for_once_per_commit(orch, repo, monkeypatch):
    """A build costs money now, and a forced re-review must not buy a second one."""
    ci = []
    monkeypatch.setattr(publish_mod, "clear_needs_work", _async(PublishResult(True, "c")))
    monkeypatch.setattr(
        publish_mod, "request_ci",
        lambda *a, **k: _mark(ci, PublishResult(True, "asked CI to run (run-ci)")),
    )
    stub_run(monkeypatch, ok_run("ok"))

    first = await orch._review(repo, pr(), KEY, REQ)
    second = await orch._review(repo, pr(), "forced-again", REQ)
    assert len(ci) == 1
    assert "asked CI" in first.detail and "already asked" in second.detail


async def test_a_ci_request_that_failed_is_asked_for_again(orch, repo, monkeypatch):
    """The guard means "the phrase is on the PR", so a failed post must not set it.

    Otherwise an approval nobody builds can never be recovered from: every later
    pass on that commit reads "already asked" and stays silent.
    """
    monkeypatch.setattr(publish_mod, "clear_needs_work", _async(PublishResult(True, "c")))
    stub_run(monkeypatch, ok_run("ok"))

    async def boom(*a, **k):
        raise GhError("502 from github")

    monkeypatch.setattr(publish_mod, "request_ci", boom)
    first = await orch._review(repo, pr(), KEY, REQ)
    assert "CI request failed" in first.detail
    assert any("couldn't post" in m for m in orch.slack.owner)

    ci = []
    monkeypatch.setattr(
        publish_mod, "request_ci", lambda *a, **k: _mark(ci, PublishResult(True, "asked")),
    )
    second = await orch._review(repo, pr(), "forced-again", REQ)
    assert len(ci) == 1 and "asked" in second.detail


async def test_the_ci_trigger_comment_is_the_bare_phrase(repo, monkeypatch):
    """Whatever listens for it may match the whole body, so nothing rides along."""
    sent = {}

    async def fake_gh(*args, stdin=None, **kw):
        sent["body"] = json.loads(stdin)["body"]
        return "https://x/1"

    monkeypatch.setattr(publish_mod, "gh", fake_gh)
    await publish_mod.request_ci(repo, pr())
    assert sent["body"] == "run-ci"


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


async def test_comment_publishes_without_briefing_the_owner(orch, repo, monkeypatch):
    """A published review announces itself on the PR and in the channel."""
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(True, "did it")))
    stub_run(monkeypatch, ok_run("comment"))

    await orch._review(repo, pr(), KEY, REQ)
    assert orch.db.get_review(KEY).verdict == "comment"
    assert orch.slack.authors == ["dev"]
    assert orch.slack.owner == [], "no summary DM for a review that is visible"


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


async def test_what_each_model_found_is_recorded_for_the_comparison(orch, repo, monkeypatch):
    """Verdict alone cannot compare models; how much they found at what severity can."""
    inline = json.dumps([
        {"path": "a.rb", "line": 1, "severity": "Critical", "title": "t", "body": "b"},
        {"path": "a.rb", "line": 2, "severity": "Must-fix", "title": "t", "body": "b"},
        {"path": "a.rb", "line": 3, "severity": "Should-fix", "title": "t", "body": "b"},
        {"path": "a.rb", "line": 4, "severity": "Nitpick", "title": "t", "body": "b"},
    ])
    monkeypatch.setattr(
        publish_mod, "publish_review",
        _async(PublishResult(True, "posted", inline=3)),
    )
    stub_run(monkeypatch, ok_run("needs-work", inline=inline))
    await orch._review(repo, pr(), KEY, REQ)

    row = orch.db.conn.execute(
        "SELECT findings, blocking, should_fix, inline, model FROM reviews WHERE key=?", (KEY,)
    ).fetchone()
    assert (row["findings"], row["blocking"], row["should_fix"]) == (4, 2, 1)
    assert row["inline"] == 3, "one of the four could not be anchored to the diff"


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


async def test_no_publish_runs_the_review_but_writes_nothing_outward(orch, repo, monkeypatch):
    orch.no_publish = True
    seen: dict = {}

    async def spy(*a, **kw):
        seen.update(kw)
        return PublishResult(False, "dry run")

    monkeypatch.setattr(publish_mod, "publish_review", spy)
    ran = []
    stub_run(monkeypatch, ok_run("needs-work"))
    monkeypatch.setattr(
        orch_mod, "run_review",
        lambda *a, **kw: ran.append(1) or _async(ok_run("needs-work"))(),
    )

    outcome = await orch._review(repo, pr(), KEY, REQ)
    assert ran == [1], "the review itself must actually run, unlike --dry-run"
    assert seen.get("dry_run") is True, "publishing must be suppressed"
    assert orch.slack.channels == [] and orch.slack.authors == []
    assert outcome.action == "review"


async def test_no_publish_still_records_the_cost(orch, repo, monkeypatch):
    orch.no_publish = True
    monkeypatch.setattr(publish_mod, "publish_review", _async(PublishResult(False, "dry run")))
    stub_run(monkeypatch, ok_run("needs-work"))
    await orch._review(repo, pr(), KEY, REQ)
    assert orch.db.spend_since(0) == pytest.approx(0.42), "the run cost real money"


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
