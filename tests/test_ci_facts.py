"""CI is handed to the model as data, never discovered by it.

Two properties matter: the model's CI line can't contradict the gate that let the
review happen, and a linter missing from the image is never reported as a gap.
"""

from __future__ import annotations

from robbie.contract import preamble
from robbie.github import CheckSummary, PrMeta, ci_outcome, ci_started, summarize_checks


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


GREEN = CheckSummary(passing=("rspec",))


def _flat(**kw) -> str:
    """Prompt text with wrapping collapsed, so these assert content not layout."""
    return " ".join(preamble(author="dev", **kw).split())


def test_the_ci_state_reaches_the_prompt():
    assert "passing (1): rspec" in _flat(ci=GREEN)


def test_the_model_is_told_not_to_go_looking_for_ci_itself():
    text = _flat(ci=GREEN)
    assert "Do not shell out to discover CI state" in text
    assert "you have no CI-provider credentials" in text


def test_a_green_run_says_the_gate_would_have_stopped_a_red_one():
    text = _flat(ci=GREEN)
    assert "would have stopped this review before it started" in text


def test_a_forced_run_over_red_ci_is_not_told_the_failure_must_be_harmless():
    # `robbie once` bypasses the gates, so the green-path claim would be a lie
    text = _flat(ci=CheckSummary(passing=("a",), failing=("ci/setup",)))
    assert "would have stopped this review before it started" not in text
    assert "do not assume the failure is harmless" in text
    assert "forced past" in text


def test_a_missing_linter_is_declared_expected_rather_than_a_gap():
    text = _flat(ci=GREEN)
    assert "that is expected and correct" in text
    assert 'Never report a tool as "unavailable"' in text


def test_an_unbuilt_commit_is_not_told_its_linters_passed():
    """CI runs on approval now, so silence means nothing ran — not that it is green."""
    text = _flat(ci=summarize_checks(pr()))
    assert "CI already ran them on this commit" not in text
    assert "nobody has linted this commit" in text
    assert "approving this commit is what starts one" in text
    assert "would have stopped this review before it started" not in text


def test_which_branch_the_prompt_takes_is_read_off_the_summary_not_its_wording():
    """The two used to be one substring apart: `"FAILING:" in ci`, formatted in
    another module. A summary that only *mentions* a failure is not a red build."""
    text = _flat(ci=CheckSummary(other=("FAILING: not really=?",)))
    assert "do not assume the failure is harmless" not in text


def test_without_ci_data_the_block_is_omitted_entirely():
    text = preamble(author="dev")
    assert "CI state on the head commit" not in text
    assert "<<<VERDICT>>>" in text, "the contract itself still has to be there"


def test_the_summary_is_forbidden_from_guessing_the_ci_line():
    text = preamble(author="dev", ci=GREEN)
    assert "never from a guess" in text


# ----- what the build said about a commit robbie approved -------------------


def _outcome(checks, ignore=("CodeRabbit",)):
    return ci_outcome(pr(*checks), ignore=ignore)


def test_all_green_is_green():
    assert _outcome(({"context": "ci/build", "state": "SUCCESS"},)) == "green"


def test_anything_still_running_is_not_green_yet():
    assert _outcome((
        {"context": "ci/build", "state": "SUCCESS"},
        {"name": "rspec", "status": "IN_PROGRESS"},
    )) == "waiting"


def test_nothing_reporting_is_waiting_not_green():
    """The build the approval asked for may not have started; that is not a pass."""
    assert _outcome(()) == "waiting"


def test_a_failure_is_red():
    assert _outcome((
        {"context": "ci/build", "state": "SUCCESS"},
        {"name": "rspec", "conclusion": "FAILURE"},
    )) == "red"


def test_an_ignored_check_cannot_make_it_red():
    """CodeRabbit reports as a status and is never a broken build — gate 6's rule."""
    assert _outcome((
        {"context": "ci/build", "state": "SUCCESS"},
        {"context": "CodeRabbit", "state": "FAILURE"},
    )) == "green"


def test_an_ignored_check_cannot_make_it_green_either():
    """It ticks every push, so on its own it is a commit with no build at all."""
    assert _outcome(({"context": "CodeRabbit", "state": "SUCCESS"},)) == "waiting"


def test_an_ignored_check_running_does_not_hold_a_green_build_back():
    assert _outcome((
        {"context": "ci/build", "state": "SUCCESS"},
        {"context": "CodeRabbit", "status": "IN_PROGRESS"},
    )) == "green"


# ----- whether a build exists at all ----------------------------------------


def _started(checks, ignore=("CodeRabbit",)):
    return ci_started(pr(*checks), ignore=ignore)


def test_a_commit_with_no_checks_has_no_build():
    assert not _started(())


def test_only_the_review_bot_is_still_no_build():
    assert not _started(({"context": "CodeRabbit", "state": "SUCCESS"},))


def test_a_build_counts_whatever_it_is_reporting():
    """Queued, running, passed or failed: all of them mean nobody owes it a trigger."""
    for state in ({"state": "PENDING"}, {"status": "QUEUED"}, {"conclusion": "FAILURE"}):
        assert _started(({"context": "ci/build", **state},))
