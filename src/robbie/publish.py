"""The only write surface robbie's reviews get.

One review or one comment, plus the needs-work label. No approve, no merge, no
close, no arbitrary API, no body from the caller's argv. The model never reaches
this module — it emits text, and this publishes it, so the comment and the label
are deterministic rather than something a model has to remember to do.

`needs-work` submits a real review, which clears the reviewer's pending request
and parks the PR out of their queue until the author re-requests. `comment` does
not: the author gets the findings and the label, and the PR stays in the queue.
That is why comment posts its inline notes one at a time instead of as a review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from robbie.anchor import Anchored, anchor, commentable
from robbie.config import RepoConfig
from robbie.github import GhError, PrMeta, _gh, _gh_json

logger = logging.getLogger(__name__)

MAX_BYTES = 60_000  # GitHub caps comment bodies at 65536
SIGNATURE = "🤖 **Automated pre-review by robbie**"


@dataclass(frozen=True)
class PublishResult:
    posted: bool
    detail: str
    inline: int = 0
    url: str | None = None


async def diff_lines(repo: str, pr: int) -> dict[str, set[int]]:
    """Map each changed file to the RIGHT-side lines a comment can attach to."""
    raw = await _gh(
        "api", f"repos/{repo}/pulls/{pr}/files", "--paginate",
        "--jq", ".[] | {filename, patch}",
    )
    out: dict[str, set[int]] = {}
    for row in raw.splitlines():
        if not row.strip():
            continue
        entry = json.loads(row)
        out[entry["filename"]] = commentable(entry.get("patch") or "")
    return out


async def publish_review(
    verdict: str,
    repo: RepoConfig,
    meta: PrMeta,
    *,
    body: str,
    findings: list[dict],
    dry_run: bool = False,
    self_login: str | None = None,
) -> PublishResult:
    """Post a needs-work review or a plain comment, then set the label."""
    if verdict not in {"needs-work", "comment"}:
        raise ValueError(f"publish_review does not handle verdict {verdict!r}")
    if not body.strip():
        return PublishResult(False, "summary body was empty; posting nothing")

    # GitHub rejects REQUEST_CHANGES on a PR the token's own user authored, so
    # the findings go out as a comment rather than being lost. The label still
    # goes on, which is what actually holds the next pass.
    own_pr = bool(self_login) and meta.author == self_login
    as_review = verdict == "needs-work" and not own_pr
    if own_pr and verdict == "needs-work":
        body = (
            f"{body.rstrip()}\n\n<sub>Posted as a comment rather than a "
            "changes-requested review: GitHub does not allow requesting changes on "
            "your own pull request.</sub>"
        )

    marker = f"<!-- robbie-review sha={meta.head_sha} -->"
    # a review and a comment live at different endpoints, so dedup where we post
    endpoint = (
        f"pulls/{meta.number}/reviews" if as_review
        else f"issues/{meta.number}/comments"
    )
    if await _already_posted(repo.slug, endpoint, marker):
        return PublishResult(False, f"already has a robbie {verdict} for {meta.head_sha[:8]}")

    anchored = anchor(findings, await diff_lines(repo.slug, meta.number))
    full = _assemble(marker, repo.needs_work_label, body, anchored)

    if dry_run:
        logger.info(
            "DRY %s on %s#%s (%d bytes, %d inline) + label %r\n%s",
            verdict, repo.slug, meta.number, len(full.encode()),
            len(anchored.comments), repo.needs_work_label, full,
        )
        return PublishResult(False, "dry run", inline=len(anchored.comments))

    if as_review:
        payload = json.dumps({
            "body": full,
            "event": "REQUEST_CHANGES",
            "commit_id": meta.head_sha,
            "comments": anchored.comments,
        })
        url = (await _gh(
            "api", f"repos/{repo.slug}/pulls/{meta.number}/reviews",
            "--input", "-", "--jq", ".html_url", stdin=payload,
        )).strip()
    else:
        url = (await _gh(
            "api", f"repos/{repo.slug}/issues/{meta.number}/comments",
            "--input", "-", "--jq", ".html_url",
            stdin=json.dumps({"body": full}),
        )).strip()
        for comment in anchored.comments:
            try:
                await _gh(
                    "api", f"repos/{repo.slug}/pulls/{meta.number}/comments",
                    "--input", "-", "--jq", ".html_url",
                    stdin=json.dumps({**comment, "commit_id": meta.head_sha}),
                )
            except GhError as ex:
                logger.warning(
                    "inline comment failed %s:%s — %s", comment["path"], comment["line"], ex
                )

    await _set_label(repo, meta.number, add=True)
    verb = "requested changes" if as_review else "commented"
    return PublishResult(
        True,
        f"{verb} + {len(anchored.comments)} inline + set {repo.needs_work_label!r}",
        inline=len(anchored.comments),
        url=url or None,
    )


async def clear_needs_work(repo: RepoConfig, pr: int, *, dry_run: bool = False) -> PublishResult:
    labels = await _gh_json(
        "pr", "view", str(pr), "--repo", repo.slug, "--json", "labels",
        "--jq", "[.labels[].name]",
    )
    if repo.needs_work_label not in (labels or []):
        return PublishResult(False, "no needs-work label to clear")
    if dry_run:
        return PublishResult(False, "dry run: would clear the needs-work label")
    await _set_label(repo, pr, add=False)
    return PublishResult(True, f"cleared {repo.needs_work_label!r}")


async def post_ci_note(
    repo: RepoConfig, meta: PrMeta, checks: tuple[str, ...], *, dry_run: bool = False
) -> PublishResult:
    """Not a review: no label, no review event, so the pending request stays."""
    # its own marker — sharing the review one would make the real review skip later
    marker = f"<!-- robbie-ci-red sha={meta.head_sha} -->"
    if await _already_posted(repo.slug, f"issues/{meta.number}/comments", marker):
        return PublishResult(False, f"already has the CI note for {meta.head_sha[:8]}")

    what = "CI is red on this commit"
    if checks:
        what += " (`" + "`, `".join(checks) + "`)"
    body = (
        f"{marker}\n{SIGNATURE}\n\n{what}, so I'm holding my review — too much of what "
        "I'd say tends to change once the build is green. Push a fix and I'll pick it up "
        "on my next pass; no need to re-request the review."
    )
    if dry_run:
        logger.info("DRY CI note on %s#%s:\n%s", repo.slug, meta.number, body)
        return PublishResult(False, "dry run")
    await _gh(
        "api", f"repos/{repo.slug}/issues/{meta.number}/comments",
        "--input", "-", "--jq", ".html_url", stdin=json.dumps({"body": body}),
    )
    return PublishResult(True, f"posted the CI note ({', '.join(checks) or 'red'})")


# ----- internals ---------------------------------------------------------


def _assemble(marker: str, label: str, body: str, anchored: Anchored) -> str:
    parts = [
        marker,
        f"{SIGNATURE}\n",
        # above the body on purpose: the footer is what truncation eats first
        f"_Once the fixes are in, take the `{label}` label off and I'll take another "
        "pass — no need to re-request the review._\n",
        body.rstrip(),
    ]
    if anchored.leftovers:
        parts.append("\n" + anchored.leftovers)
    return _truncate("\n".join(parts))


def _truncate(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= MAX_BYTES:
        return text
    # decode with errors='ignore' drops the multibyte char the cut split in half
    return raw[:MAX_BYTES].decode("utf-8", errors="ignore") + "\n\n_…truncated._\n"


async def _already_posted(repo: str, endpoint: str, marker: str) -> bool:
    bodies = await _gh(
        "api", f"repos/{repo}/{endpoint}", "--paginate", "--jq", ".[].body"
    )
    return marker in bodies


async def _set_label(repo: RepoConfig, pr: int, *, add: bool) -> None:
    flag = "--add-label" if add else "--remove-label"
    await _gh("pr", "edit", str(pr), "--repo", repo.slug, flag, repo.needs_work_label)
