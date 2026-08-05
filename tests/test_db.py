"""State semantics. The distinction that matters: a failed run must be retried,
a recorded hold must not."""

from __future__ import annotations

import pytest

from robbie.db import SCHEMA_VERSION, Db, now_ms

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


def test_the_model_is_recorded_so_runs_can_be_compared(db):
    db.start_review(key=KEY, repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review(KEY, state="published", verdict="ok", model="glm-5.2:cloud")
    row = db.conn.execute("SELECT model FROM reviews WHERE key=?", (KEY,)).fetchone()
    assert row["model"] == "glm-5.2:cloud"


@pytest.mark.parametrize("start_version", [0, 1])
def test_a_database_older_than_the_code_is_stepped_up(tmp_path, start_version):
    """`CREATE TABLE IF NOT EXISTS` skips a new column, so the ALTERs have to run.

    Built here the way the old code built it, and from each version it could have
    stopped at: a migration only ever tested against a fresh schema tests nothing,
    and one only tested from zero misses resuming halfway.
    """
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
            repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('running','published','held','failed')),
            verdict TEXT, hold_reason TEXT, cost_usd REAL, tokens_in INTEGER,
            tokens_out INTEGER, duration_s REAL, transcript TEXT,
            created_at INTEGER NOT NULL, finished_at INTEGER
        );
        INSERT INTO reviews (key, repo, pr, head_sha, requested_at, state, cost_usd, created_at)
        VALUES ('old-key', 'acme/app', 7, 'abc', 't', 'published', 2.5, 1);
    """)
    if start_version:
        old.execute("ALTER TABLE reviews ADD COLUMN model TEXT")
        old.execute(f"PRAGMA user_version = {start_version}")
    old.commit()
    old.close()

    migrated = Db(path)
    try:
        columns = {r[1] for r in migrated.conn.execute("PRAGMA table_info(reviews)")}
        assert columns >= {"model", "findings", "blocking", "should_fix", "inline"}
        assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.spend_since(0) == 2.5, "the rows that were already there survive"
        migrated.finish_review("old-key", state="published", model="glm-5.2:cloud", findings=3)
    finally:
        migrated.close()


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
