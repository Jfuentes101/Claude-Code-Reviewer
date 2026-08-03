"""Answering replies: concede, push back, or leave it alone.

The loop has to converge. Robbie answering a thread gives it the last word, so
the next run must skip it and wait for the author — otherwise two parties who
both always answer never stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robbie import orchestrator as orch_mod
from robbie import publish as publish_mod
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.contract import parse_thread_verdicts, thread_preamble
from robbie.db import Db
from robbie.github import PrMeta, Thread
from robbie.orchestrator import Orchestrator
from robbie.publish import PublishResult
from robbie.runner import ReviewRun


def thread(cid: int, **kw) -> Thread:
    base = dict(
        path="app/models/payment.rb", line=42, resolved=False, outdated=False,
        mine="🔴 **Must-fix** — Guard the nil case", replies=(),
        node_id=f"PRRT_{cid}", comment_id=cid,
    )
    merged = {**base, **kw}
    merged.setdefault("mine_is_last", not merged["replies"])
    return Thread(**merged)


# ----- the contract -----------------------------------------------------


def test_resolve_needs_no_body():
    v = parse_thread_verdicts("<<<THREAD 555>>>\nresolve\n<<<END>>>")
    assert (v[0].comment_id, v[0].action) == (555, "resolve")


def test_reply_carries_its_text():
    v = parse_thread_verdicts(
        "<<<THREAD 555>>>\nreply\nThe orphaned case still reaches it.\n<<<END>>>"
    )
    assert v[0].action == "reply"
    assert v[0].body == "The orphaned case still reaches it."


def test_leave_is_accepted():
    assert parse_thread_verdicts("<<<THREAD 9>>>\nleave\n<<<END>>>")[0].action == "leave"


def test_several_threads_in_one_run():
    text = (
        "<<<THREAD 1>>>\nresolve\n<<<END>>>\n"
        "<<<THREAD 2>>>\nreply\nnot quite\n<<<END>>>\n"
        "<<<THREAD 3>>>\nleave\n<<<END>>>"
    )
    assert [(v.comment_id, v.action) for v in parse_thread_verdicts(text)] == [
        (1, "resolve"), (2, "reply"), (3, "leave")
    ]


@pytest.mark.parametrize("text", [
    "<<<THREAD 555>>>\nreply\n\n<<<END>>>",      # reply with no body
    "<<<THREAD 555>>>\nmaybe\n<<<END>>>",        # not one of the three words
    "<<<THREAD abc>>>\nresolve\n<<<END>>>",      # non-numeric id
    "<<<THREAD 555>>>\n\n<<<END>>>",             # empty
    "resolve thread 555 please",                  # no markers
])
def test_anything_malformed_is_dropped_rather_than_guessed(text):
    assert parse_thread_verdicts(text) == []


def test_the_prompt_lists_each_thread_with_its_reply():
    text = thread_preamble(
        author="dev", url="https://x/7",
        threads=[thread(555, replies=(("dev", "the payout is always set"),))],
    )
    assert "THREAD 555" in text
    assert "app/models/payment.rb:42" in text
    assert "dev replied: the payout is always set" in text


def test_the_prompt_pushes_toward_conceding():
    text = " ".join(thread_preamble(author="dev", url="u", threads=[thread(1)]).split())
    assert "Prefer this" in text
    assert "A reviewer who cannot concede a point is noise" in text
    assert "If it is a matter of taste, resolve" in text


def test_the_prompt_says_a_reply_holds_the_next_pass():
    text = " ".join(thread_preamble(author="dev", url="u", threads=[thread(1)]).split())
    assert "puts the ball back in their court and holds the next review pass" in text


# ----- the loop ---------------------------------------------------------


class FakeSlack:
    def __init__(self) -> None:
        self.owner: list[str] = []

    async def dm_owner(self, text: str) -> bool:
        self.owner.append(text)
        return True

    async def post(self, channel: str, text: str) -> bool:
        return True

    async def dm_author(self, login: str, text: str) -> str:
        return "sent"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    repo = RepoConfig(slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"))
    cfg = Config(
        slack=SlackConfig(owner_id="U0"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5),
    )
    db = Db(tmp_path / "robbie.db")
    db.start_review(key="k7", repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review("k7", state="published", verdict="needs-work")
    o = Orchestrator(
        cfg,
        Secrets(gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="k"),
        db, FakeSlack(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(orch_mod, "pr_meta", _async(PrMeta(
        number=7, title="t", url="https://x/7", author="dev", head_sha="abc",
        changed_files=1, labels=(), checks=(), state="OPEN",
    )))
    yield o
    db.close()


def _async(value):
    async def _call(*a, **kw):
        return value
    return _call


@pytest.fixture
def acted(monkeypatch) -> dict:
    seen: dict = {"resolved": [], "replied": []}

    async def fake_resolve(repo, node_id, *, dry_run=False):
        seen["resolved"].append(node_id)
        return PublishResult(True, "resolved")

    async def fake_reply(repo, pr, cid, body, *, dry_run=False):
        seen["replied"].append((cid, body))
        return PublishResult(True, "replied")

    monkeypatch.setattr(publish_mod, "resolve_thread", fake_resolve)
    monkeypatch.setattr(publish_mod, "reply_to_thread", fake_reply)
    return seen


def stub_run(monkeypatch, text: str) -> None:
    monkeypatch.setattr(orch_mod, "run_review", _async(
        ReviewRun(ok=True, text=text, cost_usd=0.05, duration_s=1.0)
    ))


async def test_a_thread_we_spoke_last_on_is_left_alone(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([thread(555)]))
    ran = []
    monkeypatch.setattr(orch_mod, "run_review", lambda *a, **k: ran.append(1))
    assert await orch.answer_threads() == []
    assert ran == [], "no container should start when nobody is waiting on us"


async def test_after_answering_it_will_not_answer_again(orch, monkeypatch, acted):
    """The convergence property. Two parties who both always answer never stop."""
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "cannot happen"), ("rev", "an orphan reaches it")),
               mine_is_last=True),
    ]))
    ran = []
    monkeypatch.setattr(orch_mod, "run_review", lambda *a, **k: ran.append(1))
    assert await orch.answer_threads() == []
    assert ran == [], "robbie already had the last word; it is the author's move"


async def test_the_author_coming_back_again_reopens_our_move(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "no"), ("rev", "yes"), ("dev", "still no")),
               mine_is_last=False),
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nresolve\n<<<END>>>")
    out = await orch.answer_threads()
    assert acted["resolved"] == ["PRRT_555"]
    assert "1 resolve" in out[0].detail


async def test_a_resolved_thread_is_ignored(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, resolved=True, replies=(("dev", "fixed"),))
    ]))
    assert await orch.answer_threads() == []


async def test_conceding_closes_the_thread_and_says_nothing(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "the payout is always set here"),))
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nresolve\n<<<END>>>")
    out = await orch.answer_threads()
    assert acted["resolved"] == ["PRRT_555"]
    assert acted["replied"] == []
    assert "1 resolve" in out[0].detail
    assert orch.slack.owner == [], "a concession is not worth a notification"


async def test_pushing_back_posts_the_reply_and_tells_the_owner(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "cannot happen"),))
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nreply\nAn orphaned payment reaches it.\n<<<END>>>")
    out = await orch.answer_threads()
    assert acted["replied"] == [(555, "An orphaned payment reaches it.")]
    assert acted["resolved"] == []
    assert "1 reply" in out[0].detail
    assert "ball is back with dev" in orch.slack.owner[0]


async def test_leave_touches_nothing(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "depends on the other PR"),))
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nleave\n<<<END>>>")
    out = await orch.answer_threads()
    assert (acted["resolved"], acted["replied"]) == ([], [])
    assert "1 leave" in out[0].detail


async def test_a_thread_the_model_skipped_is_left_alone_not_guessed(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "?"),)), thread(666, replies=(("dev", "?"),))
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nresolve\n<<<END>>>")
    out = await orch.answer_threads()
    assert acted["resolved"] == ["PRRT_555"]
    assert "1 unanswered" in out[0].detail


async def test_a_mixed_batch_is_handled_in_one_container(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(1, replies=(("dev", "a"),)),
        thread(2, replies=(("dev", "b"),)),
        thread(3, replies=(("dev", "c"),)),
    ]))
    spawns = []

    async def counting(*a, **kw):
        spawns.append(1)
        return ReviewRun(ok=True, cost_usd=0.05, text=(
            "<<<THREAD 1>>>\nresolve\n<<<END>>>\n"
            "<<<THREAD 2>>>\nreply\nstill stands\n<<<END>>>\n"
            "<<<THREAD 3>>>\nleave\n<<<END>>>"
        ))

    monkeypatch.setattr(orch_mod, "run_review", counting)
    out = await orch.answer_threads()
    assert len(spawns) == 1, "one container per PR, not per thread"
    assert acted["resolved"] == ["PRRT_1"]
    assert acted["replied"] == [(2, "still stands")]
    assert out[0].detail.count(",") == 2


async def test_a_closed_pr_is_skipped_before_spawning_anything(orch, monkeypatch, acted):
    monkeypatch.setattr(orch_mod, "pr_meta", _async(PrMeta(
        number=7, title="t", url="u", author="dev", head_sha="abc",
        changed_files=1, labels=(), checks=(), state="MERGED",
    )))
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "done"),))
    ]))
    ran = []
    monkeypatch.setattr(orch_mod, "run_review", lambda *a, **k: ran.append(1))
    out = await orch.answer_threads()
    assert out[0].action == "skip" and "merged" in out[0].detail
    assert ran == []


async def test_only_prs_with_published_reviews_are_looked_at(orch, monkeypatch, acted):
    looked: list[int] = []

    async def spy(repo, pr, reviewer):
        looked.append(pr)
        return []

    monkeypatch.setattr(orch_mod, "my_threads", spy)
    await orch.answer_threads()
    assert looked == [7], "robbie can only have threads where it published a review"


async def test_no_publish_acts_on_nothing(orch, monkeypatch):
    orch.no_publish = True
    dry: list[bool] = []

    async def fake_resolve(repo, node_id, *, dry_run=False):
        dry.append(dry_run)
        return PublishResult(False, "dry run")

    monkeypatch.setattr(publish_mod, "resolve_thread", fake_resolve)
    monkeypatch.setattr(orch_mod, "my_threads", _async([
        thread(555, replies=(("dev", "x"),))
    ]))
    stub_run(monkeypatch, "<<<THREAD 555>>>\nresolve\n<<<END>>>")
    await orch.answer_threads()
    assert dry == [True]
