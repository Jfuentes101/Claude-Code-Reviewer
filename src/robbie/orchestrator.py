"""The loop: poll, gate, spawn, publish, notify.

Everything stateful about a review lives here; the modules it calls are either
pure (gates, anchor, contract) or a single narrow surface (publish, slack,
runner). That split is what makes the policy testable without a GitHub account.

The other two phases of a tick are their own modules, because neither is about
reviewing a diff: `threads` answers replies to earlier findings and `ci_watch`
reads the build an approval paid for. What the sweep borrows from here is one
thing, `_slot` — capacity for a container, already gated — since it spends the
same money out of the same cap.

Concurrency is per PR, capped by `max_concurrent_reviews`. A slow review cannot
delay the others and cannot collide with the next tick, because the tick only
schedules work the semaphore has room for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

from robbie import budget, publish
from robbie import slack as slackmod
from robbie.anchor import parse_findings, severity_count, summary_findings
from robbie.ci_watch import CiWatch
from robbie.config import Choice, Config, RepoConfig, Secrets
from robbie.contract import Blocks, preamble, threads_block
from robbie.db import Db
from robbie.gates import Decision, already_judged, dedup_key, evaluate, label_hold
from robbie.github import (
    GhError,
    PrMeta,
    Thread,
    last_review_request,
    my_threads,
    pr_meta,
    queue,
    summarize_checks,
    whoami,
)
from robbie.outcome import Outcome, Slot
from robbie.runner import ReviewRun, prune_transcripts, run_review
from robbie.slack import Slack
from robbie.threads import Sweeper

logger = logging.getLogger(__name__)


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
        model: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.db = db
        self.slack = slack
        self.dry_run = dry_run
        # unlike dry_run, the review still runs; only the outward writes stop
        self.no_publish = no_publish
        self.model = model  # `once --model` only; see Secrets.review_base_url
        self._self_login: str | None = None
        # (repo, pr) → what the gate read, handed to the prompt in the same tick
        self._threads: dict[tuple[str, int], list[Thread]] = {}
        self._inflight = 0  # containers spending right now, which no gate can see
        self._sem = asyncio.Semaphore(cfg.max_concurrent_reviews)
        self._gate_sem = asyncio.Semaphore(cfg.max_concurrent_checks)
        # Reading a meter suspends (it runs in a thread), so deciding and taking
        # the reserve have to happen under one lock: without it every waiting
        # review reads the same pre-reserve number and admits itself, which is
        # the stampede `reserve_usd`/`reserve_pct` exist to prevent.
        self._admit_lock = asyncio.Lock()

    # Built per call rather than held: both are frozen views over this object's
    # own state, and `dry_run` / `no_publish` can be flipped after construction.

    @property
    def sweeper(self) -> Sweeper:
        return Sweeper(
            self.cfg, self.secrets, self.db, self.slack, self._slot, self._gate_sem,
            dry_run=self.dry_run, no_publish=self.no_publish,
        )

    @property
    def ci(self) -> CiWatch:
        return CiWatch(
            self.cfg, self.db, self._gate_sem,
            dry_run=self.dry_run, no_publish=self.no_publish,
        )

    # ----- entry points --------------------------------------------------

    async def poll_once(self) -> list[Outcome]:
        """One tick across every configured repo: answer replies, then review.

        Answering first because a single `my_threads` read per PR feeds both gate
        5 and the prompt's prior-conversation block. Do it the other way and that
        read is stale: the gate rules on threads this tick is about to close, and
        a re-review re-raises findings it conceded seconds later.
        """
        self._threads.clear()
        if not self.dry_run:  # housekeeping, so it belongs to a real tick only
            prune_transcripts(self.cfg)
        answered: list[Outcome] = []
        if (await self._meter()).allowed:
            answered = await self.answer_threads()  # a container, so the same spend gate
        answered += await self.ci.watch()  # gh reads only, so no gate of its own
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
        return answered + [job.result() for job in jobs]

    async def review_one(self, slug: str, pr: int) -> Outcome:
        """Force a review, ignoring the queue, the gates and prior state."""
        repo = self.cfg.repo(slug)
        meta = await pr_meta(slug, pr)
        requested_at = await _requested_at_or_blank(slug, pr, repo.reviewer_login)
        key = dedup_key(slug, pr, meta.head_sha, requested_at)
        if self.model:
            # one row per model on the same commit; the key is what rows replace on
            key = f"{key}:{self.model}"
        return await self._review(repo, meta, key, requested_at)

    async def status(self) -> list[str]:
        rows: list[str] = []
        for repo in self.cfg.repos:
            prs = await queue(repo.slug, label=repo.label, reviewer=repo.reviewer_login)
            rows += await asyncio.gather(*(self._status_row(repo, pr) for pr in prs))
        return rows

    async def _status_row(self, repo: RepoConfig, pr: int) -> str:
        async with self._gate_sem:  # two gh calls each; a full queue is a lot at once
            meta = await pr_meta(repo.slug, pr)
            if meta.has_label(repo.needs_work_label):
                mark = f"⛔ blocked ({repo.needs_work_label} still on)"
            else:
                requested_at = await _requested_at_or_blank(repo.slug, pr, repo.reviewer_login)
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
        return f"  {repo.slug}#{pr:<6} {mark}  — {meta.title[:60]}"

    async def answer_threads(
        self, slug: str | None = None, only: tuple[int, ...] = ()
    ) -> list[Outcome]:
        """The reply sweep. Lives in `threads`; `robbie threads` starts here."""
        return await self.sweeper.sweep(slug, only)

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
        # the gate slot is released before the review starts. Holding it across a
        # 30-minute container would turn this cap into the total-in-flight cap,
        # and PRs past it would go unchecked until a review finished.
        async with self._gate_sem:
            meta, key, requested_at, decision = await self._gate(repo, pr)
        return await self._act(repo, meta, key, requested_at, decision)

    async def _gate(
        self, repo: RepoConfig, pr: int
    ) -> tuple[PrMeta, str, str, Decision]:
        meta = await pr_meta(repo.slug, pr)

        # each short-circuit skips the API call the next line would have made
        if (held := label_hold(meta, repo)) is not None:
            return meta, "", "", held

        requested_at = await last_review_request(repo.slug, pr, repo.reviewer_login)
        key = dedup_key(repo.slug, pr, meta.head_sha, requested_at)
        prior = self.db.get_review(key)
        if (judged := already_judged(prior.state if prior else None)) is not None:
            return meta, key, requested_at, judged

        # one query serves both the gate and the prompt's prior-conversation block
        threads = await my_threads(repo.slug, pr, repo.reviewer_login)
        self._threads[repo.slug, pr] = threads
        return meta, key, requested_at, evaluate(
            meta,
            repo,
            sha_judged=self.db.sha_was_judged(repo.slug, pr, meta.head_sha),
            open_threads=sum(1 for t in threads if t.awaiting_author),
        )

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
            if decision.dm:
                await self._dm_owner_once(f"hold:{key}", decision.dm)
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

        _, gate = await self._admit(key)
        if not gate.allowed:
            if gate.notice_key:
                await self._dm_owner_once(
                    gate.notice_key,
                    f"Holding off on reviews — {gate.detail}. I'll start again on my own.",
                )
            logger.info("budget gate closed: %s", gate.detail)
            return Outcome(repo.slug, meta.number, "budget", gate.detail)
        # either meter, not just the account's: an unguarded run is unguarded whoever
        # was supposed to be measuring it
        if gate.notice_key and budget.UNREADABLE in gate.notice_key:
            await self._dm_owner_once(
                gate.notice_key, f"I can't read the spend budget: {gate.detail}"
            )

        return await self._review(repo, meta, key, requested_at)

    def _choice(self, key: str) -> Choice:
        """Which model reviews this key, and therefore whose meter it spends."""
        if self.model:
            return self.cfg.named_model(self.model)
        return self.cfg.choose_model(key)

    @contextlib.asynccontextmanager
    async def _slot(self, key: str | None = None) -> AsyncIterator[Slot]:
        """Capacity to run one container: a semaphore slot and a spend reserve.

        The only thing the thread sweep needs from the review path, which is why
        it is a context manager and not four attributes shared between them.

        Deciding and reserving happen under one lock because reading a meter
        suspends. `key` picks the arm and therefore the meter; without one the
        run is not a review and goes on the account's own.
        """
        async with self._sem:
            async with self._admit_lock:
                choice, gate = (
                    await self._admit(key) if key is not None
                    else (Choice(), await self._meter())
                )
                if gate.allowed:
                    self._inflight += 1
            if not gate.allowed:
                yield Slot(False, gate.detail)
                return
            try:
                yield Slot(True, gate.detail, choice)
            finally:
                self._inflight -= 1

    async def _meter(self, *, via_endpoint: bool = False) -> budget.Verdict:
        """The spend gate, read off the event loop.

        Both plan meters are blocking HTTP with a 15s timeout. Asked inline, a hung
        one stalls the whole tick — every other PR's gating and every running
        review's bookkeeping — for as long as it hangs.
        """
        return await asyncio.to_thread(
            budget.check, self.cfg, self.secrets, self.db, self._inflight,
            via_endpoint=via_endpoint,
        )

    async def _admit(self, key: str) -> tuple[Choice, budget.Verdict]:
        """The arm that will review this key, and whether its meter allows it.

        The arms cover for each other: a PR held while the other provider sits idle
        is a review nobody gets, which trades the exact ratio for coverage. What a
        run fell back *from* stays recoverable, since `choose_model` is a pure
        function of the key. A model named on the CLI is never substituted — that
        one was a request, not a routing preference.
        """
        first = self._choice(key)
        verdict = await self._meter(via_endpoint=first.via_endpoint)
        if verdict.allowed or self.model:
            return first, verdict
        other = self.cfg.fallback_for(first)
        if other is None:
            return first, verdict
        spare = await self._meter(via_endpoint=other.via_endpoint)
        if not spare.allowed:
            return first, verdict
        logger.info(
            "%s: %s has no room (%s), falling back to %s",
            key, first.model or "the account", verdict.detail, other.model,
        )
        return other, spare

    async def _dm_owner_once(self, key: str, text: str) -> None:
        """One DM per key, ever — and a run that cannot send must not spend the key.

        `--dry-run` is the documented way to prove a deployment before it reviews
        anything. Recording the notice there would make the operator DMs for every
        currently-held PR disappear from the next real tick instead.
        """
        if self.dry_run or self.no_publish:
            if not self.db.notice_seen(key):
                await self.slack.dm_owner(text)  # this Slack only logs
            return
        if self.db.notice_once(key):
            await self.slack.dm_owner(text)

    async def _review(
        self, repo: RepoConfig, meta: PrMeta, key: str, requested_at: str
    ) -> Outcome:
        if self.dry_run:
            logger.info("DRY would review %s#%s (%s)", repo.slug, meta.number, key)
            return Outcome(repo.slug, meta.number, "review", "dry run")

        # built before the semaphore: a container slot must not be held open
        # while an API call for the prior conversation is in flight
        prompt = preamble(
            author=meta.author,
            ci=summarize_checks(meta),
            threads=threads_block(await self._prior_threads(repo, meta)),
            history=self._pass_history(repo, meta),
        )

        # the gate ruled minutes ago, behind however many reviews queued here
        async with self._slot(key) as slot:
            if not slot.ok:
                logger.info("budget closed while %s#%s waited: %s",
                            repo.slug, meta.number, slot.detail)
                return Outcome(repo.slug, meta.number, "budget", slot.detail)
            choice = slot.choice
            self.db.start_review(
                key=key, repo=repo.slug, pr=meta.number,
                head_sha=meta.head_sha, requested_at=requested_at,
            )
            run = await run_review(
                self.cfg, self.secrets, repo, meta, prompt=prompt,
                model=choice.model, via_endpoint=choice.via_endpoint,
            )

        if not run.ok:
            # not recorded as judged, so the next tick retries
            self.db.finish_review(
                key, state="failed", hold_reason=run.error, duration_s=run.duration_s,
                cost_usd=run.cost_usd, transcript=str(run.transcript or ""),
                model=choice.model,
            )
            return await self._gave_up(
                repo, meta, run.error or "unknown",
                f"I tried to review *{meta.title}* ({meta.url}) but the run failed: "
                f"{run.error}. I'll retry next cycle.",
            )

        return await self._deliver(repo, meta, key, run, choice)

    async def _deliver(
        self, repo: RepoConfig, meta: PrMeta, key: str, run: ReviewRun, choice: Choice
    ) -> Outcome:
        """What a finished run comes to: record what it cost, then post what it said.

        Split from `_review` because the two halves fail differently. Up there a
        failure is the container's and the key stays unjudged, so the next tick
        pays for another try. Down here the review exists and was paid for — every
        way out leaves a transcript and tells the operator where to find it.
        """
        blocks = run.blocks
        if blocks is None:  # mode="review" always parses; a caller could still lie
            return await self._gave_up(
                repo, meta, "no blocks",
                f"I ran a review of *{meta.title}* ({meta.url}) that parsed no blocks "
                f"at all. Transcript: {run.transcript}",
            )
        findings = parse_findings(blocks.inline)
        # heterogeneous on purpose — it is the column set every exit below writes
        common: dict[str, Any] = {
            "cost_usd": run.cost_usd, "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out, "duration_s": run.duration_s,
            "transcript": str(run.transcript or ""), "model": choice.model,
            "findings": len(findings),
            "blocking": severity_count(findings, blocking=True),
            "should_fix": severity_count(findings, blocking=False),
            "summary_findings": summary_findings(blocks.github),
        }

        if (bad := _unusable(blocks)) is not None:
            self.db.finish_review(
                key, state="held", verdict=blocks.verdict, hold_reason=bad.reason, **common
            )
            return await self._gave_up(
                repo, meta, bad.detail,
                f"I reviewed *{meta.title}* ({meta.url}) but {bad.told}, so I posted "
                f"nothing. Transcript: {run.transcript}",
            )
        # _unusable returns for a missing verdict, so past it this is one of the three
        verdict = blocks.verdict
        assert verdict is not None

        if verdict == "ok":
            await publish.clear_needs_work(repo, meta.number, dry_run=self.no_publish)
            ci = await self._request_ci(repo, meta)
            self.db.finish_review(
                key, state="published", verdict="ok",
                # nothing asked CI on a run that publishes nothing, so nothing to wait for
                ci_state=None if self.no_publish else "waiting",
                **common,
            )
            await self._announce_approval(meta, ci)
            return Outcome(repo.slug, meta.number, "review", f"ok — {ci}")

        # recorded as judged either way: retrying a permanent publish failure
        # would burn a full review every tick
        self.db.finish_review(key, state="published", verdict=verdict, **common)
        try:
            result = await publish.publish_review(
                verdict, repo, meta, body=blocks.github, findings=findings,
                dry_run=self.no_publish, self_login=await self._token_login(),
            )
        except Exception as ex:  # noqa: BLE001 — the review is done; only delivery failed
            logger.exception("publish failed for %s#%s", repo.slug, meta.number)
            return await self._gave_up(
                repo, meta, f"publish: {ex}",
                f"I couldn't publish my {verdict} review of *{meta.title}* "
                f"({meta.url}): {ex}. It's ready to post by hand: {run.transcript}",
            )

        if result.posted:
            # how many of them reached a diff line, which is not the same number
            self.db.finish_review(
                key, state="published", verdict=verdict,
                inline=result.inline, **common,
            )
            await self._notify(repo, meta, verdict, findings)
        return Outcome(repo.slug, meta.number, "review", f"{verdict}: {result.detail}")

    async def _gave_up(
        self, repo: RepoConfig, meta: PrMeta, detail: str, told: str
    ) -> Outcome:
        """A paid-for review that reached nobody. Tell the operator, say so upward.

        Not `_dm_owner_once`: each of these is about one run, not about a standing
        condition, and a second failure on the same PR is news again.
        """
        await self.slack.dm_owner(told)
        return Outcome(repo.slug, meta.number, "failed", detail)

    async def _prior_threads(self, repo: RepoConfig, meta: PrMeta) -> list[Thread]:
        """Reuse what the gate fetched; fetch it for a forced run that skipped it."""
        cached = self._threads.pop((repo.slug, meta.number), None)
        if cached is not None:
            return cached
        try:
            return await my_threads(repo.slug, meta.number, repo.reviewer_login)
        except GhError:
            logger.warning("%s#%s: could not read prior threads", repo.slug, meta.number)
            return []

    def _pass_history(self, repo: RepoConfig, meta: PrMeta) -> str:
        rows = self.db.passes_for(repo.slug, meta.number)
        if not rows:
            return ""
        past = ", ".join(
            f"{r['head_sha'][:8]} → {r['verdict'] or r['hold_reason'] or r['state']}"
            for r in rows
        )
        return f"This is pass {len(rows) + 1} on this PR. Earlier passes: {past}.\n"

    async def _request_ci(self, repo: RepoConfig, meta: PrMeta) -> str:
        """Trigger a build for an approved commit, at most once per commit.

        CI stopped running on push, so a build is now something robbie spends
        rather than something it observes: a forced re-review of a commit already
        approved must not pay for a second one.
        """
        key = f"run-ci:{repo.slug}:{meta.number}:{meta.head_sha}"
        if not self.no_publish and self.db.notice_seen(key):
            return f"CI already asked for {meta.head_sha[:8]}"
        try:
            result = await publish.request_ci(repo, meta, dry_run=self.no_publish)
        except GhError as ex:
            logger.warning("could not ask for CI on %s#%s: %s", repo.slug, meta.number, ex)
            await self.slack.dm_owner(
                f"I approved *{meta.title}* ({meta.url}) but couldn't post "
                f"`{repo.ci_phrase}`, so CI has not started: {ex}"
            )
            return "CI request failed"
        if result.posted:
            self.db.notice_once(key)
        return result.detail

    async def _token_login(self) -> str | None:
        if self._self_login is None:
            try:
                self._self_login = await whoami()
            except GhError:
                logger.warning("could not resolve the token's own login")
                self._self_login = ""
        return self._self_login or None

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

    async def _announce_approval(self, meta: PrMeta, ci: str) -> None:
        """One line, not a briefing: the reviews worth reading announce themselves.

        A needs-work review shows up on the PR and in the channel. An `ok` shows
        up nowhere, so it is the only verdict that has to be told — to everyone
        whose queue it just left, not only to the operator.
        """
        await self.slack.dm_reviewers(
            slackmod.approved_note(meta.number, meta.title, meta.url, ci)
        )

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


class _Unusable(NamedTuple):
    reason: str  # the hold_reason on the row
    told: str  # the middle of the operator's DM
    detail: str  # what the Outcome carries


def _unusable(blocks: Blocks) -> _Unusable | None:
    """Why a finished run cannot be published, if it cannot.

    Both cases are the model not honouring the output contract, and both are held
    rather than failed: the row keeps no verdict, so the PR comes back on its own.
    """
    if blocks.verdict is None:
        return _Unusable("run gave no verdict", "couldn't parse a verdict", "no verdict")
    if blocks.verdict != "ok" and not blocks.publishable:
        return _Unusable(
            "verdict without a summary body",
            f"got a {blocks.verdict} verdict with no summary body",
            "no summary body",
        )
    return None


async def _requested_at_or_blank(slug: str, pr: int, reviewer: str) -> str:
    try:
        return await last_review_request(slug, pr, reviewer)
    except GhError:
        return "forced"
