"""GitHub access through the `gh` CLI.

Why the CLI and not httpx: auth, pagination, retries and the GraphQL endpoint
come for free, and these exact queries are the ones git-sentinel proved against
the real repo. Reimplementing them would be more code and new bugs.

Every function distinguishes three outcomes, not two: a value, a documented
"nothing recorded" sentinel, or GhError. A transient API failure must never be
read as zero — that is how a hiccup turns into a duplicate review.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

NO_DIRECT_REQUEST = "norq"  # review requested via a team, or no event recorded


class GhError(RuntimeError):
    """The gh call failed. Callers skip this round; they never guess."""


@dataclass(frozen=True)
class PrMeta:
    number: int
    title: str
    url: str
    author: str
    head_sha: str
    changed_files: int
    labels: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]

    def has_label(self, name: str) -> bool:
        return name in self.labels


async def _gh(*args: str, stdin: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} exited {proc.returncode}: {err.decode()[:400]}")
    return out.decode()


async def _gh_json(*args: str, stdin: str | None = None) -> Any:
    raw = await _gh(*args, stdin=stdin)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as ex:
        raise GhError(f"gh returned non-JSON: {raw[:200]}") from ex


async def whoami() -> str:
    return (await _gh("api", "user", "--jq", ".login")).strip()


async def queue(repo: str, *, label: str, reviewer: str) -> list[int]:
    """PRs carrying the label AND pending this reviewer — gates 1 and 2.

    A submitted changes-requested review clears the request, so those drop out
    of here until the author re-requests; `digest` nags the ones that never do.
    """
    rows = await _gh_json(
        "search", "prs", "--repo", repo,
        f"--review-requested={reviewer}", "--label", label,
        "--state", "open", "--limit", "50", "--json", "number",
    )
    return [int(r["number"]) for r in rows or []]


async def pr_meta(repo: str, pr: int) -> PrMeta:
    data = await _gh_json(
        "pr", "view", str(pr), "--repo", repo, "--json",
        "number,title,url,author,headRefOid,changedFiles,labels,statusCheckRollup",
    )
    if not data:
        raise GhError(f"could not fetch {repo}#{pr}")
    return PrMeta(
        number=int(data["number"]),
        title=data.get("title") or "",
        url=data.get("url") or "",
        author=(data.get("author") or {}).get("login") or "someone",
        head_sha=data.get("headRefOid") or "",
        changed_files=int(data.get("changedFiles") or 0),
        labels=tuple(lbl["name"] for lbl in data.get("labels") or []),
        checks=tuple(data.get("statusCheckRollup") or []),
    )


async def last_review_request(repo: str, pr: int, reviewer: str) -> str:
    """ISO timestamp of the most recent review request for `reviewer`.

    Folded into the dedup key so a re-request triggers a fresh look even when
    the head commit has not moved. Returns NO_DIRECT_REQUEST when the timeline
    has no such event — a stable value, unlike an error.
    """
    raw = await _gh(
        "api", f"repos/{repo}/issues/{pr}/timeline", "--paginate",
        "--jq", f'.[] | select(.event=="review_requested" '
                f'and .requested_reviewer.login=="{reviewer}") | .created_at',
    )
    lines = [line for line in raw.splitlines() if line.strip()]
    return lines[-1] if lines else NO_DIRECT_REQUEST


async def open_threads(repo: str, pr: int, reviewer: str) -> int:
    """Comments of `reviewer` still waiting on the author.

    Unresolved, not outdated (a fix push moves the code and outdates the
    thread), and the last word is the reviewer's.
    """
    owner, name = repo.split("/", 1)
    query = """
      query($owner:String!,$name:String!,$num:Int!){
        repository(owner:$owner,name:$name){ pullRequest(number:$num){
          reviewThreads(first:100){ nodes {
            isResolved isOutdated comments(first:50){ nodes { author { login } } }
          }}}}}
    """
    n = await _gh_json(
        "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}",
        "-F", f"num={pr}", "-f", f"query={query}",
        "--jq", "[ .data.repository.pullRequest.reviewThreads.nodes[] "
                "| select(.isResolved == false and .isOutdated == false) "
                f'| select((.comments.nodes | last | .author.login) == "{reviewer}") ] | length',
    )
    return int(n or 0)


def failing_checks(meta: PrMeta, *, ignore: tuple[str, ...]) -> list[str]:
    """Names of red contexts on the head commit; empty when green or running.

    Both spellings: commit statuses carry `state`, check runs carry
    `conclusion`. Still-running is not red — the code is judged as it stands.
    """
    bad_states = {"FAILURE", "ERROR"}
    bad_conclusions = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
    out: list[str] = []
    for check in meta.checks:
        name = check.get("context") or check.get("name") or "check"
        if name in ignore:
            continue
        if (check.get("state") or "") in bad_states or (
            check.get("conclusion") or ""
        ) in bad_conclusions:
            out.append(name)
    return sorted(set(out))


async def review_still_requested(repo: str, pr: int, reviewer: str) -> bool:
    logins = await _gh_json(
        "pr", "view", str(pr), "--repo", repo, "--json", "reviewRequests",
        "--jq", "[.reviewRequests[].login]",
    )
    return reviewer in (logins or [])


async def stale_changes_requested(repo: str, reviewer: str, days: int) -> list[dict[str, Any]]:
    """PRs sitting on a standing changes-requested review for `days`+.

    Clocked on the age of the review, not on last activity: these authors keep
    pushing, they just never re-request, so "no recent activity" finds nothing.
    """
    query = """
      query($q: String!, $me: String!) {
        search(query: $q, type: ISSUE, first: 50) {
          nodes { ... on PullRequest {
            number title url author { login }
            reviews(last: 20, author: $me, states: CHANGES_REQUESTED) { nodes { submittedAt } }
            commits(last: 1) { nodes { commit { committedDate } } }
          }}
        }
      }
    """
    search = (
        f'repo:{repo} is:pr is:open label:"Code Review" '
        f"reviewed-by:{reviewer} review:changes_requested"
    )
    data = await _gh_json(
        "api", "graphql", "-f", f"q={search}", "-f", f"me={reviewer}", "-f", f"query={query}"
    )
    nodes = (((data or {}).get("data") or {}).get("search") or {}).get("nodes") or []
    return [n for n in nodes if n]
