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
