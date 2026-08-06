"""Act on replies to the reviewer's own open threads.

Only threads where somebody else spoke last are considered: after robbie answers
one it holds the last word, so the next run leaves it alone and the ball stays
with the author. That is also what keeps gate 5 honest — conceded threads get
closed instead of blocking reviews forever.

This spawns the same kind of container a review does, so it spends the same money
and takes the same capacity. `slot` is the whole of what it borrows from the
review path: one container's worth of room, already gated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from robbie import publish
from robbie.config import Config, RepoConfig, Secrets
from robbie.contract import parse_thread_verdicts, thread_preamble
from robbie.db import Db, now_ms
from robbie.github import GhError, Thread, my_threads, pr_meta
from robbie.outcome import Outcome, Slot
from robbie.runner import run_review
from robbie.slack import Slack

logger = logging.getLogger(__name__)

# a reply this long after the review is a conversation a human should pick up, and
# every PR inside the window costs a thread read on every tick
SWEEP_DAYS = 30


# frozen so a flag cannot be flipped on a view instead of on the orchestrator;
# eq=False because the generated one would compare (and hash) a pydantic Config
@dataclass(frozen=True, eq=False)
class Sweeper:
    cfg: Config
    secrets: Secrets
    db: Db
    slack: Slack
    slot: Callable[[], AbstractAsyncContextManager[Slot]]
    gate_sem: asyncio.Semaphore
    dry_run: bool = False
    no_publish: bool = False

    async def sweep(
        self, slug: str | None = None, only: tuple[int, ...] = ()
    ) -> list[Outcome]:
        jobs: list[asyncio.Task[Outcome | None]] = []
        async with asyncio.TaskGroup() as tg:
            for repo in self.cfg.repos:
                if slug and repo.slug != slug:
                    continue
                # named PRs override the DB: threads can predate robbie's own passes
                for pr in only or self.db.reviewed_prs(repo.slug, since_ms=_sweep_from()):
                    jobs.append(tg.create_task(self._one(repo, pr)))
        return [out for job in jobs if (out := job.result()) is not None]

    async def _one(self, repo: RepoConfig, pr: int) -> Outcome | None:
        """One PR's replies. Same shape as the queue phase, and for the same two
        reasons: a thread read per PR in series is the slowest thing in the tick,
        and one unreachable PR must not take the whole sweep down with it."""
        try:
            if self.db.notice_seen(merged_key(repo.slug, pr)):
                return None
            async with self.gate_sem:  # a paginated GraphQL read, like the gates
                threads = await my_threads(repo.slug, pr, repo.reviewer_login)
            pending = [
                t for t in threads
                if t.answered and not self.db.notice_seen(thread_state(repo.slug, pr, t))
            ]
            if not pending:
                return None
            return await self._answer(repo, pr, pending)
        except GhError as ex:
            logger.warning("%s#%s: could not read threads: %s", repo.slug, pr, ex)
            return None
        except Exception as ex:  # noqa: BLE001 — inside a TaskGroup it cancels the siblings
            logger.exception("%s#%s: unhandled error answering replies", repo.slug, pr)
            return Outcome(repo.slug, pr, "failed", str(ex))

    async def _answer(self, repo: RepoConfig, pr: int, pending: list[Thread]) -> Outcome:
        meta = await pr_meta(repo.slug, pr)
        if meta.state != "OPEN":
            if meta.state == "MERGED" and not self.dry_run:
                self.db.notice_once(merged_key(repo.slug, pr))
            return Outcome(repo.slug, pr, "skip", f"pr is {meta.state.lower()}")

        if self.dry_run:
            logger.info(
                "DRY would work through %d reply(s) on %s#%s", len(pending), repo.slug, pr
            )
            return Outcome(repo.slug, pr, "threads", "dry run")

        prompt = thread_preamble(author=meta.author, url=meta.url, threads=pending)
        async with self.slot() as slot:
            if not slot.ok:
                logger.info("budget closed while %s#%s waited: %s", repo.slug, pr, slot.detail)
                return Outcome(repo.slug, pr, "budget", slot.detail)
            run = await run_review(
                self.cfg, self.secrets, repo, meta, prompt=prompt, mode="threads"
            )
        self.db.record_spend(
            repo=repo.slug, pr=pr, kind="threads",
            cost_usd=run.cost_usd, duration_s=run.duration_s,
        )
        if not run.ok:
            await self.slack.dm_owner(
                f"I couldn't work through the replies on *{meta.title}* ({meta.url}): "
                f"{run.error}"
            )
            return Outcome(repo.slug, pr, "failed", run.error or "unknown")

        verdicts = {v.comment_id: v for v in parse_thread_verdicts(run.text)}
        # keyed off what was offered, so a verdict for a thread nobody showed the
        # model — one a reply could have named in prose — touches nothing
        by_comment = {t.comment_id: t for t in pending}
        done = {"resolve": 0, "reply": 0, "leave": 0, "unanswered": 0, "failed": 0}
        for cid, thread in by_comment.items():
            verdict = verdicts.get(cid)
            if verdict is None:
                done["unanswered"] += 1
                self.db.notice_once(thread_state(repo.slug, pr, thread))
                continue
            try:
                if verdict.action == "resolve":
                    await publish.resolve_thread(thread.node_id, dry_run=self.no_publish)
                elif verdict.action == "reply":
                    await publish.reply_to_thread(
                        repo, pr, cid, verdict.body, dry_run=self.no_publish
                    )
                else:
                    self.db.notice_once(thread_state(repo.slug, pr, thread))
            except GhError as ex:
                # this run is already paid for: one thread GitHub will not take must
                # not throw away the decisions made about all the others
                logger.warning(
                    "%s#%s thread %s: could not %s: %s", repo.slug, pr, cid, verdict.action, ex
                )
                done["failed"] += 1
                continue
            done[verdict.action] += 1

        detail = ", ".join(f"{n} {k}" for k, n in done.items() if n)
        if done["reply"]:
            await self.slack.dm_owner(
                f"I answered {done['reply']} of my review threads on *{meta.title}* "
                f"({meta.url}) and closed {done['resolve']}. The ball is back with "
                f"{meta.author}."
            )
        return Outcome(repo.slug, pr, "threads", detail or "nothing to do")


def _sweep_from() -> int:
    """How far back the reply sweep looks. `robbie threads --pr N` ignores it."""
    return now_ms() - SWEEP_DAYS * 86_400_000


def merged_key(slug: str, pr: int) -> str:
    """A PR nobody will reply on again, so the sweep stops paying to read it.

    Every PR published in the last 30 days costs a paginated GraphQL read on every
    tick, and most of them are merged long before that. Written by whoever learns
    the state for free — this sweep and the CI watch — because asking on purpose
    would cost the call it is trying to save.

    Merged only, never closed: a closed PR can be reopened, and this key would
    then keep it unswept for the rest of the window.
    """
    return f"merged:{slug}:{pr}"


def thread_state(slug: str, pr: int, thread: Thread) -> str:
    """Identifies a thread *and* the reply that is waiting on it.

    Judging one and leaving it alone must not be judged again, but a new reply
    has to bring it straight back, so the reply count is part of the key.
    """
    return f"thread:{slug}:{pr}:{thread.comment_id}:{len(thread.replies)}"
