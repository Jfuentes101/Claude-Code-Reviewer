"""The review policy. If these pass, robbie will not review the wrong thing."""

from __future__ import annotations

import pytest

from robbie.config import RepoConfig
from robbie.gates import dedup_key, evaluate
from robbie.github import PrMeta, failing_checks

NW = "❌ NEEDS WORK! ❌"


@pytest.fixture
def repo(tmp_path) -> RepoConfig:
    return RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=tmp_path / "app.git", needs_work_label=NW
    )


def pr(**kw) -> PrMeta:
    base = dict(
        number=7, title="Add widgets", url="https://x/7", author="dev",
        head_sha="abc123", changed_files=3, labels=("Code Review",), checks=(),
    )
    return PrMeta(**{**base, **kw})


def decide(repo, meta, *, key_done=False, sha_judged=False, threads=0):
    return evaluate(meta, repo, key_done=key_done, sha_judged=sha_judged, open_threads=threads)


# ----- the happy path ----------------------------------------------------


def test_clean_pr_is_reviewed(repo):
    assert decide(repo, pr()).action == "review"


# ----- gate 3: the needs-work brake -------------------------------------


def test_needs_work_label_holds_silently_and_leaves_no_trace(repo):
    d = decide(repo, pr(labels=("Code Review", NW)))
    assert d.action == "hold"
    assert d.dm is None, "a blocked PR is the normal state; it must not DM every tick"
    assert d.record is False, "it has to re-check once the author clears the label"


def test_a_burst_of_commits_under_the_label_still_holds(repo):
    for sha in ("aaa", "bbb", "ccc"):
        assert decide(repo, pr(head_sha=sha, labels=(NW,))).action == "hold"


# ----- gate 4: nothing new ----------------------------------------------


def test_re_request_with_no_new_commits_dms_and_is_recorded(repo):
    d = decide(repo, pr(), sha_judged=True)
    assert d.action == "hold"
    assert d.dm is not None
    assert d.record is True, "no new code to judge; do not look at this key again"


def test_re_request_message_mentions_open_comments_when_there_are_some(repo):
    d = decide(repo, pr(), sha_judged=True, threads=2)
    assert "2 of my comments" in d.dm


def test_empty_pr_is_held(repo):
    d = decide(repo, pr(changed_files=0))
    assert (d.action, d.record) == ("hold", True)


# ----- gate 5: our own open comments ------------------------------------


def test_unanswered_comments_hold_but_stay_retryable(repo):
    d = decide(repo, pr(), threads=1)
    assert d.action == "hold"
    assert d.dm is not None
    assert d.record is False, "a reply or a resolve must bring this straight back"


def test_open_comments_lose_to_the_needs_work_label(repo):
    assert decide(repo, pr(labels=(NW,)), threads=3).dm is None


# ----- gate 6: red CI ---------------------------------------------------


def test_red_ci_posts_the_note_and_stays_retryable(repo):
    d = decide(repo, pr(checks=({"context": "ci/build", "state": "FAILURE"},)))
    assert d.action == "ci-note"
    assert d.checks == ("ci/build",)
    assert d.record is False, "a green build gets the real review next tick"


def test_already_judged_key_is_skipped(repo):
    assert decide(repo, pr(), key_done=True).action == "skip"


# ----- failing_checks: both spellings, and the CodeRabbit exception -----


@pytest.mark.parametrize(
    "checks,expected",
    [
        ((), []),
        (({"context": "ci/build", "state": "SUCCESS"},), []),
        (({"context": "ci/build", "state": "PENDING"},), []),
        (({"context": "CodeRabbit", "state": "FAILURE"},), []),
        (({"context": "ci/build", "state": "FAILURE"},), ["ci/build"]),
        (({"context": "ci/build", "state": "ERROR"},), ["ci/build"]),
        (({"name": "rspec", "conclusion": "FAILURE"},), ["rspec"]),
        (({"name": "rspec", "conclusion": "TIMED_OUT"},), ["rspec"]),
        (({"name": "rspec", "conclusion": "SUCCESS"},), []),
        (({"name": "rspec", "status": "IN_PROGRESS"},), []),
        (
            ({"context": "CodeRabbit", "state": "FAILURE"},
             {"context": "ci/build", "state": "FAILURE"}),
            ["ci/build"],
        ),
    ],
)
def test_failing_checks(checks, expected):
    assert failing_checks(pr(checks=checks), ignore=("CodeRabbit",)) == expected


# ----- the dedup key ---------------------------------------------------


def test_key_changes_with_the_commit_and_with_the_request():
    a = dedup_key("acme/app", 7, "abc", "2026-01-01T00:00:00Z")
    assert a != dedup_key("acme/app", 7, "def", "2026-01-01T00:00:00Z")
    assert a != dedup_key("acme/app", 7, "abc", "2026-02-01T00:00:00Z")
    assert a != dedup_key("acme/other", 7, "abc", "2026-01-01T00:00:00Z")
    assert a == dedup_key("acme/app", 7, "abc", "2026-01-01T00:00:00Z")
