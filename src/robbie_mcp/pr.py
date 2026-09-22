"""The fixer's one write, as an MCP sidecar: open a draft pull request.

The model never runs git and never holds a credential. It fills in a form — the
issue it fixed, a title, a summary, and the patch — and this server decides
everything else: which branch, which base, that it is a draft, and whether the
patch is allowed to exist at all.

That split is the whole security argument. A fix container has no GitHub token,
so a poisoned bug report has nothing in reach to write with; the most it can do
is talk this server into opening a draft pull request that a person then reads.
Which is what the tool is for.

What the model cannot choose: the repository, the base branch, the branch name,
draft or ready, the commit author, or any of the limits below.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("robbie.mcp.pr")

API = "https://api.github.com"

# A fix is meant to be small. These are refusals, not warnings: a patch over the
# line is one nobody asked for and the issue goes back to a person.
MAX_FILES = 12
MAX_LINES = 400
MAX_PATCH_BYTES = 200_000
# where a change has to leave a trace, or there is nothing for CI to prove
TEST_DIRS = ("spec/", "test/", "tests/", "__tests__/")
# blast radius a bot does not get: the build, the dependencies, the schema, and
# anything that runs on a machine other than the one under test
OFF_LIMITS = (
    ".github/", "db/migrate/", "db/schema.rb", "db/structure.sql",
    "Gemfile", "Gemfile.lock", "package.json", "yarn.lock", "package-lock.json",
    "Dockerfile", "docker-compose.yml", ".env", "config/credentials",
)


@dataclass(frozen=True)
class Settings:
    token: str
    repo: str
    mirror: Path
    base: str = "main"
    label: str = "robbie-fix"
    branch_prefix: str = "fix/issue-"
    author_name: str = "robbie"
    author_email: str = "robbie@users.noreply.github.com"
    host: str = "0.0.0.0"
    port: int = 8080
    allowed_hosts: tuple[str, ...] = ("mcp-pr:8080", "mcp-pr", "localhost:8080")

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("FIXER_GH_TOKEN", "").strip()
        repo = os.environ.get("FIXER_REPO", "").strip()
        mirror = os.environ.get("FIXER_MIRROR", "").strip()
        if not token or not repo or not mirror:
            raise SystemExit("FIXER_GH_TOKEN, FIXER_REPO and FIXER_MIRROR are required")
        hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
        return cls(
            token=token,
            repo=repo,
            mirror=Path(mirror),
            base=os.environ.get("FIXER_BASE", "main").strip() or "main",
            label=os.environ.get("FIXER_LABEL", "robbie-fix").strip() or "robbie-fix",
            author_name=os.environ.get("FIXER_AUTHOR", "robbie").strip() or "robbie",
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8080")),
            allowed_hosts=(
                tuple(h.strip() for h in hosts.split(",") if h.strip())
                if hosts
                else cls.allowed_hosts
            ),
        )


# ----- pure: what a patch is allowed to be ---------------------------------

_DIFF_LINE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.M)


def changed_paths(patch: str) -> list[str]:
    """Every path a patch names, both sides, in order and without duplicates.

    Both sides because a rename touches two: reading only the destination lets a
    patch move a file out of a protected directory without the check noticing.
    """
    seen: dict[str, None] = {}
    for a, b in _DIFF_LINE.findall(patch):
        for path in (a, b):
            if path != "/dev/null":
                seen.setdefault(PurePosixPath(path).as_posix())
    return list(seen)


def changed_lines(patch: str) -> int:
    return sum(
        1 for line in patch.splitlines()
        if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    )


def refuse(patch: str) -> str | None:
    """Why this patch may not become a pull request, or None if it may.

    Deterministic and first-match: the model is told these rules in its prompt,
    but being told is not the check — this is.
    """
    if not patch.strip():
        return "the patch is empty"
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return f"the patch is over {MAX_PATCH_BYTES} bytes"
    paths = changed_paths(patch)
    if not paths:
        return "no `diff --git` header in the patch, so nothing can be read from it"
    for path in paths:
        if path.startswith("../") or PurePosixPath(path).is_absolute():
            return f"{path} is outside the repository"
        for banned in OFF_LIMITS:
            if path == banned or path.startswith(banned):
                return f"{path} is off limits to an automated fix"
    if not any(part in path for path in paths for part in TEST_DIRS):
        return "the patch changes no test, so CI cannot show the bug was fixed"
    if len(paths) > MAX_FILES:
        return f"the patch touches {len(paths)} files, over the limit of {MAX_FILES}"
    if (lines := changed_lines(patch)) > MAX_LINES:
        return f"the patch changes {lines} lines, over the limit of {MAX_LINES}"
    return None


def clean_title(raw: str, *, issue: int) -> str:
    """One line, no markers, and never empty."""
    title = " ".join(raw.split())[:120].strip()
    return title or f"fix: issue #{issue}"


# ----- the write itself ----------------------------------------------------


class GitError(RuntimeError):
    pass


async def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        raise GitError(f"git {args[0]}: {err.decode(errors='replace')[:600]}")
    return out.decode(errors="replace")


# a helper reads the password out of the environment, so the token is never an
# argument and never lands in this repo's config file
_CRED_HELPER = '!f() { echo username=x-access-token; echo "password=$GIT_PASSWORD"; }; f'


def build_server(settings: Settings, api: Any | None = None) -> MCPServer:
    client = api or httpx.AsyncClient(
        base_url=API,
        headers={
            "Authorization": f"Bearer {settings.token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30.0,
    )

    mcp: MCPServer = MCPServer(
        name="pr",
        version="0.1.0",
        instructions=(
            "Opens the draft pull request for a fix. This is the only way to get "
            "work out of this container: there is no git remote and no token here."
        ),
    )

    async def _guard_issue(issue: int) -> str | None:
        resp = await client.get(f"/repos/{settings.repo}/issues/{issue}")
        if resp.status_code != 200:
            return f"issue #{issue} could not be read ({resp.status_code})"
        data = resp.json()
        if data.get("state") != "open":
            return f"issue #{issue} is not open"
        labels = {lbl.get("name") for lbl in data.get("labels") or []}
        if settings.label not in labels:
            return (
                f"issue #{issue} does not carry the {settings.label!r} label, so it "
                "is not one an automated fix was asked for"
            )
        return None

    @mcp.tool(
        description=(
            "Open the draft pull request for the bug you just fixed. Give it the "
            "issue number, a one-line title, a summary for the person who reviews "
            "it, and the complete patch in `git diff` format (unified, with "
            "`diff --git` headers, paths relative to the repository root).\n\n"
            "This server chooses the branch, the base and the draft state; you "
            "cannot. It refuses a patch that changes no test, that touches CI "
            f"config, dependencies or migrations, that spans more than {MAX_FILES} "
            f"files or {MAX_LINES} lines, or that does not apply cleanly to a fresh "
            "checkout. A refusal comes back as `error` with the reason, and it is "
            "worth reading: a patch that does not apply usually means you edited "
            "against something the patch text does not describe."
        )
    )
    async def open_pull_request(
        issue: int, title: str, summary: str, patch: str
    ) -> dict[str, Any]:
        if (why := refuse(patch)) is not None:
            return {"ok": False, "error": why}
        if (why := await _guard_issue(issue)) is not None:
            return {"ok": False, "error": why}

        branch = f"{settings.branch_prefix}{issue}"
        exists = await client.get(f"/repos/{settings.repo}/git/ref/heads/{branch}")
        if exists.status_code == 200:
            return {"ok": False, "error": f"the branch {branch} already exists"}

        work = Path(tempfile.mkdtemp(prefix="robbie-fix-"))
        repo_dir = work / "repo"
        try:
            await _git("clone", "--quiet", "--shared", str(settings.mirror), str(repo_dir),
                       cwd=work)
            await _git("remote", "set-url", "origin",
                       f"https://github.com/{settings.repo}.git", cwd=repo_dir)
            await _git("config", "credential.helper", _CRED_HELPER, cwd=repo_dir)
            await _git("config", "user.name", settings.author_name, cwd=repo_dir)
            await _git("config", "user.email", settings.author_email, cwd=repo_dir)
            env = {"GIT_PASSWORD": settings.token, "GIT_TERMINAL_PROMPT": "0"}
            # onto trunk as it is now, not as the mirror remembers it
            await _git("fetch", "--quiet", "origin", settings.base, cwd=repo_dir, env=env)
            await _git("checkout", "--quiet", "-b", branch, "FETCH_HEAD", cwd=repo_dir)

            (work / "fix.patch").write_text(patch if patch.endswith("\n") else patch + "\n")
            try:
                await _git("apply", "--index", "--whitespace=nowarn", str(work / "fix.patch"),
                           cwd=repo_dir)
            except GitError as ex:
                return {"ok": False, "error": f"the patch did not apply: {ex}"}

            await _git("commit", "--quiet", "-m", clean_title(title, issue=issue),
                       cwd=repo_dir)
            await _git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}",
                       cwd=repo_dir, env=env)
        except (GitError, OSError, TimeoutError) as ex:
            logger.warning("fix for #%s failed: %s", issue, ex)
            return {"ok": False, "error": str(ex)[:600]}
        finally:
            shutil.rmtree(work, ignore_errors=True)

        body = f"Closes #{issue}\n\n{summary.strip()}\n"
        created = await client.post(
            f"/repos/{settings.repo}/pulls",
            json={
                "title": clean_title(title, issue=issue),
                "head": branch,
                "base": settings.base,
                "body": body,
                "draft": True,
            },
        )
        if created.status_code not in (200, 201):
            return {
                "ok": False,
                "error": f"the branch is pushed but the pull request was refused "
                         f"({created.status_code}); a person can open it from {branch}",
            }
        url = created.json().get("html_url", "")
        logger.info("fix for #%s opened %s", issue, url)
        return {"ok": True, "url": url, "branch": branch,
                "files": len(changed_paths(patch)), "lines": changed_lines(patch)}

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "repo": settings.repo})

    return mcp


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    logger.info("mcp-pr up on %s:%d repo=%s base=%s",
                settings.host, settings.port, settings.repo, settings.base)
    build_server(settings).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_hosts),
        ),
    )


if __name__ == "__main__":
    main()
