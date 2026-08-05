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
from typing import Literal

from robbie import budget, publish
from robbie import slack as slackmod
from robbie.anchor import parse_findings, severity_count
from robbie.config import Choice, Config, RepoConfig, Secrets
from robbie.contract import (
    parse_thread_verdicts,
    preamble,
    thread_preamble,
    threads_block,
)
from robbie.db import Db, now_ms
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
from robbie.runner import run_review
from robbie.slack import Slack

logger = logging.getLogger(__name__)

# a reply this long after the review is a conversation a human should pick up, and
# every PR inside the window costs a thread read on every tick
SWEEP_DAYS = 30


@dataclass(frozen=True)
class Outcome:
    repo: str
    pr: int
    action: Literal["review", "skip", "hold", "ci-note", "threads", "failed", "budget"]
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

    # ----- entry points --------------------------------------------------

    async def poll_once(self) -> list[Outcome]:
        """One tick across every configured repo: answer replies, then review.

        Answering first because a single `my_threads` read per PR feeds both gate
        5 and the prompt's prior-conversation block. Do it the other way and that
        read is stale: the gate rules on threads this tick is about to close, and
        a re-review re-raises findings it conceded seconds later.
        """
        self._threads.clear()
        answered: list[Outcome] = []
        if budget.check(self.cfg, self.secrets, self.db, self._inflight).allowed:
            answered = await self.answer_threads()  # a container, so the same spend gate
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
        """Act on replies to the reviewer's own open threads.

        Only threads where somebody else spoke last are considered: after robbie
        answers one it holds the last word, so the next run leaves it alone and
        the ball stays with the author. That is also what keeps gate 5 honest —
        conceded threads get closed instead of blocking reviews forever.
        """
        out: list[Outcome] = []
        for repo in self.cfg.repos:
            if slug and repo.slug != slug:
                continue
            # named PRs override the DB: threads can predate robbie's own passes
            for pr in only or self.db.reviewed_prs(repo.slug, since_ms=_sweep_from()):
                try:
                    threads = await my_threads(repo.slug, pr, repo.reviewer_login)
                except GhError as ex:
                    logger.warning("%s#%s: could not read threads: %s", repo.slug, pr, ex)
                    continue
                pending = [
                    t for t in threads
                    if t.answered and not self.db.notice_seen(_thread_state(repo.slug, pr, t))
                ]
                if not pending:
                    continue
                out.append(await self._answer_one(repo, pr, pending))
        return out

    async def _answer_one(self, repo: RepoConfig, pr: int, pending: list[Thread]) -> Outcome:
        meta = await pr_meta(repo.slug, pr)
        if meta.state != "OPEN":
            return Outcome(repo.slug, pr, "skip", f"pr is {meta.state.lower()}")

        if self.dry_run:
            logger.info(
                "DRY would work through %d reply(s) on %s#%s", len(pending), repo.slug, pr
            )
            return Outcome(repo.slug, pr, "threads", "dry run")

        prompt = thread_preamble(author=meta.author, url=meta.url, threads=pending)
        async with self._sem:
            gate = budget.check(self.cfg, self.secrets, self.db, self._inflight)
            if not gate.allowed:
                logger.info("budget closed while %s#%s waited: %s", repo.slug, pr, gate.detail)
                return Outcome(repo.slug, pr, "budget", gate.detail)
            self._inflight += 1
            try:
                run = await run_review(
                    self.cfg, self.secrets, repo, meta, prompt=prompt, mode="threads"
                )
            finally:
                self._inflight -= 1
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
        by_comment = {t.comment_id: t for t in pending}
        done = {"resolve": 0, "reply": 0, "leave": 0, "unanswered": 0}
        for cid, thread in by_comment.items():
            verdict = verdicts.get(cid)
            if verdict is None:
                done["unanswered"] += 1
                self.db.notice_once(_thread_state(repo.slug, pr, thread))
                continue
            if verdict.action == "resolve":
                await publish.resolve_thread(thread.node_id, dry_run=self.no_publish)
            elif verdict.action == "reply":
                await publish.reply_to_thread(
                    repo, pr, cid, verdict.body, dry_run=self.no_publish
                )
            else:
                self.db.notice_once(_thread_state(repo.slug, pr, thread))
            done[verdict.action] += 1

        detail = ", ".join(f"{n} {k}" for k, n in done.items() if n)
        if done["reply"]:
            await self.slack.dm_owner(
                f"I answered {done['reply']} of my review threads on *{meta.title}* "
                f"({meta.url}) and closed {done['resolve']}. The ball is back with "
                f"{meta.author}."
            )
        return Outcome(repo.slug, pr, "threads", detail or "nothing to do")

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

        _, gate = self._admit(key)
        if not gate.allowed:
            if gate.notice_key:
                await self._dm_owner_once(
                    gate.notice_key,
                    f"Holding off on reviews — {gate.detail}. I'll start again on my own.",
                )
            logger.info("budget gate closed: %s", gate.detail)
            return Outcome(repo.slug, meta.number, "budget", gate.detail)
        if gate.notice_key == "budget:unreadable":
            await self._dm_owner_once(
                gate.notice_key, f"I can't read the spend budget: {gate.detail}"
            )

        return await self._review(repo, meta, key, requested_at)

    def _choice(self, key: str) -> Choice:
        """Which model reviews this key, and therefore whose meter it spends."""
        if self.model:
            return self.cfg.named_model(self.model)
        return self.cfg.choose_model(key)

    def _admit(self, key: str) -> tuple[Choice, budget.Verdict]:
        """The arm that will review this key, and whether its meter allows it.

        The arms cover for each other: a PR held while the other provider sits idle
        is a review nobody gets, which trades the exact ratio for coverage. What a
        run fell back *from* stays recoverable, since `choose_model` is a pure
        function of the key. A model named on the CLI is never substituted — that
        one was a request, not a routing preference.
        """
        first = self._choice(key)
        verdict = budget.check(
            self.cfg, self.secrets, self.db, self._inflight, via_endpoint=first.via_endpoint
        )
        if verdict.allowed or self.model:
            return first, verdict
        other = self.cfg.fallback_for(first)
        if other is None:
            return first, verdict
        spare = budget.check(
            self.cfg, self.secrets, self.db, self._inflight, via_endpoint=other.via_endpoint
        )
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
            ci=summarize_checks(meta).as_prompt(),
            threads=threads_block(await self._prior_threads(repo, meta)),
            history=self._pass_history(repo, meta),
        )

        async with self._sem:
            # the gate ruled minutes ago, behind however many reviews queued here
            choice, gate = self._admit(key)
            if not gate.allowed:
                logger.info("budget closed while %s#%s waited: %s",
                            repo.slug, meta.number, gate.detail)
                return Outcome(repo.slug, meta.number, "budget", gate.detail)
            self.db.start_review(
                key=key, repo=repo.slug, pr=meta.number,
                head_sha=meta.head_sha, requested_at=requested_at,
            )
            self._inflight += 1
            try:
                run = await run_review(
                    self.cfg, self.secrets, repo, meta, prompt=prompt, model=choice.model
                )
            finally:
                self._inflight -= 1

        if not run.ok:
            # not recorded as judged, so the next tick retries
            self.db.finish_review(
                key, state="failed", hold_reason=run.error, duration_s=run.duration_s,
                cost_usd=run.cost_usd, transcript=str(run.transcript or ""),
                model=choice.model,
            )
            await self.slack.dm_owner(
                f"I tried to review *{meta.title}* ({meta.url}) but the run failed: "
                f"{run.error}. I'll retry next cycle."
            )
            return Outcome(repo.slug, meta.number, "failed", run.error or "unknown")

        blocks = run.blocks
        assert blocks is not None
        findings = parse_findings(blocks.inline)
        common = {
            "cost_usd": run.cost_usd, "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out, "duration_s": run.duration_s,
            "transcript": str(run.transcript or ""), "model": choice.model,
            "findings": len(findings),
            "blocking": severity_count(findings, blocking=True),
            "should_fix": severity_count(findings, blocking=False),
        }

        if blocks.verdict is None:
            self.db.finish_review(key, state="held", hold_reason="run gave no verdict", **common)
            await self.slack.dm_owner(
                f"I reviewed *{meta.title}* ({meta.url}) but couldn't parse a verdict, so I "
                f"posted nothing. Transcript: {run.transcript}"
            )
            return Outcome(repo.slug, meta.number, "failed", "no verdict")

        if blocks.verdict == "ok":
            await publish.clear_needs_work(repo, meta.number, dry_run=self.no_publish)
            ci = await self._request_ci(repo, meta)
            self.db.finish_review(key, state="published", verdict="ok", **common)
            await self._announce_approval(meta, ci)
            return Outcome(repo.slug, meta.number, "review", f"ok — {ci}")

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
                dry_run=self.no_publish, self_login=await self._token_login(),
            )
        except Exception as ex:  # noqa: BLE001 — the review is done; only delivery failed
            logger.exception("publish failed for %s#%s", repo.slug, meta.number)
            await self.slack.dm_owner(
                f"I couldn't publish my {blocks.verdict} review of *{meta.title}* "
                f"({meta.url}): {ex}. It's ready to post by hand: {run.transcript}"
            )
            return Outcome(repo.slug, meta.number, "failed", f"publish: {ex}")

        if result.posted:
            # how many of them reached a diff line, which is not the same number
            self.db.finish_review(
                key, state="published", verdict=blocks.verdict,
                inline=result.inline, **common,
            )
            await self._notify(repo, meta, blocks.verdict, findings)
        return Outcome(repo.slug, meta.number, "review", f"{blocks.verdict}: {result.detail}")

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


def _sweep_from() -> int:
    """How far back the reply sweep looks. `robbie threads --pr N` ignores it."""
    return now_ms() - SWEEP_DAYS * 86_400_000


def _thread_state(slug: str, pr: int, thread: Thread) -> str:
    """Identifies a thread *and* the reply that is waiting on it.

    Judging one and leaving it alone must not be judged again, but a new reply
    has to bring it straight back, so the reply count is part of the key.
    """
    return f"thread:{slug}:{pr}:{thread.comment_id}:{len(thread.replies)}"


async def _requested_at_or_blank(slug: str, pr: int, reviewer: str) -> str:
    try:
        return await last_review_request(slug, pr, reviewer)
    except GhError:
        return "forced"
