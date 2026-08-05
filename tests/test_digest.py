"""The nag for PRs parked on a standing changes-requested review.

These fall out of every queue — the review cleared the request — so this is the
only thing that ever mentions them again. It goes to a team channel with an
`<!here>`, so who lands in it has to be right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robbie import github as gh_mod
from robbie.config import Config, RepoConfig, SlackConfig
from robbie.digest import _lines, post_digest
from robbie.github import stale_changes_requested


def node(number: int, *, review_days: int, push_days: int = 0) -> dict:
    now = datetime.now(UTC)
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://x/{number}",
        "author": {"login": "dev"},
        "reviews": {"nodes": [{"submittedAt": (now - timedelta(days=review_days)).isoformat()}]},
        "commits": {"nodes": [
            {"commit": {"committedDate": (now - timedelta(days=push_days)).isoformat()}}
        ]},
    }


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def post(self, channel: str, text: str) -> bool:
        self.posts.append((channel, text))
        return True


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"),
        state_dir=tmp_path,
        repos=[RepoConfig(
            slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
            label="Needs Review", slack_channel="C0CHAN",
        )],
    )


async def test_the_search_uses_the_repo_own_queue_label(monkeypatch):
    """The label is per repo in the config, so it cannot be baked into the query."""
    seen: list[str] = []

    async def fake(*args, **kw):
        seen.extend(args)
        return {}

    monkeypatch.setattr(gh_mod, "gh_json", fake)
    await stale_changes_requested("acme/app", "rev", label="Needs Review")
    assert any('label:"Needs Review"' in a for a in seen)
    assert not any("Code Review" in a for a in seen)


def test_only_reviews_older_than_the_threshold_are_listed():
    lines = _lines([node(1, review_days=9), node(2, review_days=3)], 7)
    assert len(lines) == 1 and "#1" in lines[0]


def test_the_oldest_comes_first():
    lines = _lines([node(1, review_days=8), node(2, review_days=30)], 7)
    assert ["#2" in lines[0], "#1" in lines[1]] == [True, True]


def test_a_recent_push_is_reported_as_such():
    assert "last push today" in _lines([node(1, review_days=9, push_days=0)], 7)[0]


def test_an_unparseable_date_drops_the_row_instead_of_guessing():
    broken = node(1, review_days=9)
    broken["reviews"]["nodes"] = [{"submittedAt": "not a date"}]
    assert _lines([broken], 7) == []


async def test_nothing_stale_posts_nothing(cfg, monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr("robbie.digest.stale_changes_requested", _async([]))
    assert await post_digest(cfg, slack, days=7) == 0  # type: ignore[arg-type]
    assert slack.posts == []


async def test_a_repo_without_a_channel_is_skipped(cfg, monkeypatch):
    cfg.repos[0].slack_channel = None
    slack = FakeSlack()
    monkeypatch.setattr("robbie.digest.stale_changes_requested", _async([node(1, review_days=9)]))
    assert await post_digest(cfg, slack, days=7) == 0  # type: ignore[arg-type]
    assert slack.posts == [], "there is nowhere to nag"


async def test_the_digest_names_the_channel_and_the_prs(cfg, monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr(
        "robbie.digest.stale_changes_requested",
        _async([node(1, review_days=9), node(2, review_days=20)]),
    )
    assert await post_digest(cfg, slack, days=7) == 2  # type: ignore[arg-type]
    channel, text = slack.posts[0]
    assert channel == "C0CHAN"
    assert "2 PRs stuck" in text and "#2" in text and "#1" in text


def _async(value):
    async def _call(*a, **kw):
        return value
    return _call
