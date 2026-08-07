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
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

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
    model: str | None = None,
    via_endpoint: bool = False,
) -> ReviewRun:
    """Run the review in a throwaway container and parse its output."""
    tag = _stem(cfg, repo, meta, model, mode=mode)
    name = f"robbie-{tag}"
    stem = cfg.transcript_dir / tag
    argv = _docker_argv(cfg, secrets, repo, meta, name=name, mode=mode,
                        model=model, via_endpoint=via_endpoint)

    logger.info("spawning %s for %s#%s", name, repo.slug, meta.number)
    started = asyncio.get_running_loop().time()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_docker_env(cfg, secrets, via_endpoint=via_endpoint)},
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=cfg.docker.timeout_s
        )
    except TimeoutError:
        await _kill(name)
        await proc.wait()  # the client exits once the container is gone
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
            transcript=_kept(stem),
            error=_why_it_failed(proc.returncode, out, err),
        )

    try:
        result = json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError as ex:
        return ReviewRun(
            ok=False,
            duration_s=elapsed,
            transcript=_kept(stem),
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


async def check_model_proxy(cfg: Config, secrets: Secrets) -> None:
    """Fail at boot if the reviewers cannot authenticate to the proxy.

    Every model credential now reaches a run through it, and a wrong token does
    not fail fast: the CLI retries a 401 until the container hits `timeout_s`, so
    each PR would burn the full deadline and be recorded as a timeout.
    """
    if not cfg.model_proxy:
        return
    url = f"{cfg.model_proxy.rstrip('/')}/verify"
    token = _plain(secrets.model_proxy_token)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as ex:
        raise SystemExit(
            f"model_proxy {cfg.model_proxy} is unreachable ({ex}). It is where every "
            "model credential lives now, so no review can run without it."
        ) from ex
    if resp.status_code != 200:
        raise SystemExit(
            f"model_proxy {cfg.model_proxy} rejected MODEL_PROXY_TOKEN "
            f"({resp.status_code}); it must be the same value the sidecar was given."
        )
    logger.info("model proxy at %s accepted our token", cfg.model_proxy)


TRANSCRIPT_DAYS = 30


def prune_transcripts(cfg: Config) -> int:
    """Drop transcripts older than TRANSCRIPT_DAYS, and say how many went.

    Nothing reads one that old. The numbers a review is judged on live in SQLite;
    these are the raw run, kept for the days where somebody might still go and
    read why a verdict came out the way it did. Past a month a PR has been rebased
    out from under it anyway, so the honest answer is a fresh pass, not this file.

    Every failure is swallowed per file: a disk that will not let go of one
    transcript is not a reason to skip the tick that was about to start.
    """
    cutoff = time.time() - TRANSCRIPT_DAYS * 86_400
    try:
        entries = list(cfg.transcript_dir.iterdir())
    except OSError as ex:
        logger.warning("could not read %s to prune it: %s", cfg.transcript_dir, ex)
        return 0
    gone = 0
    for path in entries:
        with contextlib.suppress(OSError):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                gone += 1
    if gone:
        logger.info("pruned %d transcript(s) older than %d days", gone, TRANSCRIPT_DAYS)
    return gone


def _stem(
    cfg: Config, repo: RepoConfig, meta: PrMeta, model: str | None, *, mode: str = "review"
) -> str:
    """Names this run's transcripts and its container — one per model per commit."""
    tag = f"{repo.name}-{meta.number}-{meta.head_sha[:8]}"
    if mode != "review":
        tag = f"{tag}-{mode}"
    if model:
        # a docker name and a filename both refuse most of what a model tag holds
        tag = f"{tag}-{re.sub(r'[^A-Za-z0-9]+', '-', model).strip('-')}"
    return tag


def _kept(stem: Path) -> Path:
    """Move a failed run's transcripts aside, and say where they went.

    `_stem` is a pure function of (repo, pr, sha, model), so the retry writes the
    same filenames — and the run that says why the first one failed is the one
    erased. Kept under the same directory and the same 30-day prune, because this
    is the file somebody reads when an operator DM arrives an hour late.

    A rename that fails is not worth losing the run over: the caller still gets a
    path, and at worst it is the one the retry will overwrite.
    """
    kept = stem.with_name(f"{stem.name}-failed-{int(time.time())}")
    for suffix in (".json", ".err"):
        with contextlib.suppress(OSError):
            stem.with_suffix(suffix).rename(kept.with_suffix(suffix))
    return kept.with_suffix(".json")


def _why_it_failed(code: int | None, out: bytes, err: bytes) -> str:
    """Whatever the run itself said, in the order the reason is likeliest to be in.

    The CLI reports its own failures in the envelope on stdout, and stderr here
    opens with git's fetch and checkout chatter — enough of it to fill a
    head-anchored excerpt on its own and bury the line that explains anything.
    """
    with contextlib.suppress(Exception):
        envelope = json.loads(out.decode(errors="replace"))
        said = str(envelope.get("result") or envelope.get("error") or "").strip()
        status = envelope.get("api_error_status")
        if said:
            return (
                f"container exited {code}: {said[:300]}"
                + (f" (api {status})" if status else "")
            )
    return f"container exited {code}: {err.decode(errors='replace').strip()[-400:]}"


def _docker_argv(
    cfg: Config, secrets: Secrets, repo: RepoConfig, meta: PrMeta, *,
    name: str, mode: str = "review", model: str | None = None,
    via_endpoint: bool = False,
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
        # by name, not by value: an argv is world-readable through /proc, and the
        # docker CLI reads these out of its own environment (see _docker_env)
        "-e", "GH_TOKEN",
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
    if cfg.docker.network and (cfg.review_mcp or cfg.model_proxy):
        # the network is how the sidecars are reached; with neither configured it is
        # only reachable surface for code the reviewer is about to run
        argv += ["--network", cfg.docker.network]
    if cfg.policy_dir:
        argv += ["-v", f"{cfg.policy_dir}:/policy:ro"]
    if model:
        argv += ["-e", f"REVIEW_MODEL={model}"]
    if cfg.model_proxy:
        # Every arm goes through it and none of them carry a real credential: the
        # arm is a path, and the proxy puts the key on the request at the far end.
        arm = "endpoint" if via_endpoint else "account"
        argv += [
            "-e", "ANTHROPIC_AUTH_TOKEN",
            "-e", f"ANTHROPIC_BASE_URL={cfg.model_proxy.rstrip('/')}/{arm}",
        ]
    elif via_endpoint:
        # naming a model does not move the run; only this does, and its endpoint
        # brings its own auth because the backend's would be the wrong key there
        argv += ["-e", "ANTHROPIC_AUTH_TOKEN"]
        if secrets.review_base_url:
            argv += ["-e", f"ANTHROPIC_BASE_URL={secrets.review_base_url}"]
    elif cfg.backend == "api":
        argv += ["-e", "ANTHROPIC_API_KEY"]
    else:
        # rw because the CLI refreshes the OAuth token in place, and the next
        # container needs the fresh one. ponytail: concurrent refreshes can race
        # on this file, and it carries every other OAuth session the host has —
        # `model_proxy` is what ends both, and this branch is what it replaces.
        argv += ["-v", f"{secrets.claude_credentials}:/home/robbie/.claude/.credentials.json"]
    if cfg.review_mcp:
        argv += ["-e", f"REVIEW_MCP={cfg.review_mcp}"]
    argv.append(repo.image)
    return argv


def _docker_env(
    cfg: Config, secrets: Secrets, *, via_endpoint: bool = False
) -> dict[str, str]:
    """The secrets `docker run -e NAME` picks up, kept out of the command line.

    The reviewer runs a model with bypassPermissions, so it gets the read-only
    token when one is configured — publishing is not its job. With `model_proxy`
    set it gets no model credential at all, only the token that reaches the proxy.
    """
    env = {"GH_TOKEN": secrets.reviewer_gh_token.get_secret_value()}
    if cfg.model_proxy:
        env["ANTHROPIC_AUTH_TOKEN"] = _plain(secrets.model_proxy_token)
    elif via_endpoint:
        env["ANTHROPIC_AUTH_TOKEN"] = _plain(secrets.review_api_token)
    elif cfg.backend == "api":
        env["ANTHROPIC_API_KEY"] = _plain(secrets.anthropic_api_key)
    return env


def _plain(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret else ""


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
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
