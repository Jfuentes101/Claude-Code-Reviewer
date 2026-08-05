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
from robbie.runner import _docker_argv, _docker_env

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


def _pr(**kw) -> PrMeta:
    base = dict(
        number=7, title="t", url="https://x/7", author="dev",
        head_sha="abc1234567", changed_files=1, labels=(), checks=(),
    )
    return PrMeta(**{**base, **kw})


# ----- the container --------------------------------------------------------


def test_the_mirror_is_mounted_read_only(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert "/srv/mirrors/app.git:/bare:ro" in argv


def test_the_reviewer_gets_the_read_only_token(tmp_path):
    cfg = _cfg(tmp_path)
    assert _docker_env(cfg, _secrets())["GH_TOKEN"] == "read-token"


def test_no_secret_is_ever_written_into_the_command_line(tmp_path):
    """An argv is readable through /proc by anyone on the host."""
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    for secret in ("read-token", "write-token", "sk-ant"):
        assert secret not in " ".join(argv)
    assert "GH_TOKEN" in argv, "it still has to be handed over, just by name"


def test_caps_and_hardening_are_always_applied(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    joined = " ".join(argv)
    for expected in ("--rm", "--cap-drop ALL", "no-new-privileges", "--cpus 4", "--memory 8g"):
        assert expected in joined


def test_no_new_privileges_can_be_turned_off_for_apparmor_hosts(tmp_path):
    cfg = _cfg(tmp_path, docker=DockerConfig(no_new_privileges=False))
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "no-new-privileges" not in " ".join(argv)
    assert "--cap-drop" in argv, "dropping caps is not negotiable"


def test_api_backend_passes_the_key_and_mounts_no_credentials(tmp_path):
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "ANTHROPIC_API_KEY" in argv
    assert _docker_env(cfg, _secrets())["ANTHROPIC_API_KEY"] == "sk-ant"
    assert not any(".credentials.json" in a for a in argv)


def test_oauth_backend_mounts_credentials_writable_for_token_refresh(tmp_path):
    cfg = _cfg(tmp_path, backend="oauth")
    argv = _docker_argv(
        cfg, _secrets(claude_credentials=Path("/etc/robbie/creds.json")),
        cfg.repos[0], _pr(), name="n",
    )
    mount = next(a for a in argv if ".credentials.json" in a)
    assert not mount.endswith(":ro"), "the CLI has to write the refreshed token back"
    assert "ANTHROPIC_API_KEY" not in argv


def test_mcp_config_is_only_passed_when_set(tmp_path):
    plain = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert not any(a.startswith("REVIEW_MCP=") for a in plain)
    cfg = _cfg(tmp_path, review_mcp='{"mcpServers":{}}')
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert 'REVIEW_MCP={"mcpServers":{}}' in argv


def test_the_reviewer_joins_the_compose_network_so_it_can_reach_the_sidecars(tmp_path):
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "--network robbie" in " ".join(argv)


def test_the_network_can_be_turned_off(tmp_path):
    cfg = _cfg(tmp_path, docker=DockerConfig(network=None))
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "--network" not in argv


def test_the_policy_dir_is_mounted_read_only_when_configured(tmp_path):
    cfg = _cfg(tmp_path, policy_dir=Path("/opt/robbie/policy"))
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "/opt/robbie/policy:/policy:ro" in argv


def test_no_policy_dir_means_no_mount(tmp_path):
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
    assert not any("/policy" in a for a in argv)


def test_the_base_ref_reaches_the_container(tmp_path):
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(base_ref="release/2026"), name="n")
    assert "BASE_REF=release/2026" in argv


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


def test_the_shipped_policy_is_present_and_whole():
    """policy/ is edited live on the host, so a truncation ships silently."""
    text = Path("policy/CLAUDE.md").read_text(encoding="utf-8")
    for heading in ("Reachability", "Severity", "Comments in the diff", "PR scope",
                    "Tests", "Recurring bug families", "Writing the review"):
        assert heading in text, heading
    assert len(text) > 6000, "policy looks truncated"


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


def _write_config(tmp_path, **repo_kw) -> Path:
    mirror = tmp_path / "app.git"
    mirror.mkdir(exist_ok=True)
    repo = {"slug": "acme/app", "reviewer_login": "rev", "bare": str(mirror), **repo_kw}
    path = tmp_path / "robbie.yaml"
    path.write_text(__import__("yaml").safe_dump({
        "slack": {"owner_id": "U0"},
        "state_dir": str(tmp_path / "state"),
        "repos": [repo],
    }))
    return path


def test_a_good_config_loads_from_disk(tmp_path):
    assert configmod.load(_write_config(tmp_path)).repos[0].slug == "acme/app"


def test_a_mirror_that_is_not_there_refuses_to_boot(tmp_path):
    """--dry-run starts no container, so nothing else would ever touch this path."""
    path = _write_config(tmp_path, bare=str(tmp_path / "nope.git"))
    with pytest.raises(SystemExit, match="mirror-sync"):
        configmod.load(path)


def test_a_policy_dir_that_is_not_there_refuses_to_boot(tmp_path):
    path = _write_config(tmp_path)
    path.write_text(path.read_text() + f"policy_dir: {tmp_path / 'gone'}\n")
    with pytest.raises(SystemExit, match="policy_dir"):
        configmod.load(path)


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
