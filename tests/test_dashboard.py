"""The read-only panel. It serves files from disk, so the path guard is the part
that matters; the rest must render from whatever state exists, including none."""

from __future__ import annotations

import threading
from functools import partial
from http.server import ThreadingHTTPServer

import httpx
import pytest

from robbie.config import Config, RepoConfig, ReviewModel, SlackConfig
from robbie.dashboard import _Handler, _transcript_body, render
from robbie.db import Db


@pytest.fixture
def cfg(tmp_path) -> Config:
    (tmp_path / "reviews").mkdir()
    (tmp_path / "mirror.git").mkdir()
    return Config(
        slack=SlackConfig(owner_id="U0"),
        repos=[RepoConfig(slug="acme/app", reviewer_login="rev", bare=tmp_path / "mirror.git")],
        state_dir=tmp_path,
        review_models=[
            ReviewModel(model="glm-5.2:cloud", via="endpoint", weight=2),
            ReviewModel(model="sonnet", weight=1),
        ],
    )


@pytest.fixture
def db(cfg) -> Db:
    d = Db(cfg.db_path)
    yield d
    d.close()


def test_an_empty_state_still_renders(cfg, db):
    page = render(cfg, None, db)
    for section in ("system", "usage", "models", "reviews", "transcripts"):
        assert f">{section}</h2>" in page
    assert "nothing recorded yet" in page


def test_a_recorded_review_shows_up(cfg, db):
    db.start_review(key="k", repo="acme/app", pr=7, head_sha="abc1234567", requested_at="t")
    db.finish_review(
        "k", state="published", verdict="needs-work", model="glm-5.2:cloud",
        findings=4, blocking=2, should_fix=1, inline=3, summary_findings=7, duration_s=121.0,
        tokens_out=10853,
    )
    page = render(cfg, None, db)
    assert "acme/app#7" in page
    assert "glm-5.2:cloud" in page
    assert "needs-work" in page
    assert "121s" in page


def test_the_arms_show_their_configured_share(cfg, db):
    page = render(cfg, None, db)
    assert "67%" in page and "33%" in page, "two-to-one is what the weights say"


def test_a_title_from_github_cannot_inject_markup(cfg, db):
    """Everything on this page came from a PR, a model or a filename."""
    db.start_review(key="k", repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review("k", state="held", hold_reason="<script>alert(1)</script>")
    page = render(cfg, None, db)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "/etc/passwd",
    "sub/../../outside.md",
    "",
])
def test_a_name_outside_the_transcript_directory_is_refused(cfg, name):
    assert _transcript_body(cfg, name) is None


def test_a_transcript_in_the_directory_is_served(cfg):
    (cfg.transcript_dir / "app-7-abc.md").write_text("the review", encoding="utf-8")
    assert _transcript_body(cfg, "app-7-abc.md") == "the review"


def test_only_transcript_suffixes_are_served(cfg):
    (cfg.transcript_dir / "robbie.db").write_text("not yours", encoding="utf-8")
    assert _transcript_body(cfg, "robbie.db") is None


def test_the_secrets_are_optional_so_the_panel_runs_without_them(cfg, db):
    assert "meters unread" in render(cfg, None, db)


def test_it_answers_over_http(cfg, db):
    """One pass through the real handler: routing, 404s and the response headers."""
    secrets = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, cfg, secrets))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page = httpx.get(base + "/", timeout=10)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "robbie" in page.text

        (cfg.transcript_dir / "app-7-abc.md").write_text("hello", encoding="utf-8")
        assert "hello" in httpx.get(base + "/transcript?name=app-7-abc.md", timeout=10).text
        assert httpx.get(base + "/transcript?name=../robbie.db", timeout=10).status_code == 404
        assert httpx.get(base + "/nope", timeout=10).status_code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_pr_cells_link_to_github(cfg, db):
    db.start_review(key="k", repo="acme/app", pr=10389, head_sha="abc", requested_at="t")
    db.finish_review("k", state="published", verdict="ok", model="sonnet")
    page = render(cfg, None, db)
    assert '<a href="https://github.com/acme/app/pull/10389"' in page
    assert ">acme/app#10389</a>" in page


def test_a_held_pr_links_too(cfg, db):
    db.record_hold(key="h", repo="acme/app", pr=42, head_sha="abc",
                   requested_at="t", reason="nothing new pushed")
    assert 'href="https://github.com/acme/app/pull/42"' in render(cfg, None, db)
