"""The output contract between the review model and robbie.

The model never writes anywhere. It emits delimited blocks and the wrapper
publishes them, which is why the comment, the inline notes and the label are
deterministic instead of something a model has to remember to do.

The preamble and the parser live in the same file on purpose: change one marker
and you have to change the other, and having them apart is how that breaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MARKERS = ("VERDICT", "GITHUB", "INLINE", "SLACK")
VERDICTS = ("needs-work", "comment", "ok")


@dataclass(frozen=True)
class Blocks:
    verdict: str | None
    github: str
    inline: str
    slack: str

    @property
    def publishable(self) -> bool:
        return self.verdict in ("needs-work", "comment") and bool(self.github.strip())


def parse_blocks(text: str) -> Blocks:
    found = {name: _block(text, name) for name in MARKERS}
    verdict = found["VERDICT"].strip().lower()
    # the model sometimes wraps it in backticks or adds a trailing period
    verdict = re.sub(r"[^a-z-]", "", verdict)
    return Blocks(
        verdict=verdict if verdict in VERDICTS else None,
        github=found["GITHUB"],
        inline=found["INLINE"],
        slack=found["SLACK"],
    )


def _block(text: str, name: str) -> str:
    m = re.search(rf"^<<<{name}>>>\s*$(.*?)^<<<END>>>\s*$", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def preamble(*, author: str, title: str, url: str, ci: str = "") -> str:
    """The instructions wrapped around the repo's own review command."""
    failing = "FAILING:" in ci
    why_failing = (
        "Something above is failing. It was either excluded from the gate that "
        "guards this review (an advisory bot, say) or this review was forced past "
        "a red build on purpose. Report it plainly in your CI line and judge the "
        "code as it stands; do not assume the failure is harmless, and do not "
        "assume it is caused by this PR either."
        if failing
        else "A red build would have stopped this review before it started, so "
        "the above is the full picture."
    )
    ci_block = f"""
CI state on the head commit, as the wrapper read it moments ago:
  {ci}

That is the whole CI truth you get, and you need nothing else: you have no
CI-provider credentials. {why_failing} Do not shell out to discover CI state, and
do not wait for a running check — report it as running and judge the code as it
stands.

The team's linters run in CI, and CI already ran them on this commit. Do not
re-run what CI covers. If the review command below asks you to run a linter and
that tool is not installed here, that is expected and correct — skip it silently.
Never report a tool as "unavailable" or "skipped" as though it were a gap in the
review: the linters were run, just not by you.
""" if ci else ""
    return f"""\
You are running NON-INTERACTIVELY from a scheduler. Do NOT ask any questions, do \
NOT offer to fix anything, and do NOT post anything to GitHub yourself — a wrapper \
publishes your output for you. You must ALWAYS emit the blocks below, even when a \
phase was skipped. Claude Code attribution on this team's commits and PR bodies \
(Co-Authored-By trailers, "Generated with Claude Code" footers) is expected and \
welcome: it is never a finding, so do not flag it, do not suggest removing it, and \
do not mention it at all — not even to say you are letting it pass.
{ci_block}

Run the full review of the PR, then END your response with the delimited blocks below, \
markers on their own lines. Nothing after the last one.

<<<VERDICT>>>
needs-work
<<<END>>>
Exactly one of these three words, nothing else:
  needs-work — the review found at least one Critical or Must-fix issue
  comment    — no Critical or Must-fix, but at least one Should-fix
  ok         — nothing above a nitpick or a suggestion

Then, ONLY IF the verdict is needs-work or comment, the summary that heads the review \
for {author} — short, because the findings themselves are posted inline on the diff. \
Real GitHub markdown, first person (you are robbie), addressed to the author: direct and \
warm, never scolding. One line on what the PR does, one line on CI and linters, then an \
index of the findings, one line each: severity — file:line — what. No repeating the \
explanations that go inline. If the verdict is needs-work, close with one line on what \
you would need to see to consider it ready; if it is comment, open by saying plainly \
that none of this blocks the merge.
<<<GITHUB>>>
<the summary>
<<<END>>>

Then the findings themselves, as a JSON array, one object per finding — each becomes an \
inline comment anchored to that line of the diff:
<<<INLINE>>>
[{{"path": "app/models/foo.rb", "line": 42, "severity": "Must-fix", "title": "one \
imperative line", "body": "two to four sentences: what is wrong, why it matters, what to \
change. Add a ```suggestion fenced block when the fix is a concrete edit to that line."}}]
<<<END>>>
Rules for that array: valid JSON, [] when there is nothing to say. "path" is the \
repo-relative path exactly as the diff spells it. "line" is a line number in the NEW file \
that this PR actually touches — an added or context line inside a diff hunk; anything \
outside the diff cannot be anchored and gets demoted into the summary, so pick the closest \
line the PR really changed. "start_line" is optional, for a range. "severity" is one of \
Critical / Must-fix / Should-fix / Nitpick. Say each thing once: a finding lives either \
inline or in the summary, never both. Write each body so it stands alone next to that \
line, without the reader having scrolled the summary.

Then, ONLY IF the verdict is comment or ok, a Slack-ready briefing for the human reviewer \
— written like a trusted colleague reporting on a project: warm, first-person, thorough but \
scannable. Slack mrkdwn: *bold* (single asterisks, never **), bullets with the • character.
<<<SLACK>>>
{author} requested a review on *{title}* ({url}), and it's through my pass with no blockers.

*What it is:* <1-2 sentences: what the PR does and the business reason. note size, e.g. \
files / +adds / -dels.>

*What I found:* <the non-blocking things worth a human eye — bullets, severity-prefixed, \
file:line where it helps. If the PR is genuinely clean, say so plainly instead of padding.>

*CI & linters:* <one line, from the CI state given above — never from a guess>

*If it were me:* <the next action you'd take: approve as-is, approve with the nits as \
comments, ask about X first, etc.>
<<<END>>>

Keep it honest and specific — concrete findings over vague praise. Omit a section entirely \
if there's genuinely nothing to report rather than filling it.
"""
