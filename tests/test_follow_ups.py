"""The follow-up queue: the same triage and fix, held until the issue's event.

What these protect: a follow-up is never decided before what it waits on has
happened, a wait robbie cannot check goes to a person instead of being guessed,
and the only patch that may skip its test is one that removes code, on an issue
the PR server itself saw carry the label.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from robbie import budget as budget_mod
from robbie import fixer as fixer_mod
from robbie import issues as issues_mod
from robbie import publish as publish_mod
from robbie import wait as wait_mod
from robbie.budget import Verdict as Meter
from robbie.config import (
    Config,
    DockerConfig,
    IssueConfig,
    RepoConfig,
    Secrets,
    SlackConfig,
    WaitConfig,
)
from robbie.contract import FIX_PREAMBLE, fix_preamble
from robbie.db import Db
from robbie.fixer import fix_tick
from robbie.github import GhError, IssueMeta
from robbie.issues import triage_tick
from robbie.publish import PublishResult
from robbie.runner import ReviewRun
from robbie.triage import Rules
from robbie.wait import wait_over
from robbie_mcp.pr import refuse

WAIT = WaitConfig(
    field="Can start", waiting_on="Waiting on", not_before="Not before",
    now=("Now",), merged=("After a PR merges",), deployed=("After a production deploy",),
    dated=("After a date or soak period",), deploy_tag=r"^v\d{8}$",
)
TODAY = date(2026, 11, 10)


def _async(value):
    async def call(*a, **k):
        return value
    return call


def fake_gh(monkeypatch, answers: dict[str, str | Exception]) -> list[tuple]:
    """`gh` in the wait module, answered by the first key found in its args."""
    calls: list[tuple] = []

    async def gh(*args, stdin=None):
        calls.append(args)
        for key, out in answers.items():
            if any(key in a for a in args):
                if isinstance(out, Exception):
                    raise out
                return out
        pytest.fail(f"unexpected gh call {args}")

    monkeypatch.setattr(wait_mod, "gh", gh)
    return calls


async def over(answers: dict[str, str]) -> tuple:
    return tuple(await wait_over("acme/app", answers, WAIT, today=TODAY))


# ----- when the wait is over --------------------------------------------------


async def test_now_is_over_without_asking_github(monkeypatch):
    fake_gh(monkeypatch, {})
    assert (await over({"Can start": "Now"}))[0] is True


async def test_a_date_still_ahead_holds_any_event(monkeypatch):
    fake_gh(monkeypatch, {})
    assert await over({"Can start": "Now", "Not before": "2026-12-01"}) == (
        False, "not before 2026-12-01")


async def test_a_date_that_passed_ends_a_dated_wait(monkeypatch):
    fake_gh(monkeypatch, {})
    got = await over({"Can start": "After a date or soak period", "Not before": "2026-11-10"})
    assert got[0] is True


async def test_a_dated_wait_with_no_date_is_a_persons_call(monkeypatch):
    fake_gh(monkeypatch, {})
    assert (await over({"Can start": "After a date or soak period"}))[0] is None


async def test_a_date_that_does_not_parse_is_a_persons_call(monkeypatch):
    fake_gh(monkeypatch, {})
    assert (await over({"Can start": "Now", "Not before": "next week"}))[0] is None


async def test_an_event_robbie_cannot_check_is_a_persons_call(monkeypatch):
    fake_gh(monkeypatch, {})
    got = await over({"Can start": "After a job or backfill finishes in production"})
    assert got == (None, "Can start: After a job or backfill finishes in production")


async def test_a_merge_wait_that_names_no_pr_is_a_persons_call(monkeypatch):
    fake_gh(monkeypatch, {})
    got = await over({"Can start": "After a PR merges", "Waiting on": "the pricing PR"})
    assert got[0] is None


async def test_an_open_pr_keeps_it_waiting(monkeypatch):
    fake_gh(monkeypatch, {"pulls/11423": "open false abc"})
    got = await over({"Can start": "After a PR merges", "Waiting on": "#11423 merged"})
    assert got == (False, "#11423 is not merged")


async def test_a_merged_pr_ends_a_merge_wait(monkeypatch):
    fake_gh(monkeypatch, {"pulls/11423": "closed true abc123"})
    got = await over({"Can start": "After a PR merges", "Waiting on": "#11423"})
    assert got == (True, "#11423 merged")


async def test_a_pr_closed_without_merging_is_a_persons_call(monkeypatch):
    fake_gh(monkeypatch, {"pulls/11423": "closed false abc123"})
    got = await over({"Can start": "After a PR merges", "Waiting on": "#11423"})
    assert got[0] is None


async def test_an_issue_number_is_not_a_pr(monkeypatch):
    fake_gh(monkeypatch, {"pulls/11423": GhError("gh api repos/ exited 1: Not Found (HTTP 404)")})
    got = await over({"Can start": "After a PR merges", "Waiting on": "#11423"})
    assert got[0] is None


async def test_a_github_failure_is_not_read_as_an_answer(monkeypatch):
    """A hiccup raises, so the tick skips the issue and asks again next time."""
    fake_gh(monkeypatch, {"pulls/11423": GhError("gh api repos/ exited 1: HTTP 502")})
    with pytest.raises(GhError):
        await over({"Can start": "After a PR merges", "Waiting on": "#11423"})


async def test_a_deploy_wait_is_over_once_the_newest_tag_has_the_merge(monkeypatch):
    calls = fake_gh(monkeypatch, {
        "matching-refs": "refs/tags/v20261020\nrefs/tags/v20261105\nrefs/tags/nightly\n",
        "pulls/11423": "closed true abc123",
        "compare/abc123...v20261105": "ahead\n",
    })
    got = await over({"Can start": "After a production deploy", "Waiting on": "#11423"})
    assert got == (True, "#11423 in v20261105")
    assert any("compare/abc123...v20261105" in a for c in calls for a in c)


async def test_a_merge_not_yet_deployed_keeps_it_waiting(monkeypatch):
    fake_gh(monkeypatch, {
        "matching-refs": "refs/tags/v20261105\n",
        "pulls/11423": "closed true abc123",
        "compare/": "diverged\n",
    })
    got = await over({"Can start": "After a production deploy", "Waiting on": "#11423"})
    assert got == (False, "#11423 is not in v20261105 yet")


async def test_every_pr_named_has_to_be_done(monkeypatch):
    fake_gh(monkeypatch, {"pulls/1": "closed true a", "pulls/2": "open false b"})
    got = await over({"Can start": "After a PR merges", "Waiting on": "#1 and #2"})
    assert got == (False, "#2 is not merged")


# ----- the queue ----------------------------------------------------------------


FOLLOW_UP_BODY = """\
### Kind

Dead code

### Can start

{event}

### Waiting on

{waiting_on}

### Not before

_No response_
"""


@pytest.fixture
def repo() -> RepoConfig:
    return RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
        follow_ups=IssueConfig(
            labels=("follow-up", "needs-triage"), clears="needs-triage",
            fixable_label="robbie-fix", assignee=("a-dev",), board="acme/3",
            rules=Rules(require={"Kind": ("Dead code",)}), wait=WAIT,
        ),
    )


@pytest.fixture
def queue(repo) -> RepoConfig:
    (only,) = repo.issue_queues()
    return only


@pytest.fixture
def cfg(tmp_path, repo) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0OWNER"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5), fix_mcp='{"mcpServers":{}}',
    )


@pytest.fixture
def db(tmp_path) -> Db:
    return Db(tmp_path / "robbie.db")


SECRETS = Secrets(
    gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="sk"
)


@pytest.fixture
def written(monkeypatch) -> dict[str, list]:
    out: dict[str, list] = {"settled": [], "moved": []}

    async def settle(repo, issue, *, say, add_label="", assignee=(), clears="", dry_run=False):
        out["settled"].append({"say": say, "label": add_label, "assignee": assignee})
        return PublishResult(True, say)

    async def move(repo, issue, *, dry_run=False):
        out["moved"].append((repo.issues.board, repo.issues.ready_status, issue.number))
        return PublishResult(True, "moved")

    monkeypatch.setattr(publish_mod, "settle_issue", settle)
    monkeypatch.setattr(publish_mod, "move_card", move)
    return out


def stub_follow_up(monkeypatch, *, event: str, waiting_on: str = "_No response_") -> None:
    monkeypatch.setattr(issues_mod, "issue_queue", _async([30]))
    monkeypatch.setattr(issues_mod, "fixing_prs", _async([]))
    monkeypatch.setattr(issues_mod, "issue_meta", _async(IssueMeta(
        number=30, title="[Follow-up]: drop it", url="https://x/30", author="dev",
        body=FOLLOW_UP_BODY.format(event=event, waiting_on=waiting_on),
        labels=("follow-up", "needs-triage"),
    )))
    monkeypatch.setattr(issues_mod, "run_review", lambda *a, **k: pytest.fail("asked a model"))


def test_the_follow_up_queue_is_its_own_copy_of_the_repo(repo, queue):
    assert queue.issues.kind == "follow_up" and queue.issues.noun == "follow-up"
    assert queue.issues.labels == ("follow-up", "needs-triage")
    assert repo.issues.labels == (), "the bug queue is still off"


def test_no_follow_up_labels_is_no_follow_up_queue(repo):
    quiet = repo.model_copy(update={"follow_ups": IssueConfig()})
    assert quiet.issue_queues() == []


async def test_a_follow_up_still_waiting_is_left_alone(cfg, db, queue, monkeypatch, written):
    stub_follow_up(monkeypatch, event="After a PR merges", waiting_on="#11423")
    fake_gh(monkeypatch, {"pulls/11423": "open false x"})

    assert await triage_tick(cfg, SECRETS, queue, db) == []
    assert written == {"settled": [], "moved": []}


async def test_a_follow_up_whose_wait_is_over_moves_to_ready_and_is_triaged(
    cfg, db, queue, monkeypatch, written
):
    stub_follow_up(monkeypatch, event="After a PR merges", waiting_on="#11423")
    fake_gh(monkeypatch, {"pulls/11423": "closed true abc"})

    out = await triage_tick(cfg, SECRETS, queue, db)

    assert [s.action for s in out] == ["attempt"]
    assert written["moved"] == [("acme/3", "Ready", 30)]
    assert written["settled"][0]["label"] == "robbie-fix"
    assert written["settled"][0]["say"].startswith("Its wait is over: #11423 merged.")


async def test_a_wait_robbie_cannot_check_goes_to_a_person_and_stays_put(
    cfg, db, queue, monkeypatch, written
):
    stub_follow_up(monkeypatch, event="After a dependency upgrade")
    fake_gh(monkeypatch, {})
    notified: list[str] = []
    monkeypatch.setattr(issues_mod.jane, "notify",
                        lambda cfg, secrets, title, **k: _async(notified.append(title))())

    out = await triage_tick(cfg, SECRETS, queue, db)

    assert [s.action for s in out] == ["assign"]
    assert written["moved"] == [], "a person decides when it is Ready"
    assert written["settled"][0]["assignee"] == ("a-dev",)
    assert notified == ["Triage assigned follow-up #30 to a person: [Follow-up]: drop it"]


async def test_the_bug_queue_never_waits(cfg, db, monkeypatch, written):
    """No `wait.field` is no gate: a bug is triaged on the tick it is read."""
    bugs = RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
        issues=IssueConfig(labels=("bug", "needs-triage"), clears="needs-triage",
                           fixable_label="robbie-fix",
                           rules=Rules(require={"Kind": ("Dead code",)})),
    )
    stub_follow_up(monkeypatch, event="After a PR merges", waiting_on="#11423")
    monkeypatch.setattr(wait_mod, "gh", lambda *a, **k: pytest.fail("asked GitHub about a wait"))

    out = await triage_tick(cfg, SECRETS, bugs, db)

    assert [s.action for s in out] == ["attempt"]
    assert written["moved"] == []


async def test_a_follow_up_fix_gets_the_follow_up_prompt(cfg, db, queue, monkeypatch, written):
    spawned: list[dict] = []
    monkeypatch.setattr(fixer_mod, "issue_queue", _async([30]))
    monkeypatch.setattr(fixer_mod, "issue_meta", _async(IssueMeta(
        number=30, title="[Follow-up]: drop it", url="https://x/30", author="dev",
        body="### Kind\n\nDead code", labels=("follow-up", "robbie-fix"),
    )))
    monkeypatch.setattr(fixer_mod, "fixing_prs", _async([]))
    monkeypatch.setattr(fixer_mod, "open_pr_for", _async(""))
    monkeypatch.setattr(budget_mod, "check", lambda *a, **k: Meter(True, "fine"))
    monkeypatch.setattr(fixer_mod.jane, "notify", _async(None))

    async def run(cfg, secrets, repo, meta, **kw):
        spawned.append(kw)
        return ReviewRun(ok=True, text="<<<VERDICT>>>\ncannot\n<<<END>>>")

    monkeypatch.setattr(fixer_mod, "run_review", run)

    await fix_tick(cfg, SECRETS, queue, db)

    assert spawned[0]["prompt"].startswith("You are doing one follow-up in this repository")


def test_the_bug_prompt_is_unchanged_by_the_follow_up_one():
    assert fix_preamble(number=1, title="t", body="b") == FIX_PREAMBLE.format(
        number=1, title="t", body="b")
    assert "follow-up" not in FIX_PREAMBLE


# ----- what a follow-up patch may skip ---------------------------------------


REMOVAL = """\
diff --git a/app/models/x.rb b/app/models/x.rb
--- a/app/models/x.rb
+++ b/app/models/x.rb
@@ -1,3 +1 @@
-def legacy
-end
 keep
"""
SWAP = """\
diff --git a/app/models/x.rb b/app/models/x.rb
--- a/app/models/x.rb
+++ b/app/models/x.rb
@@ -1 +1 @@
-old_api
+new_api
"""


def test_a_removal_on_a_labelled_issue_may_skip_the_test():
    assert refuse(REMOVAL, removal_may_skip_test=True) is None


def test_a_removal_without_the_label_still_needs_a_test():
    assert refuse(REMOVAL) == "the patch changes no test, so CI cannot show the bug was fixed"


def test_a_patch_that_adds_as_much_as_it_removes_needs_a_test_even_with_the_label():
    assert refuse(SWAP, removal_may_skip_test=True) == (
        "the patch changes no test and adds as much as it removes; only a "
        "patch that removes code may go without one"
    )


# ----- the board ------------------------------------------------------------------


async def test_the_card_moves_to_the_configured_status(monkeypatch, queue):
    sent: list[tuple] = []

    async def gh_json(*args, stdin=None):
        sent.append(args)
        if any("addProjectV2ItemById" in a for a in args):
            return {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM"}}}}
        return {"data": {
            "organization": {"projectV2": {"id": "BOARD", "field": {"id": "STATUS", "options": [
                {"id": "o-wait", "name": "Waiting"}, {"id": "o-ready", "name": "Ready"}]}}},
            "repository": {"issue": {"id": "ISSUE"}},
        }}

    async def gh(*args, stdin=None):
        sent.append(args)
        return "{}"

    monkeypatch.setattr(publish_mod, "gh_json", gh_json)
    monkeypatch.setattr(publish_mod, "gh", gh)
    issue = IssueMeta(number=30, title="t", url="u", author="a", body="", labels=())

    result = await publish_mod.move_card(queue, issue)

    assert result.posted
    assert "org=acme" in sent[0] and "board=3" in sent[0]
    assert {"board=BOARD", "item=ITEM", "field=STATUS", "option=o-ready"} <= set(sent[-1])


async def test_a_board_robbie_cannot_write_to_is_logged_not_raised(monkeypatch, queue):
    async def gh_json(*args, stdin=None):
        raise GhError("gh api graphql exited 1: Resource not accessible by personal access token")

    monkeypatch.setattr(publish_mod, "gh_json", gh_json)
    issue = IssueMeta(number=30, title="t", url="u", author="a", body="", labels=())

    result = await publish_mod.move_card(queue, issue)

    assert not result.posted and "not accessible" in result.detail
