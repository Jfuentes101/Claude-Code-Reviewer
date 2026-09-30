"""What robbie knows, as files other processes read — an assistant, a board.

Off unless `export_interval_s` is set; nothing in robbie reads these back.

`export/worklist.json` and `export/usage.json` under `state_dir`, rewritten whole
(temp file + rename) every `export_interval_s`, so a reader never sees half a
file and nothing else has to ask GitHub or the usage endpoint what robbie
already asks. The worklist is the operator's plate:

- `awaiting_you`: the newest pass is an `ok` at the current head and the review
  is still requested — the panel's "ready for a human" rows;
- `needs_work`: open PRs carrying the needs-work label, in the author's hands;
- `fixer_prs`: open PRs on the fixer's branches not yet under the queue label;
- `triage_assigned`: open bug issues assigned to the operator;
- `mine`: the operator's own open PRs, with review decision and CI;
- `open_prs` / `open_issues`: every open number as `owner/repo#N`, so a reader can
  tell whether something it was told about is still open without asking GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robbie import fixer
from robbie.config import Config, RepoConfig
from robbie.db import Db
from robbie.github import GhError, ci_outcome, gh_json, pr_meta

logger = logging.getLogger(__name__)

# PRs the fixer opened before its branches moved under robbie/
LEGACY_FIX_PREFIXES = ("fix/issue-",)
READY_DAYS = 30
# (repo, pr, head) -> (ci, read at). A settled verdict holds for its commit; anything
# still moving is read again after CI_RECHECK_S. Forty PRs read every pass would be
# ~500 GraphQL calls an hour out of the pool the gates and the board share.
_CI: dict[tuple[str, int, str], tuple[str, float]] = {}
CI_RECHECK_S = 15 * 60
OPEN_FIELDS = (
    "number,title,url,author,labels,headRefName,headRefOid,isDraft,createdAt,"
    "reviewDecision"
)


def export_dir(cfg: Config) -> Path:
    return cfg.state_dir / "export"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _epoch(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _labels(pr: dict[str, Any]) -> list[str]:
    return [lbl.get("name", "") for lbl in pr.get("labels") or []]


def _brief(repo: RepoConfig, pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": repo.slug,
        "number": pr["number"],
        "title": pr.get("title") or "",
        "url": pr.get("url") or "",
        "author": (pr.get("author") or {}).get("login") or "",
    }


async def repo_worklist(repo: RepoConfig, db: Db) -> dict[str, list[dict[str, Any]]]:
    open_prs = await gh_json(
        "pr", "list", "--repo", repo.slug, "--state", "open", "--limit", "200",
        "--json", OPEN_FIELDS,
    ) or []
    by_number = {int(pr["number"]): pr for pr in open_prs}

    since = int(time.time() * 1000) - READY_DAYS * 86_400_000
    awaiting = []
    for row in db.approved_and_green(since):
        pr = by_number.get(int(row["pr"]))
        if row["repo"] != repo.slug or pr is None or pr.get("headRefOid") != row["head_sha"]:
            continue
        awaiting.append({**_brief(repo, pr), "head": row["head_sha"], "ci": row["ci_state"]})

    prefixes = (fixer.BRANCH_PREFIX, *LEGACY_FIX_PREFIXES)
    fixer_prs = []
    for pr in open_prs:
        branch = pr.get("headRefName") or ""
        prefix = next((p for p in prefixes if branch.startswith(p)), None)
        if prefix is None or repo.label in _labels(pr):
            continue
        suffix = branch[len(prefix):]
        fixer_prs.append({
            **_brief(repo, pr),
            "issue": int(suffix) if suffix.isdigit() else None,
            "opened_at": _epoch(pr.get("createdAt")),
            "draft": bool(pr.get("isDraft")),
        })

    needs_work = [
        _brief(repo, pr) for pr in open_prs if repo.needs_work_label in _labels(pr)
    ]
    mine = [await _mine(repo, pr) for pr in open_prs
            if (pr.get("author") or {}).get("login") == repo.reviewer_login]
    live = {(repo.slug, p["number"], p["head"]) for p in mine}
    for key in [k for k in _CI if k[0] == repo.slug and k not in live]:
        del _CI[key]

    assigned: list[dict[str, Any]] = []
    bug_labels = [lbl for lbl in repo.issues.labels if lbl != repo.issues.clears]
    if bug_labels:
        args = ["issue", "list", "--repo", repo.slug, "--state", "open",
                "--assignee", repo.reviewer_login, "--limit", "50",
                "--json", "number,title,url"]
        for label in bug_labels:
            args += ["--label", label]
        assigned = [
            {"repo": repo.slug, "number": i["number"], "title": i.get("title") or "",
             "url": i.get("url") or ""}
            for i in await gh_json(*args) or []
        ]

    open_issues = await gh_json(
        "issue", "list", "--repo", repo.slug, "--state", "open", "--limit", "1000",
        "--json", "number",
    ) or []

    return {
        "awaiting_you": awaiting,
        "needs_work": needs_work,
        "fixer_prs": fixer_prs,
        "triage_assigned": assigned,
        "mine": mine,
        "open_prs": [f"{repo.slug}#{n}" for n in sorted(by_number)],
        "open_issues": [f"{repo.slug}#{i['number']}" for i in open_issues],
    }


async def _mine(repo: RepoConfig, pr: dict[str, Any]) -> dict[str, Any]:
    """One PR of the operator's, with CI read the way the gates read it."""
    out = {
        **_brief(repo, pr),
        "head": pr.get("headRefOid") or "",
        "draft": bool(pr.get("isDraft")),
        "labels": _labels(pr),
        "review": pr.get("reviewDecision") or None,
        "ci": "none",
    }
    key = (repo.slug, int(pr["number"]), out["head"])
    cached = _CI.get(key)
    if cached and (cached[0] in ("green", "red") or time.time() - cached[1] < CI_RECHECK_S):
        out["ci"] = cached[0]
        return out
    try:
        meta = await pr_meta(repo.slug, int(pr["number"]))
    except GhError as ex:
        logger.warning("%s#%s: export could not read it: %s", repo.slug, pr["number"], ex)
        return out
    if meta.checks:
        state = ci_outcome(meta, ignore=repo.ignore_checks)
        out["ci"] = "pending" if state == "waiting" else state
    _CI[key] = (out["ci"], time.time())
    return out


def usage(db: Db) -> dict[str, Any]:
    out: dict[str, Any] = {"written_at": int(time.time())}
    for name in ("plan", "endpoint"):
        row = db.read_meter(name)
        if row is not None:
            out[name] = {
                "pct": row["pct"],
                "read_at": (row["read_at"] or 0) // 1000,
                "error": row["error"],
            }
    return out


async def export_once(cfg: Config, db: Db) -> None:
    lists: dict[str, list[dict[str, Any]]] = {}
    for repo in cfg.repos:
        try:
            part = await repo_worklist(repo, db)
        except GhError as ex:
            logger.warning("%s: worklist export skipped: %s", repo.slug, ex)
            return  # a partial list would read as "nothing pending"
        for key, rows in part.items():
            lists.setdefault(key, []).extend(rows)
    write_json(export_dir(cfg) / "worklist.json", {"written_at": int(time.time()), **lists})
    write_json(export_dir(cfg) / "usage.json", usage(db))


def digest_due(cfg: Config, now: datetime | None = None) -> str | None:
    """The ISO week the weekly digest is due for, once its hour has come."""
    if cfg.digest_weekday is None:
        return None
    now = now or datetime.now(UTC)
    if now.weekday() != cfg.digest_weekday or now.hour < cfg.digest_utc_hour:
        return None
    year, week, _ = now.isocalendar()
    return f"digest:{year}-W{week:02d}"
