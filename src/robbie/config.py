"""Config: one YAML file for everything non-secret, env for secrets.

Secrets never live in the YAML — they arrive as env vars so the same config
file is safe to commit and to mount into a container.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    # a typo'd key in the YAML must fail at boot, not be silently ignored
    model_config = ConfigDict(extra="forbid")


class RepoConfig(_Strict):
    slug: str  # owner/name
    reviewer_login: str  # whose pending review request defines the queue
    bare: Path  # local mirror, mounted read-only into every reviewer
    label: str = "Code Review"
    needs_work_label: str = "❌ NEEDS WORK! ❌"
    review_command: str = ".claude/commands/code-review.md"
    slack_channel: str | None = None
    image: str = "robbie-reviewer:latest"
    # CodeRabbit reports its review as a commit status; it is never a broken build
    ignore_checks: tuple[str, ...] = ("CodeRabbit",)
    # CI does not run on push any more, so an approval is what pays for a build
    ci_phrase: str = "run-ci"

    @property
    def owner(self) -> str:
        return self.slug.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.slug.split("/", 1)[1]


class DockerConfig(_Strict):
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: int = 512
    timeout_s: int = 1800
    # reviewers are spawned over the docker socket, so they are not on the
    # compose network by default and cannot resolve the mcp sidecars by name
    network: str | None = "robbie"
    # on by default, but AppArmor's docker-default profile treats the exec-time
    # profile transition as gaining privileges, so on an AppArmor host (Ubuntu,
    # Pop!_OS) every execve in the container returns EPERM. Turn it off there;
    # the container still runs unprivileged with every capability dropped.
    no_new_privileges: bool = True


class BudgetConfig(_Strict):
    daily_usd: float = 20.0  # backend=api
    stop_pct: int = 70  # backend=oauth: pause at this % of the 5h window
    # Held back for each review in flight, because the gate reads what has been
    # spent and a running container has not finished spending. Deliberately
    # generous: $81.83 of reviews shared one 5h window with a human's own
    # sessions, which caps a $5 review at ~6% of a window and puts it well under
    # that in practice. Erring high costs idle quota, erring low costs the cutoff.
    reserve_pct: float = 8.0  # backend=oauth
    reserve_usd: float = 5.0  # backend=api, the p90 of 50 observed reviews


class SlackConfig(_Strict):
    owner_id: str  # who gets briefings and operator warnings
    users_file: Path = Path("config/slack-users.tsv")


class Config(_Strict):
    slack: SlackConfig
    repos: list[RepoConfig] = Field(min_length=1)
    backend: Literal["api", "oauth"] = "api"
    poll_interval_s: int = 600
    max_concurrent_reviews: int = 3
    # the gate phase is API calls, not containers, so it gets its own wider cap:
    # a full queue would otherwise fire three gh subprocesses per PR at once
    max_concurrent_checks: int = 8
    state_dir: Path = Path("/var/lib/robbie")
    review_effort: str = "high"
    stale_review_days: int = 7  # --digest threshold
    # HOST path, mounted read-only into every reviewer and copied to its user
    # scope, where the CLI loads it without being asked. A PR cannot touch it.
    policy_dir: Path | None = None
    # passed to the reviewer as --mcp-config; empty means no MCP servers at all
    review_mcp: str = ""
    docker: DockerConfig = DockerConfig()
    budget: BudgetConfig = BudgetConfig()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "robbie.db"

    @property
    def transcript_dir(self) -> Path:
        return self.state_dir / "reviews"

    def repo(self, slug: str) -> RepoConfig:
        for r in self.repos:
            if r.slug == slug:
                return r
        raise KeyError(f"repo {slug!r} is not in the config")


class Secrets(_Strict):
    gh_token: str  # publishes reviews, so it needs write
    slack_bot_token: str
    # handed to the reviewer containers; a read-only token belongs here
    reviewer_gh_token: str
    anthropic_api_key: str | None = None
    claude_credentials: Path | None = None


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("ROBBIE_CONFIG", "config/robbie.yaml"))
    if not p.is_file():
        raise SystemExit(f"config not found: {p} (set ROBBIE_CONFIG or pass --config)")
    cfg = Config.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
    try:
        cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        raise SystemExit(f"state_dir {cfg.state_dir} is not writable: {ex}") from ex
    return cfg


def load_secrets(cfg: Config) -> Secrets:
    """Read secrets from env and fail loudly on whatever the backend needs.

    Checked at boot rather than at first use: a missing key should stop the
    daemon, not surface 10 minutes later as a failed review.
    """
    gh_token = _require("GH_TOKEN")
    s = Secrets(
        gh_token=gh_token,
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        reviewer_gh_token=os.environ.get("GH_TOKEN_REVIEWER", "").strip() or gh_token,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        claude_credentials=(
            Path(os.environ["CLAUDE_CREDENTIALS"])
            if os.environ.get("CLAUDE_CREDENTIALS")
            else None
        ),
    )
    if cfg.backend == "api" and not s.anthropic_api_key:
        raise SystemExit("backend=api needs ANTHROPIC_API_KEY")
    if cfg.backend == "oauth":
        if s.claude_credentials is None:
            raise SystemExit("backend=oauth needs CLAUDE_CREDENTIALS=/path/to/.credentials.json")
        if not s.claude_credentials.is_file():
            raise SystemExit(f"CLAUDE_CREDENTIALS not readable: {s.claude_credentials}")
    return s


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set")
    return value
