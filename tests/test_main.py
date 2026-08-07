"""Process lifecycle: the daemon loop, which no other test drives."""

from __future__ import annotations

import asyncio
from pathlib import Path

from robbie.config import Config, RepoConfig, ReviewModel, Secrets, SlackConfig
from robbie.main import _loop, why_no_model
from robbie.outcome import Outcome


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


async def _one_tick(cfg, orch, monkeypatch, *, quiet: bool = False) -> int:
    """Let _loop build its own Event, keep a handle on it, run exactly one tick."""
    real = asyncio.Event

    def capture() -> asyncio.Event:
        orch.stop = real()
        return orch.stop

    monkeypatch.setattr(asyncio, "Event", capture)
    return await _loop(cfg, orch, quiet=quiet)


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


# ----- what `once --model` needs in the env ---------------------------------


def _secrets(**kw) -> Secrets:
    base = dict(gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="k")
    return Secrets(**{**base, **kw})


ARM = ReviewModel(model="glm-5.2:cloud", via="endpoint")


def test_no_model_named_needs_nothing(tmp_path):
    assert why_no_model(_cfg(tmp_path), _secrets(), None) is None


def test_a_model_on_the_accounts_own_backend_needs_no_endpoint_token(tmp_path):
    """`--model sonnet` is a request about the account's model, not about a third
    party, so demanding a third party's token to run it refuses it for nothing."""
    cfg = _cfg(tmp_path, review_models=[ARM])
    assert why_no_model(cfg, _secrets(), "sonnet") is None


def test_an_endpoint_arm_without_its_auth_is_refused_at_boot(tmp_path):
    cfg = _cfg(tmp_path, review_models=[ARM])
    why = why_no_model(cfg, _secrets(), "glm-5.2:cloud")
    assert why is not None and "REVIEW_API_TOKEN" in why


def test_an_endpoint_arm_with_both_halves_runs(tmp_path):
    cfg = _cfg(tmp_path, review_models=[ARM])
    secrets = _secrets(review_base_url="https://x", review_api_token="t")
    assert why_no_model(cfg, secrets, "glm-5.2:cloud") is None


def test_a_base_url_without_a_token_is_still_not_enough(tmp_path):
    cfg = _cfg(tmp_path, review_models=[ARM])
    assert why_no_model(cfg, _secrets(review_base_url="https://x"), "glm-5.2:cloud") is not None


# ----- draining, rather than idling on top of a queue of containers ---------


class _Ticker:
    """Answers each tick from a script, then asks the loop to stop."""

    def __init__(self, script: list[list[Outcome]]) -> None:
        self.script = list(script)
        self.ticks = 0
        self.stop: asyncio.Event | None = None

    async def poll_once(self) -> list[Outcome]:
        self.ticks += 1
        assert self.stop is not None
        out = self.script.pop(0) if self.script else []
        if not self.script:
            self.stop.set()
        return out


def _count_waits(monkeypatch) -> list[float]:
    waited: list[float] = []
    real = asyncio.wait_for

    async def counted(aw, timeout):
        waited.append(timeout)
        return await real(aw, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", counted)
    return waited


def _reviewed() -> Outcome:
    return Outcome("acme/app", 7, "review", "ok — asked CI to run")


def _failed() -> Outcome:
    return Outcome("acme/app", 7, "failed", "container exited 1")


async def test_a_tick_that_reviewed_goes_straight_round(tmp_path, monkeypatch):
    """A tick waits for its containers, so anything pushed meanwhile would sit out
    the whole of that plus a full idle interval."""
    waited = _count_waits(monkeypatch)
    orch = _Ticker([[_reviewed()], []])
    await _one_tick(_cfg(tmp_path, poll_interval_s=600), orch, monkeypatch)
    assert orch.ticks == 2
    assert waited == [600], "only the tick that reviewed nothing may idle"


async def test_a_failed_review_does_not_buy_a_free_round(tmp_path, monkeypatch):
    """The interval is the only thing rate-limiting a container that dies fast."""
    waited = _count_waits(monkeypatch)
    orch = _Ticker([[_failed()]])
    await _one_tick(_cfg(tmp_path, poll_interval_s=600), orch, monkeypatch)
    assert (orch.ticks, waited) == (1, [600])


async def test_a_quiet_pass_never_buys_a_free_round(tmp_path, monkeypatch):
    """`--dry-run` and `--no-publish` record nothing a gate reads as judged, so the
    same commit is reviewable again next tick: going round is an unbounded loop."""
    waited = _count_waits(monkeypatch)
    orch = _Ticker([[_reviewed()]])
    await _one_tick(_cfg(tmp_path, poll_interval_s=600), orch, monkeypatch, quiet=True)
    assert (orch.ticks, waited) == (1, [600])


async def test_a_drained_queue_stops_going_round(tmp_path, monkeypatch):
    waited = _count_waits(monkeypatch)
    orch = _Ticker([[_reviewed()], [_reviewed()], []])
    await _one_tick(_cfg(tmp_path, poll_interval_s=600), orch, monkeypatch)
    assert (orch.ticks, waited) == (3, [600])
