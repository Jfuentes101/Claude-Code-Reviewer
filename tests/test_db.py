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


def test_settle_done_retires_every_row_for_the_pr(db):
    """Not just the approval: the reply sweep reads needs-work passes too."""
    for key, verdict in ((KEY, "ok"), (KEY + ":2", "needs-work")):
        start(db, key=key)
        db.finish_review(key, state="published", verdict=verdict)
    assert db.settle_done("acme/app", 7) == 2
    assert db.reviewed_prs("acme/app") == []
    assert db.settle_done("acme/app", 7) == 0, "a no-op once it is already done"


def test_settle_done_leaves_other_prs_alone(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", inline=1)
    assert db.settle_done("acme/app", 999) == 0
    assert db.reviewed_prs("acme/app") == [7]


def test_the_panel_shows_one_row_per_pr_not_one_per_commit(db):
    """Pushes during an open request share `requested_at` and differ only by sha."""
    for sha in ("abc123", "def456", "ghi789"):
        key = f"acme/app:7:{sha}:2026-01-01T00:00:00Z"
        start(db, key=key, sha=sha)
        db.finish_review(key, state="published", verdict="ok", ci_state="green")
    db.set_requested("acme/app", [7])
    rows = db.approved_and_green(0)
    assert [(r["pr"], r["head_sha"]) for r in rows] == [(7, "ghi789")]


def test_the_panel_skips_an_approval_nobody_is_waiting_on(db):
    """A changes-requested consumes the request; an author who never asks again
    leaves an approval that is true and that nobody is blocked on."""
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", ci_state="green")
    assert db.approved_and_green(0) == [], "no queue read has seen this PR"
    db.set_requested("acme/app", [7])
    assert [r["pr"] for r in db.approved_and_green(0)] == [7]
    db.set_requested("acme/app", [])
    assert db.approved_and_green(0) == [], "the request went away, so did the row"


def test_a_brake_label_is_reversible_but_a_moved_head_is_not(db):
    """The two ways off the board differ on purpose. Taking a needs-work label off
    puts the PR back with the approval it already had; a new commit must not — that
    code has not been reviewed, so it waits for a pass."""
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", ci_state="green")
    db.set_requested("acme/app", [7])
    assert len(db.approved_and_green(0)) == 1

    assert db.unrequest("acme/app", [7]) == 1, "a brake label went on"
    assert db.approved_and_green(0) == []
    db.set_requested("acme/app", [7])  # next tick, label gone, head unchanged
    assert [r["pr"] for r in db.approved_and_green(0)] == [7], "same commit, still approved"

    db.settle_stale("acme/app", {7: "def456"})  # they pushed
    db.set_requested("acme/app", [7])
    assert db.approved_and_green(0) == [], "new code needs a pass before the board"

    later = "acme/app:7:def456:2026-01-01T00:00:00Z"
    start(db, key=later, sha="def456")
    db.finish_review(later, state="published", verdict="ok", ci_state="green")
    approved = [r["head_sha"] for r in db.approved_and_green(0)]
    assert approved == ["def456"], "the pass brings it back"


def test_unrequest_leaves_other_prs_and_repos_alone(db):
    for pr_num in (7, 8):
        key = f"acme/app:{pr_num}:abc123:t"
        db.start_review(key=key, repo="acme/app", pr=pr_num, head_sha="abc123",
                        requested_at="t")
        db.finish_review(key, state="published", verdict="ok", ci_state="green")
    db.set_requested("acme/app", [7, 8])
    db.set_requested("other/repo", [7])
    assert db.unrequest("acme/app", [7]) == 1
    assert [r["pr"] for r in db.approved_and_green(0)] == [8]
    assert db.unrequest("acme/app", []) == 0, "an empty list is a no-op, not a wipe"


def test_settle_stale_drops_approvals_of_a_commit_that_moved(db):
    """`ci_watch` stops looking once a build reports, so the sweep is what notices."""
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", ci_state="green")
    db.set_requested("acme/app", [7])
    assert db.settle_stale("acme/app", {7: "abc123"}) == 0, "still the head"
    assert len(db.approved_and_green(0)) == 1
    assert db.settle_stale("acme/app", {7: "def456"}) == 1
    assert db.approved_and_green(0) == []
    assert db.settle_stale("acme/app", {7: "def456"}) == 0, "a no-op the second time"


def test_a_later_verdict_retires_the_earlier_approval(db):
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", ci_state="green")
    db.set_requested("acme/app", [7])
    assert len(db.approved_and_green(0)) == 1
    later = "acme/app:7:def456:2026-01-01T00:00:00Z"
    start(db, key=later, sha="def456")
    db.finish_review(later, state="published", verdict="needs-work")
    assert db.approved_and_green(0) == [], "the newest pass says the PR is not ready"


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
    db.finish_review("k1", state="published", verdict="ok", inline=1)
    assert db.reviewed_prs("acme/app") == [7]
    assert db.reviewed_prs("acme/app", since_ms=now_ms() + 1000) == []


def test_finishing_twice_keeps_what_the_first_call_wrote(db):
    """The publish path writes the row, posts, then writes the anchored count.

    A full-column UPDATE made that second call wipe the cost, the model and the
    counts unless every caller repeated them — silently, on a real review.
    """
    db.start_review(key="k1", repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review(
        "k1", state="published", verdict="needs-work",
        cost_usd=4.2, model="sonnet", findings=6, tokens_out=900,
    )
    db.finish_review("k1", state="published", verdict="needs-work", inline=4)

    row = db.conn.execute("SELECT * FROM reviews WHERE key='k1'").fetchone()
    assert (row["cost_usd"], row["model"], row["findings"], row["tokens_out"]) == (
        4.2, "sonnet", 6, 900
    )
    assert row["inline"] == 4


def test_seeding_is_per_repo(db):
    assert not db.is_seeded("acme/app")
    db.mark_seeded("acme/app")
    assert db.is_seeded("acme/app")
    assert not db.is_seeded("acme/other")


def test_expired_ci_watch_settles_instead_of_freezing(db):
    """A daemon that slept through the watch window must not leave the
    approval 'waiting' forever — unsweepable and unsettled on the panel."""
    start(db)
    db.finish_review(KEY, state="published", verdict="ok", ci_state="waiting")
    assert [r["key"] for r in db.watching_ci(0)] == [KEY]
    assert db.expire_ci_watch(now_ms() + 1) == 1
    assert db.watching_ci(0) == [], "expired rows leave the sweep"
    assert db.expire_ci_watch(now_ms() + 1) == 0, "settling is terminal, not repeated"


def test_threadless_reviews_leave_the_reply_sweep(db):
    """A pass that anchored nothing opened no threads — sweeping it every tick
    buys nothing and the reads drain the user-wide GraphQL pool."""
    start(db)
    db.finish_review(KEY, state="published", verdict="needs-work", inline=0)
    assert db.reviewed_prs("acme/app") == [], "anchored nothing, nothing to sweep"
    key2 = "acme/app:8:def456:2026-01-01T00:00:00Z"
    db.start_review(key=key2, repo="acme/app", pr=8, head_sha="def456",
                    requested_at="2026-01-01T00:00:00Z")
    db.finish_review(key2, state="published", verdict="needs-work", inline=2)
    assert db.reviewed_prs("acme/app") == [8], "anchored comments earn the sweep"


def test_a_pass_that_never_recorded_inline_keeps_its_place_in_the_sweep(db):
    """NULL is not zero: rows predating the column, and every approval, leave it
    unwritten. The sweep answers every thread the login opened, robbie's or the
    operator's own, so an unwritten count is no evidence there is nothing there."""
    start(db)
    db.finish_review(KEY, state="published", verdict="ok")
    assert db.reviewed_prs("acme/app") == [7]
