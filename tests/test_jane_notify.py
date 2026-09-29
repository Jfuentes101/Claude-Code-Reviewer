"""jane.notify: a notice that arrives when Jane is there, and costs nothing when not."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from robbie import jane
from robbie.config import Config, RepoConfig, Secrets, SlackConfig


def _cfg(tmp: Path, sock: str) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0"), state_dir=tmp, jane_socket=sock,
        repos=[RepoConfig(slug="a/b", reviewer_login="r", bare=Path("/m"))],
    )


SECRETS = Secrets(gh_token="w", slack_bot_token="s", reviewer_gh_token="r",
                  jane_token=SecretStr("t0k"))


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="jn-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def test_off_unless_a_socket_is_configured(tmp_path):
    assert await jane.notify(_cfg(tmp_path, ""), SECRETS, "nothing happens") is False


async def test_a_jane_that_is_down_costs_a_warning(short_dir, caplog):
    cfg = _cfg(short_dir, str(short_dir / "nobody.sock"))
    assert await jane.notify(cfg, SECRETS, "hi") is False
    assert "could not tell jane" in caplog.text


async def test_the_notice_reaches_her_with_the_token(short_dir):
    seen: dict = {}

    async def notify(request: Request):
        seen["token"] = request.headers.get("x-jane-token")
        seen["body"] = await request.json()
        return JSONResponse({"delivered": True, "channel": "desktop"})

    sock = short_dir / "jane.sock"
    app = Starlette(routes=[Route("/notify", notify, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        sent = await jane.relay(
            _cfg(short_dir, str(sock)), SECRETS,
            "⚠️ <https://gh/pr/1|*#1*> held\nthe *reason*",
        )
    finally:
        server.should_exit = True
        await serving
    assert sent is True
    assert seen["token"] == "t0k"
    assert seen["body"]["title"] == "⚠️ #1 held"
    assert seen["body"]["url"] == "https://gh/pr/1"
    assert seen["body"]["body"] == "the reason"
    assert seen["body"]["source"] == "robbie"
