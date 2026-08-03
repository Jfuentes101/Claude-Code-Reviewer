"""SQLite state. Replaces git-sentinel's seen.txt / state.tsv / hold: prefixes.

Two things the text files could not do: express "held, retry next tick" vs
"held, done with this key" as data instead of an absent line, and survive
several reviews finishing at once.

ponytail: sync sqlite3, single writer process. Calls are sub-ms against a poll
loop, so the async wrapper would buy nothing. If robbie ever runs more than one
orchestrator, this is the thing to move to postgres.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT    NOT NULL UNIQUE,   -- repo:pr:head_sha:requested_at
    repo         TEXT    NOT NULL,
    pr           INTEGER NOT NULL,
    head_sha     TEXT    NOT NULL,
    requested_at TEXT    NOT NULL,
    state        TEXT    NOT NULL
                 CHECK (state IN ('running','published','held','failed')),
    verdict      TEXT,                      -- needs-work | comment | ok
    hold_reason  TEXT,
    cost_usd     REAL,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    duration_s   REAL,
    transcript   TEXT,
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reviews_pr      ON reviews(repo, pr, head_sha);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at DESC);

-- one-shot operator notices, so a hold never DMs twice for the same key
CREATE TABLE IF NOT EXISTS notices (
    key        TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);

-- cold start: the first poll of a repo records its backlog instead of
-- reviewing it, so enabling robbie can't trigger a review storm
CREATE TABLE IF NOT EXISTS seeded (
    repo      TEXT PRIMARY KEY,
    seeded_at INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ReviewRow:
    key: str
    repo: str
    pr: int
    head_sha: str
    state: str
    verdict: str | None
    hold_reason: str | None


class Db:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ----- reviews ------------------------------------------------------

    def get_review(self, key: str) -> ReviewRow | None:
        row = self.conn.execute(
            "SELECT key, repo, pr, head_sha, state, verdict, hold_reason "
            "FROM reviews WHERE key = ?",
            (key,),
        ).fetchone()
        return ReviewRow(**dict(row)) if row else None

    def sha_was_judged(self, repo: str, pr: int, head_sha: str) -> bool:
        """True when this exact commit already got a pass or a recorded hold.

        Gate 4 leans on this: a re-request that changes the key but not the sha
        means the author asked again with no new code.
        """
        row = self.conn.execute(
            "SELECT 1 FROM reviews WHERE repo=? AND pr=? AND head_sha=? "
            "AND state IN ('published','held') LIMIT 1",
            (repo, pr, head_sha),
        ).fetchone()
        return row is not None

    def start_review(
        self, *, key: str, repo: str, pr: int, head_sha: str, requested_at: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reviews "
            "(key, repo, pr, head_sha, requested_at, state, created_at) "
            "VALUES (?,?,?,?,?, 'running', ?)",
            (key, repo, pr, head_sha, requested_at, now_ms()),
        )

    def finish_review(
        self,
        key: str,
        *,
        state: str,
        verdict: str | None = None,
        hold_reason: str | None = None,
        cost_usd: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        duration_s: float | None = None,
        transcript: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE reviews SET state=?, verdict=?, hold_reason=?, cost_usd=?, "
            "tokens_in=?, tokens_out=?, duration_s=?, transcript=?, finished_at=? "
            "WHERE key=?",
            (state, verdict, hold_reason, cost_usd, tokens_in, tokens_out,
             duration_s, transcript, now_ms(), key),
        )

    def record_hold(self, *, key: str, repo: str, pr: int, head_sha: str,
                    requested_at: str, reason: str) -> None:
        """A hold we are done with for this key — it will not be retried."""
        self.conn.execute(
            "INSERT OR REPLACE INTO reviews "
            "(key, repo, pr, head_sha, requested_at, state, hold_reason, created_at, finished_at) "
            "VALUES (?,?,?,?,?, 'held', ?, ?, ?)",
            (key, repo, pr, head_sha, requested_at, reason, now_ms(), now_ms()),
        )

    def passes_for(self, repo: str, pr: int) -> list[sqlite3.Row]:
        """Earlier judged passes on this PR, oldest first."""
        return list(
            self.conn.execute(
                "SELECT head_sha, verdict, state, hold_reason, created_at FROM reviews "
                "WHERE repo=? AND pr=? AND state IN ('published','held') "
                "ORDER BY created_at",
                (repo, pr),
            )
        )

    def reviewed_prs(self, repo: str) -> list[int]:
        """PRs this repo has published reviews on — where threads of ours can exist."""
        return [
            int(r["pr"]) for r in self.conn.execute(
                "SELECT DISTINCT pr FROM reviews WHERE repo=? AND state='published' "
                "ORDER BY pr DESC",
                (repo,),
            )
        ]

    def running(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT repo, pr, head_sha, created_at FROM reviews WHERE state='running' "
                "ORDER BY created_at"
            )
        )

    def reap_running(self) -> int:
        """Mark orphaned 'running' rows failed at boot.

        A row can only be 'running' while this process holds the container; if
        we are starting up, whoever owned it is gone.
        """
        cur = self.conn.execute(
            "UPDATE reviews SET state='failed', hold_reason='orchestrator restarted', "
            "finished_at=? WHERE state='running'",
            (now_ms(),),
        )
        return cur.rowcount or 0

    def spend_since(self, since_ms: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS usd FROM reviews WHERE created_at >= ?",
            (since_ms,),
        ).fetchone()
        return float(row["usd"])

    # ----- notices / seeding --------------------------------------------

    def notice_once(self, key: str) -> bool:
        """True the first time this key is seen; False every time after."""
        try:
            self.conn.execute(
                "INSERT INTO notices (key, created_at) VALUES (?, ?)", (key, now_ms())
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def is_seeded(self, repo: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM seeded WHERE repo=?", (repo,)).fetchone()
            is not None
        )

    def mark_seeded(self, repo: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seeded (repo, seeded_at) VALUES (?, ?)", (repo, now_ms())
        )
