"""Whether a follow-up's wait is over: the event its form names, checked on GitHub.

Three answers, not two. `True` is the event happened. `False` is not yet: the
issue stays in the queue and nothing is written, so the next tick asks again.
`None` is nothing on the form that robbie can check — a job finishing in
production, an upgrade, a pull request that was closed instead of merged — and
that is a person's call, never a guess either way.

ponytail: every waiting issue costs a few API reads per tick and nothing else.
Remember the last answer per issue the day the queue is long enough to matter.
"""

from __future__ import annotations

import re
from datetime import date
from typing import NamedTuple

from robbie.config import WaitConfig
from robbie.github import GhError, gh

# `#1234`, not `org/repo#1234` and not the middle of a URL fragment
_PR_REF = re.compile(r"(?<![\w/&])#(\d+)\b")


class Wait(NamedTuple):
    over: bool | None
    reason: str


async def wait_over(slug: str, answers: dict[str, str], rules: WaitConfig, *, today: date) -> Wait:
    event = answers.get(rules.field, "")
    raw = answers.get(rules.not_before, "") if rules.not_before else ""
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return Wait(None, f"{rules.not_before}: {raw!r} is not a YYYY-MM-DD date")
        if today < day:
            return Wait(False, f"not before {day}")
    if event in rules.now:
        return Wait(True, f"{rules.field}: {event}")
    if event in rules.dated:
        if raw:
            return Wait(True, f"{raw} has passed")
        return Wait(None, f"{rules.not_before or 'the date'}: unanswered")
    if event not in (*rules.merged, *rules.deployed):
        return Wait(None, f"{rules.field}: {event or 'unanswered'}")

    prs = sorted({int(n) for n in _PR_REF.findall(answers.get(rules.waiting_on, ""))})
    if not prs:
        return Wait(None, f"{rules.waiting_on}: names no pull request as #1234")
    tag = ""
    if event in rules.deployed:
        tag = await _live_tag(slug, rules.deploy_tag)
        if not tag:
            return Wait(None, "no deploy tag to compare against")
    for number in prs:
        merged = await _merge_commit(slug, number)
        if merged is None:
            return Wait(None, f"#{number} is not a pull request, or was closed without merging")
        if not merged:
            return Wait(False, f"#{number} is not merged")
        if tag and not await _contains(slug, tag, merged):
            return Wait(False, f"#{number} is not in {tag} yet")
    refs = ", ".join(f"#{n}" for n in prs)
    return Wait(True, f"{refs} in {tag}" if tag else f"{refs} merged")


async def _merge_commit(slug: str, number: int) -> str | None:
    """The merge commit, "" while it is open, None when it will never merge.

    A 404 is an issue number, not a pull request — an answer about the form, so
    it is None. Any other failure raises, and the tick tries again: a GitHub
    hiccup must not hand a follow-up to a person.
    """
    try:
        out = await gh("api", f"repos/{slug}/pulls/{number}", "--jq",
                       '[.state, (.merged|tostring), .merge_commit_sha // ""] | join(" ")')
    except GhError as ex:
        if "HTTP 404" in str(ex):
            return None
        raise
    state, merged, sha = (out.split() + ["", "", ""])[:3]
    if merged == "true":
        return sha
    return "" if state == "open" else None


async def _live_tag(slug: str, pattern: str) -> str:
    if not pattern:
        return ""
    out = await gh("api", "--paginate", f"repos/{slug}/git/matching-refs/tags/",
                   "--jq", ".[].ref")
    names = [ref.removeprefix("refs/tags/") for ref in out.split()]
    return max((n for n in names if re.search(pattern, n)), default="")


async def _contains(slug: str, tag: str, sha: str) -> bool:
    status = await gh("api", f"repos/{slug}/compare/{sha}...{tag}", "--jq", ".status")
    return status.strip() in ("ahead", "identical")
