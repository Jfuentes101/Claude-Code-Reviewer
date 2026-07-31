"""Container argv, body assembly and config validation — the glue that has no
GitHub in it but would still break production if it were wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from robbie import config as configmod
from robbie.anchor import Anchored
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.github import PrMeta
from robbie.publish import MAX_BYTES, _assemble, _truncate
from robbie.runner import _docker_argv

NW = "❌ NEEDS WORK! ❌"


def _cfg(tmp_path, **kw) -> Config:
    base = dict(
        slack=SlackConfig(owner_id="U0OWNER"),
        repos=[
            RepoConfig(slug="acme/app", reviewer_login="rev", bare=Path("/srv/mirrors/app.git"))
        ],
        state_dir=tmp_path,
        docker=DockerConfig(cpus="4", memory="8g", timeout_s=60),
    )
    return Config(**{**base, **kw})


def _secrets(**kw) -> Secrets:
    base = dict(
        gh_token="write-token",
        slack_bot_token="xoxb",
        reviewer_gh_token="read-token",
        anthropic_api_key="sk-ant",
    )
    return Secrets(**{**base, **kw})


def _pr() -> PrMeta:
    return PrMeta(
        number=7, title="t", url="https://x/7", author="dev",
        head_sha="abc1234567", changed_files=1, labels=(), checks=(),
    )


# ----- the container --------------------------------------------------------


def test_the_mirror_is_mounted_read_only(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert "/srv/mirrors/app.git:/bare:ro" in argv


def test_the_reviewer_gets_the_read_only_token(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert "GH_TOKEN=read-token" in argv
    assert "GH_TOKEN=write-token" not in argv


def test_caps_and_hardening_are_always_applied(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    joined = " ".join(argv)
    for expected in ("--rm", "--cap-drop ALL", "no-new-privileges", "--cpus 4", "--memory 8g"):
        assert expected in joined


def test_api_backend_passes_the_key_and_mounts_no_credentials(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert "ANTHROPIC_API_KEY=sk-ant" in argv
    assert not any(".credentials.json" in a for a in argv)


def test_oauth_backend_mounts_credentials_writable_for_token_refresh(tmp_path):
    cfg = _cfg(tmp_path, backend="oauth")
    argv = _docker_argv(
        cfg, _secrets(claude_credentials=Path("/etc/robbie/creds.json")),
        cfg.repos[0], _pr(), name="n",
    )
    mount = next(a for a in argv if ".credentials.json" in a)
    assert not mount.endswith(":ro"), "the CLI has to write the refreshed token back"
    assert "ANTHROPIC_API_KEY" not in " ".join(argv)


def test_mcp_config_is_only_passed_when_set(tmp_path):
    plain = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert not any(a.startswith("REVIEW_MCP=") for a in plain)
    cfg = _cfg(tmp_path, review_mcp='{"mcpServers":{}}')
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert 'REVIEW_MCP={"mcpServers":{}}' in argv


def test_the_image_is_the_last_argument(tmp_path):
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert argv[-1] == "robbie-reviewer:latest"


# ----- the comment body ----------------------------------------------------


def test_the_label_instruction_sits_above_the_body():
    out = _assemble("<!-- m -->", NW, "the summary", Anchored())
    assert out.index(NW) < out.index("the summary"), "truncation eats the tail first"


def test_leftovers_are_appended():
    out = _assemble("<!-- m -->", NW, "body", Anchored(leftovers="**Not tied:**\n\n- x"))
    assert out.rstrip().endswith("- x")


def test_a_short_body_is_untouched():
    assert _truncate("short") == "short"


def test_truncation_never_leaves_a_half_character():
    out = _truncate("é" * MAX_BYTES)  # 2 bytes each, so this must be cut
    out.encode("utf-8")  # would raise on a broken surrogate
    assert out.endswith("_…truncated._\n")
    assert len(out.encode("utf-8")) < MAX_BYTES + 200


# ----- config -------------------------------------------------------------


def test_the_shipped_example_config_loads():
    cfg = configmod.Config.model_validate(
        __import__("yaml").safe_load(
            Path("config/robbie.yaml.example").read_text(encoding="utf-8")
        )
    )
    assert cfg.repos[0].slug == "acme/webapp"
    assert cfg.backend == "api"


def test_a_typo_in_the_config_is_a_startup_error():
    with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
        Config(
            slack=SlackConfig(owner_id="U0"),
            repos=[RepoConfig(slug="a/b", reviewer_login="r", bare=Path("/x"))],
            poll_intervall_s=600,  # typo
        )


def test_api_backend_without_a_key_refuses_to_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        configmod.load_secrets(_cfg(tmp_path))


def test_oauth_backend_without_readable_credentials_refuses_to_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "g")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("CLAUDE_CREDENTIALS", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="not readable"):
        configmod.load_secrets(_cfg(tmp_path, backend="oauth"))


def test_the_reviewer_token_falls_back_to_the_write_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "only-one")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.delenv("GH_TOKEN_REVIEWER", raising=False)
    assert configmod.load_secrets(_cfg(tmp_path)).reviewer_gh_token == "only-one"
