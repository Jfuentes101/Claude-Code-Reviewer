"""Config: one YAML file for everything non-secret, env for secrets.

Secrets never live in the YAML — they arrive as env vars so the same config
file is safe to commit and to mount into a container.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
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


class ReviewModel(_Strict):
    """One arm of a model comparison, and how many reviews it takes.

    `via: endpoint` sends the run to REVIEW_BASE_URL with its own token instead of
    to the account's backend, which is also what tells the spend gate whose meter
    it is about to spend.
    """

    model: str
    via: Literal["backend", "endpoint"] = "backend"
    weight: int = Field(default=1, ge=1)


@dataclass(frozen=True)
class Choice:
    model: str | None = None  # None = whatever the account defaults to
    via_endpoint: bool = False


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
    # the review endpoint's own session/weekly allowance, in percent of it
    endpoint_stop_pct: int = 80
    endpoint_reserve_pct: float = 5.0


class SlackConfig(_Strict):
    owner_id: str  # who gets holds, failures and every other operator warning
    users_file: Path = Path("config/slack-users.tsv")
    # an `ok` is the one verdict that leaves no trace on the PR, so everyone who
    # shares the review queue wants the line. Empty means just the owner.
    approved_ids: list[str] = Field(default_factory=list)


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
    # Empty (the default) means every review runs on the account's own model, which
    # is the only shape the spend gates can price. Listing arms splits reviews
    # between them by weight, for comparing models on one unchanged harness.
    review_models: list[ReviewModel] = Field(default_factory=list)
    docker: DockerConfig = DockerConfig()
    budget: BudgetConfig = BudgetConfig()

    @property
    def endpoint_models(self) -> tuple[str, ...]:
        return tuple(m.model for m in self.review_models if m.via == "endpoint")

    def choose_model(self, key: str) -> Choice:
        """Which arm reviews this key. Stable per key, so a re-review is comparable.

        Bucketed by hashing the dedup key rather than counting: no state to keep,
        and the same commit always lands on the same model, so a second pass
        compares like with like instead of moving the variable being measured.
        """
        arms = [m for m in self.review_models for _ in range(m.weight)]
        if not arms:
            return Choice()
        digest = hashlib.sha256(key.encode()).digest()
        arm = arms[int.from_bytes(digest[:8], "big") % len(arms)]
        return Choice(model=arm.model, via_endpoint=arm.via == "endpoint")

    def named_model(self, model: str) -> Choice:
        """A model asked for by name on the CLI, routed by what the config says.

        A tag nobody configured runs on the account, where an unknown name fails
        cleanly — the alternative is handing a third party the account's own key.
        """
        return Choice(model=model, via_endpoint=model in self.endpoint_models)

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
    # Any endpoint speaking the Anthropic Messages API, for comparing models
    # against the same harness. Used only by `robbie once --model`, so the daemon
    # cannot pick one up: neither budget backend can measure spend there.
    review_base_url: str | None = None
    review_api_token: str | None = None


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("ROBBIE_CONFIG", "config/robbie.yaml"))
    if not p.is_file():
        raise SystemExit(f"config not found: {p} (set ROBBIE_CONFIG or pass --config)")
    cfg = Config.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
    try:
        cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        raise SystemExit(f"state_dir {cfg.state_dir} is not writable: {ex}") from ex
    _check_paths(cfg)
    return cfg


def _check_paths(cfg: Config) -> None:
    """Both of these are HOST paths handed to the docker daemon on a spawn.

    Compose mounts them into the orchestrator at the same path so the two agree,
    which is what makes checking them from in here meaningful. A wrong one is
    otherwise invisible until the first real review — `--dry-run` starts no
    container, so it never touches either.
    """
    for repo in cfg.repos:
        if not repo.bare.is_dir():
            raise SystemExit(
                f"{repo.slug}: mirror {repo.bare} is not a directory here. It must be a "
                "host path that exists inside this process too (see docker-compose.yml); "
                "./scripts/mirror-sync creates it."
            )
    if cfg.policy_dir is not None and not cfg.policy_dir.is_dir():
        raise SystemExit(f"policy_dir {cfg.policy_dir} is not a directory")


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
        review_base_url=os.environ.get("REVIEW_BASE_URL", "").strip() or None,
        review_api_token=os.environ.get("REVIEW_API_TOKEN", "").strip() or None,
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
