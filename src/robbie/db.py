"""SQLite state. Replaces git-sentinel's seen.txt / state.tsv / hold: prefixes.

Two things the text files could not do: express "held, retry next tick" vs
"held, done with this key" as data instead of an absent line, and survive
several reviews finishing at once.

ponytail: sync sqlite3, single writer process. Calls are sub-ms against a poll
loop, so the async wrapper would buy nothing. If robbie ever runs more than one
orchestrator, this is the thing to move to postgres.

Adding a table is free — `IF NOT EXISTS` runs on every boot. Adding a *column* to
one of these is not: the CREATE is skipped on an existing database and nothing
notices until a query mentions the column. That one needs an explicit ALTER,
guarded by `PRAGMA user_version`.
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
    model        TEXT,                      -- null = whatever the account defaults to
    findings     INTEGER,                   -- what the run reported, by severity
    blocking     INTEGER,                   -- critical + must-fix
    should_fix   INTEGER,
    inline       INTEGER,                   -- of those, anchored to a diff line
    summary_findings INTEGER,               -- indexed in the summary instead
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

-- containers that spend without being a review pass, so the budget can see them
CREATE TABLE IF NOT EXISTS spend (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT    NOT NULL,
    pr         INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    cost_usd   REAL,
    duration_s REAL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_created ON spend(created_at DESC);

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


SCHEMA_VERSION = 3


class Db:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            # a reader cannot create the schema or migrate it, and must not: the
            # dashboard opens this way so a bug there cannot touch a review's row.
            # The file's directory still has to be writable — SQLite needs the
            # -shm file to read a WAL database at all.
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            return
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an existing database up to what the code above assumes.

        `IF NOT EXISTS` covers a new table but skips a new column entirely, so a
        column has to be added here or nothing fails until a query names it.
        """
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        added = {
            1: [("model", "TEXT")],
            2: [("findings", "INTEGER"), ("blocking", "INTEGER"),
                ("should_fix", "INTEGER"), ("inline", "INTEGER")],
            3: [("summary_findings", "INTEGER")],
        }
        for step in range(version + 1, SCHEMA_VERSION + 1):
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(reviews)")}
            for name, kind in added.get(step, []):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {name} {kind}")
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
        model: str | None = None,
        findings: int | None = None,
        blocking: int | None = None,
        should_fix: int | None = None,
        inline: int | None = None,
        summary_findings: int | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE reviews SET state=?, verdict=?, hold_reason=?, cost_usd=?, "
            "tokens_in=?, tokens_out=?, duration_s=?, transcript=?, model=?, "
            "findings=?, blocking=?, should_fix=?, inline=?, summary_findings=?, "
            "finished_at=? "
            "WHERE key=?",
            (state, verdict, hold_reason, cost_usd, tokens_in, tokens_out,
             duration_s, transcript, model, findings, blocking, should_fix, inline,
             summary_findings, now_ms(), key),
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

    def reviewed_prs(self, repo: str, *, since_ms: int = 0) -> list[int]:
        """PRs reviewed since `since_ms` — where threads of ours can exist.

        Windowed because every one of these costs an API read on every tick, and
        the list only ever grows: most of it is PRs that merged months ago.
        """
        return [
            int(r["pr"]) for r in self.conn.execute(
                "SELECT DISTINCT pr FROM reviews WHERE repo=? AND state='published' "
                "AND created_at >= ? ORDER BY pr DESC",
                (repo, since_ms),
            )
        ]

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

    def record_spend(
        self, *, repo: str, pr: int, kind: str,
        cost_usd: float | None, duration_s: float | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO spend (repo, pr, kind, cost_usd, duration_s, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (repo, pr, kind, cost_usd, duration_s, now_ms()),
        )

    def spend_since(self, since_ms: int, exclude_models: tuple[str, ...] = ()) -> float:
        """Dollars the account was billed since `since_ms`.

        A run on a third-party endpoint reports a `cost_usd` the CLI computed from
        its own price table, which is not that provider's bill and was never the
        account's. Counting it would trip `daily_usd` on money nobody spent.
        """
        holes = ",".join("?" * len(exclude_models))
        skip = f" AND COALESCE(model, '') NOT IN ({holes})" if exclude_models else ""
        row = self.conn.execute(
            "SELECT COALESCE((SELECT SUM(cost_usd) FROM reviews "
            f"                WHERE created_at >= ?{skip}), 0.0) "
            "     + COALESCE((SELECT SUM(cost_usd) FROM spend WHERE created_at >= ?), 0.0) "
            "AS usd",
            (since_ms, *exclude_models, since_ms),
        ).fetchone()
        return float(row["usd"])

    # ----- notices / seeding --------------------------------------------

    def notice_seen(self, key: str) -> bool:
        """Whether this key was ever recorded, without recording it."""
        return (
            self.conn.execute("SELECT 1 FROM notices WHERE key=?", (key,)).fetchone()
            is not None
        )

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
