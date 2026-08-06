"""What the build said about a commit robbie approved.

An `ok` asks CI to run and never looks at the answer again by itself. Green is
what makes a PR ready for a human to pick up; red is news the author needs and
nobody else has, since robbie is what asked for that build.

No containers and no spend: this phase is gh reads and at most one comment, which
is why it has no gate of its own and only borrows the tick's read cap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robbie import publish
from robbie.config import Config, RepoConfig
from robbie.db import Db, now_ms
from robbie.github import GhError, ci_outcome, failing_checks, pr_meta
from robbie.outcome import Outcome
from robbie.threads import merged_key

logger = logging.getLogger(__name__)

# an approval whose build never reports stops being news; it also stops being a read
CI_WATCH_HOURS = 24


@dataclass(frozen=True, eq=False)  # see Sweeper: a Config is not worth comparing
class CiWatch:
    cfg: Config
    db: Db
    gate_sem: asyncio.Semaphore
    dry_run: bool = False
    no_publish: bool = False

    async def watch(self) -> list[Outcome]:
        """Every approval still waiting on the build it paid for."""
        jobs: list[asyncio.Task[Outcome | None]] = []
        async with asyncio.TaskGroup() as tg:
            for row in self.db.watching_ci(now_ms() - CI_WATCH_HOURS * 3_600_000):
                try:
                    repo = self.cfg.repo(row["repo"])
                except KeyError:
                    continue  # the repo left the config; its approvals are not ours to chase
                jobs.append(tg.create_task(self._one(repo, row)))
        return [out for job in jobs if (out := job.result()) is not None]

    async def _one(self, repo: RepoConfig, row: Mapping[str, Any]) -> Outcome | None:
        """One approval's build. Concurrent and capped like every other gh read:
        a day of approvals read in series is the slowest thing in the tick."""
        where = f"{row['repo']}#{row['pr']}"
        try:
            async with self.gate_sem:
                meta = await pr_meta(row["repo"], row["pr"])
        except GhError as ex:
            logger.warning("%s: could not read CI: %s", where, ex)
            return None

        if meta.state != "OPEN":
            self._settle(row["key"], "gone")
            if meta.state == "MERGED" and not self.dry_run:
                # the usual way a merge is learned: robbie approved, CI went green,
                # a human merged. Free here, and it retires the PR from the sweep.
                self.db.notice_once(merged_key(row["repo"], row["pr"]))
            return None
        if meta.head_sha != row["head_sha"]:
            # they pushed after the approval, so this build is about older code
            self._settle(row["key"], "stale")
            return None

        outcome = ci_outcome(meta, ignore=repo.ignore_checks)
        if outcome == "waiting":
            return None
        by = row["model"] or "the account's model"
        if outcome == "green":
            self._settle(row["key"], outcome)
            logger.info("%s: green after %s approved it — ready for a human", where, by)
            return Outcome(row["repo"], row["pr"], "ready", f"green after {by}")

        red = tuple(failing_checks(meta, ignore=repo.ignore_checks))
        try:
            result = await publish.report_red_build(
                repo, meta, red, dry_run=self.dry_run or self.no_publish
            )
        except GhError as ex:
            # left unsettled on purpose: the next tick owes the author this note
            logger.warning("%s: could not post the red-build note: %s", where, ex)
            return None
        except Exception as ex:  # noqa: BLE001 — inside a TaskGroup it cancels the siblings
            logger.exception("%s: unhandled error reporting the build", where)
            return Outcome(row["repo"], row["pr"], "failed", str(ex))
        self._settle(row["key"], outcome)
        logger.info("%s: red after %s approved it — %s", where, by, result.detail)
        return Outcome(row["repo"], row["pr"], "ci-note", result.detail)

    def _settle(self, key: str, state: str) -> None:
        """A pass that publishes nothing decides but must not settle it.

        `--no-publish` counts here as much as `--dry-run` does: settling a red build
        whose note never went out retires the row, and the author is then owed a
        comment no later tick will ever post.
        """
        if not (self.dry_run or self.no_publish):
            self.db.set_ci_state(key, state)
