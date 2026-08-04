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

from robbie.github import NO_CHECKS

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


SIG_LINE = "<sub>🤖 automated pre-review by robbie</sub>"
THREAD_ACTIONS = ("resolve", "reply", "leave")


@dataclass(frozen=True)
class ThreadVerdict:
    comment_id: int
    action: str  # resolve | reply | leave
    body: str = ""

    @property
    def valid(self) -> bool:
        if self.action not in THREAD_ACTIONS:
            return False
        return bool(self.body.strip()) if self.action == "reply" else True


def parse_thread_verdicts(text: str) -> list[ThreadVerdict]:
    """Read the per-thread decisions out of a run.

    Anything malformed is dropped rather than guessed: leaving a thread alone is
    always safe, and acting on a misparsed id would touch the wrong conversation.
    """
    out: list[ThreadVerdict] = []
    for m in re.finditer(
        r"^<<<THREAD (\d+)>>>\s*$(.*?)^<<<END>>>\s*$", text, re.S | re.M
    ):
        lines = [line for line in m.group(2).strip().splitlines()]
        if not lines:
            continue
        action = lines[0].strip().lower()
        verdict = ThreadVerdict(
            comment_id=int(m.group(1)),
            action=action,
            body="\n".join(lines[1:]).strip(),
        )
        if verdict.valid:
            out.append(verdict)
    return out


def _linters(nothing_ran: bool) -> str:
    """The linters are CI's job either way — what changes is whether they ran."""
    if nothing_ran:
        return (
            "The team's linters run in CI, which has not run yet, so nobody has linted "
            "this commit: do not say the linters pass, and do not imply they do. You "
            "cannot run them either — they are deliberately not installed here — so "
            "leave lint to CI and judge the code itself."
        )
    return (
        "The team's linters run in CI, and CI already ran them on this commit. Do not "
        "re-run what CI covers. If the review command below asks you to run a linter "
        "and that tool is not installed here, that is expected and correct — skip it "
        'silently. Never report a tool as "unavailable" or "skipped" as though it were '
        "a gap in the review: the linters were run, just not by you."
    )


def thread_preamble(*, author: str, url: str, threads: list) -> str:
    """Ask for a decision on each thread somebody answered."""
    blocks = []
    for t in threads:
        where = _where(t)
        if t.outdated:
            where += " (OUTDATED — the code moved; a reply here stays collapsed)"
        rows = [f"THREAD {t.comment_id} — {where}", f"  you said: {_strip(t.mine, 1200)}"]
        rows += [f"  {who} replied: {_strip(body, 1200)}" for who, body in t.replies]
        blocks.append("\n".join(rows))
    listing = "\n\n".join(blocks)

    return f"""\
You are running NON-INTERACTIVELY from a scheduler, continuing a code review you \
already posted on {url}. Do NOT ask questions and do NOT post anything yourself — a \
wrapper acts on your output.

{author} (or someone else) has replied to review comments of yours. For each thread \
below, work out whether the reply is RIGHT. Read the actual code to check — you have \
the PR checked out, so verify rather than assume, and remember that whoever replied \
knows this codebase and this product better than you do.

{listing}

Then, for EACH thread above, emit exactly one block. Markers on their own lines, \
nothing else in your response after the first one.

<<<THREAD {threads[0].comment_id if threads else 0}>>>
resolve
<<<END>>>

The first line is one of these three words:

  resolve — the reply is right, or right enough. Your finding does not stand: the \
code already handles it, the concern does not apply here, the risk is accepted for a \
reason that holds, or you simply misread it. Say nothing; the thread gets closed. \
Prefer this. A reviewer who cannot concede a point is noise, and a thread closed by \
agreement is the loop working.

  reply — the reply does not settle it and the difference matters, OR they asked you \
a question you should answer. Put the reply body on the lines after the word. Address \
what they actually said, in one short paragraph: name the specific case their \
reasoning misses, with a file:line if you have one. No preamble, no restating their \
point back at them, no thanks. If you were wrong about part of it, say that part \
plainly before the part you still hold. Never repeat the original finding as though \
it were unanswered. Not available on a thread marked OUTDATED — nobody would read it. \
There, judge the code as it stands now and choose resolve or leave.

  leave — you cannot tell from the code, or the reply is about something outside this \
PR. The thread stays as it is and a human picks it up.

Choosing `reply` puts the ball back in their court and holds the next review pass, so \
spend it only where it changes what they would do. If it is a matter of taste, resolve.
"""
BODY_CAP = 400


def _strip(body: str, cap: int = BODY_CAP) -> str:
    """Flatten anything that came from GitHub to a single capped line.

    Everything embedded in a prompt goes through here, and the flattening is the
    load-bearing half: a block marker is only a marker on a line of its own, so
    text that can never carry a newline can never forge one. That covers reply
    bodies — written by whoever can comment — and paths, since git allows a
    newline in a filename and a PR chooses its own filenames.
    """
    lines = [
        line for line in body.splitlines()
        if line.strip() and SIG_LINE not in line and not line.startswith("<sub>")
    ]
    text = " ".join(lines)
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0] + "…"


def _where(t) -> str:
    path = _strip(t.path, 200)
    return f"{path}:{t.line}" if t.line else path


def threads_block(threads: list) -> str:
    """What the reviewer already said on this PR, and what came back.

    Read from GitHub each time rather than stored: it owns the threads, their
    replies and their resolution state, so a local copy would only go stale.
    """
    live = [t for t in threads if not t.outdated]
    if not live:
        return ""
    rows: list[str] = []
    for t in live:
        where = _where(t)
        state = "RESOLVED" if t.resolved else ("ANSWERED" if t.replies else "no reply yet")
        rows.append(f"- [{state}] {where}\n    you said: {_strip(t.mine)}")
        for who, body in t.replies:
            rows.append(f"    {who} replied: {_strip(body)}")
    return "\n".join(rows)


def preamble(
    *, author: str, title: str, url: str, ci: str = "",
    threads: str = "", history: str = "",
) -> str:
    """The instructions wrapped around the repo's own review command."""
    failing = "FAILING:" in ci
    nothing_ran = ci.startswith(NO_CHECKS)
    if failing:
        why_failing = (
            "Something above is failing. It was either excluded from the gate that "
            "guards this review (an advisory bot, say) or this review was forced past "
            "a red build on purpose. Report it plainly in your CI line and judge the "
            "code as it stands; do not assume the failure is harmless, and do not "
            "assume it is caused by this PR either."
        )
    elif nothing_ran:
        why_failing = (
            "That is not a broken integration: builds cost money here, so CI no longer "
            "runs on a push — approving this commit is what starts one. Nothing above "
            "vouches for the code, and no test has been run on it."
        )
    else:
        why_failing = (
            "A red build would have stopped this review before it started, so "
            "the above is the full picture."
        )
    ci_block = f"""
CI state on the head commit, as the wrapper read it moments ago:
  {ci}

That is the whole CI truth you get, and you need nothing else: you have no
CI-provider credentials. {why_failing} Do not shell out to discover CI state, and
do not wait for a running check — report it as running and judge the code as it
stands.

{_linters(nothing_ran)}
""" if ci else ""
    prior = f"""
{history}You have reviewed this PR before. These are your own threads on it and
what came back:

{threads}

Read them as a conversation you are continuing, not as history to ignore:

- A reply that gives a REASON settles the point. Do not raise it again as a new
  finding. If the reason does not hold, say so ONCE in the summary and address
  the reply directly — never by posting a second inline comment on the same line.
- A RESOLVED thread is closed. Reopen it only if the code changed in a way that
  brings the problem back, and say that is why.
- A thread with no reply yet is ALREADY POSTED on this PR. Do not repeat it
  inline; GitHub would show the author two identical comments. Mention it in the
  summary as still open if it still matters.
- Where an earlier finding is genuinely fixed, say so in one line. Credit is
  cheap and it tells the author the loop is working.
""" if threads else (f"\n{history}" if history else "")
    return f"""\
You are running NON-INTERACTIVELY from a scheduler. Do NOT ask any questions, do \
NOT offer to fix anything, and do NOT post anything to GitHub yourself — a wrapper \
publishes your output for you. You must ALWAYS emit the blocks below, even when a \
phase was skipped. Claude Code attribution on this team's commits and PR bodies \
(Co-Authored-By trailers, "Generated with Claude Code" footers) is expected and \
welcome: it is never a finding, so do not flag it, do not suggest removing it, and \
do not mention it at all — not even to say you are letting it pass.
{ci_block}{prior}

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
