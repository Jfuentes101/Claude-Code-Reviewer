"""Anchoring. A finding that can't attach to the diff must be demoted, never
dropped and never allowed to take the whole review down with it."""

from __future__ import annotations

from robbie.anchor import (
    anchor,
    commentable,
    parse_findings,
    severity_count,
    summary_findings,
)

PATCH = """\
@@ -1,3 +1,6 @@
 first
+added_two
+added_three
 fourth
-removed
+added_six
"""


def test_commentable_counts_added_and_context_lines_only():
    assert commentable(PATCH) == {1, 2, 3, 4, 5}


def test_commentable_handles_an_empty_patch():
    assert commentable("") == set()


def test_no_newline_marker_does_not_shift_numbering():
    patch = "@@ -1,1 +1,2 @@\n one\n+two\n\\ No newline at end of file\n"
    assert commentable(patch) == {1, 2}


def test_exact_line_is_anchored():
    out = anchor([{"path": "a.rb", "line": 2, "severity": "Must-fix", "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert len(out.comments) == 1
    assert out.comments[0]["line"] == 2
    assert out.comments[0]["side"] == "RIGHT"
    assert out.leftovers == ""


def test_a_near_miss_is_snapped_and_says_so():
    out = anchor([{"path": "a.rb", "line": 7, "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments[0]["line"] == 5
    assert "reported for line 7" in out.comments[0]["body"]


def test_a_far_miss_becomes_a_leftover():
    out = anchor([{"path": "a.rb", "line": 400, "title": "far", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments == []
    assert "a.rb:400" in out.leftovers
    assert "far" in out.leftovers


def test_a_file_outside_the_diff_becomes_a_leftover():
    out = anchor([{"path": "untouched.rb", "line": 2, "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments == []
    assert "untouched.rb" in out.leftovers


def test_a_missing_line_field_becomes_a_leftover():
    out = anchor([{"path": "a.rb", "title": "no line", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments == []
    assert "no line" in out.leftovers


def test_a_valid_range_is_kept():
    out = anchor([{"path": "a.rb", "line": 4, "start_line": 2, "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments[0]["start_line"] == 2
    assert out.comments[0]["start_side"] == "RIGHT"


def test_an_out_of_diff_range_start_is_dropped_but_the_comment_survives():
    out = anchor([{"path": "a.rb", "line": 4, "start_line": 999, "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert "start_line" not in out.comments[0]
    assert out.comments[0]["line"] == 4


def test_severity_badge_is_rendered():
    out = anchor([{"path": "a.rb", "line": 1, "severity": "Critical", "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert "Critical" in out.comments[0]["body"]


def test_an_unknown_severity_still_renders():
    out = anchor([{"path": "a.rb", "line": 1, "severity": "whatever", "title": "t", "body": "b"}],
                 {"a.rb": commentable(PATCH)})
    assert out.comments[0]["body"].startswith("🔵 **Note**")


# ----- parsing the model's block ---------------------------------------


def test_parse_findings_accepts_an_array():
    assert parse_findings('[{"path": "a.rb", "line": 1}]') == [{"path": "a.rb", "line": 1}]


def test_parse_findings_treats_garbage_as_nothing_to_say():
    for raw in ("", "   ", "not json", '{"path": "a.rb"}', "null"):
        assert parse_findings(raw) == []


def test_severity_count_splits_blocking_from_advisory():
    findings = [
        {"severity": "Critical"}, {"severity": "Must-fix"}, {"severity": "must fix"},
        {"severity": "Should-fix"}, {"severity": "Nitpick"}, {},
    ]
    assert severity_count(findings, blocking=True) == 3
    assert severity_count(findings, blocking=False) == 1


# ----- what a summary indexes, when nothing went inline --------------------


def test_a_summary_index_is_counted():
    body = (
        "One line on the PR.\n"
        "- 🔴 **Must-fix** — `config/application.rb:17` — load_defaults flips a default\n"
        "- 🟠 Should-fix — `app/services/products_service.rb:141` — deprecated to_s\n"
        "- 🔵 Nitpick — `Gemfile:9` — pin loosened\n"
    )
    assert summary_findings(body) == 3


def test_prose_naming_a_severity_is_not_a_finding():
    """Observed on three models: an intro and a closing line both say "Must-fix"."""
    body = (
        "this is pass 7 and the two Must-fix threads still have no reply\n"
        "- 🔴 Must-fix — `a.rb:17` — the real one\n"
        "To call this ready I'd need the two Must-fix threads resolved\n"
    )
    assert summary_findings(body) == 1


def test_no_summary_counts_nothing():
    assert summary_findings("") == 0
