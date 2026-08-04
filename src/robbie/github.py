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
    base_ref: str = "main"
    state: str = "OPEN"

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
        "number,title,url,author,headRefOid,changedFiles,labels,statusCheckRollup,baseRefName,state",
    )
    if not data:
        raise GhError(f"could not fetch {repo}#{pr}")
    return PrMeta(
        base_ref=data.get("baseRefName") or "main",
        state=str(data.get("state") or "OPEN"),
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
    return sum(1 for t in await my_threads(repo, pr, reviewer) if t.awaiting_author)


@dataclass(frozen=True)
class Thread:
    """One review thread the reviewer started, with whatever came back.

    GitHub is the store for this; robbie keeps no copy. Resolution state, the
    replies and their order all live here, so a local mirror would only be a
    cache to invalidate.
    """

    path: str
    line: int | None
    resolved: bool
    outdated: bool
    mine: str  # what the reviewer said first
    replies: tuple[tuple[str, str], ...]  # (author, body), in order, after that
    node_id: str = ""  # for resolveReviewThread
    comment_id: int = 0  # databaseId of the first comment, for posting a reply
    mine_is_last: bool = True

    @property
    def live(self) -> bool:
        return not self.resolved and not self.outdated

    @property
    def awaiting_author(self) -> bool:
        """Open and the reviewer spoke last — including after answering a reply."""
        return self.live and self.mine_is_last

    @property
    def answered(self) -> bool:
        """Open and someone else spoke last, so it is the reviewer's move.

        Outdated counts here, unlike in `awaiting_author`: the code moving is
        usually the fix landing, so it is the likeliest thread to close. What it
        does rule out is a reply — GitHub collapses those out of sight.
        """
        return not self.resolved and not self.mine_is_last


THREAD_PAGE = 100  # GraphQL's per-page maximum for both connections


async def my_threads(repo: str, pr: int, reviewer: str) -> list[Thread]:
    """Every review thread opened by `reviewer`, with its replies.

    Paged to the end rather than capped at one page: gate 5 counts these, and a
    truncated read can only under-count, which is the direction that lets a
    review through while robbie's last findings still stand unanswered.
    """
    owner, name = repo.split("/", 1)
    query = """
      query($owner:String!,$name:String!,$num:Int!,$first:Int!,$after:String){
        repository(owner:$owner,name:$name){ pullRequest(number:$num){
          reviewThreads(first:$first, after:$after){
            pageInfo { hasNextPage endCursor }
            nodes {
              id isResolved isOutdated path line
              comments(first:$first){ nodes { databaseId author { login } body } }
            }
          }}}}
    """

    nodes: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        args = ["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"num={pr}", "-F", f"first={THREAD_PAGE}", "-f", f"query={query}"]
        if after:
            args += ["-f", f"after={after}"]
        data = await _gh_json(*args)
        conn = (
            (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        ).get("reviewThreads") or {}
        nodes += conn.get("nodes") or []
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = str(page.get("endCursor") or "")
        if not after:
            break

    out: list[Thread] = []
    for node in nodes:
        comments = ((node.get("comments") or {}).get("nodes")) or []
        if not comments:
            continue
        first = comments[0]
        if ((first.get("author") or {}).get("login")) != reviewer:
            continue  # someone else's thread; not ours to answer
        if len(comments) == THREAD_PAGE:
            # who spoke last is read off the end of this list, so say it is short
            logger.warning(
                "%s#%s %s: thread has %d+ comments; only the first %d were read",
                repo, pr, node.get("path"), THREAD_PAGE, THREAD_PAGE,
            )
        out.append(Thread(
            path=node.get("path") or "?",
            line=node.get("line"),
            resolved=bool(node.get("isResolved")),
            outdated=bool(node.get("isOutdated")),
            mine=str(first.get("body") or ""),
            replies=tuple(
                (((c.get("author") or {}).get("login") or "?"), str(c.get("body") or ""))
                for c in comments[1:]
            ),
            node_id=str(node.get("id") or ""),
            comment_id=int(first.get("databaseId") or 0),
            mine_is_last=((comments[-1].get("author") or {}).get("login")) == reviewer,
        ))
    return out


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


# CI runs when a review approves the commit, so this is the normal state on a
# first pass, not a broken integration
NO_CHECKS = "No CI checks are reporting on this commit."


@dataclass(frozen=True)
class CheckSummary:
    """What CI says about the head commit, bucketed.

    The reviewer container has no CI-provider credentials, so this is the only
    CI truth it gets — and it is the same data gate 6 judged, which is why the
    model's "CI & linters" line can never contradict the decision to review.
    """

    passing: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
    running: tuple[str, ...] = ()
    other: tuple[str, ...] = ()

    def as_prompt(self) -> str:
        if not (self.passing or self.failing or self.running or self.other):
            return NO_CHECKS
        parts = []
        if self.passing:
            parts.append(f"passing ({len(self.passing)}): {', '.join(self.passing)}")
        if self.running:
            parts.append(f"STILL RUNNING: {', '.join(self.running)}")
        if self.failing:
            parts.append(f"FAILING: {', '.join(self.failing)}")
        if self.other:
            parts.append(f"neutral/skipped: {', '.join(self.other)}")
        return " · ".join(parts)


def summarize_checks(meta: PrMeta) -> CheckSummary:
    """Bucket every check on the head commit, including the ones gate 6 ignores."""
    passing, failing, running, other = [], [], [], []
    for check in meta.checks:
        name = check.get("context") or check.get("name") or "check"
        state = (check.get("state") or check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if state == "SUCCESS":
            passing.append(name)
        elif state in {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}:
            failing.append(name)
        elif state in {"PENDING", "EXPECTED", "IN_PROGRESS", "QUEUED", "WAITING"} or (
            not state and status in {"IN_PROGRESS", "QUEUED", "PENDING"}
        ):
            running.append(name)
        else:
            other.append(f"{name}={state or status or '?'}")
    return CheckSummary(
        passing=tuple(sorted(set(passing))),
        failing=tuple(sorted(set(failing))),
        running=tuple(sorted(set(running))),
        other=tuple(sorted(set(other))),
    )


async def stale_changes_requested(
    repo: str, reviewer: str, *, label: str
) -> list[dict[str, Any]]:
    """PRs sitting on a standing changes-requested review from `reviewer`.

    Clocked on the age of the review, not on last activity: these authors keep
    pushing, they just never re-request, so "no recent activity" finds nothing.
    The age cut itself is the caller's, since the search cannot express it.
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
        f'repo:{repo} is:pr is:open label:"{label}" '
        f"reviewed-by:{reviewer} review:changes_requested"
    )
    data = await _gh_json(
        "api", "graphql", "-f", f"q={search}", "-f", f"me={reviewer}", "-f", f"query={query}"
    )
    nodes = (((data or {}).get("data") or {}).get("search") or {}).get("nodes") or []
    return [n for n in nodes if n]
