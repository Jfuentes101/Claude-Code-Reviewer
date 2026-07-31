"""Weekly digest of PRs parked on a standing changes-requested review.

A needs-work review clears the reviewer's pending request, which is what keeps
the queue honest — but it also means a PR nobody re-requests falls out of every
queue and is never seen again. This is the nag for those.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from robbie.config import Config
from robbie.github import stale_changes_requested
from robbie.slack import Slack

logger = logging.getLogger(__name__)


async def post_digest(cfg: Config, slack: Slack, *, days: int | None = None) -> int:
    """Post one digest per repo that has a channel configured. Returns PRs listed."""
    threshold = days if days is not None else cfg.stale_review_days
    total = 0
    for repo in cfg.repos:
        if not repo.slack_channel:
            logger.info("%s has no slack_channel; skipping its digest", repo.slug)
            continue
        nodes = await stale_changes_requested(repo.slug, repo.reviewer_login, threshold)
        lines = _lines(nodes, threshold)
        if not lines:
            logger.info("%s: nothing stuck in review %d+ day(s)", repo.slug, threshold)
            continue
        plural = "" if len(lines) == 1 else "s"
        await slack.post(
            repo.slack_channel,
            f"<!here> {len(lines)} PR{plural} stuck on requested changes for {threshold}+ "
            "days — parked out of every review queue until the author re-requests:\n\n"
            + "\n".join(lines)
            + "\n\nIf the fixes are already pushed, *re-request the review* and it comes "
            "straight back to me. If it's dead, close it. 🙏",
        )
        total += len(lines)
    return total


def _lines(nodes: list[dict], threshold: int) -> list[str]:
    now = datetime.now(UTC)
    rows: list[tuple[int, str]] = []
    for node in nodes:
        reviews = ((node.get("reviews") or {}).get("nodes")) or []
        commits = ((node.get("commits") or {}).get("nodes")) or []
        if not reviews or not commits:
            continue
        age = _days_since(reviews[-1].get("submittedAt"), now)
        pushed = _days_since((commits[0].get("commit") or {}).get("committedDate"), now)
        if age is None or pushed is None or age < threshold:
            continue
        author = (node.get("author") or {}).get("login") or "someone"
        last = "today" if pushed < 1 else f"{pushed}d ago"
        rows.append((
            age,
            f"• <{node['url']}|*#{node['number']}*> {str(node.get('title'))[:70]} — "
            f"{author} · changes requested *{age}d* ago · last push {last}",
        ))
    rows.sort(key=lambda r: -r[0])
    return [line for _, line in rows]


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((now - then).total_seconds() // 86400)
