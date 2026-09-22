"""The subprocess boundary: every GitHub read goes through `gh`.

The module's contract is a value, a documented sentinel, or GhError — never a
guess and never a hang, because callers read GhError as "skip this round" and a
tick that never ends takes every following tick with it.
"""

from __future__ import annotations

import asyncio

import pytest

from robbie import github as gh_mod
from robbie.github import GhError, gh


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Put a `gh` on PATH that does whatever the test needs."""
    def write(script: str) -> None:
        exe = tmp_path / "gh"
        exe.write_text(f"#!/usr/bin/env bash\n{script}\n")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    return write


async def test_a_hung_call_gives_up_instead_of_blocking_the_tick(fake_gh, monkeypatch):
    """And gives up on the deadline, not when the children feel like exiting.

    Killing only the process leaves anything it spawned holding our pipes, and
    the call then waits for that instead — a timeout that reports late is not one.
    """
    fake_gh("sleep 30")
    monkeypatch.setattr(gh_mod, "TIMEOUT_S", 0.3)
    started = asyncio.get_running_loop().time()
    with pytest.raises(GhError, match="timed out"):
        await gh("api", "user")
    assert asyncio.get_running_loop().time() - started < 5


async def test_output_comes_back_whole(fake_gh):
    fake_gh('printf "hello"')
    assert await gh("api", "user") == "hello"


async def test_a_nonzero_exit_carries_the_stderr(fake_gh):
    fake_gh('echo "bad credentials" >&2; exit 1')
    with pytest.raises(GhError, match="bad credentials"):
        await gh("api", "user")


async def test_stdin_reaches_the_command(fake_gh):
    fake_gh("cat")
    assert await gh("api", "x", stdin='{"body":"hi"}') == '{"body":"hi"}'


async def test_a_queue_that_fills_its_page_says_so(fake_gh, caplog):
    """A full page and a truncated one look identical from here, and the PRs past
    it are invisible to every gate rather than merely late."""
    full = ",".join(f'{{"number":{n}}}' for n in range(gh_mod.QUEUE_LIMIT))
    fake_gh(f"echo '[{full}]'")
    with caplog.at_level("WARNING"):
        prs = await gh_mod.queue("acme/app", label="Code Review", reviewer="rev")
    assert len(prs) == gh_mod.QUEUE_LIMIT
    assert "anything past it is unseen" in caplog.text


async def test_a_queue_with_room_left_stays_quiet(fake_gh, caplog):
    fake_gh("""echo '[{"number":1}]'""")
    with caplog.at_level("WARNING"):
        assert await gh_mod.queue("acme/app", label="Code Review", reviewer="rev") == [1]
    assert caplog.text == ""


async def test_an_unreadable_check_run_falls_back_to_commit_statuses(fake_gh, caplog):
    """A fine-grained PAT cannot read check runs at all, and one unreadable node
    fails the whole `pr view` — labels and sha included. The PR must still be
    judged on what the token CAN see, not skipped as unreachable."""
    fake_gh("""
if [[ "$*" == *statusCheckRollup* ]]; then
  echo "GraphQL: Resource not accessible by personal access token" >&2; exit 1
fi
if [[ "$1" == "api" ]]; then
  echo '[{"context":"ci/circleci: build","state":"failure"}]'; exit 0
fi
echo '{"number":7,"title":"t","url":"u","author":{"login":"dev"},
       "headRefOid":"abc123","changedFiles":2,"labels":[{"name":"Code Review"}],
       "baseRefName":"main","state":"OPEN"}'
""")
    with caplog.at_level("WARNING"):
        meta = await gh_mod.pr_meta("acme/app", 7)
    assert meta.head_sha == "abc123"
    assert meta.has_label("Code Review"), "the labels must survive the fallback"
    assert gh_mod.failing_checks(meta, ignore=()) == ["ci/circleci: build"]
    assert "cannot read check runs" in caplog.text


async def test_any_other_failure_is_still_a_skip(fake_gh):
    """Only the forbidden-node case degrades; a real outage must stay an error."""
    fake_gh('echo "bad credentials" >&2; exit 1')
    with pytest.raises(GhError, match="bad credentials"):
        await gh_mod.pr_meta("acme/app", 7)


# ----- the issue queue -----------------------------------------------------


async def test_the_issue_queue_asks_for_every_label_at_once(fake_gh, tmp_path):
    """Repeated `--label` is an AND. One call with both, not two calls unioned:
    an issue that lost `needs-triage` has been claimed and is nobody's to fix."""
    argv = tmp_path / "argv"
    fake_gh(f'printf "%s\\n" "$@" > {argv}\necho \'[{{"number":7}},{{"number":9}}]\'')

    assert await gh_mod.issue_queue("acme/app", labels=("bug", "needs-triage")) == [7, 9]

    sent = argv.read_text().split("\n")
    assert sent[:4] == ["search", "issues", "--repo", "acme/app"]
    assert "--label=bug" in sent
    assert "--label=needs-triage" in sent
    assert "--state" in sent and "open" in sent


async def test_an_unlabelled_queue_is_refused_before_it_reaches_github(fake_gh, tmp_path):
    """No label is every open issue in the repo. That is not a queue, and it must
    fail as a mistake rather than come back as work."""
    argv = tmp_path / "argv"
    fake_gh(f'printf "ran" > {argv}\necho "[]"')

    with pytest.raises(GhError, match="at least one label"):
        await gh_mod.issue_queue("acme/app", labels=())

    assert not argv.exists(), "it asked GitHub anyway"


async def test_an_issue_comes_back_in_the_shape_triage_reads(fake_gh):
    fake_gh(
        'echo \'{"number":12,"title":"[Bug]: spinner","url":"https://x/12",'
        '"author":{"login":"cs-person"},"body":"### Environment\\n\\nQA",'
        '"labels":[{"name":"bug"},{"name":"needs-triage"}],"state":"OPEN"}\''
    )

    issue = await gh_mod.issue_meta("acme/app", 12)

    assert (issue.number, issue.author, issue.state) == (12, "cs-person", "OPEN")
    assert issue.labels == ("bug", "needs-triage")
    assert issue.body == "### Environment\n\nQA", "the body is the form; it stays raw"


async def test_an_issue_that_does_not_come_back_is_an_error_not_an_empty_one(fake_gh):
    """An empty read is a hiccup, and a blank body would triage as a form with no
    money answer — which is a decision, taken on no information."""
    fake_gh('echo "null"')

    with pytest.raises(GhError, match="could not fetch"):
        await gh_mod.issue_meta("acme/app", 12)
