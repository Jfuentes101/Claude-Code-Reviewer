"""The worklist Jane reads: what is on the operator's plate, from robbie's side."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "src")

from robbie import export as export_mod
from robbie.config import Config, IssueConfig, RepoConfig, SlackConfig
from robbie.db import Db
from robbie.github import PrMeta
from robbie.slack import Slack


def _cfg(tmp: Path, **extra) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"), state_dir=tmp,
        repos=[RepoConfig(
            slug="a/b", reviewer_login="me", bare=Path("/m"),
            issues=IssueConfig(labels=("bug", "needs-triage"), clears="needs-triage",
                               fixable_label="robbie-fix"),
        )],
        **extra,
    )


def _pr(n, *, author="dev", labels=(), branch="feat/x", head="h", draft=False):
    return {"number": n, "title": f"t{n}", "url": f"https://gh/{n}",
            "author": {"login": author}, "labels": [{"name": x} for x in labels],
            "headRefName": branch, "headRefOid": head, "isDraft": draft,
            "createdAt": "2026-09-29T10:00:00Z", "reviewDecision": None}


async def test_worklist_sorts_the_plate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = Db(tmp_path / "r.db")
    db.start_review(key="k1", repo="a/b", pr=1, head_sha="h1", requested_at="t")
    db.finish_review("k1", state="published", verdict="ok", ci_state="green")
    db.start_review(key="k2", repo="a/b", pr=2, head_sha="old", requested_at="t")
    db.finish_review("k2", state="published", verdict="ok", ci_state="green")
    db.set_requested("a/b", [1, 2])

    open_prs = [
        _pr(1, head="h1"),
        _pr(2, head="new"),  # pushed after the ok: not approved any more
        _pr(3, labels=["❌ NEEDS WORK! ❌"]),
        _pr(4, branch="robbie/issue-77", draft=True),
        _pr(5, branch="fix/issue-78", labels=["Code Review"]),  # already in CR
        _pr(6, author="me", head="m6"),
    ]

    async def fake_gh_json(*args):
        if args[:2] == ("issue", "list"):
            assert "--assignee" in args and "needs-triage" not in args
            return [{"number": 90, "title": "bug", "url": "https://gh/i/90"}]
        return open_prs

    async def fake_meta(repo, n):
        return PrMeta(number=n, title="", url="", author="me", head_sha="m6",
                      changed_files=1, labels=(),
                      checks=({"context": "ci", "state": "FAILURE"},))

    reads: list[int] = []

    async def counted_meta(repo, n):
        reads.append(n)
        return await fake_meta(repo, n)

    monkeypatch.setattr(export_mod, "gh_json", fake_gh_json)
    monkeypatch.setattr(export_mod, "pr_meta", counted_meta)
    monkeypatch.setattr(export_mod, "_CI", {})
    await export_mod.export_once(cfg, db)
    await export_mod.export_once(cfg, db)
    assert reads == [6]  # a red commit is not read twice

    data = json.loads((tmp_path / "export" / "worklist.json").read_text())
    assert [p["number"] for p in data["awaiting_you"]] == [1]
    assert [p["number"] for p in data["needs_work"]] == [3]
    assert [(p["number"], p["issue"]) for p in data["fixer_prs"]] == [(4, 77)]
    assert [p["number"] for p in data["triage_assigned"]] == [90]
    assert data["mine"][0]["number"] == 6 and data["mine"][0]["ci"] == "red"
    assert (tmp_path / "export" / "usage.json").exists()


def test_digest_is_due_once_its_hour_comes(tmp_path):
    off = _cfg(tmp_path)
    monday_16 = datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert export_mod.digest_due(off, monday_16) is None
    on = _cfg(tmp_path, digest_weekday=0, digest_utc_hour=15)
    assert export_mod.digest_due(on, monday_16) == "digest:2026-W40"
    assert export_mod.digest_due(on, datetime(2026, 9, 28, 14, tzinfo=UTC)) is None
    assert export_mod.digest_due(on, datetime(2026, 9, 29, 16, tzinfo=UTC)) is None


async def test_owner_dm_goes_to_jane_and_falls_back_to_slack(tmp_path, monkeypatch):
    posted: list[str] = []
    answers = iter([True, False])

    async def relay(text):
        return next(answers)

    slack = Slack(token="x", owner_id="U0", users_file=tmp_path / "u.tsv", relay=relay)

    async def post(channel, text):
        posted.append(channel)
        return True

    monkeypatch.setattr(slack, "post", post)
    assert await slack.dm_owner("taken by jane")
    assert posted == []
    assert await slack.dm_owner("jane said no")
    assert posted == ["U0"]
