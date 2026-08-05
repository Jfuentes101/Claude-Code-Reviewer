"""Author lookup and copy. Guessing a Slack id would DM a stranger, so an
unmapped login has exactly one correct outcome: no DM."""

from __future__ import annotations

import pytest

from robbie.config import SlackConfig
from robbie.slack import Slack, verdict_phrase


@pytest.fixture
def slack(tmp_path):
    users = tmp_path / "slack-users.tsv"
    users.write_text(
        "# commented-out\tU0NOPE\n"
        "octocat\tU01000001\n"
        "hubot\tU01000002\n"
        "monalisa\tU01000003\t# Example Person\n",
        encoding="utf-8",
    )
    return Slack(token="x", owner_id="U0OWNER", users_file=users)


def test_mapped_login(slack):
    assert slack.lookup("octocat") == "U01000001"


def test_case_does_not_matter(slack):
    assert slack.lookup("Hubot") == "U01000002"


def test_a_trailing_comment_on_the_row_is_ignored(slack):
    assert slack.lookup("monalisa") == "U01000003"


def test_a_commented_out_row_does_not_map(slack):
    assert slack.lookup("commented-out") is None


def test_an_unmapped_login_returns_nothing(slack):
    assert slack.lookup("someone-else") is None


def test_a_missing_file_returns_nothing_instead_of_raising(tmp_path):
    s = Slack(token="x", owner_id="U0", users_file=tmp_path / "nope.tsv")
    assert s.lookup("octocat") is None


async def test_bots_never_get_dms(slack):
    assert await slack.dm_author("app/dependabot", "hi") == "bot"
    assert await slack.dm_author("renovate[bot]", "hi") == "bot"


async def test_unmapped_authors_are_reported_not_guessed(slack):
    assert await slack.dm_author("stranger", "hi") == "unmapped"


# ----- copy -------------------------------------------------------------


async def test_an_approval_reaches_everyone_who_shares_the_queue(tmp_path, monkeypatch):
    """The one verdict with no trace on the PR, so the DM is the whole signal."""
    sent: list[str] = []
    slack = Slack(
        token="t", owner_id="U0OWNER", users_file=tmp_path / "none.tsv",
        approved_ids=("U0ME", "U0JIMMY"),
    )
    monkeypatch.setattr(slack, "post", _record(sent))
    assert await slack.dm_reviewers("nothing to fix")
    assert sent == ["U0ME", "U0JIMMY"]


def test_no_list_configured_means_just_the_owner():
    cfg = SlackConfig(owner_id="U0OWNER")
    assert cfg.approved_dm == ("U0OWNER",)
    assert SlackConfig(owner_id="U0OWNER", approved_ids=["U0A", "U0B"]).approved_dm == (
        "U0A", "U0B"
    )


def _record(sent: list[str]):
    async def post(channel: str, text: str) -> bool:
        sent.append(channel)
        return True
    return post


def test_needs_work_phrase_counts_blockers():
    assert "2 things to fix" in verdict_phrase("needs-work", blocking=2, should_fix=0)
    assert "1 thing to fix" in verdict_phrase("needs-work", blocking=1, should_fix=5)


def test_needs_work_without_a_count_still_reads_right():
    assert verdict_phrase("needs-work", blocking=0, should_fix=0) == "*changes requested*"


def test_comment_phrase_leads_with_not_blocking():
    phrase = verdict_phrase("comment", blocking=0, should_fix=3)
    assert phrase.startswith("*reviewed, nothing blocking*")
    assert "3 things should be fixed" in phrase
