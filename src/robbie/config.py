"""Config: one YAML file for everything non-secret, env for secrets.

Secrets never live in the YAML — they arrive as env vars so the same config
file is safe to commit and to mount into a container.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    # a typo'd key in the YAML must fail at boot, not be silently ignored
    model_config = ConfigDict(extra="forbid")


class RepoConfig(_Strict):
    slug: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")  # owner/name
    # whose pending review request defines the queue. Shaped like a real GitHub
    # login because it is interpolated into a `--jq` filter and a search query,
    # where a stray quote fails as an unreadable parse error three calls later.
    reviewer_login: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
    bare: Path  # local mirror, mounted read-only into every reviewer
    label: str = "Code Review"
    needs_work_label: str = "❌ NEEDS WORK! ❌"
    # Temporary: the PR waits on something that is not the author, so it comes
    # back on its own the moment the label comes off and no row is written.
    hold_labels: tuple[str, ...] = ()
    # A human already reviewed and approved it. Excluding, whatever else the PR
    # carries: `label` and one of these together means not reviewed.
    done_labels: tuple[str, ...] = ()
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
    # capped because choose_model expands the weights into one slot each
    weight: int = Field(default=1, ge=1, le=100)


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
    # Where the model-credential proxy listens, e.g. http://model-proxy:8080.
    # Empty (the default) hands each reviewer the real key or the real credentials
    # file, which is what this exists to stop: see src/robbie_proxy.
    model_proxy: str = ""
    # Empty (the default) means every review runs on the account's own model, which
    # is the only shape the spend gates can price. Listing arms splits reviews
    # between them by weight, for comparing models on one unchanged harness.
    review_models: list[ReviewModel] = Field(default_factory=list)
    docker: DockerConfig = DockerConfig()
    budget: BudgetConfig = BudgetConfig()

    @model_validator(mode="after")
    def _proxy_needs_a_network(self) -> Config:
        """A reviewer reaches the proxy by service name, or it does not reach it.

        Spawned over the docker socket, a container is on the default bridge and
        cannot resolve one. Every review would then fail on a name lookup, which
        reads as the model being unreachable rather than as this line.
        """
        if self.model_proxy and not self.docker.network:
            raise ValueError(
                "model_proxy is set but docker.network is null, so a reviewer cannot "
                "resolve it. Set docker.network to the compose network name."
            )
        return self

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

    def fallback_for(self, choice: Choice) -> Choice | None:
        """The arm to try when `choice`'s own meter is spent, if there is one.

        The heaviest arm on the other side of the endpoint divide, since that is
        the one the split leans on anyway. None when nothing is configured over
        there: an arm nobody asked for is not a fallback, it is a surprise.
        """
        others = [m for m in self.review_models if (m.via == "endpoint") != choice.via_endpoint]
        if not others:
            return None
        best = max(others, key=lambda m: m.weight)
        return Choice(model=best.model, via_endpoint=best.via == "endpoint")

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
    # SecretStr, not str: a pydantic repr prints its fields and a ValidationError
    # echoes the input it rejected, so one stray log line is the whole keyring.
    gh_token: SecretStr  # publishes reviews, so it needs write
    slack_bot_token: SecretStr
    # handed to the reviewer containers; a read-only token belongs here
    reviewer_gh_token: SecretStr
    anthropic_api_key: SecretStr | None = None
    claude_credentials: Path | None = None
    # Any endpoint speaking the Anthropic Messages API, for comparing models
    # against the same harness. Used only by `robbie once --model`, so the daemon
    # cannot pick one up: neither budget backend can measure spend there.
    review_base_url: str | None = None
    review_api_token: SecretStr | None = None
    # what a reviewer presents to the model proxy. Worth nothing off the compose
    # network, which is the point: it is what a container holds instead of a key.
    model_proxy_token: SecretStr | None = None


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("ROBBIE_CONFIG", "config/robbie.yaml"))
    if not p.is_file():
        raise SystemExit(f"config not found: {p} (set ROBBIE_CONFIG or pass --config)")
    cfg = Config.model_validate(_read_yaml(p))
    try:
        cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        raise SystemExit(f"state_dir {cfg.state_dir} is not writable: {ex}") from ex
    _check_paths(cfg)
    return cfg


class _NoDupes(yaml.SafeLoader):
    """Same reason as `extra="forbid"`: a key written twice is a typo, not an edit.

    PyYAML keeps the last one silently, and pydantic never sees the first — a whole
    `review_models:` block sat dead in the local config for a day that way.
    """

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        seen = set()
        for key, _ in node.value:
            name = self.construct_object(key, deep=deep)
            if name in seen:
                raise SystemExit(f"config: {name!r} is set twice (line {key.start_mark.line + 1})")
            seen.add(name)
        return super().construct_mapping(node, deep)


def _read_yaml(p: Path) -> object:
    return yaml.load(p.read_text(encoding="utf-8"), Loader=_NoDupes)  # noqa: S506 — SafeLoader


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
        gh_token=SecretStr(gh_token),
        slack_bot_token=SecretStr(_require("SLACK_BOT_TOKEN")),
        reviewer_gh_token=SecretStr(
            os.environ.get("GH_TOKEN_REVIEWER", "").strip() or gh_token
        ),
        anthropic_api_key=_optional_secret("ANTHROPIC_API_KEY"),
        claude_credentials=(
            Path(os.environ["CLAUDE_CREDENTIALS"])
            if os.environ.get("CLAUDE_CREDENTIALS")
            else None
        ),
        review_base_url=os.environ.get("REVIEW_BASE_URL", "").strip() or None,
        review_api_token=_optional_secret("REVIEW_API_TOKEN"),
        model_proxy_token=_optional_secret("MODEL_PROXY_TOKEN"),
    )
    if cfg.model_proxy and s.model_proxy_token is None:
        raise SystemExit("model_proxy is set, so MODEL_PROXY_TOKEN has to be too")
    if cfg.backend == "api" and not s.anthropic_api_key:
        raise SystemExit("backend=api needs ANTHROPIC_API_KEY")
    if cfg.backend == "oauth":
        if s.claude_credentials is None:
            raise SystemExit("backend=oauth needs CLAUDE_CREDENTIALS=/path/to/.credentials.json")
        if not s.claude_credentials.is_file():
            raise SystemExit(f"CLAUDE_CREDENTIALS not readable: {s.claude_credentials}")
    if s.reviewer_gh_token.get_secret_value() == s.gh_token.get_secret_value():
        # The whole security bet is that the worst a reviewer can do with its token
        # is read and exfiltrate it; that only holds while the token is read-only.
        # A warning rather than a refusal — one token is a legitimate way to start.
        logger.warning(
            "GH_TOKEN_REVIEWER is not set, so the reviewer containers are getting the "
            "token that publishes reviews. They run a PR's own code with "
            "bypassPermissions and unrestricted network: put a READ-ONLY token there."
        )
    return s


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def _optional_secret(name: str) -> SecretStr | None:
    """None, not an empty SecretStr: callers read absence as "not configured"."""
    value = os.environ.get(name, "").strip()
    return SecretStr(value) if value else None
