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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from robbie import branding
from robbie.triage import Rules

logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    # a typo'd key in the YAML must fail at boot, not be silently ignored
    model_config = ConfigDict(extra="forbid")


class IssueConfig(_Strict):
    """The bug queue a fixer works from. No `labels` is the whole feature off.

    `clears` is what robbie takes off once it has decided, and it has to be one of
    the labels the queue selects on — otherwise every tick re-reads the same issue
    and pays for the same model call again, forever.
    """

    labels: tuple[str, ...] = ()  # an issue must carry ALL of these to be a candidate
    clears: str = ""  # ...and robbie takes this one off once it has triaged it
    fixable_label: str = ""  # applied instead, when nothing forbids a bot trying
    assignee: tuple[str, ...] = ()
    # the fix, plus up to two review_patch rounds of ~10 min each (25 max apiece)
    fix_timeout_s: int = 5400  # who gets the ones a bot may not touch. Empty = nobody
    rules: Rules = Rules()
    # Which arm answers the money question: a classification, so a cheap one does.
    # `via` has the same meaning as in `review_models` — it picks the endpoint and
    # the meter the spend gate reads.
    money_model: str | None = None
    money_via_endpoint: bool = False
    # And which one writes the fix. Not a classification: it has to find the code,
    # write a test that fails, keep the change small and drive a tool call to the
    # end, which is where a cheap arm stops being cheap. None = the account default.
    fix_model: str | None = None
    fix_via_endpoint: bool = False
    # A fix runs on its own image: it needs a runtime, a database and the native
    # libraries the repo's gems build against, none of which a review needs and
    # all of which are the difference between a small image and a huge one.
    # Empty = the reviewer's image, and a fix that cannot run what it writes.
    fix_image: str = ""
    # Handed to the fix container as DATABASE_URL, and what the entrypoint reads
    # to know which role and database to create. Empty = no database is started.
    fix_db_url: str = ""

    @field_validator("assignee", mode="before")
    @classmethod
    def _one_or_many(cls, v: object) -> object:
        if isinstance(v, str):
            return (v,) if v else ()
        return v

    @model_validator(mode="after")
    def _clears_must_narrow_the_queue(self) -> IssueConfig:
        if self.labels and self.clears not in self.labels:
            raise ValueError(
                f"issues.clears must be one of issues.labels {list(self.labels)}; "
                "a label that does not narrow the queue leaves every issue in it"
            )
        if self.labels and not self.fixable_label:
            raise ValueError("issues.fixable_label is what an attemptable issue gets")
        return self


class RepoConfig(_Strict):
    slug: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")  # owner/name
    # whose pending review request defines the queue. Constrained because it is
    # interpolated into a `--jq` filter and into a search query.
    reviewer_login: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
    bare: Path  # local mirror, mounted read-only into every reviewer
    # no quote, no newline: unlike the other label fields this one goes inside a
    # GraphQL search string (`label:"..."`)
    label: str = Field(default="Code Review", pattern=r'^[^"\n]+$')
    needs_work_label: str = "❌ NEEDS WORK! ❌"
    # Temporary: the PR waits on something that is not the author, so it comes
    # back on its own the moment the label comes off and no row is written.
    hold_labels: tuple[str, ...] = ()
    # A human already reviewed and approved it. Excluding, whatever else the PR
    # carries: `label` and one of these together means not reviewed.
    done_labels: tuple[str, ...] = ()
    # also review PRs AUTHORED by reviewer_login that carry `label` — the
    # self-queue (a review request cannot name the author, so these are
    # otherwise invisible to the daemon). Off by default.
    self_review: bool = False
    review_command: str = ".claude/commands/code-review.md"
    # Where `review_command` is read from. Not the PR's base branch: the PR picks
    # that, and the rules it is judged by are not its to choose.
    criteria_ref: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]+$")
    slack_channel: str | None = None
    image: str = "robbie-reviewer:latest"
    # CodeRabbit reports its review as a commit status; it is never a broken build
    ignore_checks: tuple[str, ...] = ("CodeRabbit",)
    # CI does not run on push any more, so an approval is what pays for a build
    ci_phrase: str = "run-ci"
    issues: IssueConfig = IssueConfig()

    @property
    def brake_labels(self) -> tuple[str, ...]:
        """The labels that mean "not ready for anyone yet".

        One definition, two readers: gate 3 holds the next pass on them, and the
        panel drops the PR off the board. A row saying "ready for a human" that
        opens onto a needs-work label is the worst kind of noise, so the two must
        not be able to disagree.
        """
        return (self.needs_work_label, *self.hold_labels)

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
    # where a fix run goes instead. Its own network because the PR tool lives
    # there, and a reviewer running a PR's code must not be able to reach it.
    fix_network: str | None = "robbie-fix"
    # Turn OFF on an AppArmor host (Ubuntu, Pop!_OS): docker-default reads the
    # exec-time profile transition as gaining privileges and every execve in the
    # container returns EPERM. It still runs unprivileged with all caps dropped.
    no_new_privileges: bool = True


class ReviewModel(_Strict):
    """One arm of a model comparison, and how many reviews it takes.

    `via: endpoint` sends the run to REVIEW_BASE_URL with its own token, and tells
    the spend gate whose meter to read.
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
    # Held back per review in flight: the gate reads what has been spent, and a
    # running container has not finished spending. Erring high costs idle quota,
    # erring low costs the cutoff, so these are deliberately generous.
    reserve_pct: float = 8.0  # backend=oauth
    reserve_usd: float = 5.0  # backend=api, the p90 of 50 observed reviews
    # the review endpoint's own session/weekly allowance, in percent of it
    endpoint_stop_pct: int = 80
    endpoint_reserve_pct: float = 5.0
    # how often the daemon asks a provider what has been spent. The only thing
    # that decides the request rate: every other reader takes the stored number.
    usage_poll_s: int = 120


class SlackConfig(_Strict):
    owner_id: str  # who gets holds, failures and every other operator warning
    users_file: Path = Path("config/slack-users.tsv")
    # an `ok` leaves no trace on the PR, so everyone sharing the queue wants the
    # line. Empty means just the owner.
    approved_ids: list[str] = Field(default_factory=list)


class Config(_Strict):
    # the byline on everything a human reads (signatures, dashboard); the
    # engine and its machine markers stay robbie. Two instances of this code
    # can sign differently.
    bot_name: str = Field(default="robbie", pattern=r'^[^"\n]+$')
    slack: SlackConfig
    repos: list[RepoConfig] = Field(min_length=1)
    backend: Literal["api", "oauth"] = "api"
    poll_interval_s: int = 600
    # the bug queue's own clock (triage, then fix) inside `poll`. 0 = off
    issue_interval_s: int = 900
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
    # the PR tool a fix run reaches. Its own server, because it is the one MCP
    # here that writes, and it holds the only credential that can.
    fix_mcp: str = ""
    # Where the model-credential proxy listens, e.g. http://model-proxy:8080.
    # Empty (the default) hands each reviewer the real key or the real credentials
    # file, which is what this exists to stop: see src/robbie_proxy.
    model_proxy: str = ""
    # Where to report the review lifecycle (the props board), e.g.
    # http://host.docker.internal:4021. Empty = no bridge; a set URL is
    # best-effort only — see src/robbie/props_bridge.py.
    props_url: str = ""
    # Empty (the default) runs every review on the account's own model, the only
    # shape the spend gates can price. Listing arms splits reviews by weight.
    review_models: list[ReviewModel] = Field(default_factory=list)
    docker: DockerConfig = DockerConfig()
    budget: BudgetConfig = BudgetConfig()

    @model_validator(mode="after")
    def _proxy_needs_a_network(self) -> Config:
        """A reviewer reaches the proxy by service name, or not at all.

        Spawned over the docker socket it lands on the default bridge, where the
        name does not resolve, and every review fails looking like a dead model.
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
        """Which arm reviews this key. Hashed rather than counted: no state to keep,
        and the same commit always lands on the same model."""
        arms = [m for m in self.review_models for _ in range(m.weight)]
        if not arms:
            return Choice()
        digest = hashlib.sha256(key.encode()).digest()
        arm = arms[int.from_bytes(digest[:8], "big") % len(arms)]
        return Choice(model=arm.model, via_endpoint=arm.via == "endpoint")

    def fallback_for(self, choice: Choice) -> Choice | None:
        """The arm to try when `choice`'s own meter is spent, if there is one.

        The heaviest arm on the other side of the endpoint divide. None when
        nothing is configured there: an arm nobody asked for is a surprise.
        """
        others = [m for m in self.review_models if (m.via == "endpoint") != choice.via_endpoint]
        if not others:
            return None
        best = max(others, key=lambda m: m.weight)
        return Choice(model=best.model, via_endpoint=best.via == "endpoint")

    def named_model(self, model: str) -> Choice:
        """A model asked for by name on the CLI, routed by what the config says.

        A tag nobody configured runs on the account, where an unknown name fails
        cleanly, rather than handing a third party the account's key.
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
    # echoes what it rejected, so one stray log line is the whole keyring.
    gh_token: SecretStr  # publishes reviews, so it needs write
    slack_bot_token: SecretStr
    # handed to the reviewer containers; a read-only token belongs here
    reviewer_gh_token: SecretStr
    anthropic_api_key: SecretStr | None = None
    claude_credentials: Path | None = None
    # Any endpoint speaking the Anthropic Messages API. `robbie once --model` only,
    # since neither budget backend can measure spend there.
    review_base_url: str | None = None
    review_api_token: SecretStr | None = None
    # what a reviewer presents to the model proxy. Worth nothing off the compose
    # network, which is the point: it is what a container holds instead of a key.
    model_proxy_token: SecretStr | None = None
    # Contents + Pull requests write, and the only token here that can change the
    # repository. It never reaches a container: the fixer's patch comes back out
    # and the push happens here. Unset = the fixer does not run.
    fixer_gh_token: SecretStr | None = None


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("ROBBIE_CONFIG", "config/robbie.yaml"))
    if not p.is_file():
        raise SystemExit(f"config not found: {p} (set ROBBIE_CONFIG or pass --config)")
    cfg = Config.model_validate(_read_yaml(p))
    branding.set_name(cfg.bot_name)
    try:
        cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        raise SystemExit(f"state_dir {cfg.state_dir} is not writable: {ex}") from ex
    _check_paths(cfg)
    return cfg


class _NoDupes(yaml.SafeLoader):
    """Same reason as `extra="forbid"`: a key written twice is a typo, not an edit.
    PyYAML keeps the last one silently, so half a config can sit dead."""

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

    Compose mounts them into the orchestrator at the same path, which is what
    makes checking them from in here mean anything. A wrong one is otherwise
    invisible until the first real review — `--dry-run` starts no container.
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

    At boot, not at first use: a missing key should stop the daemon rather than
    surface 10 minutes later as a failed review.
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
        fixer_gh_token=_optional_secret("FIXER_GH_TOKEN"),
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
    if s.fixer_gh_token is not None and (
        s.fixer_gh_token.get_secret_value() == s.reviewer_gh_token.get_secret_value()
    ):
        raise SystemExit(
            "FIXER_GH_TOKEN is the same value as the reviewer's token. The reviewer "
            "runs a PR's own code with bypassPermissions, so that hands write access "
            "to every container: give the fixer a token of its own."
        )
    if s.reviewer_gh_token.get_secret_value() == s.gh_token.get_secret_value():
        # The security bet is that the worst a reviewer can do with its token is
        # exfiltrate it, which only holds while the token is read-only. A warning
        # and not a refusal: one token is a legitimate way to start.
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
