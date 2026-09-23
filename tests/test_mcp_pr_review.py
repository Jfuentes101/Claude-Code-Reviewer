"""review_patch: a second opinion that is there when reviewq is, and never in the way."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from robbie_mcp.pr import Settings, build_server

PATCH = """\
diff --git a/test/thing_test.rb b/test/thing_test.rb
new file mode 100644
--- /dev/null
+++ b/test/thing_test.rb
@@ -0,0 +1 @@
+assert true
"""


class _Resp:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"state": "open", "labels": [{"name": "robbie-fix"}]}


class _Api:
    async def get(self, _url: str) -> _Resp:
        return _Resp()


@pytest.fixture
def short_dir():
    # AF_UNIX paths stop at 108 bytes, and pytest's tmp_path is longer than that
    d = Path(tempfile.mkdtemp(prefix="rq-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mirror(short_dir: Path) -> Path:
    src = short_dir / "src"
    src.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(src)]
    subprocess.run([*git[:5], "init", "-q", "-b", "main", str(src)], check=True)
    (src / "README").write_text("x\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    bare = short_dir / "mirror.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _settings(mirror: Path, **kw: Any) -> Settings:
    return Settings(token="t", repo="o/r", mirror=mirror, **kw)


async def _tools(settings: Settings) -> set[str]:
    async with Client(build_server(settings, api=_Api())) as c:
        return {t.name for t in (await c.list_tools()).tools}


async def test_no_reviewq_means_no_tool(mirror: Path):
    assert await _tools(_settings(mirror)) == {"open_pull_request"}


async def test_a_reviewq_that_is_down_does_not_stop_the_fix(mirror: Path, short_dir: Path):
    reviews = short_dir / "reviews"
    reviews.mkdir()
    settings = _settings(mirror, review_socket=short_dir / "nobody.sock", review_dir=reviews)
    async with Client(build_server(settings, api=_Api())) as c:
        out = (await c.call_tool("review_patch", {"issue": 1, "patch": PATCH})).structured_content
    assert out["review_unavailable"] is True
    assert list(reviews.iterdir()) == []


async def test_the_review_sees_the_patch_and_comes_back(mirror: Path, short_dir: Path):
    seen: dict[str, str] = {}
    fake = MCPServer(name="reviewq")

    @fake.tool()
    async def request_review(workspace: str, context: str = "", base_ref: str = "") -> dict[str, Any]:
        seen["diff"] = subprocess.run(
            ["git", "-C", workspace, "diff", "--cached", base_ref],
            capture_output=True, text=True, check=True,
        ).stdout
        seen["context"] = context
        return {"id": 7, "state": "queued"}

    @fake.tool()
    async def review_status(review_id: int) -> dict[str, Any]:
        return {"id": review_id, "state": "done", "verdict": "needs-work", "error": None}

    @fake.tool()
    async def review_results(review_id: int) -> dict[str, Any]:
        return {"id": review_id, "state": "done", "verdict": "needs-work",
                "summary": "s", "findings": [{"title": "f"}], "blocking": 1}

    sock = short_dir / "rq.sock"
    app = fake.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["reviewq"], allowed_origins=["reviewq"]
        ),
    )
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    reviews = short_dir / "reviews"
    reviews.mkdir()
    try:
        settings = _settings(mirror, review_socket=sock, review_dir=reviews)
        async with Client(build_server(settings, api=_Api())) as c:
            out = (await c.call_tool(
                "review_patch", {"issue": 1, "patch": PATCH, "context": "why"}
            )).structured_content
    finally:
        server.should_exit = True
        await serving
    assert out["ok"] is True and out["verdict"] == "needs-work" and out["blocking"] == 1
    assert "+assert true" in seen["diff"] and seen["context"] == "why"
    assert list(reviews.iterdir()) == [], "the workspace outlived its review"
