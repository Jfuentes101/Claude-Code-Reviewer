"""Spawn one reviewer container per PR.

The concurrency model is the container: N reviews in flight means N containers,
each with its own clone, its own resource caps and its own deadline. Nothing is
shared between them but the read-only mirror, so two reviews cannot corrupt each
other's workspace and a hung one is killed by name without touching the rest.

ponytail: sibling containers over the mounted docker socket. That socket is root
on the host, which is the accepted trade on a single-tenant VPS; if robbie ever
shares a host, move the orchestrator to the host under systemd and keep only the
workers in docker (same argv, no socket mount).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from robbie.config import Config, RepoConfig, Secrets
from robbie.contract import Blocks, parse_blocks
from robbie.github import PrMeta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewRun:
    ok: bool
    blocks: Blocks | None = None
    text: str = ""
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_s: float = 0.0
    transcript: Path | None = None
    error: str | None = None


async def run_review(
    cfg: Config,
    secrets: Secrets,
    repo: RepoConfig,
    meta: PrMeta,
    *,
    prompt: str,
    mode: str = "review",
) -> ReviewRun:
    """Run the review in a throwaway container and parse its output."""
    tag = f"{repo.name}-{meta.number}-{meta.head_sha[:8]}"
    if mode != "review":
        tag = f"{tag}-{mode}"
    name = f"robbie-{tag}"
    stem = cfg.transcript_dir / tag
    argv = _docker_argv(cfg, secrets, repo, meta, name=name, mode=mode)

    logger.info("spawning %s for %s#%s", name, repo.slug, meta.number)
    started = asyncio.get_running_loop().time()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=cfg.docker.timeout_s
        )
    except TimeoutError:
        await _kill(name)
        elapsed = asyncio.get_running_loop().time() - started
        return ReviewRun(
            ok=False,
            duration_s=elapsed,
            error=f"timed out after {cfg.docker.timeout_s}s",
        )
    elapsed = asyncio.get_running_loop().time() - started

    stem.with_suffix(".json").write_bytes(out)
    if err:
        stem.with_suffix(".err").write_bytes(err)

    if proc.returncode != 0:
        return ReviewRun(
            ok=False,
            duration_s=elapsed,
            transcript=stem.with_suffix(".json"),
            error=f"container exited {proc.returncode}: {err.decode(errors='replace')[:400]}",
        )

    try:
        result = json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError as ex:
        return ReviewRun(
            ok=False,
            duration_s=elapsed,
            transcript=stem.with_suffix(".json"),
            error=f"could not parse the run envelope: {ex}",
        )

    text = result.get("result") or ""
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    usage = result.get("usage") or {}
    return ReviewRun(
        ok=True,
        blocks=parse_blocks(text) if mode == "review" else None,
        text=text,
        cost_usd=_as_float(result.get("total_cost_usd")),
        tokens_in=_as_int(usage.get("input_tokens")),
        tokens_out=_as_int(usage.get("output_tokens")),
        duration_s=elapsed,
        transcript=stem.with_suffix(".md"),
    )


def _docker_argv(
    cfg: Config, secrets: Secrets, repo: RepoConfig, meta: PrMeta, *,
    name: str, mode: str = "review",
) -> list[str]:
    argv = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--cpus", cfg.docker.cpus,
        "--memory", cfg.docker.memory,
        "--pids-limit", str(cfg.docker.pids_limit),
        "--cap-drop", "ALL",
        # the mirror is the only host path a reviewer can see, and it cannot write to it
        "-v", f"{repo.bare}:/bare:ro",
        # the reviewer runs a model with bypassPermissions, so it gets the
        # read-only token when one is configured — publishing is not its job
        "-e", f"GH_TOKEN={secrets.reviewer_gh_token}",
        "-e", f"REPO_SLUG={repo.slug}",
        "-e", f"PR_NUMBER={meta.number}",
        "-e", f"PR_URL={meta.url}",
        "-e", f"REVIEW_COMMAND={repo.review_command}",
        "-e", f"REVIEW_EFFORT={cfg.review_effort}",
        "-e", f"BASE_REF={meta.base_ref}",
        "-e", f"REVIEW_MODE={mode}",
    ]
    if cfg.docker.no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]
    if cfg.docker.network:
        argv += ["--network", cfg.docker.network]
    if cfg.policy_dir:
        argv += ["-v", f"{cfg.policy_dir}:/policy:ro"]
    if cfg.backend == "api":
        argv += ["-e", f"ANTHROPIC_API_KEY={secrets.anthropic_api_key}"]
    else:
        # rw because the CLI refreshes the OAuth token in place, and the next
        # container needs the fresh one. ponytail: concurrent refreshes can race
        # on this file; backend=api has no such problem, which is why it's default.
        argv += ["-v", f"{secrets.claude_credentials}:/home/robbie/.claude/.credentials.json"]
    if cfg.review_mcp:
        argv += ["-e", f"REVIEW_MCP={cfg.review_mcp}"]
    argv.append(repo.image)
    return argv


async def _kill(name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
