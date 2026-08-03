"""CI is handed to the model as data, never discovered by it.

Two properties matter: the model's CI line can't contradict the gate that let the
review happen, and a linter missing from the image is never reported as a gap.
"""

from __future__ import annotations

from robbie.contract import preamble
from robbie.github import PrMeta, summarize_checks


def pr(*checks) -> PrMeta:
    return PrMeta(
        number=7, title="t", url="https://x/7", author="dev", head_sha="abc",
        changed_files=1, labels=(), checks=tuple(checks),
    )


# ----- bucketing ---------------------------------------------------------


def test_a_green_status_and_a_green_check_run_both_pass():
    s = summarize_checks(pr(
        {"context": "ci/circleci: build", "state": "SUCCESS"},
        {"name": "rspec", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ))
    assert s.passing == ("ci/circleci: build", "rspec")
    assert not (s.failing or s.running or s.other)


def test_pending_and_in_progress_are_running_not_failures():
    s = summarize_checks(pr(
        {"context": "ci/circleci: build", "state": "PENDING"},
        {"name": "rspec", "status": "IN_PROGRESS"},
    ))
    assert s.running == ("ci/circleci: build", "rspec")
    assert s.failing == ()


def test_failures_are_named_in_both_spellings():
    s = summarize_checks(pr(
        {"context": "lint", "state": "FAILURE"},
        {"name": "rspec", "conclusion": "TIMED_OUT"},
    ))
    assert s.failing == ("lint", "rspec")


def test_neutral_and_skipped_land_in_other_with_their_state():
    s = summarize_checks(pr(
        {"name": "optional", "conclusion": "NEUTRAL"},
        {"name": "danger", "conclusion": "SKIPPED"},
    ))
    assert s.other == ("danger=SKIPPED", "optional=NEUTRAL")
    assert not (s.passing or s.failing)


def test_ignored_checks_are_still_shown_to_the_model():
    # gate 6 skips CodeRabbit; the model should still know it is unhappy
    s = summarize_checks(pr({"context": "CodeRabbit", "state": "FAILURE"}))
    assert s.failing == ("CodeRabbit",)


def test_duplicate_reports_of_the_same_check_collapse():
    s = summarize_checks(pr(
        {"context": "build", "state": "SUCCESS"}, {"context": "build", "state": "SUCCESS"},
    ))
    assert s.passing == ("build",)


def test_no_checks_at_all_is_said_plainly():
    assert summarize_checks(pr()).as_prompt() == "No CI checks are reporting on this commit."


# ----- what the model reads ---------------------------------------------


def test_running_checks_are_shouted_because_the_model_must_report_them():
    line = summarize_checks(pr({"name": "rspec", "status": "IN_PROGRESS"})).as_prompt()
    assert "STILL RUNNING: rspec" in line


def test_the_prompt_line_counts_passing_checks_instead_of_burying_them():
    line = summarize_checks(pr(
        {"context": "a", "state": "SUCCESS"}, {"context": "b", "state": "SUCCESS"},
    )).as_prompt()
    assert "passing (2): a, b" in line


def _flat(**kw) -> str:
    """Prompt text with wrapping collapsed, so these assert content not layout."""
    return " ".join(preamble(author="dev", title="t", url="u", **kw).split())


def test_the_ci_state_reaches_the_prompt():
    assert "passing (1): rspec" in _flat(ci="passing (1): rspec")


def test_the_model_is_told_not_to_go_looking_for_ci_itself():
    text = _flat(ci="passing (1): rspec")
    assert "Do not shell out to discover CI state" in text
    assert "you have no CI-provider credentials" in text


def test_a_green_run_says_the_gate_would_have_stopped_a_red_one():
    text = _flat(ci="passing (1): rspec")
    assert "would have stopped this review before it started" in text


def test_a_forced_run_over_red_ci_is_not_told_the_failure_must_be_harmless():
    # `robbie once` bypasses the gates, so the green-path claim would be a lie
    text = _flat(ci="passing (1): a · FAILING: ci/setup")
    assert "would have stopped this review before it started" not in text
    assert "do not assume the failure is harmless" in text
    assert "forced past" in text


def test_a_missing_linter_is_declared_expected_rather_than_a_gap():
    text = _flat(ci="passing (1): rspec")
    assert "that is expected and correct" in text
    assert 'Never report a tool as "unavailable"' in text


def test_without_ci_data_the_block_is_omitted_entirely():
    text = preamble(author="dev", title="t", url="u")
    assert "CI state on the head commit" not in text
    assert "<<<VERDICT>>>" in text, "the contract itself still has to be there"


def test_the_briefing_template_forbids_guessing_the_ci_line():
    text = preamble(author="dev", title="t", url="u", ci="x")
    assert "never from a guess" in text
