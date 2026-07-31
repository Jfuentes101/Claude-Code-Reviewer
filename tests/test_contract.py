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
    assert b.slack == ""
    assert b.publishable


def test_verdict_survives_backticks_and_punctuation():
    for raw in ("`ok`", "ok.", " OK ", "**ok**"):
        assert parse_blocks(f"<<<VERDICT>>>\n{raw}\n<<<END>>>").verdict == "ok"


def test_an_unknown_verdict_is_none_rather_than_a_guess():
    assert parse_blocks("<<<VERDICT>>>\nlooks fine to me\n<<<END>>>").verdict is None


def test_missing_blocks_are_empty_not_an_error():
    b = parse_blocks("no markers here at all")
    assert b.verdict is None
    assert (b.github, b.inline, b.slack) == ("", "", "")
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
    text = preamble(author="dev", title="Add widgets", url="https://x/7")
    for marker in ("<<<VERDICT>>>", "<<<GITHUB>>>", "<<<INLINE>>>", "<<<SLACK>>>", "<<<END>>>"):
        assert marker in text
    assert "dev" in text and "Add widgets" in text and "https://x/7" in text
    assert "do NOT post anything to GitHub yourself" in text
