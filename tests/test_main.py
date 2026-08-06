"""Process lifecycle: the daemon loop, which no other test drives."""

from __future__ import annotations

import asyncio
from pathlib import Path

from robbie.config import Config, RepoConfig, SlackConfig
from robbie.main import _loop


def _cfg(tmp_path, **kw) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"),
        repos=[RepoConfig(slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"))],
        state_dir=tmp_path,
        **kw,
    )


class _Orch:
    """Ticks once, then asks the loop it is running inside to stop."""

    def __init__(self) -> None:
        self.ticks = 0
        self.stop: asyncio.Event | None = None
        self.raises: Exception | None = None

    async def poll_once(self) -> list:
        self.ticks += 1
        assert self.stop is not None
        self.stop.set()
        if self.raises is not None:
            raise self.raises
        return []


async def _one_tick(cfg, orch, monkeypatch) -> int:
    """Let _loop build its own Event, keep a handle on it, run exactly one tick."""
    real = asyncio.Event

    def capture() -> asyncio.Event:
        orch.stop = real()
        return orch.stop

    monkeypatch.setattr(asyncio, "Event", capture)
    return await _loop(cfg, orch)


async def test_a_tick_slower_than_the_interval_says_so(tmp_path, monkeypatch, caplog):
    """Ticks cannot overlap, so outgrowing the interval is silent drift. This
    line is the only thing that ever mentions it."""
    with caplog.at_level("WARNING"):
        await _one_tick(_cfg(tmp_path, poll_interval_s=0), _Orch(), monkeypatch)
    assert "longer than the 0s interval" in caplog.text


async def test_a_tick_inside_the_interval_stays_quiet(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        await _one_tick(_cfg(tmp_path, poll_interval_s=600), _Orch(), monkeypatch)
    assert caplog.text == ""


async def test_a_failed_tick_does_not_end_the_daemon(tmp_path, monkeypatch):
    orch = _Orch()
    orch.raises = RuntimeError("github fell over")
    assert await _one_tick(_cfg(tmp_path), orch, monkeypatch) == 0
    assert orch.ticks == 1, "it swallowed the tick, not the loop"
