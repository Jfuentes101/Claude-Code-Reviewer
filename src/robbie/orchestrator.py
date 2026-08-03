"""The loop: poll, gate, spawn, publish, notify.

Everything stateful about a review lives here; the modules it calls are either
pure (gates, anchor, contract) or a single narrow surface (publish, slack,
runner). That split is what makes the policy testable without a GitHub account.

Concurrency is per PR, capped by `max_concurrent_reviews`. A slow review cannot
delay the others and cannot collide with the next tick, because the tick only
schedules work the semaphore has room for.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from robbie import budget, publish
from robbie import slack as slackmod
from robbie.anchor import parse_findings, severity_count
from robbie.config import Config, RepoConfig, Secrets
from robbie.contract import preamble
from robbie.db import Db
from robbie.gates import Decision, dedup_key, evaluate
from robbie.github import (
    GhError,
    PrMeta,
    last_review_request,
    open_threads,
    pr_meta,
    queue,
    review_still_requested,
    summarize_checks,
)
from robbie.runner import run_review
from robbie.slack import Slack

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    repo: str
    pr: int
    action: str  # review | skip | hold | ci-note | failed | budget
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.repo}#{self.pr} {self.action}" + (f" — {self.detail}" if self.detail else "")


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        db: Db,
        slack: Slack,
        *,
        dry_run: bool = False,
        no_publish: bool = False,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.db = db
        self.slack = slack
        self.dry_run = dry_run
        # unlike dry_run, the review still runs; only the outward writes stop
        self.no_publish = no_publish
        self._sem = asyncio.Semaphore(cfg.max_concurrent_reviews)

    # ----- entry points --------------------------------------------------

    async def poll_once(self) -> list[Outcome]:
        """One tick across every configured repo."""
        jobs: list[asyncio.Task[Outcome]] = []
        async with asyncio.TaskGroup() as tg:
            for repo in self.cfg.repos:
                try:
                    prs = await queue(repo.slug, label=repo.label, reviewer=repo.reviewer_login)
                except GhError as ex:
                    logger.warning("could not fetch the queue for %s: %s", repo.slug, ex)
                    continue
                logger.info("%s: %d PR(s) in queue", repo.slug, len(prs))

                if not self.db.is_seeded(repo.slug):
                    await self._seed(repo, prs)
                    continue

                for pr in prs:
                    jobs.append(tg.create_task(self._handle(repo, pr)))
        return [job.result() for job in jobs]

    async def review_one(self, slug: str, pr: int) -> Outcome:
        """Force a review, ignoring the queue, the gates and prior state."""
        repo = self.cfg.repo(slug)
        meta = await pr_meta(slug, pr)
        requested_at = await _requested_at_or_blank(slug, pr, repo.reviewer_login)
        return await self._review(repo, meta, dedup_key(slug, pr, meta.head_sha, requested_at),
                                  requested_at)

    async def status(self) -> list[str]:
        rows: list[str] = []
        for repo in self.cfg.repos:
            prs = await queue(repo.slug, label=repo.label, reviewer=repo.reviewer_login)
            for pr in prs:
                meta = await pr_meta(repo.slug, pr)
                if meta.has_label(repo.needs_work_label):
                    mark = f"⛔ blocked ({repo.needs_work_label} still on)"
                else:
                    requested_at = await _requested_at_or_blank(
                        repo.slug, pr, repo.reviewer_login
                    )
                    prior = self.db.get_review(
                        dedup_key(repo.slug, pr, meta.head_sha, requested_at)
                    )
                    if prior is None and self.db.sha_was_judged(repo.slug, pr, meta.head_sha):
                        mark = "↻ needs re-review (re-requested since last pass)"
                    elif prior is None:
                        mark = "• not reviewed yet"
                    elif prior.state == "held":
                        mark = f"⏸ held — {prior.hold_reason}"
                    elif prior.state == "published":
                        mark = f"✓ reviewed ({prior.verdict})"
                    else:
                        mark = f"… {prior.state}"
                rows.append(f"  {repo.slug}#{pr:<6} {mark}  — {meta.title[:60]}")
        return rows

    # ----- per-PR pipeline ----------------------------------------------

    async def _handle(self, repo: RepoConfig, pr: int) -> Outcome:
        try:
            return await self._handle_inner(repo, pr)
        except GhError as ex:
            # one unreachable PR must not take down the tick
            logger.warning("%s#%s: github unreachable: %s", repo.slug, pr, ex)
            return Outcome(repo.slug, pr, "skip", "github unreachable")
        except Exception as ex:  # noqa: BLE001 — same reasoning, wider net
            logger.exception("%s#%s: unhandled error", repo.slug, pr)
            return Outcome(repo.slug, pr, "failed", str(ex))

    async def _handle_inner(self, repo: RepoConfig, pr: int) -> Outcome:
        meta = await pr_meta(repo.slug, pr)

        # short-circuit before paging the timeline; `evaluate` decides the same
        if meta.has_label(repo.needs_work_label):
            return Outcome(repo.slug, pr, "hold", f"{repo.needs_work_label} still on")

        requested_at = await last_review_request(repo.slug, pr, repo.reviewer_login)
        key = dedup_key(repo.slug, pr, meta.head_sha, requested_at)
        prior = self.db.get_review(key)
        if prior is not None and prior.state in ("published", "held"):
            return Outcome(repo.slug, pr, "skip", "already judged at this commit and request")

        threads = await open_threads(repo.slug, pr, repo.reviewer_login)
        decision = evaluate(
            meta,
            repo,
            key_done=False,
            sha_judged=self.db.sha_was_judged(repo.slug, pr, meta.head_sha),
            open_threads=threads,
        )
        return await self._act(repo, meta, key, requested_at, decision)

    async def _act(
        self,
        repo: RepoConfig,
        meta: PrMeta,
        key: str,
        requested_at: str,
        decision: Decision,
    ) -> Outcome:
        if decision.action == "skip":
            return Outcome(repo.slug, meta.number, "skip", decision.reason)

        if decision.action == "hold":
            if decision.dm and self.db.notice_once(f"hold:{key}"):
                await self.slack.dm_owner(decision.dm)
            if decision.record and not self.dry_run:
                self.db.record_hold(
                    key=key, repo=repo.slug, pr=meta.number, head_sha=meta.head_sha,
                    requested_at=requested_at, reason=decision.reason,
                )
            return Outcome(repo.slug, meta.number, "hold", decision.reason)

        if decision.action == "ci-note":
            result = await publish.post_ci_note(
                repo, meta, decision.checks, dry_run=self.dry_run or self.no_publish
            )
            return Outcome(repo.slug, meta.number, "ci-note", result.detail)

        gate = budget.check(self.cfg, self.secrets, self.db)
        if not gate.allowed:
            if gate.notice_key and self.db.notice_once(gate.notice_key):
                await self.slack.dm_owner(
                    f"Holding off on reviews — {gate.detail}. I'll start again on my own."
                )
            logger.info("budget gate closed: %s", gate.detail)
            return Outcome(repo.slug, meta.number, "budget", gate.detail)
        if gate.notice_key == "budget:unreadable" and self.db.notice_once(gate.notice_key):
            await self.slack.dm_owner(f"I can't read the spend budget: {gate.detail}")

        return await self._review(repo, meta, key, requested_at)

    async def _review(
        self, repo: RepoConfig, meta: PrMeta, key: str, requested_at: str
    ) -> Outcome:
        if self.dry_run:
            logger.info("DRY would review %s#%s (%s)", repo.slug, meta.number, key)
            return Outcome(repo.slug, meta.number, "review", "dry run")

        async with self._sem:
            self.db.start_review(
                key=key, repo=repo.slug, pr=meta.number,
                head_sha=meta.head_sha, requested_at=requested_at,
            )
            run = await run_review(
                self.cfg, self.secrets, repo, meta,
                prompt=preamble(
                    author=meta.author, title=meta.title, url=meta.url,
                    ci=summarize_checks(meta).as_prompt(),
                ),
            )

        if not run.ok:
            # not recorded as judged, so the next tick retries
            self.db.finish_review(
                key, state="failed", hold_reason=run.error, duration_s=run.duration_s,
                cost_usd=run.cost_usd, transcript=str(run.transcript or ""),
            )
            await self.slack.dm_owner(
                f"I tried to review *{meta.title}* ({meta.url}) but the run failed: "
                f"{run.error}. I'll retry next cycle."
            )
            return Outcome(repo.slug, meta.number, "failed", run.error or "unknown")

        blocks = run.blocks
        assert blocks is not None
        common = {
            "cost_usd": run.cost_usd, "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out, "duration_s": run.duration_s,
            "transcript": str(run.transcript or ""),
        }

        if blocks.verdict is None:
            self.db.finish_review(key, state="held", hold_reason="run gave no verdict", **common)
            await self.slack.dm_owner(
                f"I reviewed *{meta.title}* ({meta.url}) but couldn't parse a verdict, so I "
                f"posted nothing. Transcript: {run.transcript}"
            )
            return Outcome(repo.slug, meta.number, "failed", "no verdict")

        findings = parse_findings(blocks.inline)

        if blocks.verdict == "ok":
            await publish.clear_needs_work(repo, meta.number, dry_run=self.no_publish)
            self.db.finish_review(key, state="published", verdict="ok", **common)
            await self._brief_owner(repo, meta, blocks.slack, run.transcript)
            return Outcome(repo.slug, meta.number, "review", "ok — nothing posted")

        if not blocks.publishable:
            self.db.finish_review(
                key, state="held", verdict=blocks.verdict,
                hold_reason="verdict without a summary body", **common,
            )
            await self.slack.dm_owner(
                f"My {blocks.verdict} review of *{meta.title}* ({meta.url}) had no summary "
                f"body, so I posted nothing. Transcript: {run.transcript}"
            )
            return Outcome(repo.slug, meta.number, "failed", "no summary body")

        # recorded as judged either way: retrying a permanent publish failure
        # would burn a full review every tick
        self.db.finish_review(key, state="published", verdict=blocks.verdict, **common)
        try:
            result = await publish.publish_review(
                blocks.verdict, repo, meta, body=blocks.github, findings=findings,
                dry_run=self.no_publish,
            )
        except Exception as ex:  # noqa: BLE001 — the review is done; only delivery failed
            logger.exception("publish failed for %s#%s", repo.slug, meta.number)
            await self.slack.dm_owner(
                f"I couldn't publish my {blocks.verdict} review of *{meta.title}* "
                f"({meta.url}): {ex}. It's ready to post by hand: {run.transcript}"
            )
            return Outcome(repo.slug, meta.number, "failed", f"publish: {ex}")

        if result.posted:
            await self._notify(repo, meta, blocks.verdict, findings)
        if blocks.verdict == "comment":
            await self._brief_owner(repo, meta, blocks.slack, run.transcript)
        return Outcome(repo.slug, meta.number, "review", f"{blocks.verdict}: {result.detail}")

    # ----- notifications -------------------------------------------------

    async def _notify(
        self, repo: RepoConfig, meta: PrMeta, verdict: str, findings: list[dict]
    ) -> None:
        phrase = slackmod.verdict_phrase(
            verdict,
            blocking=severity_count(findings, blocking=True),
            should_fix=severity_count(findings, blocking=False),
        )
        if repo.slack_channel:
            await self.slack.post(
                repo.slack_channel,
                slackmod.channel_note(meta.number, meta.title, meta.url, meta.author, phrase),
            )
        state = await self.slack.dm_author(
            meta.author, slackmod.author_note(meta.number, meta.title, meta.url, phrase)
        )
        if state == "unmapped" and self.db.notice_once(f"nomap:{meta.author}"):
            await self.slack.dm_owner(
                slackmod.unmapped_note(meta.author, self.cfg.slack.users_file)
            )

    async def _brief_owner(
        self, repo: RepoConfig, meta: PrMeta, briefing: str, transcript
    ) -> None:
        """The reviewer still has to look at these, so they get the full briefing."""
        text = briefing.strip() or (
            f"I reviewed *{meta.title}* ({meta.url}) and found no blockers, but couldn't "
            "parse a clean summary from the run."
        )
        if not await review_still_requested(repo.slug, meta.number, repo.reviewer_login):
            text += "\n\n_(this one isn't in your pending-review list, so take it from the link.)_"
        await self.slack.dm_owner(f"{text}\n\n— full review: {transcript}")

    # ----- cold start ----------------------------------------------------

    async def _seed(self, repo: RepoConfig, prs: list[int]) -> None:
        """Record the current backlog instead of reviewing it.

        Enabling robbie on a repo with 20 pending PRs must not post 20 reviews.
        """
        for pr in prs:
            try:
                meta = await pr_meta(repo.slug, pr)
                requested_at = await last_review_request(repo.slug, pr, repo.reviewer_login)
            except GhError as ex:
                logger.warning("could not seed %s#%s: %s", repo.slug, pr, ex)
                return  # leave the repo unseeded; the next tick tries again
            if not self.dry_run:
                self.db.record_hold(
                    key=dedup_key(repo.slug, pr, meta.head_sha, requested_at),
                    repo=repo.slug, pr=pr, head_sha=meta.head_sha,
                    requested_at=requested_at, reason="backlog at cold start",
                )
        if self.dry_run:
            logger.info("DRY would seed %s with %d PR(s)", repo.slug, len(prs))
            return
        self.db.mark_seeded(repo.slug)
        await self.slack.dm_owner(
            f"robbie is on for *{repo.slug}*. {len(prs)} PR(s) currently request "
            f"{repo.reviewer_login}'s review with the \"{repo.label}\" label; I've noted them "
            "and will review *new* requests from here on. Run "
            f"`robbie once --repo {repo.slug} --pr <n>` to go through one from the backlog."
        )


async def _requested_at_or_blank(slug: str, pr: int, reviewer: str) -> str:
    try:
        return await last_review_request(slug, pr, reviewer)
    except GhError:
        return "forced"
