"""The proxy exists so a reviewer holds no model credential. These say so.

The one that matters most is `test_no_real_credential_reaches_the_container`:
everything else here is about the proxy doing its job, and that one is about the
property the proxy was built for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from robbie import runner as runner_mod
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.github import PrMeta
from robbie.runner import (
    _docker_argv,
    _docker_env,
    check_model_proxy,
)
from robbie_proxy.main import Denied, Settings, account_bearer, build_app, merge_beta

TOKEN = "the-fleet-token"
CLI_BETAS = "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24"


def creds(tmp_path: Path, *, minutes_left: float = 60.0) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "oauth-access-token",
            "refreshToken": "oauth-refresh-token",
            "expiresAt": (time.time() + minutes_left * 60) * 1000,
        },
        # the neighbours that used to ride into every container with it
        "mcpOAuth": {"asana|x": {"accessToken": "asana-token"}},
    }))
    return path


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


def proxy(settings: Settings, seen: list[httpx.Request], status: int = 200):
    async def body():
        # a real stream, not loaded content: the proxy forwards with aiter_raw so
        # it never decodes a payload, and a loaded response cannot be read that way
        yield b'{"ok": true}'

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body())

    app = build_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model-proxy"
    )


def both_arms(tmp_path: Path, **kw) -> Settings:
    return Settings(
        token=TOKEN, credentials=creds(tmp_path, **kw),
        review_base_url="https://ollama.com", review_api_token="ollama-key",
    )


# ----- the credential swap ------------------------------------------------


async def test_the_account_arm_carries_the_oauth_bearer(tmp_path, seen):
    async with proxy(both_arms(tmp_path), seen) as client:
        r = await client.post(
            "/account/v1/messages", params={"beta": "true"},
            headers={"authorization": f"Bearer {TOKEN}", "anthropic-beta": CLI_BETAS},
            content=b"{}",
        )
    assert r.status_code == 200
    sent = seen[0]
    assert sent.headers["authorization"] == "Bearer oauth-access-token"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true", (
        "the arm is the proxy's own routing; upstream never sees it"
    )


async def test_the_endpoint_arm_carries_its_own_key_and_no_oauth_beta(tmp_path, seen):
    async with proxy(both_arms(tmp_path), seen) as client:
        await client.post(
            "/endpoint/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}", "anthropic-beta": CLI_BETAS},
            content=b"{}",
        )
    sent = seen[0]
    assert sent.headers["authorization"] == "Bearer ollama-key"
    assert str(sent.url) == "https://ollama.com/v1/messages"
    assert "oauth" not in sent.headers.get("anthropic-beta", "")


async def test_the_cli_betas_survive_the_one_we_add(tmp_path, seen):
    """Replacing the header drops flags the run depends on, and the failure looks
    like the model quietly behaving as a different one."""
    async with proxy(both_arms(tmp_path), seen) as client:
        await client.post(
            "/account/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}", "anthropic-beta": CLI_BETAS},
            content=b"{}",
        )
    got = seen[0].headers["anthropic-beta"].split(",")
    assert got[0] == "oauth-2025-04-20"
    assert set(CLI_BETAS.split(",")) <= set(got)


def test_merge_beta_is_idempotent_and_keeps_order():
    assert merge_beta("a,b", "x") == "x,a,b"
    assert merge_beta("x,a", "x") == "x,a"
    assert merge_beta("", "x") == "x"


async def test_accept_encoding_reaches_upstream(tmp_path, seen):
    """Strip it and forward the raw body anyway and the CLI reports
    `API Error: Failed to parse JSON` with nothing else to go on."""
    async with proxy(both_arms(tmp_path), seen) as client:
        await client.post(
            "/account/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}", "accept-encoding": "gzip, zstd"},
            content=b"{}",
        )
    assert seen[0].headers["accept-encoding"] == "gzip, zstd"


# ----- the account arm's other shape --------------------------------------


async def test_an_api_key_account_uses_x_api_key_and_no_oauth_beta(seen):
    """backend=api is the shape a VPS wants: an API key does not expire, so the
    arm that has no refresh flow never needs one."""
    settings = Settings(token=TOKEN, api_key="sk-ant-real")
    async with proxy(settings, seen) as client:
        r = await client.post(
            "/account/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}", "anthropic-beta": CLI_BETAS},
            content=b"{}",
        )
    assert r.status_code == 200
    sent = seen[0]
    assert sent.headers["x-api-key"] == "sk-ant-real"
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-beta"] == CLI_BETAS, "the oauth beta is not its shape"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"


async def test_an_oauth_session_wins_when_both_are_configured(tmp_path, seen):
    settings = Settings(token=TOKEN, credentials=creds(tmp_path), api_key="sk-ant-real")
    async with proxy(settings, seen) as client:
        await client.post(
            "/account/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}"}, content=b"{}",
        )
    assert seen[0].headers["authorization"] == "Bearer oauth-access-token"
    assert "x-api-key" not in seen[0].headers


async def test_an_api_key_account_serves_the_arm(seen):
    async with proxy(Settings(token=TOKEN, api_key="sk-ant-real"), seen) as client:
        assert (await client.get("/healthz")).json()["arms"] == ["account"]


def test_from_env_takes_the_api_key(monkeypatch):
    for name in ("CLAUDE_CREDENTIALS", "REVIEW_BASE_URL", "REVIEW_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_PROXY_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    settings = Settings.from_env()
    assert (settings.api_key, settings.credentials) == ("sk-ant-real", None)
    assert settings.serves_account


def test_from_env_refuses_a_proxy_with_nothing_to_proxy(monkeypatch):
    for name in ("CLAUDE_CREDENTIALS", "ANTHROPIC_API_KEY", "REVIEW_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_PROXY_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="nothing to proxy"):
        Settings.from_env()


# ----- refusing -----------------------------------------------------------


@pytest.mark.parametrize("header", ["", "Bearer wrong", "wrong", f"Bearer {TOKEN}x"])
async def test_a_token_that_is_not_ours_forwards_nothing(tmp_path, seen, header):
    async with proxy(both_arms(tmp_path), seen) as client:
        r = await client.post(
            "/account/v1/messages", headers={"authorization": header}, content=b"{}"
        )
    assert r.status_code == 401
    assert seen == [], "nothing may reach an upstream on a token we did not mint"


async def test_an_expired_session_says_so_instead_of_forwarding(tmp_path, seen):
    settings = both_arms(tmp_path, minutes_left=-30)
    async with proxy(settings, seen) as client:
        r = await client.post(
            "/account/v1/messages", headers={"authorization": f"Bearer {TOKEN}"}, content=b"{}"
        )
    assert r.status_code == 502
    assert "expired" in r.json()["error"]["message"]
    assert seen == []


async def test_an_unknown_arm_is_refused(tmp_path, seen):
    async with proxy(both_arms(tmp_path), seen) as client:
        r = await client.post(
            "/elsewhere/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}"}, content=b"{}",
        )
    assert r.status_code == 502
    assert seen == []


async def test_an_arm_nobody_configured_is_refused_not_guessed(tmp_path, seen):
    only_account = Settings(token=TOKEN, credentials=creds(tmp_path))
    async with proxy(only_account, seen) as client:
        r = await client.post(
            "/endpoint/v1/messages",
            headers={"authorization": f"Bearer {TOKEN}"}, content=b"{}",
        )
    assert r.status_code == 502
    assert seen == []


def test_a_credentials_file_that_is_not_one_raises_denied(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(Denied):
        account_bearer(bad)
    with pytest.raises(Denied):
        account_bearer(tmp_path / "missing.json")


async def test_verify_answers_without_spending_anything(tmp_path, seen):
    """The boot check. A wrong token does not fail fast on its own: the CLI retries
    a 401 until the container hits `timeout_s`, so every PR burns the full 30
    minutes and lands in the database as a timeout."""
    async with proxy(both_arms(tmp_path), seen) as client:
        good = await client.get("/verify", headers={"authorization": f"Bearer {TOKEN}"})
        bad = await client.get("/verify", headers={"authorization": "Bearer nope"})
    assert (good.status_code, good.json()) == (200, {"ok": True})
    assert bad.status_code == 401
    assert seen == [], "it must answer from here, never by asking an upstream"


async def test_healthz_names_the_arms_it_can_serve(tmp_path, seen):
    async with proxy(Settings(token=TOKEN, credentials=creds(tmp_path)), seen) as client:
        body = (await client.get("/healthz")).json()
    assert body == {"status": "ok", "arms": ["account"]}


# ----- what the container ends up holding ---------------------------------


def _cfg(tmp_path, **kw) -> Config:
    (tmp_path / "app.git").mkdir(exist_ok=True)
    repo = RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path / "app.git")
    return Config(
        slack=SlackConfig(owner_id="U0"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(network="robbie"), **kw,
    )


def _secrets() -> Secrets:
    return Secrets(
        gh_token="write-token", slack_bot_token="s", reviewer_gh_token="read-token",
        anthropic_api_key="sk-ant-real", claude_credentials=Path("/host/.credentials.json"),
        review_base_url="https://ollama.com", review_api_token="ollama-key",
        model_proxy_token="the-fleet-token",
    )


def _pr() -> PrMeta:
    return PrMeta(
        number=7, title="t", url="u", author="dev", head_sha="abc1234",
        changed_files=1, labels=(), checks=(),
    )


@pytest.mark.parametrize("backend,via", [("api", False), ("oauth", False), ("api", True)])
def test_no_real_credential_reaches_the_container(tmp_path, backend, via):
    """The whole point, on every arm: no key, no session file, nothing reusable."""
    cfg = _cfg(tmp_path, backend=backend, model_proxy="http://model-proxy:8080")
    secrets = _secrets()
    argv = _docker_argv(cfg, secrets, cfg.repos[0], _pr(), name="n", via_endpoint=via)
    env = _docker_env(cfg, secrets, via_endpoint=via)

    assert "ANTHROPIC_API_KEY" not in argv and "ANTHROPIC_API_KEY" not in env
    assert not any(".credentials.json" in a for a in argv), (
        "the oauth mount carried Asana and Sentry sessions into the container too"
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "the-fleet-token"
    assert "sk-ant-real" not in " ".join(argv) + " ".join(env.values())
    assert "ollama-key" not in " ".join(argv) + " ".join(env.values())


@pytest.mark.parametrize("via,arm", [(False, "account"), (True, "endpoint")])
def test_the_arm_is_the_path_the_container_is_pointed_at(tmp_path, via, arm):
    cfg = _cfg(tmp_path, backend="oauth", model_proxy="http://model-proxy:8080/")
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n", via_endpoint=via)
    assert f"ANTHROPIC_BASE_URL=http://model-proxy:8080/{arm}" in argv


def test_the_reviewer_joins_the_network_to_reach_the_proxy(tmp_path):
    """It only joined for MCP before, and `review_mcp` is empty on most installs."""
    cfg = _cfg(tmp_path, model_proxy="http://model-proxy:8080")
    assert cfg.review_mcp == ""
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert argv[argv.index("--network") + 1] == "robbie"


def test_without_the_proxy_nothing_changes(tmp_path):
    cfg = _cfg(tmp_path, backend="oauth")
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert any(".credentials.json" in a for a in argv)
    assert "--network" not in argv


# ----- the boot check -----------------------------------------------------


def _probe(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*a, **kw):
        return real(*a, **{**kw, "transport": transport})

    monkeypatch.setattr(runner_mod.httpx, "AsyncClient", patched)


async def test_a_reachable_proxy_that_takes_our_token_lets_boot_continue(tmp_path, monkeypatch):
    _probe(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    await check_model_proxy(_cfg(tmp_path, model_proxy="http://p:8080"), _secrets())


async def test_a_rejected_token_stops_the_daemon(tmp_path, monkeypatch):
    _probe(monkeypatch, lambda r: httpx.Response(401, json={"ok": False}))
    with pytest.raises(SystemExit, match="rejected MODEL_PROXY_TOKEN"):
        await check_model_proxy(_cfg(tmp_path, model_proxy="http://p:8080"), _secrets())


async def test_an_unreachable_proxy_stops_the_daemon(tmp_path, monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _probe(monkeypatch, refuse)
    with pytest.raises(SystemExit, match="unreachable"):
        await check_model_proxy(_cfg(tmp_path, model_proxy="http://p:8080"), _secrets())


async def test_no_proxy_configured_asks_nothing(tmp_path, monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing to check when the proxy is off")

    _probe(monkeypatch, boom)
    await check_model_proxy(_cfg(tmp_path), _secrets())
