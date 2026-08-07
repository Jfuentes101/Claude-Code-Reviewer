"""The model credentials live here instead of in the reviewer.

A reviewer runs a model with bypassPermissions over a PR's own code, so anything
in that container is readable by whatever the PR can talk the model into running.
The keys that matter are not GitHub's — a read-only token buys an attacker what
they already have, since opening the PR is what started the review. It is the
model credentials: a bearer key with spend behind it, and on `backend=oauth` a
whole subscription session, refresh token and all, mounted read-write.

So the container gets `ANTHROPIC_BASE_URL` pointed here and a token that is worth
nothing outside the compose network, and the real credential goes on the request
here, on its way past. Stealing what the container holds buys the length of one
review on a network the thief is not on.

Two arms, one per upstream, picked by the first path segment: `/account` for the
account's own backend and `/endpoint` for REVIEW_BASE_URL. The CLI preserves a
base URL's path prefix, so that is the whole of the routing.

A sidecar rather than a task inside the orchestrator, which already holds every
one of these secrets: that process also holds the docker socket, which is root on
the host, and a listener reachable by the containers it spawns does not belong in
it.
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
# raw, so this never has to understand a payload. Strip it and forward the raw
# bytes anyway and the CLI reports "Failed to parse JSON" with nothing else to go on.
DROP_REQUEST = frozenset({
    "host", "authorization", "x-api-key", "content-length", "connection",
    "transfer-encoding",
})
DROP_RESPONSE = frozenset({"content-length", "transfer-encoding"})


@dataclass(frozen=True)
class Settings:
    token: str  # what a reviewer must present; minted by whoever runs the fleet
    credentials: Path | None = None  # backend=oauth, the account's own session
    api_key: str | None = None  # backend=api, and the one that never expires
    review_base_url: str | None = None
    review_api_token: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080

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
        )


class Denied(Exception):
    """Why this request cannot be forwarded, in words a reviewer log can show.

    401 is only for a token we did not mint — the caller's problem, and retrying
    it cannot help. Everything else here is this proxy's own problem, so it goes
    back as 502 and says which.
    """

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def account_bearer(path: Path) -> str:
    """The account's current OAuth access token, read fresh on every request.

    ponytail: no refresh flow here. The host's own CLI refreshes this file, and
    re-reading it is how that arrives — which also ends the race `runner` used to
    document, since one process now touches the file instead of N containers. On a
    host where nobody runs `claude` interactively it goes stale within hours and
    every review fails loudly; that is the day this needs to refresh for itself.
    """
    try:
        oauth = json.loads(path.read_text())["claudeAiOauth"]
    except (OSError, KeyError, json.JSONDecodeError) as ex:
        raise Denied(f"could not read the account credentials: {ex}", 502) from ex
    left = float(oauth.get("expiresAt", 0)) / 1000 - time.time()
    if left <= 0:
        raise Denied(
            f"the account's access token expired {-left / 60:.0f} min ago and this "
            "proxy does not refresh it; run `claude` on the host to renew it", 502
        )
    return str(oauth["accessToken"])


def merge_beta(existing: str, wanted: str) -> str:
    """Add a beta flag to whatever the CLI already asked for.

    Replacing the header instead of merging drops the flags the run depends on —
    the CLI sends eight of them, versioned — and the failure surfaces as a model
    that quietly behaves like a different one.
    """
    flags = [f for f in re.split(r"\s*,\s*", existing) if f]
    if wanted not in flags:
        flags.insert(0, wanted)
    return ",".join(flags)


def authorize(header: str, expected: str) -> None:
    offered = header[7:] if header.lower().startswith("bearer ") else header
    if not secretslib.compare_digest(offered, expected):
        raise Denied("this token is not the one the fleet was given")


class Upstream(NamedTuple):
    base: str
    auth: dict[str, str]
    beta: str | None = None  # a flag to merge into whatever the CLI already asked for


def upstream_for(arm: str, settings: Settings) -> Upstream:
    """Where an arm's traffic goes, and what authenticates it there.

    The account arm has two shapes and they are not interchangeable: an OAuth
    session is a `Bearer` plus a beta flag and expires; an API key is `x-api-key`
    and does not. Which one is here is which one the deployment has — a VPS that
    nobody logs into wants the key precisely because it never needs refreshing.
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

        Answered here rather than forwarded: it needs no upstream and carries no
        auth, so refusing it logged a warning that read as a stolen token on every
        single review — the CLI ignores the 401 and carries on regardless.
        """
        return JSONResponse({"ok": True})

    async def forward(request: Request) -> Response:
        arm = request.path_params["arm"]
        try:
            authorize(request.headers.get("authorization", ""), settings.token)
            up = upstream_for(arm, settings)
        except Denied as ex:
            logger.warning("refused %s %s: %s", request.method, request.url.path, ex)
            # the shape the SDK expects, so the reason reaches the run's transcript
            # instead of surfacing as an unexplained parse failure
            return JSONResponse(
                {"type": "error", "error": {"type": "authentication_error", "message": str(ex)}},
                status_code=ex.status,
            )

        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST
        }
        headers.update(up.auth)
        if up.beta:
            headers["anthropic-beta"] = merge_beta(headers.get("anthropic-beta", ""), up.beta)

        # the arm is this proxy's own routing; upstream never sees it
        rest = request.path_params["path"]
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

        The orchestrator asks at boot. Without it a wrong token is invisible until
        a review runs, and then it is not even a fast failure: the CLI retries a
        401 until the container hits `docker.timeout_s`, so every PR burns the full
        30 minutes and lands in the database as a timeout.
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
        "model-proxy up on %s:%d — account=%s endpoint=%s",
        settings.host, settings.port, account, settings.review_base_url or "off",
    )
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
