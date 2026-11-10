"""A review that finds nothing leaves no trace on the PR, and that is the whole
gap this closes.

`ok` clears the label, retracts the rejection and asks for CI — then posts
nothing, so the only record of a clean pass is in Slack and the panel. A reader
of the PR sees silence, which is indistinguishable from robbie never having run.

Off is the shipped behaviour byte for byte; these tests pin both halves.
"""

from __future__ import annotations

import json

import pytest

from robbie import publish as publish_mod
from robbie.config import RepoConfig
from robbie.contract import parse_blocks, preamble
from robbie.github import PrMeta
from robbie.orchestrator import _unusable

META = PrMeta(
    number=7, title="Add widgets", url="https://github.com/acme/app/pull/7",
    author="dev", head_sha="abc123", changed_files=2, labels=(), checks=(),
)


@pytest.fixture
def repo(tmp_path) -> RepoConfig:
    return RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path / "app.git")


@pytest.fixture
def calls(monkeypatch) -> list[tuple]:
    seen: list[tuple] = []

    async def fake_gh(*args, stdin=None):
        seen.append((args, stdin))
        return "https://github.com/acme/app/pull/7#issuecomment-1"

    monkeypatch.setattr(publish_mod, "gh", fake_gh)
    return seen


# ----- the prompt ----------------------------------------------------------

def test_off_asks_for_the_summary_only_when_there_is_something_to_say():
    text = preamble(author="dev")
    assert "ONLY IF the verdict is needs-work or comment" in text
    assert "WALKTHROUGH" not in text


def test_on_the_summary_is_owed_on_every_verdict_including_ok():
    text = preamble(author="dev", walkthrough=True)
    assert "ALWAYS, whatever the verdict, including ok" in text
    assert "WALKTHROUGH" in text
    assert "owed even" in text, "the clean-pass case is the one models skip"
    assert "ONLY IF the verdict is needs-work or comment" not in text


def test_the_default_is_off_so_nobody_gets_this_by_upgrading(repo):
    assert repo.walkthrough is False
    assert "WALKTHROUGH" not in preamble(author="dev", walkthrough=repo.walkthrough)


# ----- the two new finding axes --------------------------------------------

def test_the_json_example_carries_both_keys():
    """The half that matters most: models copy the example far more faithfully
    than they read the prose under it."""
    text = preamble(author="dev")
    example = text.split("<<<INLINE>>>", 1)[1].split("<<<END>>>", 1)[0]
    assert '"path": "app/models' in example, "the INLINE example moved; this test is blind"
    assert '"category"' in example and '"effort"' in example


def test_the_prose_gives_the_vocabulary_for_both():
    """Without this the model invents its own categories and the tags stop
    grouping anything."""
    text = preamble(author="dev")
    rules = text.split("Rules for that array:", 1)[1]
    # the key names too: prose that explains fields the JSON example does not
    # carry is worse than no prose, and renaming one is how that happens
    assert '"category"' in rules and '"effort"' in rules
    assert "Security & Privacy" in rules
    assert "Quick win" in rules and "Bigger change" in rules
    assert "Leave either out rather than guessing" in rules, "a wrong tag is worse than none"


# ----- what counts as unusable ---------------------------------------------

OK_NO_BODY = "<<<VERDICT>>>\nok\n<<<END>>>\n<<<GITHUB>>>\n\n<<<END>>>"
OK_WITH_BODY = "<<<VERDICT>>>\nok\n<<<END>>>\n<<<GITHUB>>>\nThis adds widgets.\n<<<END>>>"


def test_an_empty_ok_is_fine_when_no_walkthrough_was_asked_for():
    assert _unusable(parse_blocks(OK_NO_BODY), summary_on_ok=False) is None


def test_an_empty_ok_is_held_when_the_repo_asked_for_a_walkthrough():
    bad = _unusable(parse_blocks(OK_NO_BODY), summary_on_ok=True)
    assert bad is not None
    assert "walkthrough" in bad.reason


def test_an_ok_that_brought_a_body_is_usable_either_way():
    for asked in (True, False):
        assert _unusable(parse_blocks(OK_WITH_BODY), summary_on_ok=asked) is None


# ----- publishing it -------------------------------------------------------

@pytest.mark.asyncio
async def test_it_posts_a_plain_comment_and_never_touches_the_label(repo, calls):
    result = await publish_mod.publish_walkthrough(repo, META, body="This adds widgets.")
    assert result.posted
    assert len(calls) == 1, "one comment, nothing else"
    args, stdin = calls[0]
    assert "repos/acme/app/issues/7/comments" in args
    body = json.loads(stdin)["body"]
    assert "This adds widgets." in body
    assert "robbie-walkthrough sha=abc123" in body
    # the pairing that matters: publish_review's footer tells the author to take
    # the needs-work label off for another pass. Here there is no label and
    # nothing to come back for, so saying it would be a lie.
    assert "label off" not in body
    assert repo.needs_work_label not in body


@pytest.mark.asyncio
async def test_an_empty_body_posts_nothing_rather_than_an_empty_comment(repo, calls):
    result = await publish_mod.publish_walkthrough(repo, META, body="   \n")
    assert not result.posted
    assert calls == []


@pytest.mark.asyncio
async def test_a_dry_run_reaches_github_not_at_all(repo, calls):
    result = await publish_mod.publish_walkthrough(
        repo, META, body="This adds widgets.", dry_run=True
    )
    assert not result.posted
    assert calls == []
