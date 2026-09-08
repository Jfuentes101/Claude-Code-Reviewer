"""GitHub access through the `gh` CLI.

The CLI and not httpx because auth, pagination, retries and the GraphQL endpoint
come for free.

Every function distinguishes three outcomes, not two: a value, a documented
"nothing recorded" sentinel, or GhError. A transient API failure must never be
read as zero — that is how a hiccup turns into a duplicate review.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

NO_DIRECT_REQUEST = "norq"  # review requested via a team, or no event recorded
# a hung call would otherwise hang the tick, and with it every following one
TIMEOUT_S = 120


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


async def gh(*args: str, stdin: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # its own process group, so the timeout can take the children with it:
        # anything still holding a pipe keeps this call waiting past the deadline
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=TIMEOUT_S,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise GhError(f"gh {' '.join(args[:3])} timed out after {TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} exited {proc.returncode}: {err.decode()[:400]}")
    return out.decode()


async def gh_json(*args: str, stdin: str | None = None) -> Any:
    raw = await gh(*args, stdin=stdin)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as ex:
        raise GhError(f"gh returned non-JSON: {raw[:200]}") from ex


async def whoami() -> str:
    return (await gh("api", "user", "--jq", ".login")).strip()


# `gh search` takes one page and has no --paginate, so this is the whole read.
# Well above any real review queue, because a PR past it is invisible to every
# gate rather than merely late — the warning below is the only trace it leaves.
QUEUE_LIMIT = 200


async def queue(repo: str, *, label: str, reviewer: str | None = None) -> list[int]:
    """PRs carrying the label AND pending this reviewer — gates 1 and 2.

    A changes-requested review clears the request, so those drop out until the
    author re-requests; `digest` nags the ones that never do.

    Without a reviewer it answers the wider question the panel needs — everything
    still open under the label, whoever it is waiting on.
    """
    rows = await gh_json(
        "search", "prs", "--repo", repo,
        *([f"--review-requested={reviewer}"] if reviewer else []), "--label", label,
        "--state", "open", "--limit", str(QUEUE_LIMIT), "--json", "number",
    )
    prs = [int(r["number"]) for r in rows or []]
    if len(prs) == QUEUE_LIMIT:
        # a full page is indistinguishable from a truncated one, and the PRs past it
        # are invisible to every gate rather than merely late
        logger.warning(
            "%s: the queue read filled its %d-PR page; anything past it is unseen",
            repo, QUEUE_LIMIT,
        )
    return prs


async def authored(repo: str, *, label: str, author: str) -> list[int]:
    """PRs the reviewer AUTHORED that carry the label — the self-queue.

    GitHub cannot request your review on your own PR, so the request queue
    structurally never contains them. The label alone is the author's trigger:
    label your PR, your own instance clears it before a human's is asked.
    Same page-limit caveat as `queue`.
    """
    rows = await gh_json(
        "search", "prs", "--repo", repo,
        f"--author={author}", "--label", label,
        "--state", "open", "--limit", str(QUEUE_LIMIT), "--json", "number",
    )
    return [int(r["number"]) for r in rows or []]


PR_FIELDS = (
    "number,title,url,author,headRefOid,changedFiles,labels,"
    "statusCheckRollup,baseRefName,state"
)
FORBIDDEN_NODE = "not accessible by personal access token"


async def _commit_statuses(repo: str, sha: str) -> list[dict[str, Any]]:
    """The rollup's commit statuses over REST, in the shape `summarize_checks` reads."""
    if not sha:
        return []
    return await gh_json(
        "api", f"repos/{repo}/commits/{sha}/status",
        "--jq", "[.statuses[] | {context, state}]",
    ) or []


async def pr_meta(repo: str, pr: int) -> PrMeta:
    try:
        data = await gh_json("pr", "view", str(pr), "--repo", repo, "--json", PR_FIELDS)
    except GhError as ex:
        if FORBIDDEN_NODE not in str(ex):
            raise
        # No fine-grained PAT can read a check run — GitHub has no Checks permission
        # for them — and one unreadable node fails the whole view rather than nulling
        # itself out, taking the labels and the sha with it. Commit statuses still
        # come back over REST, so the PR is judged on those instead of being skipped.
        data = await gh_json(
            "pr", "view", str(pr), "--repo", repo,
            "--json", PR_FIELDS.replace("statusCheckRollup,", ""),
        )
        if data:
            data["statusCheckRollup"] = await _commit_statuses(
                repo, data.get("headRefOid") or ""
            )
            logger.warning(
                "%s#%s: this token cannot read check runs; judged on commit statuses only",
                repo, pr,
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

    Folded into the dedup key, so a re-request triggers a fresh look even when the
    head commit has not moved. NO_DIRECT_REQUEST when the timeline has no such
    event — a stable value, unlike an error.
    """
    raw = await gh(
        "api", f"repos/{repo}/issues/{pr}/timeline", "--paginate",
        "--jq", f'.[] | select(.event=="review_requested" '
                f'and .requested_reviewer.login=="{reviewer}") | .created_at',
    )
    lines = [line for line in raw.splitlines() if line.strip()]
    return lines[-1] if lines else NO_DIRECT_REQUEST


@dataclass(frozen=True)
class Thread:
    """One review thread the reviewer started, with whatever came back. GitHub owns
    resolution state and the replies; robbie keeps no copy."""

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

        Outdated counts here, unlike in `awaiting_author` — the code moving is
        usually the fix landing. It does rule out replying: GitHub collapses those.
        """
        return not self.resolved and not self.mine_is_last


THREAD_PAGE = 100  # GraphQL's per-page maximum for both connections


@dataclass(frozen=True)
class PrThreads:
    """The reviewer's threads on a PR, and whether the PR is still alive.

    The state rides along because learning a merge here retires the PR from the
    sweep for free. Empty when unreadable, never guessed.
    """

    state: str = ""
    threads: list[Thread] = field(default_factory=list)


async def my_threads(repo: str, pr: int, reviewer: str) -> PrThreads:
    """Every review thread opened by `reviewer`, with its replies.

    Paged to the end, not capped at one page: gate 5 counts these, and a truncated
    read under-counts, which is the direction that lets a review through while the
    last findings stand unanswered.
    """
    owner, name = repo.split("/", 1)
    query = """
      query($owner:String!,$name:String!,$num:Int!,$first:Int!,$after:String){
        repository(owner:$owner,name:$name){ pullRequest(number:$num){
          state
          reviewThreads(first:$first, after:$after){
            pageInfo { hasNextPage endCursor }
            nodes {
              id isResolved isOutdated path line
              comments(first:$first){ nodes { databaseId author { login } body } }
            }
          }}}}
    """

    nodes: list[dict[str, Any]] = []
    state = ""
    after: str | None = None
    while True:
        args = ["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"num={pr}", "-F", f"first={THREAD_PAGE}", "-f", f"query={query}"]
        if after:
            args += ["-f", f"after={after}"]
        data = await gh_json(*args)
        pull = (
            ((data or {}).get("data") or {}).get("repository") or {}
        ).get("pullRequest") or {}
        state = str(pull.get("state") or "")
        conn = pull.get("reviewThreads") or {}
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
    return PrThreads(state=state, threads=out)


# CI runs when a review approves the commit, so this is the normal state on a
# first pass, not a broken integration
NO_CHECKS = "No CI checks are reporting on this commit."
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
PENDING = {"PENDING", "EXPECTED", "IN_PROGRESS", "QUEUED", "WAITING"}


@dataclass(frozen=True)
class CheckSummary:
    """What CI says about the head commit, bucketed. The reviewer has no CI-provider
    credentials, so this is the only CI truth it gets, and gate 6 judged the same."""

    passing: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
    running: tuple[str, ...] = ()
    other: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Nothing is reporting at all — the normal state of an unbuilt commit."""
        return not (self.passing or self.failing or self.running or self.other)

    def as_prompt(self) -> str:
        if self.empty:
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


CHECK_NAME_CAP = 120


def _one_line(name: str) -> str:
    """Flatten a check's name — it reaches a prompt, and a PR chooses it.

    Same untrusted shape `contract._strip` exists for: a marker is only a marker on
    a line of its own, so text that cannot carry a newline cannot forge one.
    """
    flat = " ".join(name.split())
    return flat if len(flat) <= CHECK_NAME_CAP else flat[:CHECK_NAME_CAP] + "…"


def _check_name(check: dict[str, Any]) -> str:
    return _one_line(check.get("context") or check.get("name") or "check")


def summarize_checks(meta: PrMeta) -> CheckSummary:
    """Bucket every check on the head commit, including the ones gate 6 ignores.

    Both spellings: commit statuses carry `state`, check runs carry `conclusion`.
    """
    passing, failing, running, other = [], [], [], []
    for check in meta.checks:
        name = _check_name(check)
        state = (check.get("state") or check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if state == "SUCCESS":
            passing.append(name)
        elif state in FAILED:
            failing.append(name)
        elif state in PENDING or (not state and status in PENDING):
            running.append(name)
        else:
            other.append(f"{name}={state or status or '?'}")
    return CheckSummary(
        passing=tuple(sorted(set(passing))),
        failing=tuple(sorted(set(failing))),
        running=tuple(sorted(set(running))),
        other=tuple(sorted(set(other))),
    )


def ci_started(meta: PrMeta, *, ignore: tuple[str, ...]) -> bool:
    """Whether a build exists for this commit at all, however it got asked for.

    `ignore` is what makes the question answerable: a review bot posts a status on
    every push, so "this commit has a check" is not "this commit has a build".
    """
    return any(_check_name(check) not in ignore for check in meta.checks)


def ci_outcome(meta: PrMeta, *, ignore: tuple[str, ...]) -> str:
    """`green`, `red` or `waiting` for a commit robbie already approved.

    Nothing reporting yet is `waiting`, never green: the build may not have started,
    and an approval is not evidence about a test. An ignored check is not a build
    either, so a bot's green tick on its own is still `waiting`.
    """
    summary = summarize_checks(meta)
    kept = lambda names: [name for name in names if name not in ignore]  # noqa: E731
    if kept(summary.failing):
        return "red"
    if kept(summary.running) or not kept(summary.passing):
        return "waiting"
    return "green"


def failing_checks(meta: PrMeta, *, ignore: tuple[str, ...]) -> list[str]:
    """Gate 6's red list: what the summary calls failing, minus the ignored ones.

    Derived from the bucketing the reviewer is handed, so the model's CI line cannot
    contradict the gate that let it run. Still-running is not red.
    """
    return [name for name in summarize_checks(meta).failing if name not in ignore]


class LabeledPr(NamedTuple):
    head: str
    labels: tuple[str, ...]


async def labeled_heads(repo: str, *, label: str) -> dict[int, LabeledPr]:
    """Every open PR under the label, with its head sha and its labels.

    `queue` cannot answer this: `gh search prs` has no headRefOid, and the sha is
    the whole point. `gh pr list` filters on the label just as well, so the sweep
    pays the same one call it was already paying — and the labels ride along for
    free, which is what tells the panel a PR is no longer ready.
    """
    rows = await gh_json(
        "pr", "list", "--repo", repo, "--label", label, "--state", "open",
        "--limit", str(QUEUE_LIMIT), "--json", "number,headRefOid,labels",
    )
    return {
        int(r["number"]): LabeledPr(
            head=r.get("headRefOid") or "",
            labels=tuple((lb.get("name") or "") for lb in r.get("labels") or []),
        )
        for r in rows or []
    }


async def standing_rejection(repo: str, pr: int, reviewer: str) -> str | None:
    """The node id of `reviewer`'s standing changes-requested review, or None.

    `latestOpinionatedReviews` is the field that answers "what still counts": it
    keeps one review per person, drops the COMMENTED ones a later pass leaves
    behind, and omits anything already dismissed. Reading `reviews` instead finds
    a rejection GitHub no longer applies.
    """
    query = """
      query($n: Int!, $owner: String!, $name: String!) {
        repository(owner: $owner, name: $name) { pullRequest(number: $n) {
          latestOpinionatedReviews(first: 20) {
            nodes { id state author { login } }
          }
        } }
      }
    """
    owner, name = repo.split("/", 1)
    data = await gh_json(
        "api", "graphql", "-F", f"n={pr}", "-f", f"owner={owner}", "-f", f"name={name}",
        "-f", f"query={query}",
    )
    pull = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    for node in (pull.get("latestOpinionatedReviews") or {}).get("nodes") or []:
        if (
            node
            and node.get("state") == "CHANGES_REQUESTED"
            and ((node.get("author") or {}).get("login") or "") == reviewer
        ):
            return str(node.get("id") or "") or None
    return None


async def stale_changes_requested(
    repo: str, reviewer: str, *, label: str
) -> list[dict[str, Any]]:
    """PRs sitting on a standing changes-requested review from `reviewer`.

    Clocked on the review's age, not on last activity: these authors keep pushing,
    they just never re-request. The age cut is the caller's — the search cannot
    express it.
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
    data = await gh_json(
        "api", "graphql", "-f", f"q={search}", "-f", f"me={reviewer}", "-f", f"query={query}"
    )
    nodes = (((data or {}).get("data") or {}).get("search") or {}).get("nodes") or []
    return [n for n in nodes if n]
