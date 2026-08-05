"""Block parsing. A model that formats its answer slightly off must degrade to
"no verdict, post nothing", never to a wrong verdict."""

from __future__ import annotations

from robbie.contract import parse_blocks, preamble

FULL = """\
Here is my review, blah blah.

<<<VERDICT>>>
needs-work
<<<END>>>
<<<GITHUB>>>
This PR adds widgets.

- Must-fix — a.rb:12 — nil guard
<<<END>>>
<<<INLINE>>>
[{"path": "a.rb", "line": 12, "severity": "Must-fix", "title": "t", "body": "b"}]
<<<END>>>
"""


def test_full_run_parses():
    b = parse_blocks(FULL)
    assert b.verdict == "needs-work"
    assert "adds widgets" in b.github
    assert b.inline.startswith("[")
    assert b.publishable


def test_verdict_survives_backticks_and_punctuation():
    for raw in ("`ok`", "ok.", " OK ", "**ok**"):
        assert parse_blocks(f"<<<VERDICT>>>\n{raw}\n<<<END>>>").verdict == "ok"


def test_an_unknown_verdict_is_none_rather_than_a_guess():
    assert parse_blocks("<<<VERDICT>>>\nlooks fine to me\n<<<END>>>").verdict is None


def test_missing_blocks_are_empty_not_an_error():
    b = parse_blocks("no markers here at all")
    assert b.verdict is None
    assert (b.github, b.inline) == ("", "")
    assert not b.publishable


def test_a_verdict_without_a_body_is_not_publishable():
    b = parse_blocks("<<<VERDICT>>>\ncomment\n<<<END>>>\n<<<GITHUB>>>\n\n<<<END>>>")
    assert b.verdict == "comment"
    assert not b.publishable


def test_ok_is_never_publishable_because_nothing_gets_posted():
    b = parse_blocks("<<<VERDICT>>>\nok\n<<<END>>>\n<<<GITHUB>>>\nsomething\n<<<END>>>")
    assert not b.publishable


def test_only_the_first_end_closes_a_block():
    b = parse_blocks(FULL)
    assert "<<<END>>>" not in b.github


def test_preamble_carries_the_pr_context_and_every_marker():
    text = preamble(author="dev")
    for marker in ("<<<VERDICT>>>", "<<<GITHUB>>>", "<<<INLINE>>>", "<<<END>>>"):
        assert marker in text
    assert "<<<SLACK>>>" not in text, "nothing reads a briefing; do not pay to write one"
    assert "dev" in text, "the summary is addressed to the author"
    assert "do NOT post anything to GitHub yourself" in text
