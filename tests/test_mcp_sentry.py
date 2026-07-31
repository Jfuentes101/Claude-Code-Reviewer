"""The Sentry sidecar. Every test drives the real tool functions through the
MCP server with a fake fetch, so the wiring is covered without a token."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robbie_mcp.sentry import (
    MAX_PATHS,
    Settings,
    build_server,
    path_query,
    shape_frames,
    shape_issue,
)

ISSUE = {
    "id": "42", "title": "NoMethodError: undefined method `charge'",
    "culprit": "app/models/payment.rb in charge", "level": "error",
    "count": "4213", "userCount": 87, "firstSeen": "2026-07-01T00:00:00Z",
    "lastSeen": "2026-07-30T00:00:00Z", "permalink": "https://sentry.io/x/42/",
    "extra": "dropped",
}


def settings(**kw) -> Settings:
    return Settings(token="t", org="acme", **kw)


class FakeSentry:
    """Records every request and answers from a path→payload map."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, path: str, params: dict) -> Any:
        self.calls.append((path, params))
        for key, value in self.answers.items():
            if key in json.dumps(params) or key == path:
                return value
        return []

    @property
    def queries(self) -> list[str]:
        return [p.get("query", "") for _, p in self.calls]


async def call_tool(server, name: str, args: dict) -> dict:
    """Invoke a tool the way a client does, and read back what the model sees."""
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# ----- pure helpers -----------------------------------------------------


def test_exact_query_uses_the_full_path():
    assert path_query("app/models/payment.rb", exact=True) == (
        'is:unresolved stack.filename:"app/models/payment.rb"'
    )


def test_fallback_query_wildcards_the_basename():
    assert path_query("app/models/payment.rb", exact=False) == (
        'is:unresolved stack.filename:"*payment.rb"'
    )


def test_shaping_keeps_the_decision_fields_and_drops_the_rest():
    out = shape_issue(ISSUE)
    assert out["events"] == 4213 and out["users"] == 87
    assert out["culprit"] == "app/models/payment.rb in charge"
    assert "extra" not in out


def test_shaping_survives_missing_counts():
    out = shape_issue({"id": "1", "count": None})
    assert out["events"] == 0 and out["users"] == 0


def test_frames_prefer_in_app_and_come_innermost_first():
    event = {"entries": [{"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
        {"filename": "gems/rack.rb", "function": "call", "lineNo": 1, "inApp": False},
        {"filename": "app/a.rb", "function": "outer", "lineNo": 10, "inApp": True},
        {"filename": "app/b.rb", "function": "inner", "lineNo": 20, "inApp": True},
    ]}}]}}]}
    frames = shape_frames(event)
    assert [f["function"] for f in frames] == ["inner", "outer"]


def test_frames_fall_back_to_library_frames_when_nothing_is_in_app():
    event = {"entries": [{"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
        {"filename": "gems/rack.rb", "function": "call", "lineNo": 1, "inApp": False},
    ]}}]}}]}
    assert [f["function"] for f in shape_frames(event)] == ["call"]


def test_frames_on_an_event_without_a_stacktrace_are_empty():
    assert shape_frames({}) == []
    assert shape_frames({"entries": [{"type": "message"}]}) == []


# ----- issues_for_paths -------------------------------------------------


async def test_exact_match_short_circuits_the_wildcard():
    fake = FakeSentry({"app/models/payment.rb": [ISSUE]})
    out = await call_tool(
        build_server(settings(), fake), "issues_for_paths",
        {"paths": ["app/models/payment.rb"]},
    )
    assert out["with_errors"][0]["match"] == "exact"
    assert out["with_errors"][0]["issues"][0]["events"] == 4213
    assert len(fake.calls) == 1, "an exact hit must not spend a second query"


async def test_the_wildcard_runs_only_when_the_exact_query_is_empty():
    fake = FakeSentry({"*payment.rb": [ISSUE]})
    out = await call_tool(
        build_server(settings(), fake), "issues_for_paths",
        {"paths": ["app/models/payment.rb"]},
    )
    assert out["with_errors"][0]["match"] == "basename-wildcard"
    assert len(fake.calls) == 2


async def test_files_with_no_prod_errors_are_reported_as_clean():
    out = await call_tool(
        build_server(settings(), FakeSentry()), "issues_for_paths",
        {"paths": ["app/models/quiet.rb"]},
    )
    assert out["with_errors"] == []
    assert out["clean"] == ["app/models/quiet.rb"]


async def test_too_many_paths_are_capped_and_the_answer_says_so():
    paths = [f"app/f{i}.rb" for i in range(MAX_PATHS + 5)]
    out = await call_tool(
        build_server(settings(), FakeSentry()), "issues_for_paths", {"paths": paths}
    )
    assert out["truncated"] is True
    assert len(out["checked"]) == MAX_PATHS


async def test_a_normal_sized_pr_is_not_flagged_as_truncated():
    out = await call_tool(
        build_server(settings(), FakeSentry()), "issues_for_paths", {"paths": ["a.rb", "b.rb"]}
    )
    assert out["truncated"] is False


async def test_the_window_is_passed_to_sentry():
    fake = FakeSentry()
    await call_tool(
        build_server(settings(), fake), "issues_for_paths", {"paths": ["a.rb"], "days": 30}
    )
    assert fake.calls[0][1]["statsPeriod"] == "30d"


async def test_a_nonsense_window_cannot_produce_an_invalid_period():
    fake = FakeSentry()
    await call_tool(
        build_server(settings(), fake), "issues_for_paths", {"paths": ["a.rb"], "days": 0}
    )
    assert fake.calls[0][1]["statsPeriod"] == "1d"


# ----- project scoping --------------------------------------------------


async def test_projects_are_sent_when_configured():
    fake = FakeSentry()
    await call_tool(
        build_server(settings(projects=("4501001", "4501002")), fake),
        "issues_for_paths", {"paths": ["a.rb"]},
    )
    assert fake.calls[0][1]["project"] == ["4501001", "4501002"]


async def test_no_project_filter_means_every_visible_project():
    fake = FakeSentry()
    await call_tool(build_server(settings(), fake), "issues_for_paths", {"paths": ["a.rb"]})
    assert "project" not in fake.calls[0][1]


# ----- search and detail ------------------------------------------------


async def test_search_passes_the_query_through_and_caps_the_limit():
    fake = FakeSentry({"NoMethodError": [ISSUE]})
    out = await call_tool(
        build_server(settings(), fake), "search_issues",
        {"query": "NoMethodError", "limit": 500},
    )
    assert out["issues"][0]["id"] == "42"
    assert fake.calls[0][1]["limit"] == 25


async def test_issue_detail_includes_the_latest_event_frames():
    event = {"id": "ev1", "dateCreated": "2026-07-30T12:00:00Z", "entries": [
        {"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
            {"filename": "app/models/payment.rb", "function": "charge",
             "lineNo": 44, "inApp": True},
        ]}}]}}]}
    fake = FakeSentry({"/issues/42/": ISSUE, "/issues/42/events/latest/": event})
    out = await call_tool(build_server(settings(), fake), "issue_detail", {"issue_id": "42"})
    assert out["events"] == 4213
    assert out["latest_event"]["frames"][0]["line"] == 44


async def test_a_missing_issue_is_data_not_an_exception():
    out = await call_tool(build_server(settings(), FakeSentry()), "issue_detail", {"issue_id": "9"})
    assert "not found" in out["error"]


# ----- config -----------------------------------------------------------


def test_env_requires_a_token_and_an_org(monkeypatch):
    monkeypatch.delenv("SENTRY_TOKEN", raising=False)
    monkeypatch.setenv("SENTRY_ORG_SLUG", "acme")
    with pytest.raises(SystemExit, match="SENTRY_TOKEN"):
        Settings.from_env()


@pytest.mark.parametrize("raw,expected", [
    ('["1","2"]', ("1", "2")),
    ("1,2", ("1", "2")),
    ("1, 2 ", ("1", "2")),
    ("", ()),
])
def test_projects_parse_from_json_or_a_bare_list(monkeypatch, raw, expected):
    monkeypatch.setenv("SENTRY_TOKEN", "t")
    monkeypatch.setenv("SENTRY_ORG_SLUG", "acme")
    monkeypatch.setenv("SENTRY_PROJECTS", raw)
    assert Settings.from_env().projects == expected


def test_the_service_name_is_allowed_by_default():
    # otherwise DNS rebinding protection rejects every reviewer by Host header
    assert "mcp-sentry:8080" in settings().allowed_hosts


def test_allowed_hosts_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SENTRY_TOKEN", "t")
    monkeypatch.setenv("SENTRY_ORG_SLUG", "acme")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "sentry.internal:9000, other")
    assert Settings.from_env().allowed_hosts == ("sentry.internal:9000", "other")


# ----- the catalogue the model sees -------------------------------------


async def test_only_read_tools_are_exposed():
    tools = await build_server(settings(), FakeSentry()).list_tools()
    assert {t.name for t in tools} == {"issues_for_paths", "search_issues", "issue_detail"}


async def test_every_tool_tells_the_model_when_to_reach_for_it():
    for tool in await build_server(settings(), FakeSentry()).list_tools():
        assert tool.description and len(tool.description) > 80, tool.name
