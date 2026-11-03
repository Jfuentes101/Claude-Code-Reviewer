"""The read-only panel. It serves files from disk, so the path guard is the part
that matters; the rest must render from whatever state exists, including none."""

from __future__ import annotations

import threading
from functools import partial
from http.server import ThreadingHTTPServer

import httpx
import pytest

from robbie import budget, export
from robbie.config import Config, RepoConfig, ReviewModel, SlackConfig
from robbie.dashboard import PAGES, _Handler, _transcript_body, render
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
    pages = {path: render(cfg, db, path) for path in PAGES}
    every = "".join(pages.values())
    for section in ("system", "usage", "models", "reviews", "transcripts"):
        assert f">{section}</h2>" in every
    assert "nothing recorded yet" in every
    assert "nothing is waiting on you" in pages["/"]


def test_an_unknown_path_is_not_a_page(cfg, db):
    assert render(cfg, db, "/nope") is None


def test_every_page_links_every_other(cfg, db):
    page = render(cfg, db, "/fixes")
    for path in PAGES:
        assert f'href="{path}"' in page
    assert 'href="/fixes" class=on' in page


def test_a_recorded_review_shows_up(cfg, db):
    db.start_review(key="k", repo="acme/app", pr=7, head_sha="abc1234567", requested_at="t")
    db.finish_review(
        "k", state="published", verdict="needs-work", model="glm-5.2:cloud",
        findings=4, blocking=2, should_fix=1, inline=3, summary_findings=7, duration_s=121.0,
        tokens_out=10853,
    )
    page = render(cfg, db, "/reviews")
    assert ">#7</a>" in page
    assert "glm-5.2:cloud" in page
    assert "needs-work" in page
    assert "121s" in page


def test_the_arms_show_their_configured_share(cfg, db):
    page = render(cfg, db, "/system")
    assert "67%" in page and "33%" in page, "two-to-one is what the weights say"


def test_a_title_from_github_cannot_inject_markup(cfg, db):
    """Everything on this page came from a PR, a model or a filename."""
    db.start_review(key="k", repo="acme/app", pr=7, head_sha="abc", requested_at="t")
    db.finish_review("k", state="held", hold_reason="<script>alert(1)</script>")
    page = render(cfg, db, "/reviews")
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


def test_the_panel_shows_the_stored_meter_without_asking_the_provider(cfg, db, monkeypatch):
    """A page that refreshes every 30s used to fetch on render, in its own process
    with its own cache, and spent the rate limit the daemon's gate depends on."""
    monkeypatch.setattr(budget.httpx, "get", lambda *a, **k: pytest.fail("asked a provider"))
    cfg.backend = "oauth"
    db.write_meter(budget.PLAN, pct=42.0, note="2026-08-04T19:10:00Z")

    assert "plan window at 42%" in render(cfg, db, "/system")
    assert "plan window at 42%" in render(cfg, db), "the summary card shows it too"


def test_it_answers_over_http(cfg, db):
    """One pass through the real handler: routing, 404s and the response headers."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, cfg))
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
        assert httpx.get(base + "/fixes", timeout=10).status_code == 200
        assert httpx.get(base + "/nope", timeout=10).status_code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_pr_cells_link_to_github(cfg, db):
    db.start_review(key="k", repo="acme/app", pr=10389, head_sha="abc", requested_at="t")
    db.finish_review("k", state="published", verdict="ok", model="sonnet")
    page = render(cfg, db, "/reviews")
    assert '<a href="https://github.com/acme/app/pull/10389"' in page
    assert '>#10389</a>' in page and 'title="acme/app"' in page


def test_a_held_pr_links_too(cfg, db):
    db.record_hold(key="h", repo="acme/app", pr=42, head_sha="abc",
                   requested_at="t", reason="nothing new pushed")
    assert 'href="https://github.com/acme/app/pull/42"' in render(cfg, db, "/reviews")


def test_an_approval_shows_who_made_it_and_what_ci_said(cfg, db):
    for pr_num, model, state in ((1, "glm-5.2:cloud", "green"),
                                 (2, "sonnet", "waiting"),
                                 (3, "qwen3.5:397b-cloud", "red")):
        key = f"k{pr_num}"
        db.start_review(key=key, repo="acme/app", pr=pr_num, head_sha="abc", requested_at="t")
        db.finish_review(key, state="published", verdict="ok", model=model, ci_state=state)
    db.set_requested("acme/app", (1, 2, 3))
    page = render(cfg, db, "/approved")
    assert "ready for a human" in page
    assert "waiting on the build" in page and "build went red" in page
    assert "glm-5.2:cloud" in page and "sonnet" in page


def test_a_third_party_approval_is_marked_as_such(cfg, db):
    """He is checking the non-Anthropic ones personally, so they have to stand out."""
    db.start_review(key="k", repo="acme/app", pr=1, head_sha="abc", requested_at="t")
    db.finish_review("k", state="published", verdict="ok",
                     model="glm-5.2:cloud", ci_state="green")
    db.set_requested("acme/app", [1])
    assert "3rd-party" in render(cfg, db, "/approved")


def test_an_account_approval_is_not_marked(cfg, db):
    db.start_review(key="k", repo="acme/app", pr=1, head_sha="abc", requested_at="t")
    db.finish_review("k", state="published", verdict="ok", model="sonnet", ci_state="green")
    assert "3rd-party" not in render(cfg, db, "/approved")


async def test_the_panel_process_never_opens_a_writable_connection(tmp_path, monkeypatch):
    """serve() blocks forever, so a Db built before it holds a write connection for
    the life of the process — and migrates the schema onto a fresh volume."""
    import yaml

    from robbie import main as main_mod

    (tmp_path / "app.git").mkdir()
    conf = tmp_path / "robbie.yaml"
    conf.write_text(yaml.safe_dump({
        "slack": {"owner_id": "U0"},
        "state_dir": str(tmp_path / "state"),
        "repos": [{
            "slug": "acme/app", "reviewer_login": "rev", "bare": str(tmp_path / "app.git"),
        }],
    }))
    for name in ("GH_TOKEN", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "x")
    served: list[int] = []
    monkeypatch.setattr(main_mod.dashboard, "serve", lambda *a, **k: served.append(1))

    args = main_mod._parser().parse_args(["--config", str(conf), "dashboard"])
    assert await main_mod._run(args) == 0
    assert served == [1]
    assert not (tmp_path / "state" / "robbie.db").exists(), "the panel created the DB"


def test_the_fixes_and_assigned_issues_come_from_the_export(cfg, db):
    export.write_json(export.export_dir(cfg) / "worklist.json", {
        "written_at": 0,
        "fixes": [{"repo": "acme/app", "number": 117, "issue": 116, "title": "a fix",
                   "status": "draft", "ci": "pending", "opened_at": 0}],
        "triage_assigned": [{"repo": "acme/app", "number": 65, "title": "a bug",
                             "labels": ["bug", "robbie-fix"], "updated_at": 0,
                             "prs": [{"number": 66, "status": "code review"}]},
                            {"repo": "acme/app", "number": 90, "title": "<b>x</b>",
                             "labels": [], "updated_at": 0, "prs": []}],
    })
    page = render(cfg, db, "/fixes")
    assert 'href="https://github.com/acme/app/pull/117"' in page
    assert 'href="https://github.com/acme/app/issues/116"' in page
    page = render(cfg, db, "/issues")
    assert 'href="https://github.com/acme/app/issues/65"' in page
    assert 'href="https://github.com/acme/app/pull/66"' in page
    assert "code review" in page and "bug, robbie-fix" in page and ">none<" in page
    assert "<b>x</b>" not in page


def test_without_an_export_the_sections_say_how_to_get_one(cfg, db):
    assert "export_interval_s" in render(cfg, db, "/fixes")
    assert "export_interval_s" in render(cfg, db, "/issues")
    assert "export_interval_s" in render(cfg, db)


def test_the_summary_lists_what_waits_on_the_operator(cfg, db):
    """A green approval and a fix draft both need a human; a red build does not yet."""
    for n, state in ((1, "green"), (2, "red")):
        db.start_review(key=f"k{n}", repo="acme/app", pr=n, head_sha="abc", requested_at="t")
        db.finish_review(f"k{n}", state="published", verdict="ok", ci_state=state)
    db.set_requested("acme/app", (1, 2))
    export.write_json(export.export_dir(cfg) / "worklist.json", {
        "written_at": 0,
        "awaiting_you": [{"repo": "acme/app", "number": 1, "title": "the approved one"}],
        "fixes": [
            {"repo": "acme/app", "number": 3, "issue": 9, "title": "the draft",
             "status": "draft", "opened_at": 0},
            {"repo": "acme/app", "number": 4, "issue": 8, "title": "in review",
             "status": "code review", "opened_at": 0},
        ],
        "triage_assigned": [],
    })
    page = render(cfg, db)
    waiting = page.split("waiting on you</h2>")[1]
    assert "the approved one" in waiting and "fix draft for #9" in waiting
    assert ">#2</a>" not in waiting and "in review" not in waiting
    assert 'href="/fixes"' in page and "1 draft(s) to look at" in page


def test_the_issues_filter_on_whether_a_pull_request_closes_them(cfg, db):
    export.write_json(export.export_dir(cfg) / "worklist.json", {
        "written_at": 0,
        "triage_assigned": [
            {"repo": "acme/app", "number": 1, "title": "has one", "labels": [],
             "updated_at": 0, "prs": [{"number": 2, "status": "draft"}]},
            {"repo": "acme/app", "number": 3, "title": "has none", "labels": [],
             "updated_at": 0, "prs": []},
        ],
    })
    every = render(cfg, db, "/issues")
    assert "has one" in every and "has none" in every
    assert ">all 2</a>" in every and ">with a pull request 1</a>" in every
    with_pr = render(cfg, db, "/issues", {"pr": "with"})
    assert "has one" in with_pr and "has none" not in with_pr
    assert 'href="/issues?pr=with" class=on' in with_pr
    without = render(cfg, db, "/issues", {"pr": "without"})
    assert "has none" in without and "has one" not in without
    assert "has none" in render(cfg, db, "/issues", {"pr": "junk"}), "unknown means all"
