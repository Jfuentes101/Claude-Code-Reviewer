"""Container argv, body assembly and config validation — the glue that has no
GitHub in it but would still break production if it were wrong."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from robbie import config as configmod
from robbie.anchor import Anchored
from robbie.config import Config, DockerConfig, RepoConfig, Secrets, SlackConfig
from robbie.github import PrMeta
from robbie.publish import MAX_BYTES, _assemble, _truncate
from robbie.runner import (
    TRANSCRIPT_DAYS,
    _docker_argv,
    _docker_env,
    _stem,
    _why_it_failed,
    prune_transcripts,
)

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
    cfg = _cfg(tmp_path, review_mcp='{"mcpServers":{}}')
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "--network robbie" in " ".join(argv)


def test_the_network_can_be_turned_off(tmp_path):
    cfg = _cfg(tmp_path, docker=DockerConfig(network=None))
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert "--network" not in argv


def test_no_mcp_means_no_network_for_the_code_it_is_about_to_run(tmp_path):
    """The network exists to reach the sidecars. With none, it is only reachable
    surface for a PR whose code the reviewer runs under bypassPermissions."""
    argv = _docker_argv(_cfg(tmp_path), _secrets(), _cfg(tmp_path).repos[0], _pr(), name="n")
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


def test_no_model_override_leaves_the_container_exactly_as_it_was(tmp_path):
    cfg = _cfg(tmp_path)
    argv = _docker_argv(cfg, _secrets(), cfg.repos[0], _pr(), name="n")
    assert not any(a.startswith("REVIEW_MODEL") for a in argv)
    assert "ANTHROPIC_AUTH_TOKEN" not in argv
    assert "ANTHROPIC_API_KEY" in argv, "the backend's own auth still applies"


def test_a_run_sent_to_the_endpoint_brings_its_own_auth(tmp_path):
    """The endpoint replaces the backend's credentials rather than joining them.

    Pointing a run at another provider with the account's key attached would send
    the wrong secret to a third party, and `ANTHROPIC_API_KEY` would win anyway.
    """
    cfg = _cfg(tmp_path)
    secrets = _secrets(review_base_url="https://ollama.com", review_api_token="k-ollama")
    argv = _docker_argv(
        cfg, secrets, cfg.repos[0], _pr(), name="n",
        model="glm-5.2:cloud", via_endpoint=True,
    )
    assert "REVIEW_MODEL=glm-5.2:cloud" in argv
    assert "ANTHROPIC_BASE_URL=https://ollama.com" in argv
    assert "ANTHROPIC_AUTH_TOKEN" in argv
    assert "ANTHROPIC_API_KEY" not in argv
    assert _docker_env(cfg, secrets, via_endpoint=True) == {
        "GH_TOKEN": "read-token", "ANTHROPIC_AUTH_TOKEN": "k-ollama"
    }
    assert "k-ollama" not in " ".join(argv), "a token never goes in an argv"


def test_naming_an_account_model_does_not_move_the_run(tmp_path):
    """`--model sonnet` reached ollama.com and 404'd, because naming a model and
    changing provider were one branch. The model is a flag; the endpoint is not."""
    cfg = _cfg(tmp_path, backend="oauth")
    secrets = _secrets(
        claude_credentials=Path("/etc/robbie/creds.json"),
        review_base_url="https://ollama.com", review_api_token="k-ollama",
    )
    argv = _docker_argv(cfg, secrets, cfg.repos[0], _pr(), name="n", model="sonnet")
    assert "REVIEW_MODEL=sonnet" in argv, "the flag still has to reach the CLI"
    assert not any(a.startswith("ANTHROPIC_BASE_URL") for a in argv)
    assert "ANTHROPIC_AUTH_TOKEN" not in argv
    assert any(".credentials.json" in a for a in argv), "the account's own session"
    assert "ANTHROPIC_AUTH_TOKEN" not in _docker_env(cfg, secrets)


def test_a_run_sent_to_the_endpoint_mounts_no_credentials(tmp_path):
    """Otherwise a third-party endpoint gets a mount of a human's own session."""
    cfg = _cfg(tmp_path, backend="oauth")
    secrets = _secrets(
        claude_credentials=Path("/etc/robbie/creds.json"), review_api_token="k-ollama"
    )
    argv = _docker_argv(
        cfg, secrets, cfg.repos[0], _pr(), name="n", model="gemma4:cloud", via_endpoint=True
    )
    assert not any(".credentials.json" in a for a in argv)


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


def test_a_slug_without_an_owner_refuses_to_boot(tmp_path):
    """Otherwise `.name` raises IndexError mid-tick, which is what boot checks avoid."""
    with pytest.raises(Exception, match="string_pattern_mismatch|pattern"):
        RepoConfig(slug="app", reviewer_login="rev", bare=tmp_path)


def test_a_label_that_would_break_the_search_query_refuses_to_boot(tmp_path):
    """It is interpolated into `label:"..."` in a GraphQL search, where a stray
    quote surfaces as an unreadable parse error rather than as a bad label."""
    for bad in ('Code "Review"', "Code\nReview"):
        with pytest.raises(Exception, match="string_pattern_mismatch|pattern"):
            RepoConfig(slug="a/b", reviewer_login="rev", bare=tmp_path, label=bad)
    RepoConfig(slug="a/b", reviewer_login="rev", bare=tmp_path, label="Code Review ✨")


def test_a_key_set_twice_in_the_yaml_refuses_to_boot(tmp_path):
    """PyYAML keeps the last one silently; a dead block is not a config."""
    path = _write_config(tmp_path)
    path.write_text(path.read_text() + "poll_interval_s: 60\npoll_interval_s: 120\n")
    with pytest.raises(SystemExit, match="set twice"):
        configmod.load(path)


def test_a_policy_dir_that_is_not_there_refuses_to_boot(tmp_path):
    path = _write_config(tmp_path)
    path.write_text(path.read_text() + f"policy_dir: {tmp_path / 'gone'}\n")
    with pytest.raises(SystemExit, match="policy_dir"):
        configmod.load(path)


def _split(tmp_path) -> Config:
    return _cfg(tmp_path, review_models=[
        {"model": "glm-5.2:cloud", "via": "endpoint", "weight": 2},
        {"model": "sonnet", "weight": 1},
    ])


def test_no_arms_configured_keeps_every_review_on_the_account(tmp_path):
    choice = _cfg(tmp_path).choose_model("acme/app:7:abc:t")
    assert (choice.model, choice.via_endpoint) == (None, False)


def test_the_weights_are_the_ratio(tmp_path):
    cfg = _split(tmp_path)
    picks = [cfg.choose_model(f"acme/app:{n}:sha{n}:t").model for n in range(600)]
    third = picks.count("sonnet") / len(picks)
    assert 0.28 < third < 0.39, f"one in three should be sonnet, got {third:.2f}"


def test_the_same_commit_always_lands_on_the_same_model(tmp_path):
    """Otherwise a re-review moves the very variable being measured."""
    cfg = _split(tmp_path)
    key = "acme/app:7:abc1234:2026-01-01T00:00:00Z"
    assert len({cfg.choose_model(key).model for _ in range(20)}) == 1


def test_the_arm_decides_whose_meter_the_run_spends(tmp_path):
    cfg = _split(tmp_path)
    endpoint = [c for c in (cfg.choose_model(f"k{n}") for n in range(200)) if c.via_endpoint]
    assert {c.model for c in endpoint} == {"glm-5.2:cloud"}
    assert cfg.endpoint_models == ("glm-5.2:cloud",)


def test_a_model_named_on_the_cli_is_routed_by_the_config(tmp_path):
    cfg = _split(tmp_path)
    assert cfg.named_model("glm-5.2:cloud").via_endpoint
    assert not cfg.named_model("sonnet").via_endpoint
    assert not cfg.named_model("something-nobody-configured").via_endpoint, (
        "an unconfigured tag runs on the account, where it fails cleanly — the "
        "alternative hands a third party the account's own key"
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
    secrets = configmod.load_secrets(_cfg(tmp_path))
    assert secrets.reviewer_gh_token.get_secret_value() == "only-one"
    # the whole reason for SecretStr: a stray log line must not print the keyring
    assert "only-one" not in repr(secrets)


def test_the_fallback_says_out_loud_that_it_handed_over_the_write_token(
    tmp_path, monkeypatch, caplog
):
    """The security posture is 'the worst it can do is exfiltrate a read-only
    token'. Falling back silently is how that stops being true without anyone
    noticing."""
    monkeypatch.setenv("GH_TOKEN", "only-one")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    monkeypatch.delenv("GH_TOKEN_REVIEWER", raising=False)
    with caplog.at_level("WARNING"):
        configmod.load_secrets(_cfg(tmp_path))
    assert "READ-ONLY" in caplog.text
    assert "only-one" not in caplog.text, "the warning must not print the token"


def test_a_separate_reviewer_token_warns_about_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GH_TOKEN", "write")
    monkeypatch.setenv("GH_TOKEN_REVIEWER", "read")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    with caplog.at_level("WARNING"):
        configmod.load_secrets(_cfg(tmp_path))
    assert caplog.text == ""


# ----- transcripts ---------------------------------------------------------


def test_a_transcript_past_the_window_is_dropped_and_a_fresh_one_is_not(tmp_path):
    """Kept for the days somebody might still read why a verdict came out that
    way. Past a month the PR has been rebased out from under it."""
    cfg = _cfg(tmp_path)
    cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    old = cfg.transcript_dir / "app-7-abc12345.md"
    new = cfg.transcript_dir / "app-8-def67890.md"
    old.write_text("stale")
    new.write_text("fresh")
    aged = time.time() - (TRANSCRIPT_DAYS + 1) * 86_400
    os.utime(old, (aged, aged))

    assert prune_transcripts(cfg) == 1
    assert not old.exists() and new.exists()


def test_pruning_an_unwritable_directory_is_not_a_reason_to_skip_the_tick(tmp_path):
    cfg = _cfg(tmp_path)  # transcript_dir was never created
    assert prune_transcripts(cfg) == 0


# ----- why a run failed ----------------------------------------------------


def test_the_cli_own_reason_wins_over_git_chatter():
    """Observed live: a 404 on the model, reported as "Switched to a new branch"."""
    envelope = json.dumps({
        "is_error": True, "api_error_status": 404,
        "result": "There's an issue with the selected model (claude-opus-5[1m]).",
    }).encode()
    noise = b"From https://github.com/acme/app\n  Switched to a new branch 'x'\n"
    why = _why_it_failed(1, envelope, noise)
    assert "selected model" in why and "api 404" in why
    assert "Switched to a new branch" not in why


def test_without_an_envelope_it_takes_the_end_of_stderr_not_the_start():
    """A failure explains itself last; the clone and the checkout come first."""
    err = ("git noise\n" * 200).encode() + b"fatal: the actual problem\n"
    why = _why_it_failed(3, b"not json at all", err)
    assert "fatal: the actual problem" in why


def test_an_empty_envelope_still_says_the_exit_code():
    assert "exited 9" in _why_it_failed(9, b"{}", b"")


def test_each_model_writes_its_own_transcript(tmp_path):
    """Five models on one commit wrote over each other's transcript and left one."""
    cfg = _cfg(tmp_path)
    stems = {
        _stem(cfg, cfg.repos[0], _pr(), m)
        for m in ("glm-5.2:cloud", "qwen3.5:397b-cloud", None)
    }
    assert len(stems) == 3


def test_a_container_name_stays_legal_for_docker(tmp_path):
    """A model tag carries colons and dots; a --name may not."""
    import re as _re
    cfg = _cfg(tmp_path)
    name = _stem(cfg, cfg.repos[0], _pr(), "glm-5.2:cloud")
    assert _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", f"robbie-{name}"), name
