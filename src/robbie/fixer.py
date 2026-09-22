"""The fixer's tick: take an issue triage cleared, and try to fix it.

One container per issue, with no GitHub credential in it at all. The model reads
the mirror, writes a failing test and a fix, and hands the patch to the PR tool
on the sidecar — which is the only thing in this system holding a token that can
write to the repository, and which decides the branch, the base and the draft
state itself.

Nothing the model says is taken as proof. When the run is over this asks GitHub
whether the branch has a pull request on it, and that answer is the outcome; the
blocks it emits are only how it explains itself to the person who picks the issue
up when the answer is no.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from robbie import budget, publish
from robbie.config import Config, RepoConfig, Secrets
from robbie.contract import fix_preamble, parse_fix
from robbie.db import Db
from robbie.github import GhError, gh_json, issue_meta, issue_queue
from robbie.runner import run_review

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "fix/issue-"


@dataclass(frozen=True)
class Fixed:
    number: int
    opened: str  # the pull request url, or "" when it went back to a person
    reason: str


def queue_labels(repo: RepoConfig) -> tuple[str, ...]:
    """What an issue has to carry to be the fixer's: everything the triage queue
    selected on except the label it cleared, plus the one it added."""
    issues = repo.issues
    return tuple(
        lbl for lbl in issues.labels if lbl != issues.clears
    ) + (issues.fixable_label,)


async def fix_tick(
    cfg: Config, secrets: Secrets, repo: RepoConfig, db: Db, *, dry_run: bool = False
) -> list[Fixed]:
    """One pass over the issues triage cleared. Sequential, like the triage tick:
    each one is a container, and two at once is two of the same cap."""
    if not repo.issues.labels or not cfg.fix_mcp:
        return []
    labels = queue_labels(repo)
    fixed: list[Fixed] = []
    for number in await issue_queue(repo.slug, labels=labels):
        try:
            done = await _fix_one(cfg, secrets, repo, db, number, dry_run=dry_run)
        except GhError as ex:
            logger.warning("%s#%s: skipped this round: %s", repo.slug, number, ex)
            continue
        if done is not None:
            fixed.append(done)
    return fixed


async def _fix_one(
    cfg: Config, secrets: Secrets, repo: RepoConfig, db: Db, number: int, *, dry_run: bool
) -> Fixed | None:
    issue = await issue_meta(repo.slug, number)
    if issue.state != "OPEN":
        return None
    if dry_run:
        logger.info("%s#%s: dry run: would try to fix it", repo.slug, number)
        return None
    if (opened := await open_pr_for(repo.slug, number)):
        # somebody's earlier run already got there; the label is all that is left
        return await _settle(repo, issue, opened, "a pull request was already open")

    gate = budget.check(cfg, db, 1, via_endpoint=repo.issues.fix_via_endpoint)
    if not gate.allowed:
        logger.info("%s#%s: the fix waits for the meter: %s", repo.slug, number, gate.detail)
        return None

    run = await run_review(
        cfg, secrets, repo, issue,
        prompt=fix_preamble(number=number, title=issue.title, body=issue.body),
        mode="fix",
        model=repo.issues.fix_model,
        via_endpoint=repo.issues.fix_via_endpoint,
    )
    db.record_spend(
        repo=repo.slug, pr=number, kind="fix",
        cost_usd=run.cost_usd, duration_s=run.duration_s,
    )

    opened = await open_pr_for(repo.slug, number)
    if opened:
        blocks = parse_fix(run.text)
        logger.info("%s#%s: opened %s with %s in %.0fs costing %s",
                    repo.slug, number, opened,
                    repo.issues.fix_model or "the account default",
                    run.duration_s, run.cost_usd)
        return await _settle(repo, issue, opened, blocks.body or "fixed")

    reason = _why_not(run.ok, run.error, run.text)
    logger.info("%s#%s: no pull request — %s", repo.slug, number, reason)
    return await _settle(repo, issue, "", reason)


def _why_not(ok: bool, error: str | None, text: str) -> str:
    """What to tell the person who picks this up. The model's own words when it
    has them, because "I could not write a failing test for this" is the single
    most useful thing it can hand over."""
    if not ok:
        return f"the fix run could not finish ({error})"
    said = parse_fix(text)
    if said.body.strip():
        return said.body.strip()
    return "the run ended without opening a pull request and without saying why"


async def open_pr_for(slug: str, number: int) -> str:
    """The url of the pull request on this issue's branch, or empty.

    Read from GitHub rather than from the run: whether the work got out is not
    something the thing doing the work gets to report on.
    """
    rows = await gh_json(
        "pr", "list", "--repo", slug, "--head", f"{BRANCH_PREFIX}{number}",
        "--state", "all", "--limit", "1", "--json", "url",
    )
    return (rows or [{}])[0].get("url", "") if rows else ""


async def _settle(repo: RepoConfig, issue, opened: str, reason: str) -> Fixed:
    if opened:
        say = f"Opened {opened} for this — still a draft, and it needs a human review."
        assignee = ""
    else:
        say = f"I could not fix this one, so it is back with a person.\n\n_{reason}_"
        assignee = repo.issues.assignee
    await publish.settle_issue(
        repo, issue, say=say, assignee=assignee, clears=repo.issues.fixable_label,
    )
    return Fixed(issue.number, opened, reason)
