"""The self-queue: authored PRs join the candidates when self_review is on."""

from pathlib import Path

import pytest

from robbie import orchestrator as orch_mod
from robbie.config import RepoConfig


def repo(tmp_path: Path, **kw) -> RepoConfig:
    return RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path, **kw)


def test_self_review_defaults_off(tmp_path):
    assert repo(tmp_path).self_review is False


async def test_candidates_is_requests_only_by_default(tmp_path, monkeypatch):
    calls = []

    async def fake_queue(slug, *, label, reviewer):
        calls.append("queue")
        return [1, 2]

    async def fake_authored(slug, *, label, author):
        calls.append("authored")
        return [9]

    monkeypatch.setattr(orch_mod, "queue", fake_queue)
    monkeypatch.setattr(orch_mod, "authored", fake_authored)
    assert await orch_mod.candidates(repo(tmp_path)) == [1, 2]
    assert calls == ["queue"], "authored search must not run when self_review is off"


async def test_candidates_unions_authored_without_duplicates(tmp_path, monkeypatch):
    seen = {}

    async def fake_queue(slug, *, label, reviewer):
        return [1, 2]

    async def fake_authored(slug, *, label, author):
        seen["author"] = author
        seen["label"] = label
        return [2, 7]

    monkeypatch.setattr(orch_mod, "queue", fake_queue)
    monkeypatch.setattr(orch_mod, "authored", fake_authored)
    got = await orch_mod.candidates(repo(tmp_path, self_review=True))
    assert got == [1, 2, 7], "order-preserving union, no duplicate for #2"
    assert seen == {"author": "rev", "label": "Code Review"}, (
        "the self-queue is the reviewer's own PRs behind the same label gate"
    )
