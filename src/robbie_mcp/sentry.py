"""Sentry read tools for the review model, as an MCP sidecar.

Why a sidecar and not stdio inside the reviewer image: the token stays here. The
reviewer runs a model with bypassPermissions, and a read-only Sentry token is
still a credential worth keeping out of that container.

Only GET requests are made. There is no tool here that resolves, assigns,
comments or mutates anything — the review model observes prod, it does not act
on it.

What this is for: a finding's severity often depends on whether the code being
touched is already failing in production. "You dropped the nil guard here" reads
differently when that line is throwing 4k times a day.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("robbie.mcp.sentry")

API = "https://sentry.io/api/0"
MAX_PATHS = 12
DEFAULT_DAYS = 14
Fetch = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Settings:
    token: str
    org: str
    projects: tuple[str, ...] = ()
    host: str = "0.0.0.0"
    port: int = 8080
    # the Host header a reviewer sends is the compose service name, which DNS
    # rebinding protection rejects unless it is listed
    allowed_hosts: tuple[str, ...] = ("mcp-sentry:8080", "mcp-sentry", "localhost:8080")

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("SENTRY_TOKEN", "").strip()
        org = os.environ.get("SENTRY_ORG_SLUG", "").strip()
        if not token or not org:
            raise SystemExit("SENTRY_TOKEN and SENTRY_ORG_SLUG are both required")
        raw_projects = os.environ.get("SENTRY_PROJECTS", "").strip()
        projects: tuple[str, ...] = ()
        if raw_projects:
            if raw_projects.startswith("["):
                projects = tuple(str(p) for p in json.loads(raw_projects))
            else:
                projects = tuple(p.strip() for p in raw_projects.split(",") if p.strip())
        hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
        return cls(
            token=token,
            org=org,
            projects=projects,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8080")),
            allowed_hosts=(
                tuple(h.strip() for h in hosts.split(",") if h.strip())
                if hosts
                else cls.allowed_hosts
            ),
        )


# ----- pure helpers ------------------------------------------------------


def path_query(path: str, *, exact: bool) -> str:
    """Issue search for code at `path`.

    Exact first, then the basename with a wildcard: Sentry indexes the filename
    as the stack trace spells it, which for bundled or relocated code is not the
    repo-relative path. Without the fallback the tool answers "nothing" for most
    real PRs, which is worse than over-matching and saying so.
    """
    if exact:
        return f'is:unresolved stack.filename:"{path}"'
    return f'is:unresolved stack.filename:"*{PurePosixPath(path).name}"'


def shape_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields that change a review decision, drop the rest.

    This lands in a review prompt alongside a diff, so it is deliberately small:
    what is broken, how often, since when, and a link.
    """
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "culprit": raw.get("culprit"),
        "level": raw.get("level"),
        "events": _int(raw.get("count")),
        "users": _int(raw.get("userCount")),
        "first_seen": raw.get("firstSeen"),
        "last_seen": raw.get("lastSeen"),
        "permalink": raw.get("permalink"),
    }


def shape_frames(event: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    """The innermost frames of the latest event, app frames first.

    Sentry orders frames outermost-first; the interesting ones are at the end.
    """
    entries = event.get("entries") or []
    frames: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") != "exception":
            continue
        for value in (entry.get("data") or {}).get("values") or []:
            for frame in ((value.get("stacktrace") or {}).get("frames") or []):
                frames.append({
                    "filename": frame.get("filename"),
                    "function": frame.get("function"),
                    "line": frame.get("lineNo"),
                    "in_app": bool(frame.get("inApp")),
                })
    in_app = [f for f in frames if f["in_app"]]
    chosen = in_app or frames
    return list(reversed(chosen[-limit:]))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ----- server ------------------------------------------------------------


def build_server(settings: Settings, fetch: Fetch | None = None) -> MCPServer:
    """Wire the tools. `fetch` is injectable so tests never touch the network."""
    call = fetch or _http_fetch(settings)

    def project_params() -> dict[str, Any]:
        return {"project": list(settings.projects)} if settings.projects else {}

    mcp: MCPServer = MCPServer(
        name="sentry",
        version="0.1.0",
        instructions=(
            "Read-only production error data from Sentry, for judging how risky a "
            "code change is. Nothing here can modify an issue."
        ),
    )

    @mcp.tool(
        description=(
            "Unresolved production errors whose stack traces touch any of these "
            "files. Call this with the paths a PR changes BEFORE deciding how "
            "severe a finding is: a missing guard on a line that is already "
            f"throwing in prod is not a nitpick. Checks at most {MAX_PATHS} paths "
            "and says so via `truncated`. Each path is tried as an exact stack "
            "filename first, then as a wildcard on the basename — `match` tells "
            "you which hit, and a wildcard match may be a different file with the "
            "same name, so read `culprit` before trusting it."
        )
    )
    async def issues_for_paths(
        paths: list[str], days: int = DEFAULT_DAYS, limit_per_path: int = 3
    ) -> dict[str, Any]:
        wanted = paths[:MAX_PATHS]
        results: list[dict[str, Any]] = []
        for path in wanted:
            found, match = [], "none"
            for exact in (True, False):
                raw = await call(
                    f"/organizations/{settings.org}/issues/",
                    {
                        "query": path_query(path, exact=exact),
                        "statsPeriod": f"{max(1, days)}d",
                        "limit": max(1, limit_per_path),
                        **project_params(),
                    },
                )
                if raw:
                    found = [shape_issue(i) for i in raw]
                    match = "exact" if exact else "basename-wildcard"
                    break
            if found:
                results.append({"path": path, "match": match, "issues": found})
        return {
            "checked": wanted,
            "truncated": len(paths) > MAX_PATHS,
            "window_days": days,
            "with_errors": results,
            "clean": [p for p in wanted if p not in {r["path"] for r in results}],
        }

    @mcp.tool(
        description=(
            "Search unresolved production issues with Sentry's own query syntax "
            '(e.g. `is:unresolved "NoMethodError"`, `stack.function:charge`, '
            "`release:1.2.3`). Use when a PR description or a diff mentions a "
            "specific error, endpoint or release and you want to know whether it "
            "is actually happening in prod."
        )
    )
    async def search_issues(
        query: str, days: int = DEFAULT_DAYS, limit: int = 10
    ) -> dict[str, Any]:
        raw = await call(
            f"/organizations/{settings.org}/issues/",
            {
                "query": query,
                "statsPeriod": f"{max(1, days)}d",
                "limit": max(1, min(limit, 25)),
                **project_params(),
            },
        )
        return {"query": query, "window_days": days, "issues": [shape_issue(i) for i in raw or []]}

    @mcp.tool(
        description=(
            "One issue in detail, plus the in-app stack frames of its latest "
            "event. Use after issues_for_paths or search_issues to see whether "
            "the failing frames are the code this PR is actually touching."
        )
    )
    async def issue_detail(issue_id: str) -> dict[str, Any]:
        issue = await call(f"/issues/{issue_id}/", {})
        if not issue:
            return {"error": f"issue {issue_id} not found or not visible to this token"}
        out = shape_issue(issue)
        out["metadata"] = issue.get("metadata") or {}
        event = await call(f"/issues/{issue_id}/events/latest/", {})
        out["latest_event"] = {
            "id": (event or {}).get("id"),
            "date": (event or {}).get("dateCreated"),
            "frames": shape_frames(event or {}),
        }
        return out

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "org": settings.org})

    return mcp


def _http_fetch(settings: Settings) -> Fetch:
    client = httpx.AsyncClient(
        base_url=API,
        headers={"Authorization": f"Bearer {settings.token}"},
        timeout=20.0,
    )

    async def fetch(path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as ex:
            # a tool that raises kills the turn; the model can reason about a
            # failed lookup, so hand it back as data
            logger.warning("sentry %s -> %s", path, ex.response.status_code)
            return []
        except Exception as ex:  # noqa: BLE001 — same reasoning
            logger.warning("sentry %s failed: %s", path, ex)
            return []

    return fetch


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    logger.info(
        "mcp-sentry up on %s:%d org=%s projects=%s",
        settings.host, settings.port, settings.org,
        ",".join(settings.projects) or "all",
    )
    build_server(settings).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        # every reviewer is a short-lived client, so there is no session worth
        # keeping across N concurrent ones
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_hosts),
        ),
    )


if __name__ == "__main__":
    main()
