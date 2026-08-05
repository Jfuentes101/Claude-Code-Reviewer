"""GitHub refuses REQUEST_CHANGES on a PR the token's own user authored. A
$2.42 review must not be thrown away over that."""

from __future__ import annotations

import json

import pytest

from robbie import publish as publish_mod
from robbie.config import RepoConfig
from robbie.github import PrMeta

FINDING = {"path": "a.rb", "line": 2, "severity": "Must-fix", "title": "t", "body": "b"}
PATCH = "@@ -1,2 +1,3 @@\n one\n+two\n+three\n"


@pytest.fixture
def repo(tmp_path) -> RepoConfig:
    return RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path / "app.git")


@pytest.fixture
def calls(monkeypatch) -> list[tuple]:
    """Record every gh invocation instead of making one."""
    seen: list[tuple] = []

    async def fake_gh(*args, stdin=None):
        seen.append((args, stdin))
        joined = " ".join(args)
        if "--paginate" in args and "files" in joined:
            return json.dumps({"filename": "a.rb", "patch": PATCH})
        if "--paginate" in args:  # the marker dedup scan
            return ""
        return "https://github.com/acme/app/pull/7#issuecomment-1"

    monkeypatch.setattr(publish_mod, "gh", fake_gh)
    monkeypatch.setattr(publish_mod, "gh_json", lambda *a, **k: _none())
    return seen


def _none():
    async def _c():
        return []
    return _c()


def pr(author: str = "someone-else") -> PrMeta:
    return PrMeta(
        number=7, title="t", url="https://x/7", author=author, head_sha="abc",
        changed_files=1, labels=(), checks=(),
    )


def endpoints(calls) -> list[str]:
    return [" ".join(a) for a, _ in calls]


async def test_someone_elses_pr_gets_a_real_changes_requested_review(repo, calls):
    result = await publish_mod.publish_review(
        "needs-work", repo, pr(), body="summary", findings=[FINDING], self_login="robbie-bot"
    )
    posted = [e for e in endpoints(calls) if "pulls/7/reviews" in e and "--input" in e]
    assert posted, "a PR by someone else must get the real review event"
    assert result.posted
    assert "requested changes" in result.detail


async def test_your_own_pr_falls_back_to_a_comment_instead_of_losing_the_review(repo, calls):
    result = await publish_mod.publish_review(
        "needs-work", repo, pr(author="me"), body="summary", findings=[FINDING],
        self_login="me",
    )
    assert not any("pulls/7/reviews" in e and "--input" in e for e in endpoints(calls))
    assert any("issues/7/comments" in e for e in endpoints(calls))
    assert result.posted
    assert "commented" in result.detail


async def test_the_fallback_says_why_in_the_body(repo, calls):
    await publish_mod.publish_review(
        "needs-work", repo, pr(author="me"), body="summary", findings=[FINDING],
        self_login="me",
    )
    body = next(
        json.loads(s)["body"] for a, s in calls
        if s and "issues/7/comments" in " ".join(a)
    )
    assert "does not allow requesting changes on your own" in body
    assert "summary" in body


async def test_the_fallback_still_carries_the_inline_findings(repo, calls):
    await publish_mod.publish_review(
        "needs-work", repo, pr(author="me"), body="summary", findings=[FINDING],
        self_login="me",
    )
    inline = [s for a, s in calls if s and "pulls/7/comments" in " ".join(a)]
    assert len(inline) == 1
    assert json.loads(inline[0])["line"] == 2


async def test_the_needs_work_label_still_goes_on(repo, calls):
    await publish_mod.publish_review(
        "needs-work", repo, pr(author="me"), body="summary", findings=[FINDING],
        self_login="me",
    )
    assert any("--add-label" in e for e in endpoints(calls)), (
        "the label is what actually holds the next pass"
    )


async def test_an_unresolved_token_login_changes_nothing(repo, calls):
    await publish_mod.publish_review(
        "needs-work", repo, pr(author="me"), body="summary", findings=[FINDING],
        self_login=None,
    )
    assert any("pulls/7/reviews" in e and "--input" in e for e in endpoints(calls))


async def test_a_plain_comment_verdict_is_unaffected(repo, calls):
    result = await publish_mod.publish_review(
        "comment", repo, pr(), body="summary", findings=[FINDING], self_login="robbie-bot"
    )
    assert "commented" in result.detail
    assert not any("pulls/7/reviews" in e and "--input" in e for e in endpoints(calls))
