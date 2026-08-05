"""The review policy, as one pure function.

This module is the whole reason robbie is predictable: no I/O, no clock, no
subprocess. Everything it decides is a function of data the caller already
fetched, which is what makes the policy unit-testable instead of only
observable in production.

A PR is reviewed when all of these hold:
  1. it carries the queue label                    } the search query, so a PR
  2. the reviewer's review is actually requested   } missing either is invisible
  3. it does NOT carry the needs-work label
  4. it changes something, and something new since the last pass
  5. no comment of ours is still open there without a reply, fix or resolve
  6. CI on the head commit is not red (still running is fine)

Gate 3 is the brake on review storms: while our last pass stands unaddressed,
commits land without triggering anything. Taking the label off is how the author
says "ready for another pass" — every review that sets it says so — and an ok
verdict clears it.

`record=False` is the other load-bearing bit: those holds leave no row, so the
next tick re-evaluates them. That is how a reply, a resolve or a green build
brings a PR back on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from robbie.config import RepoConfig
from robbie.github import PrMeta, failing_checks

Action = Literal["review", "skip", "hold", "ci-note"]


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""
    # DM for the operator, sent at most once per dedup key. None = silent hold.
    dm: str | None = None
    # False: leave no trace, so the next tick looks again.
    record: bool = True
    checks: tuple[str, ...] = field(default_factory=tuple)


def label_hold(meta: PrMeta, repo: RepoConfig) -> Decision | None:
    """Gate 3 alone, so a caller can rule on it before paying for the rest.

    The normal state of a blocked PR: no DM, and it re-checks every tick.
    """
    if meta.has_label(repo.needs_work_label):
        return Decision("hold", f"{repo.needs_work_label} still on", record=False)
    return None


def already_judged(state: str | None) -> Decision | None:
    """Gate 4's first half: this exact (commit, request) has a verdict or a hold."""
    if state in ("published", "held"):
        return Decision("skip", "already judged at this commit and request")
    return None


def evaluate(
    meta: PrMeta,
    repo: RepoConfig,
    *,
    sha_judged: bool,
    open_threads: int,
) -> Decision:
    """Decide what to do with one PR. See the module docstring for the order.

    Gates 3 and 4 are also callable on their own above, because each of them makes
    an API call the caller would otherwise have made to get here.
    """
    where = f"{repo.slug}#{meta.number}"

    if (held := label_hold(meta, repo)) is not None:
        return held

    if meta.changed_files == 0:
        return Decision("hold", "changes no files")

    if sha_judged:
        if open_threads > 0:
            why = (
                f"{open_threads} of my comments there are still open with no reply — "
                "my read is they want to talk one of them through."
            )
        else:
            why = (
                "nothing of mine is left open there either, so I can't tell what "
                "changed for them."
            )
        return Decision(
            "hold",
            "nothing new pushed",
            dm=(
                f"*{meta.author}* asked for a review again on <{meta.url}|{where}> "
                f"— {meta.title[:70]} — but the head commit hasn't moved since my last "
                f"pass, so there's no new code for me to judge. {why} Over to you."
            ),
        )

    if open_threads > 0:
        return Decision(
            "hold",
            f"{open_threads} of my comments unanswered",
            dm=(
                f"*{meta.author}* pushed to <{meta.url}|{where}> — {meta.title[:70]} — "
                f"but {open_threads} of my review comments are still open there with no "
                "reply, fix or resolve. I'm holding my pass while the ball is in their "
                "court; it comes back on its own the moment they answer. "
                f"(`robbie once --repo {repo.slug} --pr {meta.number}` to force it.)"
            ),
            record=False,  # a reply or a resolve must bring this straight back
        )

    red = failing_checks(meta, ignore=repo.ignore_checks)
    if red:
        return Decision(
            "ci-note",
            f"ci red ({', '.join(red)})",
            record=False,  # a green build gets the real review next tick
            checks=tuple(red),
        )

    return Decision("review")


def dedup_key(repo: str, pr: int, head_sha: str, requested_at: str) -> str:
    """One review per (commit, request).

    A re-request with no new commits changes this too, but gate 4 turns that
    into a DM instead of a second review of the same diff.
    """
    return f"{repo}:{pr}:{head_sha}:{requested_at}"
