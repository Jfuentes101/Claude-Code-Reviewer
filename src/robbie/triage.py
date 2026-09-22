"""Read a GitHub issue form, and decide whether a bot may touch the issue at all.

Pure: no network, no state, nothing spawned. An issue form renders as `### <label>`
followed by the answer, so splitting on that is the whole parser — which labels a
form has is the repo's business, never robbie's.

The verdict is one-sided on purpose. It never says "this is fixable"; it says
"nothing here forbids trying", and anything it cannot read forbids trying. A bot
that opens a bad pull request wastes a review; a bot that touches a refund does
not, so every unreadable answer lands on the side that costs less.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

# what GitHub writes into a field nobody answered
NO_RESPONSE = "_No response_"

_FENCES = ("```", "~~~")


def fields(body: str) -> dict[str, str]:
    """`{heading: answer}` for one issue body, in the order the form asked.

    First heading wins: a `### Steps to reproduce` typed *inside* an answer is
    somebody's text, not a second field, and it must not be able to overwrite the
    real one. Fenced blocks are skipped whole for the same reason — the logs field
    is rendered as a fence and a log line is allowed to look like anything.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for line in body.replace("\r\n", "\n").split("\n"):
        stripped = line.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
        elif stripped[:3] in _FENCES:
            fence = stripped[:3]
        elif line.startswith("### ") and line[4:].strip() not in out:
            current = line[4:].strip()
            out[current] = []
            continue
        if current is not None:
            out[current].append(line)
    return {head: _answer("\n".join(lines)) for head, lines in out.items()}


def _answer(text: str) -> str:
    text = text.strip()
    return "" if text == NO_RESPONSE else text


class Rules(NamedTuple):
    """Which answers put an issue out of a bot's reach. All three are exact-match.

    `require` is the fail-closed one: an answer outside the list is a no, and so is
    an answer the form did not have when the list was written. That is the polarity
    to use for the money question, where a dropdown option added next month must
    not quietly become fixable.
    """

    require: Mapping[str, tuple[str, ...]] = {}
    needs: tuple[str, ...] = ()
    block_if: Mapping[str, tuple[str, ...]] = {}

    def __bool__(self) -> bool:
        return bool(self.require or self.needs or self.block_if)


class Verdict(NamedTuple):
    attempt: bool
    reason: str


def verdict(answers: Mapping[str, str], rules: Rules) -> Verdict:
    """Whether a bot may attempt this issue, and the answer that decided it.

    No rules at all is a no. An unconfigured repo is one nobody has said this may
    run on, and reading that as consent would let an empty config file fix bugs.
    """
    if not rules:
        return Verdict(False, "no triage rules configured")
    for field, allowed in rules.require.items():
        if answers.get(field, "") not in allowed:
            return Verdict(False, f"{field}: {answers.get(field) or 'unanswered'}")
    for field in rules.needs:
        if not answers.get(field, ""):
            return Verdict(False, f"{field}: unanswered")
    for field, blocked in rules.block_if.items():
        if answers.get(field, "") in blocked:
            return Verdict(False, f"{field}: {answers[field]}")
    return Verdict(True, "no answer forbids an attempt")
