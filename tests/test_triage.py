"""The brake on the issue queue. If these pass, a bot cannot talk itself into a
refund bug, and no text a reporter types can rewrite the answers around it."""

from __future__ import annotations

import pytest

from robbie.triage import Rules, fields, verdict

BODY = """\
### Account

acme-widgets

### Page URL

https://example.test/checkout

### Environment

Production

### Affected account

_No response_

### What happens

The page spins forever.
Reloading does not help.

### Steps to reproduce

1. Open the checkout
2. Press Pay

### How often?

Always

### Is anyone blocked?

Yes

### Who is affected?

One user

### Does it touch payments?

No

### Logs

```shell
E, [2026-09-22] ERROR -- : timeout
### Steps to reproduce
```

### Support notes

They tried another browser first.
"""


@pytest.fixture
def answers() -> dict[str, str]:
    return fields(BODY)


RULES = Rules(
    require={"Does it touch payments?": ("No",)},
    needs=("Steps to reproduce",),
    block_if={
        "How often?": ("Cannot reproduce",),
        "Who is affected?": ("Everyone",),
    },
)


# ----- parsing -------------------------------------------------------------


def test_every_heading_becomes_a_field(answers):
    assert answers["Account"] == "acme-widgets"
    assert answers["Environment"] == "Production"
    assert answers["Does it touch payments?"] == "No"


def test_a_multi_line_answer_keeps_its_lines(answers):
    assert answers["What happens"] == "The page spins forever.\nReloading does not help."


def test_an_unanswered_field_reads_as_empty_not_as_its_placeholder(answers):
    """`_No response_` is GitHub's text, not the reporter's. A rule asking whether
    a field was answered must not see it as an answer."""
    assert answers["Affected account"] == ""


def test_a_heading_inside_a_fence_is_log_text_not_a_field(answers):
    """The logs field renders as a fence and a log line may look like anything —
    including like the form. Splitting on it would truncate the real answer."""
    assert answers["Steps to reproduce"] == "1. Open the checkout\n2. Press Pay"
    assert "### Steps to reproduce" in answers["Logs"]


def test_a_second_copy_of_a_heading_cannot_overwrite_the_first():
    """Typed into an answer, it is somebody's text. First one wins, and the text
    stays in the field it was typed into."""
    parsed = fields(
        "### Does it touch payments?\n\nNo\n\n"
        "### Support notes\n\n### Does it touch payments?\n\nRefund\n"
    )
    assert parsed["Does it touch payments?"] == "No"
    assert "Refund" in parsed["Support notes"]


def test_a_body_from_the_web_form_parses_with_crlf():
    assert fields("### Environment\r\n\r\nQA\r\n") == {"Environment": "QA"}


def test_anything_before_the_first_heading_is_not_a_field():
    assert fields("free text nobody asked for\n\n### Environment\n\nQA\n") == {
        "Environment": "QA"
    }


# ----- the verdict ---------------------------------------------------------


def test_a_clean_report_is_left_open_for_an_attempt(answers):
    assert verdict(answers, RULES).attempt


def test_an_unconfigured_repo_is_not_consent():
    """Empty rules are a repo nobody enabled this on, not a repo with nothing to
    forbid — the one default that cannot be wrong in the expensive direction."""
    assert verdict(fields(BODY), Rules()) == (False, "no triage rules configured")


@pytest.mark.parametrize(
    "money", ["Refund", "Payment or checkout", "Dispute or chargeback", "Not sure"]
)
def test_money_is_never_a_bots_to_attempt(answers, money):
    answers["Does it touch payments?"] = money
    ok, reason = verdict(answers, RULES)
    assert not ok
    assert money in reason


def test_a_money_option_added_after_the_rules_were_written_still_blocks(answers):
    """`require` lists what is allowed, so a new dropdown option is a no until
    somebody says otherwise. Listing what is forbidden would have opened it."""
    answers["Does it touch payments?"] = "Chargeback fees"
    assert not verdict(answers, RULES).attempt


def test_a_missing_money_field_blocks_too(answers):
    """A form that dropped the question is not a form that answered `No`."""
    del answers["Does it touch payments?"]
    assert verdict(answers, RULES) == (False, "Does it touch payments?: unanswered")


def test_a_report_with_no_steps_goes_to_a_human(answers):
    """Nothing to reproduce means nothing to write a failing test against, which
    is the whole of what a bot has to offer here."""
    answers["Steps to reproduce"] = ""
    assert verdict(answers, RULES) == (False, "Steps to reproduce: unanswered")


def test_a_bug_nobody_can_reproduce_goes_to_a_human(answers):
    answers["How often?"] = "Cannot reproduce"
    assert not verdict(answers, RULES).attempt


def test_a_bug_hitting_everyone_goes_to_a_human(answers):
    answers["Who is affected?"] = "Everyone"
    assert not verdict(answers, RULES).attempt
