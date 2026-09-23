"""The bug queue's tick: read a report, decide who it belongs to, record that.

Not a review and not yet a fix. This loop reads the issues a form filed, takes
the decision the form itself settles, buys one model call for the question it
does not, and writes the answer back as a comment, an assignee and a label. It
opens no branch and no pull request: that half needs a token this one does not
have, and keeping them apart is what makes this one safe to leave running.

Every way out of the loop that is not a decision leaves the issue in the queue.
A failed read, a paused meter, a model that never came back — all of them mean
the next tick tries again, because an issue still carrying its queue label is
the only record either half of this keeps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from robbie import budget, jane, publish
from robbie.config import Config, RepoConfig, Secrets
from robbie.db import Db
from robbie.github import GhError, IssueMeta, issue_meta, issue_queue
from robbie.runner import run_review
from robbie.triage import (
    ASK,
    ASSIGN,
    ATTEMPT,
    Verdict,
    fields,
    money_prompt,
    touches_money,
    verdict,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settled:
    number: int
    action: str
    reason: str


async def triage_tick(
    cfg: Config, secrets: Secrets, repo: RepoConfig, db: Db, *, dry_run: bool = False
) -> list[Settled]:
    """One pass over the bug queue. Returns what it settled, in the order it did.

    ponytail: one issue at a time. The queue is a handful a day and each one is a
    couple of API calls plus at most one container; give it a semaphore the day a
    tick stops finishing before the next one starts.
    """
    if not repo.issues.labels:
        return []
    numbers = await issue_queue(repo.slug, labels=repo.issues.labels)
    settled: list[Settled] = []
    for number in numbers:
        try:
            done = await _triage_one(cfg, secrets, repo, db, number, dry_run=dry_run)
        except GhError as ex:
            logger.warning("%s#%s: skipped this round: %s", repo.slug, number, ex)
            continue
        if done is not None:
            settled.append(done)
    return settled


async def _triage_one(
    cfg: Config, secrets: Secrets, repo: RepoConfig, db: Db, number: int, *, dry_run: bool
) -> Settled | None:
    issue = await issue_meta(repo.slug, number)
    if issue.state != "OPEN":
        # the search said open, but a queue read and a decision are minutes apart
        return None
    call = verdict(fields(issue.body), repo.issues.rules)
    if call.action == ASK:
        asked = await _ask_about_money(cfg, secrets, repo, db, issue, dry_run=dry_run)
        if asked is None:
            return None
        call = asked
    if call.action == ATTEMPT:
        note = (
            "Nothing in this report puts it out of an automated fix's reach, so it is "
            f"queued for one. A person reviews whatever comes out of that.\n\n_{call.reason}._"
        )
        result = await publish.settle_issue(
            repo, issue, say=note, add_label=repo.issues.fixable_label, dry_run=dry_run
        )
    else:
        note = f"Over to a human.\n\n_{call.reason}._"
        result = await publish.settle_issue(
            repo, issue, say=note, assignee=repo.issues.assignee, dry_run=dry_run
        )
        if not dry_run:
            await jane.tell(cfg, secrets, (
                f"Triage sent bug #{number} ({issue.title}) to "
                f"{', '.join(repo.issues.assignee) or 'nobody'} instead of fixing it: "
                f"{call.reason}. {issue.url}"
            ))
    logger.info("%s#%s: %s — %s", repo.slug, number, call.action, result.detail)
    return Settled(number, call.action, call.reason)


async def _ask_about_money(
    cfg: Config, secrets: Secrets, repo: RepoConfig, db: Db, issue: IssueMeta,
    *, dry_run: bool,
) -> Verdict | None:
    """The one question the form left open. None means ask again next tick.

    Which arm answered is logged with the answer it gave, from the first issue
    onwards: the choice of model here is a guess until there are real reports to
    replay it against, and a guess nobody wrote down stays one.
    """
    arm = repo.issues.money_model or "the account default"
    if dry_run:
        logger.info("%s#%s: dry run: would ask %s the money question",
                    repo.slug, issue.number, arm)
        return None
    gate = budget.check(cfg, db, 1, via_endpoint=repo.issues.money_via_endpoint)
    if not gate.allowed:
        logger.info("%s#%s: the money question waits for the meter: %s",
                    repo.slug, issue.number, gate.detail)
        return None

    run = await run_review(
        cfg, secrets, repo, issue,
        prompt=money_prompt(issue.title, issue.body),
        mode="money",
        model=repo.issues.money_model,
        via_endpoint=repo.issues.money_via_endpoint,
    )
    db.record_spend(
        repo=repo.slug, pr=issue.number, kind="money",
        cost_usd=run.cost_usd, duration_s=run.duration_s,
    )
    if not run.ok:
        logger.warning("%s#%s: the money question did not come back (%s)",
                       repo.slug, issue.number, run.error)
        return Verdict(ASSIGN, f"the money check could not finish ({run.error})")

    money = touches_money(run.text)
    logger.info("%s#%s: money=%s from %s in %.0fs costing %s",
                repo.slug, issue.number, money, arm, run.duration_s, run.cost_usd)
    if money:
        return Verdict(ASSIGN, f"{arm} found a money path in the code this report lands in")
    return Verdict(ATTEMPT, f"{arm} found no money path in the code this report lands in")
