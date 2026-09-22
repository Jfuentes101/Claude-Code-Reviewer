"""The bug queue's decisions, with the container and GitHub faked out.

What these protect: nothing settles an issue on a guess. A failed model call, a
paused meter and a report the form already answered all have to come out
somewhere the next tick can still reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robbie import budget as budget_mod
from robbie import issues as issues_mod
from robbie import publish as publish_mod
from robbie.budget import Verdict as Meter
from robbie.config import Config, DockerConfig, IssueConfig, RepoConfig, Secrets, SlackConfig
from robbie.db import Db
from robbie.github import IssueMeta
from robbie.issues import triage_tick
from robbie.publish import PublishResult
from robbie.runner import ReviewRun
from robbie.triage import Rules

BODY = """\
### Steps to reproduce

1. Open it
2. It spins

### Does this involve money?

{money}
"""

RULES = Rules(
    require={"Does this involve money?": ("No",)},
    needs=("Steps to reproduce",),
    ask_if={"Does this involve money?": ("Not sure",)},
)


@pytest.fixture
def repo() -> RepoConfig:
    return RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
        issues=IssueConfig(
            labels=("bug", "needs-triage"), clears="needs-triage",
            fixable_label="robbie-fix", assignee="a-dev", rules=RULES,
        ),
    )


@pytest.fixture
def cfg(tmp_path, repo) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0OWNER"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5),
    )


@pytest.fixture
def db(tmp_path) -> Db:
    return Db(tmp_path / "robbie.db")


SECRETS = Secrets(
    gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="sk"
)


def _async(value):
    async def call(*a, **k):
        return value
    return call


@pytest.fixture
def settled(monkeypatch) -> list[dict]:
    """What the loop asked publish to write, without writing it."""
    calls: list[dict] = []

    async def fake(repo, issue, *, say, add_label="", assignee="", dry_run=False):
        calls.append({"number": issue.number, "say": say, "label": add_label,
                      "assignee": assignee})
        return PublishResult(True, say)

    monkeypatch.setattr(publish_mod, "settle_issue", fake)
    return calls


def stub_issue(monkeypatch, *, money="No", state="OPEN", numbers=(12,)) -> None:
    monkeypatch.setattr(issues_mod, "issue_queue", _async(list(numbers)))
    monkeypatch.setattr(issues_mod, "issue_meta", _async(IssueMeta(
        number=12, title="[Bug]: it spins", url="https://x/12", author="cs",
        body=BODY.format(money=money), labels=("bug", "needs-triage"), state=state,
    )))


def stub_run(monkeypatch, run: ReviewRun) -> list[dict]:
    """Stand in for the container, and report how it was asked to run."""
    spawned: list[dict] = []

    async def fake(cfg, secrets, repo, meta, **kw):
        spawned.append({"meta": meta, **kw})
        return run

    monkeypatch.setattr(issues_mod, "run_review", fake)
    monkeypatch.setattr(budget_mod, "check", lambda *a, **k: Meter(True, "fine"))
    return spawned


async def test_the_feature_is_off_until_a_queue_is_configured(cfg, db, monkeypatch, repo):
    """No labels means no read at all — not an empty queue read every tick."""
    def boom(*a, **k):
        pytest.fail("asked GitHub for a queue nobody configured")

    monkeypatch.setattr(issues_mod, "issue_queue", boom)
    quiet = repo.model_copy(update={"issues": IssueConfig()})

    assert await triage_tick(cfg, SECRETS, quiet, db) == []


async def test_a_report_the_form_clears_is_queued_for_a_fix(cfg, db, repo, monkeypatch, settled):
    """`No` to the money question is an answer, and it costs nothing to read."""
    stub_issue(monkeypatch, money="No")
    monkeypatch.setattr(issues_mod, "run_review", lambda *a, **k: pytest.fail("asked a model"))

    out = await triage_tick(cfg, SECRETS, repo, db)

    assert [s.action for s in out] == ["attempt"]
    assert settled[0]["label"] == "robbie-fix"
    assert not settled[0]["assignee"], "a fixable issue is not a person's yet"


async def test_a_report_the_form_blocks_goes_straight_to_a_person(
    cfg, db, repo, monkeypatch, settled
):
    stub_issue(monkeypatch, money="Refund")
    monkeypatch.setattr(issues_mod, "run_review", lambda *a, **k: pytest.fail("asked a model"))

    out = await triage_tick(cfg, SECRETS, repo, db)

    assert [s.action for s in out] == ["assign"]
    assert settled[0]["assignee"] == "a-dev"
    assert not settled[0]["label"]
    assert "Refund" in out[0].reason


async def test_the_default_dropdown_is_settled_by_the_model(
    cfg, db, repo, monkeypatch, settled
):
    stub_issue(monkeypatch, money="Not sure")
    spawned = stub_run(monkeypatch, ReviewRun(ok=True, text="MONEY: no", cost_usd=0.01))

    out = await triage_tick(cfg, SECRETS, repo, db)

    assert [s.action for s in out] == ["attempt"]
    assert settled[0]["label"] == "robbie-fix"
    assert spawned[0]["mode"] == "money"
    assert isinstance(spawned[0]["meta"], IssueMeta), "the run is about an issue, not a PR"


async def test_a_model_that_finds_money_sends_it_to_a_person(
    cfg, db, repo, monkeypatch, settled
):
    stub_issue(monkeypatch, money="Not sure")
    stub_run(monkeypatch, ReviewRun(ok=True, text="MONEY: yes", cost_usd=0.01))

    out = await triage_tick(cfg, SECRETS, repo, db)

    assert [s.action for s in out] == ["assign"]
    assert settled[0]["assignee"] == "a-dev"


async def test_a_run_that_never_came_back_is_read_as_money(cfg, db, repo, monkeypatch, settled):
    """A timeout is not a clearance. The issue leaves the queue with a person on
    it, because a report that keeps timing out would otherwise be retried forever."""
    stub_issue(monkeypatch, money="Not sure")
    stub_run(monkeypatch, ReviewRun(ok=False, error="timed out after 1800s"))

    out = await triage_tick(cfg, SECRETS, repo, db)

    assert [s.action for s in out] == ["assign"]
    assert "timed out" in out[0].reason


async def test_what_the_run_cost_is_recorded_even_when_it_failed(
    cfg, db, repo, monkeypatch, settled
):
    """The meter reads what was spent. A failed container still burned tokens, and
    a gate that cannot see them is a gate that never closes."""
    stub_issue(monkeypatch, money="Not sure")
    stub_run(monkeypatch, ReviewRun(ok=False, error="boom", cost_usd=0.07, duration_s=3.0))

    await triage_tick(cfg, SECRETS, repo, db)

    assert db.spend_since(0) == pytest.approx(0.07)


async def test_a_paused_meter_leaves_the_issue_in_the_queue(cfg, db, repo, monkeypatch, settled):
    """Not a decision — an issue nobody could afford to think about yet. It keeps
    its label so the next tick reads it again."""
    stub_issue(monkeypatch, money="Not sure")
    monkeypatch.setattr(issues_mod, "run_review", lambda *a, **k: pytest.fail("spent anyway"))
    monkeypatch.setattr(budget_mod, "check", lambda *a, **k: Meter(False, "daily cap"))

    assert await triage_tick(cfg, SECRETS, repo, db) == []
    assert settled == []


async def test_an_issue_closed_since_the_queue_read_is_left_alone(
    cfg, db, repo, monkeypatch, settled
):
    """Minutes pass between the search and the decision, and commenting on a bug
    somebody just closed is the noise that gets a bot muted."""
    stub_issue(monkeypatch, money="No", state="CLOSED")

    assert await triage_tick(cfg, SECRETS, repo, db) == []
    assert settled == []


async def test_a_dry_run_asks_nobody_and_writes_nothing(cfg, db, repo, monkeypatch, settled):
    stub_issue(monkeypatch, money="Not sure")
    monkeypatch.setattr(
        issues_mod, "run_review", lambda *a, **k: pytest.fail("spawned a container"),
    )

    assert await triage_tick(cfg, SECRETS, repo, db, dry_run=True) == []


async def test_the_money_question_runs_on_its_own_arm(cfg, db, repo, monkeypatch, settled):
    """The cheap one, and billed against the endpoint's meter rather than the
    account's — a run a third party bills is not the account's spend."""
    armed = repo.model_copy(update={
        "issues": repo.issues.model_copy(update={
            "money_model": "glm-5.3-flash:cloud", "money_via_endpoint": True,
        })
    })
    stub_issue(monkeypatch, money="Not sure")
    spawned = stub_run(monkeypatch, ReviewRun(ok=True, text="MONEY: no"))

    await triage_tick(cfg, SECRETS, armed, db)

    assert spawned[0]["model"] == "glm-5.3-flash:cloud"
    assert spawned[0]["via_endpoint"] is True
