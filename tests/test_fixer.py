"""What a bot is allowed to change, and who decides that it worked.

The validation half runs in the sidecar, which is the only place a token that
can write lives — so these are the rules that hold even if everything inside the
container was talked into something by a bug report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from robbie import budget as budget_mod
from robbie import fixer as fixer_mod
from robbie import publish as publish_mod
from robbie.budget import Verdict as Meter
from robbie.config import Config, DockerConfig, IssueConfig, RepoConfig, Secrets, SlackConfig
from robbie.db import Db
from robbie.fixer import fix_tick, queue_labels
from robbie.github import IssueMeta
from robbie.publish import PublishResult
from robbie.runner import ReviewRun
from robbie.triage import Rules
from robbie_mcp.pr import Settings as PrSettings
from robbie_mcp.pr import changed_lines, changed_paths, clean_title, refuse

FIX = """\
diff --git a/app/models/x.rb b/app/models/x.rb
--- a/app/models/x.rb
+++ b/app/models/x.rb
@@ -1 +1 @@
-old
+new
diff --git a/spec/models/x_spec.rb b/spec/models/x_spec.rb
--- a/spec/models/x_spec.rb
+++ b/spec/models/x_spec.rb
@@ -1 +1 @@
-a
+b
"""


# ----- what the patch is allowed to be -------------------------------------


def test_a_patch_with_a_fix_and_a_test_is_allowed():
    assert refuse(FIX) is None
    assert changed_paths(FIX) == ["app/models/x.rb", "spec/models/x_spec.rb"]
    assert changed_lines(FIX) == 4


def test_a_fix_with_no_test_is_refused():
    """CI is what proves the bug is gone. Without a test there is nothing to run,
    and the reviewer is left taking the diff on faith."""
    only_fix = FIX.split("diff --git a/spec")[0]
    assert refuse(only_fix) == (
        "the patch changes no test, so CI cannot show the bug was fixed"
    )


@pytest.mark.parametrize(
    "path", ["Gemfile", ".github/workflows/ci.yml", "db/migrate/20260101_x.rb", ".env"]
)
def test_the_blast_radius_a_bot_does_not_get(path):
    assert refuse(FIX.replace("app/models/x.rb", path)) == (
        f"{path} is off limits to an automated fix"
    )


def test_a_rename_out_of_a_protected_directory_is_caught():
    """Only reading the destination would let `a/.github/x b/app/x` walk a file
    out of a directory the rules protect."""
    sneaky = FIX.replace(
        "diff --git a/app/models/x.rb b/app/models/x.rb",
        "diff --git a/.github/workflows/ci.yml b/app/models/x.rb",
    )
    assert refuse(sneaky) == ".github/workflows/ci.yml is off limits to an automated fix"


def test_a_patch_that_says_nothing_is_refused():
    assert refuse("") == "the patch is empty"
    assert refuse("I fixed it, trust me") is not None


def test_a_patch_over_the_size_cap_is_refused():
    big = FIX + "".join(
        f"diff --git a/app/{i}.rb b/app/{i}.rb\n--- a/app/{i}.rb\n+++ b/app/{i}.rb\n+x\n"
        for i in range(20)
    )
    assert "over the limit" in (refuse(big) or "")


def test_a_title_is_one_line_and_never_empty():
    assert clean_title("a\nb   c", issue=7) == "a b c"
    assert clean_title("   ", issue=7) == "fix: issue #7"


# ----- the loop ------------------------------------------------------------


RULES = Rules(require={"Does this involve money?": ("No",)})


@pytest.fixture
def repo() -> RepoConfig:
    return RepoConfig(
        slug="acme/app", reviewer_login="rev", bare=Path("/srv/m/app.git"),
        issues=IssueConfig(
            labels=("bug", "needs-triage"), clears="needs-triage",
            fixable_label="robbie-fix", assignee=("a-dev",), rules=RULES,
        ),
    )


@pytest.fixture
def cfg(tmp_path, repo) -> Config:
    return Config(
        slack=SlackConfig(owner_id="U0OWNER"), repos=[repo], state_dir=tmp_path,
        docker=DockerConfig(timeout_s=5), fix_mcp='{"mcpServers":{}}',
    )


@pytest.fixture
def db(tmp_path) -> Db:
    return Db(tmp_path / "robbie.db")


SECRETS = Secrets(
    gh_token="w", slack_bot_token="s", reviewer_gh_token="r", anthropic_api_key="sk"
)
ISSUE = IssueMeta(
    number=12, title="[Bug]: it spins", url="https://x/12", author="cs",
    body="### Steps to reproduce\n\n1. do it", labels=("bug", "robbie-fix"),
)


def _async(value):
    async def call(*a, **k):
        return value
    return call


@pytest.fixture
def settled(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(repo, issue, *, say, add_label="", assignee=(), clears="", dry_run=False):
        calls.append({"say": say, "assignee": assignee, "clears": clears})
        return PublishResult(True, say)

    monkeypatch.setattr(publish_mod, "settle_issue", fake)
    return calls


def stub(monkeypatch, *, run: ReviewRun | None = None, pr_url: str = "", before: str = "",
         spawned: list | None = None):
    monkeypatch.setattr(fixer_mod, "issue_queue", _async([12]))
    monkeypatch.setattr(fixer_mod, "issue_meta", _async(ISSUE))
    monkeypatch.setattr(budget_mod, "check", lambda *a, **k: Meter(True, "fine"))
    seen = iter([before, pr_url])
    monkeypatch.setattr(fixer_mod, "open_pr_for", lambda *a, **k: _async(next(seen))())
    if run is not None:
        async def fake(cfg, secrets, repo, meta, **kw):
            if spawned is not None:
                spawned.append(kw)
            return run
        monkeypatch.setattr(fixer_mod, "run_review", fake)
    else:
        monkeypatch.setattr(
            fixer_mod, "run_review", lambda *a, **k: pytest.fail("spawned a container")
        )


def test_the_fixers_queue_is_what_triage_cleared(repo):
    assert queue_labels(repo) == ("bug", "robbie-fix")


async def test_nothing_runs_until_a_pr_tool_is_configured(cfg, db, repo, monkeypatch):
    """Without it a fix has no way out of the container, so the run is pure spend."""
    monkeypatch.setattr(fixer_mod, "issue_queue", lambda *a, **k: pytest.fail("read the queue"))
    assert await fix_tick(cfg.model_copy(update={"fix_mcp": ""}), SECRETS, repo, db) == []


async def test_a_pull_request_is_the_proof_not_what_the_model_says(
    cfg, db, repo, monkeypatch, settled
):
    """The model can claim anything. The branch either has a pull request on it
    or it does not, and that is read from GitHub."""
    stub(monkeypatch, run=ReviewRun(ok=True, text="<<<VERDICT>>>\nfixed\n<<<END>>>"),
         pr_url="https://x/pull/99")

    done = await fix_tick(cfg, SECRETS, repo, db)

    assert done[0].opened == "https://x/pull/99"
    assert settled[0]["clears"] == "robbie-fix"
    assert not settled[0]["assignee"], "a draft PR is not a person's problem yet"


async def test_a_model_claiming_fixed_with_no_pull_request_goes_to_a_person(
    cfg, db, repo, monkeypatch, settled
):
    stub(monkeypatch, run=ReviewRun(
        ok=True, text="<<<VERDICT>>>\nfixed\n<<<END>>>\n<<<GITHUB>>>\nall done!\n<<<END>>>"
    ))

    done = await fix_tick(cfg, SECRETS, repo, db)

    assert done[0].opened == ""
    assert settled[0]["assignee"] == ("a-dev",)


async def test_a_cannot_hands_over_the_models_own_words(cfg, db, repo, monkeypatch, settled):
    """"I could not write a failing test for this" is the most useful thing it
    can give the person who picks the issue up."""
    stub(monkeypatch, run=ReviewRun(
        ok=True,
        text="<<<VERDICT>>>\ncannot\n<<<END>>>\n<<<GITHUB>>>\nno test harness here\n<<<END>>>",
    ))

    done = await fix_tick(cfg, SECRETS, repo, db)

    assert "no test harness here" in done[0].reason
    assert "no test harness here" in settled[0]["say"]


async def test_a_run_that_died_goes_to_a_person(cfg, db, repo, monkeypatch, settled):
    stub(monkeypatch, run=ReviewRun(ok=False, error="timed out after 1800s"))

    done = await fix_tick(cfg, SECRETS, repo, db)

    assert "timed out" in done[0].reason
    assert settled[0]["assignee"] == ("a-dev",)


async def test_an_issue_that_already_has_a_pull_request_is_not_fixed_twice(
    cfg, db, repo, monkeypatch, settled
):
    """The branch is the record. A second run would rewrite somebody's draft."""
    stub(monkeypatch, before="https://x/pull/7")

    done = await fix_tick(cfg, SECRETS, repo, db)

    assert done[0].opened == "https://x/pull/7"


async def test_a_dry_run_spawns_nothing(cfg, db, repo, monkeypatch, settled):
    stub(monkeypatch)
    assert await fix_tick(cfg, SECRETS, repo, db, dry_run=True) == []
    assert settled == []


async def test_what_the_run_cost_is_recorded(cfg, db, repo, monkeypatch, settled):
    stub(monkeypatch, run=ReviewRun(ok=False, error="boom", cost_usd=0.4), pr_url="")
    await fix_tick(cfg, SECRETS, repo, db)
    assert db.spend_since(0) == pytest.approx(0.4)


async def test_the_fix_runs_on_the_arm_the_file_names(cfg, db, repo, monkeypatch, settled):
    """Separate from the arm that answers the money question: one classifies, the
    other has to write a test that fails and drive a tool call to the end."""
    armed = repo.model_copy(update={
        "issues": repo.issues.model_copy(update={
            "fix_model": "opus", "money_model": "glm-5.3-flash:cloud",
            "money_via_endpoint": True,
        })
    })
    spawned: list[dict] = []
    stub(monkeypatch, run=ReviewRun(ok=True, text=""), pr_url="https://x/pull/1",
         spawned=spawned)

    await fix_tick(cfg, SECRETS, armed, db)

    assert spawned[0]["model"] == "opus"
    assert spawned[0]["via_endpoint"] is False
    assert spawned[0]["mode"] == "fix"


async def test_no_arm_named_means_the_account_default(cfg, db, repo, monkeypatch, settled):
    spawned: list[dict] = []
    stub(monkeypatch, run=ReviewRun(ok=True, text=""), pr_url="https://x/pull/1",
         spawned=spawned)

    await fix_tick(cfg, SECRETS, repo, db)

    assert spawned[0]["model"] is None


def test_the_commit_author_is_nobody_on_github(monkeypatch):
    for k, v in {"FIXER_GH_TOKEN": "t", "FIXER_REPO": "o/r", "FIXER_MIRROR": "/m"}.items():
        monkeypatch.setenv(k, v)
    s = PrSettings.from_env()
    assert s.author_email.endswith(".invalid")
    monkeypatch.setenv("FIXER_AUTHOR_EMAIL", "123+bot@users.noreply.github.com")
    assert PrSettings.from_env().author_email == "123+bot@users.noreply.github.com"
