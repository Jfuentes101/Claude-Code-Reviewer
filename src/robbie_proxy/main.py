"""The model credentials live here instead of in the reviewer.

A reviewer runs a model with bypassPermissions over a PR's own code, so anything
in that container is readable by whatever the PR can talk the model into running.
The GitHub token is the cheap half — read-only, and opening the PR is what started
the review. The expensive half is the model credential: a bearer key with spend
behind it, and on `backend=oauth` a whole subscription session, refresh token and
all.

So the container gets `ANTHROPIC_BASE_URL` pointed here plus a token worth nothing
off the compose network, and the real credential goes on the request on its way
past.

Two arms, picked by the first path segment: `/account` for the account's own
backend and `/endpoint` for REVIEW_BASE_URL. The CLI preserves a base URL's path
prefix, so that is the whole of the routing.

A sidecar rather than a task inside the orchestrator, which holds all of these
secrets already: that process also holds the docker socket, and a listener
reachable by the containers it spawns does not belong in it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as secretslib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("robbie.proxy")

ACCOUNT_UPSTREAM = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
# hop-by-hop, plus the two we replace. `accept-encoding` is deliberately NOT here:
# the CLI and the upstream negotiate compression end to end and the body goes back
# raw. Strip it and the CLI reports "Failed to parse JSON" with nothing else to go on.
DROP_REQUEST = frozenset({
    "host", "authorization", "x-api-key", "content-length", "connection",
    "transfer-encoding",
})
DROP_RESPONSE = frozenset({"content-length", "transfer-encoding"})

# The model surface the CLI actually calls, and nothing else: an OAuth session also
# opens the account's usage and profile endpoints, an API key the organization ones.
DEFAULT_PATHS = ("v1/messages", "v1/models")
# a refusal reaches the run as an SDK error, so it should say which kind it was
ERROR_TYPE = {401: "authentication_error", 403: "permission_error"}


@dataclass(frozen=True)
class Settings:
    token: str  # what a reviewer must present; minted by whoever runs the fleet
    credentials: Path | None = None  # backend=oauth, the account's own session
    api_key: str | None = None  # backend=api, and the one that never expires
    review_base_url: str | None = None
    review_api_token: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    # MODEL_PROXY_PATHS widens this without a rebuild: a CLI that starts calling
    # somewhere new would otherwise fail every review until this file changes
    paths: tuple[str, ...] = DEFAULT_PATHS

    @property
    def serves_account(self) -> bool:
        return self.credentials is not None or self.api_key is not None

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("MODEL_PROXY_TOKEN", "").strip()
        if not token:
            raise SystemExit("MODEL_PROXY_TOKEN is not set")
        creds = os.environ.get("CLAUDE_CREDENTIALS", "").strip()
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        base = os.environ.get("REVIEW_BASE_URL", "").strip()
        endpoint_key = os.environ.get("REVIEW_API_TOKEN", "").strip()
        if not creds and not key and not base:
            raise SystemExit(
                "nothing to proxy: set CLAUDE_CREDENTIALS, ANTHROPIC_API_KEY, "
                "REVIEW_BASE_URL, or any of them"
            )
        return cls(
            token=token,
            credentials=Path(creds) if creds else None,
            api_key=key or None,
            review_base_url=base.rstrip("/") or None,
            review_api_token=endpoint_key or None,
            host=os.environ.get("MODEL_PROXY_HOST", "0.0.0.0"),
            port=int(os.environ.get("MODEL_PROXY_PORT", "8080")),
            paths=_paths(os.environ.get("MODEL_PROXY_PATHS", "")),
        )


def _paths(raw: str) -> tuple[str, ...]:
    listed = tuple(p.strip().strip("/") for p in raw.split(",") if p.strip().strip("/"))
    return listed or DEFAULT_PATHS


class Denied(Exception):
    """Why this request cannot be forwarded, in words a reviewer log can show.

    401 for a token we did not mint, 403 for a path we forward for nobody. Anything
    that is this proxy's own problem goes back as 502.
    """

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def account_bearer(path: Path) -> str:
    """The account's current OAuth access token, read fresh on every request.

    ponytail: no refresh flow. The host's own CLI refreshes the file and re-reading
    it is how that arrives, which also means one process touches it instead of N
    containers. On a host where nobody runs `claude` interactively it goes stale
    within hours and every review fails loudly; that is the day this needs one.

    Re-reading only arrives if the path resolves to the *live* file. The CLI renews
    by renaming a new one over the old, so a container given a bind mount of the
    file itself is pinned to the replaced inode and re-reads a corpse forever —
    mount the directory. An expired token whose file the host has already unlinked
    is exactly that, and worth saying out loud: it looks identical to a host nobody
    has logged into, and the cure is the opposite one.
    """
    try:
        oauth = json.loads(path.read_text())["claudeAiOauth"]
    except (OSError, KeyError, json.JSONDecodeError) as ex:
        raise Denied(f"could not read the account credentials: {ex}", 502) from ex
    left = float(oauth.get("expiresAt", 0)) / 1000 - time.time()
    if left <= 0:
        raise Denied(
            f"the account's access token expired {-left / 60:.0f} min ago and this "
            f"proxy does not refresh it; {_stale_hint(path)}", 502
        )
    return str(oauth["accessToken"])


def _stale_hint(path: Path) -> str:
    """Whether this container is holding a file the host has already replaced."""
    try:
        replaced = path.stat().st_nlink == 0
    except OSError:
        replaced = False
    if replaced:
        return (
            f"the host has already replaced {path} and this container still has the "
            "old one — bind-mount the directory, not the file, and restart"
        )
    return "run `claude` on the host to renew it"


def merge_beta(existing: str, wanted: str) -> str:
    """Add a beta flag to whatever the CLI already asked for.

    Replacing the header drops the flags the run depends on, and that surfaces as a
    model quietly behaving like a different one.
    """
    flags = [f for f in re.split(r"\s*,\s*", existing) if f]
    if wanted not in flags:
        flags.insert(0, wanted)
    return ",".join(flags)


def authorize(header: str, expected: str) -> None:
    offered = header[7:] if header.lower().startswith("bearer ") else header
    if not secretslib.compare_digest(offered, expected):
        raise Denied("this token is not the one the fleet was given")


def allow_path(path: str, allowed: tuple[str, ...]) -> str:
    """The upstream path to forward, or Denied.

    Forwarding whatever it is handed would give MODEL_PROXY_TOKEN the run of every
    endpoint the credential behind it reaches. `..` is refused rather than resolved,
    since this compares prefixes.
    """
    clean = path.strip("/")
    if ".." in clean.split("/"):
        raise Denied("a path segment of '..' is not forwarded", 403)
    if not any(clean == p or clean.startswith(f"{p}/") for p in allowed):
        raise Denied(
            f"this proxy forwards {', '.join(allowed)} only, not /{clean}. Set "
            "MODEL_PROXY_PATHS if the CLI legitimately needs it",
            403,
        )
    return clean


class Upstream(NamedTuple):
    base: str
    auth: dict[str, str]
    beta: str | None = None  # a flag to merge into whatever the CLI already asked for


def upstream_for(arm: str, settings: Settings) -> Upstream:
    """Where an arm's traffic goes, and what authenticates it there.

    The account arm has two shapes and they are not interchangeable: an OAuth session
    is a `Bearer` plus a beta flag and expires; an API key is `x-api-key` and does
    not, which is what a host nobody logs into wants.
    """
    if arm == "account":
        if settings.credentials is not None:
            return Upstream(
                ACCOUNT_UPSTREAM,
                {"authorization": f"Bearer {account_bearer(settings.credentials)}"},
                OAUTH_BETA,
            )
        if settings.api_key is not None:
            return Upstream(ACCOUNT_UPSTREAM, {"x-api-key": settings.api_key})
        raise Denied("no account credentials are configured on this proxy", 502)
    if arm == "endpoint":
        if not settings.review_base_url:
            raise Denied("no REVIEW_BASE_URL is configured on this proxy", 502)
        return Upstream(
            settings.review_base_url,
            {"authorization": f"Bearer {settings.review_api_token or ''}"},
        )
    raise Denied(f"unknown arm {arm!r}; expected /account or /endpoint", 502)


def build_app(settings: Settings, client: httpx.AsyncClient | None = None) -> Starlette:
    """Wire the routes. `client` is injectable so tests never touch the network."""
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))

    async def healthz(_request: Request) -> JSONResponse:
        arms = [
            name for name, on in
            (("account", settings.serves_account),
             ("endpoint", bool(settings.review_base_url)))
            if on
        ]
        return JSONResponse({"status": "ok", "arms": arms})

    async def hello(request: Request) -> JSONResponse:
        """The CLI's reachability probe, which it sends with no credential.

        Answered rather than forwarded: refusing it logs a warning that reads as a
        stolen token on every single review.
        """
        return JSONResponse({"ok": True})

    async def forward(request: Request) -> Response:
        arm = request.path_params["arm"]
        try:
            authorize(request.headers.get("authorization", ""), settings.token)
            rest = allow_path(request.path_params["path"], settings.paths)
            up = upstream_for(arm, settings)
        except Denied as ex:
            logger.warning("refused %s %s: %s", request.method, request.url.path, ex)
            # the shape the SDK expects, so the reason reaches the run's transcript
            # instead of surfacing as an unexplained parse failure
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": ERROR_TYPE.get(ex.status, "api_error"), "message": str(ex)},
                },
                status_code=ex.status,
            )

        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST
        }
        headers.update(up.auth)
        if up.beta:
            headers["anthropic-beta"] = merge_beta(headers.get("anthropic-beta", ""), up.beta)

        # the arm is this proxy's own routing; upstream never sees it
        target = f"{up.base}/{rest}" if rest else up.base
        outbound = http.build_request(
            request.method, target,
            params=dict(request.query_params),
            headers=headers,
            content=await request.body(),
        )
        try:
            resp = await http.send(outbound, stream=True)
        except httpx.HTTPError as ex:
            logger.warning("upstream %s unreachable: %s", up.base, ex)
            return JSONResponse(
                {"type": "error", "error": {"type": "api_error", "message": str(ex)}},
                status_code=502,
            )

        async def release() -> None:
            await resp.aclose()

        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers={
                k: v for k, v in resp.headers.items() if k.lower() not in DROP_RESPONSE
            },
            background=BackgroundTask(release),
        )

    async def verify(request: Request) -> Response:
        """Does this token work — asked without spending anything upstream.

        The orchestrator asks at boot; see `runner.check_model_proxy`.
        """
        try:
            authorize(request.headers.get("authorization", ""), settings.token)
        except Denied as ex:
            return JSONResponse({"ok": False, "detail": str(ex)}, status_code=ex.status)
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/healthz", healthz),
        Route("/verify", verify),
        Route("/{arm}/api/hello", hello, methods=["GET", "HEAD"]),
        Route(
            "/{arm}/{path:path}", forward,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        ),
    ])


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MODEL_PROXY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    if settings.credentials is not None and settings.api_key is not None:
        logger.warning(
            "both an oauth session and an API key are configured; the session wins. "
            "Unset CLAUDE_CREDENTIALS to serve the account arm with the key instead"
        )
    if settings.credentials is not None:
        try:
            account_bearer(settings.credentials)
        except Denied as ex:
            # at boot, not on the first review: a stale mount is a deployment
            # mistake, and finding out 10 minutes later costs a paid-for run
            print(f"model-proxy: {ex}", file=sys.stderr)
            raise SystemExit(1) from ex
    account = (
        "oauth session" if settings.credentials is not None
        else "api key" if settings.api_key else "off"
    )
    logger.info(
        "model-proxy up on %s:%d — account=%s endpoint=%s forwarding=%s",
        settings.host, settings.port, account, settings.review_base_url or "off",
        ", ".join(settings.paths),
    )
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
