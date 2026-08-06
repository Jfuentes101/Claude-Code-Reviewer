"""Turn findings into GitHub inline review comments.

GitHub rejects an entire review if any one comment points outside the diff, so a
finding that cannot be anchored is demoted into the summary body instead of
being dropped or taking the review down with it.

Pure: the caller supplies the diff. `publish.diff_lines` is what fetches it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SNAP = 3  # lines a finding may be nudged to reach the diff
MAX_BODY = 3000
SIGNATURE = "<sub>🤖 automated pre-review by robbie</sub>"
BADGE = {
    "critical": "🛑 **Critical**",
    "must-fix": "🔴 **Must-fix**",
    "should-fix": "🟠 **Should-fix**",
    "nitpick": "🔵 **Nitpick**",
}


@dataclass(frozen=True)
class Anchored:
    comments: list[dict] = field(default_factory=list)
    leftovers: str = ""


def commentable(patch: str) -> set[int]:
    """RIGHT-side line numbers inside the patch — added and context lines."""
    lines: set[int] = set()
    new = 0
    for row in patch.splitlines():
        m = HUNK.match(row)
        if m:
            new = int(m.group(1))
            continue
        if not new:
            continue
        if row.startswith(("+", " ")) or row == "":
            lines.add(new)
            new += 1
        elif row.startswith(("-", "\\")):
            continue
    return lines


def parse_findings(raw: str) -> list[dict]:
    """Parse the INLINE block. Anything unparseable is treated as no findings —
    a malformed array must not take down a review that is otherwise publishable."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    # per element too: one string in the array would reach render() as a finding
    return [f for f in parsed if isinstance(f, dict)]


def render(f: dict, snapped_from: int | None) -> str:
    badge = BADGE.get(str(f.get("severity", "")).strip().lower(), "🔵 **Note**")
    title = str(f.get("title") or "").strip()
    head = f"{badge} — {title}" if title else badge
    body = f"{head}\n\n{str(f.get('body', '')).strip()}"[:MAX_BODY]
    if snapped_from:
        body += f"\n\n<sub>reported for line {snapped_from}</sub>"
    return f"{body}\n\n{SIGNATURE}"


def anchor(findings: list[dict], valid: dict[str, set[int]]) -> Anchored:
    comments: list[dict] = []
    leftovers: list[str] = []
    for f in findings:
        path = f.get("path")
        lines = valid.get(path or "", set())
        try:
            line = int(f["line"])
        except (KeyError, TypeError, ValueError):
            leftovers.append(_leftover(path, None, f))
            continue

        target, snapped = line, None
        if line not in lines:
            near = sorted(lines, key=lambda n: (abs(n - line), n))
            if not near or abs(near[0] - line) > SNAP:
                leftovers.append(_leftover(path, line, f))
                continue
            target, snapped = near[0], line

        comment = {"path": path, "line": target, "side": "RIGHT", "body": render(f, snapped)}
        start = f.get("start_line")
        if isinstance(start, int) and start < target and start in lines:
            comment["start_line"], comment["start_side"] = start, "RIGHT"
        comments.append(comment)

    text = ""
    if leftovers:
        text = "**Not tied to a changed line:**\n\n" + "\n".join(leftovers)
    return Anchored(comments=comments, leftovers=text)


SEVERITY_LINE = re.compile(r":\d+")


def summary_findings(body: str) -> int:
    """How many findings a summary indexes, by the severity words it cites with a line.

    The inline count alone reads as zero for a re-review, which is told to keep
    findings out of the inline block when they are already posted on the diff — so
    without this, a model that found seven things scores nothing.

    A severity named without a `file:line` is not counted, which loses the odd
    finding about the PR as a whole. That is the same bar the policy sets: cite a
    changed line or you are guessing. It also keeps the closing "clear the three
    Should-fixes" line out of the count.
    """
    words = tuple(BADGE)
    return sum(
        1 for line in body.splitlines()
        if any(w in line.lower() for w in words) and SEVERITY_LINE.search(line)
    )


def severity_count(findings: list[dict], *, blocking: bool) -> int:
    """Blocking = critical/must-fix; otherwise should-fix. Drives the Slack copy."""
    wanted = ("critical", "must-fix", "mustfix") if blocking else ("should-fix", "shouldfix")
    return sum(
        1
        for f in findings
        if str(f.get("severity", "")).strip().lower().replace(" ", "-") in wanted
    )


def _leftover(path: str | None, line: int | None, f: dict) -> str:
    where = f"`{path}:{line}`" if line else f"`{path}`"
    return f"- {where} — {f.get('title', '(no title)')}\n\n  {f.get('body', '')}"
