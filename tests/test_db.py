"""State semantics. The distinction that matters: a failed run must be retried,
a recorded hold must not."""

from __future__ import annotations

import pytest

from robbie.db import Db, now_ms

KEY = "acme/app:7:abc123:2026-01-01T00:00:00Z"


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "robbie.db")
    yield d
    d.close()


def start(db, key=KEY, sha="abc123"):
    db.start_review(key=key, repo="acme/app", pr=7, head_sha=sha,
                    requested_at="2026-01-01T00:00:00Z")


def test_a_published_review_blocks_the_same_key(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="needs-work")
    assert db.get_review(KEY).state == "published"
    assert db.sha_was_judged("acme/app", 7, "abc123")


def test_a_failed_run_leaves_the_key_open_for_a_retry(db):
    start(db)
    db.finish_review(KEY, state="failed", hold_reason="container exited 1")
    assert db.get_review(KEY).state == "failed"
    assert not db.sha_was_judged("acme/app", 7, "abc123"), "a crash is not a judgement"


def test_a_recorded_hold_counts_as_judged(db):
    db.record_hold(key=KEY, repo="acme/app", pr=7, head_sha="abc123",
                   requested_at="2026-01-01T00:00:00Z", reason="changes no files")
    assert db.sha_was_judged("acme/app", 7, "abc123")


def test_a_new_commit_is_not_covered_by_the_old_one(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="ok")
    assert not db.sha_was_judged("acme/app", 7, "def456")


def test_another_pr_is_not_covered(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="ok")
    assert not db.sha_was_judged("acme/app", 8, "abc123")


def test_restarting_a_key_is_idempotent(db):
    start(db)
    start(db)
    assert db.get_review(KEY).state == "running"


def test_notice_fires_once_and_never_again(db):
    assert db.notice_once("hold:" + KEY) is True
    assert db.notice_once("hold:" + KEY) is False


def test_orphaned_running_rows_are_failed_at_boot_not_left_hanging(db):
    start(db)
    assert db.reap_running() == 1
    assert db.get_review(KEY).state == "failed"
    assert not db.sha_was_judged("acme/app", 7, "abc123"), "so the next tick retries it"


def test_spend_only_counts_recorded_cost(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", cost_usd=1.25)
    start(db, key="acme/app:8:zzz:t", sha="zzz")
    db.finish_review("acme/app:8:zzz:t", state="failed")
    assert db.spend_since(0) == pytest.approx(1.25)


def test_the_reviewed_list_is_windowed(db):
    """Every PR in it costs a thread read every tick, and the list only grows."""
    db.start_review(key="k1", repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review("k1", state="published", verdict="ok")
    assert db.reviewed_prs("acme/app") == [7]
    assert db.reviewed_prs("acme/app", since_ms=now_ms() + 1000) == []


def test_seeding_is_per_repo(db):
    assert not db.is_seeded("acme/app")
    db.mark_seeded("acme/app")
    assert db.is_seeded("acme/app")
    assert not db.is_seeded("acme/other")
