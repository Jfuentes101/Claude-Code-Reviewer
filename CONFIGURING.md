# Configuring robbie

Task-oriented. For *why* anything is shaped this way, see the README.

## Where each thing lives

| file | holds | on change |
|---|---|---|
| `.env` | every secret, plus the host paths compose interpolates | `docker compose up -d` |
| `config/robbie.yaml` | everything non-secret: repos, budgets, cadence, models | restart: `docker compose restart robbie` |
| `config/slack-users.tsv` | github login → slack member id, one per line, tab separated | nothing — read per DM |
| `policy/` | robbie's cross-repo review standards | nothing — mounted, next review uses it |
| `<repo>/.claude/commands/code-review.md` | that repo's own review criteria, read from the PR's **base** branch | nothing — next review reads it |
| `docker-compose.override.yml` | per-host tweaks, never committed | `docker compose up -d` |

A key set twice in the YAML is a startup error, and so is one robbie does not know.
Nothing is read mid-tick: the daemon loads its config at boot.

## Change the reviewer account (the "me")

The queue is *PRs with the label whose review is requested from this account*, so
this is the identity everything hangs off.

1. `repos[].reviewer_login` — the login.
2. `GH_TOKEN` — must belong to it (scope `repo`); reviews are posted **as** this account.
3. `GH_TOKEN_REVIEWER` — read-only, for the container. Not the same token.
4. `docker compose up -d`.

Two things do not move with it: reviews already published stay under the old
account, and the reply sweep only sees threads opened by the *current*
`reviewer_login` — old threads stop being answered. Close them by hand, or point
`reviewer_login` back for one `robbie threads --pr N`.

## Change who hears about it

| want | set |
|---|---|
| holds, failures, budget warnings, unmapped authors | `slack.owner_id` (one person) |
| the `ok` line — the one verdict that leaves no trace on the PR | `slack.approved_ids: [U…, U…]`; empty falls back to `owner_id` |
| the team channel note on `needs-work` / `comment` | `repos[].slack_channel`; unset posts nothing. Invite the bot to it |
| the DM to a PR's author | a row in `slack.users_file`: `githublogin<TAB>U01234567`. `#` comments a row out |

An author with no row gets no DM and the owner is told once, per author.

## Add or remove a repo

```yaml
repos:
  - slug: owner/name
    reviewer_login: the-bot-account
    bare: /srv/robbie/repos/name.git      # HOST path
    label: "Code Review"                  # what puts a PR in the queue
    needs_work_label: "❌ NEEDS WORK! ❌"   # the brake; must exist in the repo
    review_command: .claude/commands/code-review.md
    slack_channel: C0…
    ci_phrase: run-ci                     # posted verbatim on an `ok`
    ignore_checks: ["CodeRabbit"]         # never counted as a red build
```

Then `./scripts/mirror-sync owner/name` and `docker compose up -d`. Both labels have
to exist in the repo already and `review_command` has to be merged on the base
branch — robbie creates neither.

Removing one: delete the entry and restart. Its rows stay in the database, and
`ci_watch` stops chasing any approval it was still following.

## Change which model reviews

```yaml
review_models:                       # empty = the account's own model, always
  - {model: glm-5.2:cloud, via: endpoint}
  - {model: sonnet, weight: 2}       # via: backend is the default
```

Weights are relative slots. `via: endpoint` needs `REVIEW_BASE_URL` and
`REVIEW_API_TOKEN` in `.env` and is billed against the endpoint's own meter, not
the account's. One model for one run, ignoring the split:
`robbie once --repo o/r --pr N --model <tag>`.

## Change what it is allowed to spend

| | `backend: api` | `backend: oauth` |
|---|---|---|
| ceiling | `budget.daily_usd`, from measured per-run cost | `budget.stop_pct`, % of the 5h window |
| held per review in flight | `budget.reserve_usd` | `budget.reserve_pct` |

Third-party endpoints have their own pair, `budget.endpoint_stop_pct` and
`endpoint_reserve_pct`, in percent of that provider's allowance — their dollar cost
cannot be trusted, so it is not gated in dollars.

Raising the reserve lowers how many reviews can run at once; the budget can cap the
fleet below `max_concurrent_reviews`, and that is the intended behaviour.

## Change the pace

| | |
|---|---|
| `poll_interval_s` | seconds between ticks (600) |
| `max_concurrent_reviews` | containers at once (3) |
| `max_concurrent_checks` | gate/thread reads at once — API calls, not containers (8) |
| `docker.timeout_s` | per-review cap, after which the container is killed (1800) |
| `docker.cpus` / `docker.memory` | ceilings per reviewer, not reservations |
| `review_effort` | how hard the model thinks (`high`) |
| `stale_review_days` | `robbie digest` threshold |

## Look at what it did

```bash
docker compose exec robbie robbie status                  # the queue, PR by PR
docker compose logs -f robbie
docker compose --profile dashboard up -d                  # 127.0.0.1:4020
scripts/model-compare [--pr N]                            # per-model table
ls <state_dir>/reviews/                                   # one transcript per run
sqlite3 <state_dir>/robbie.db 'SELECT pr, model, verdict, cost_usd FROM reviews
                               ORDER BY created_at DESC LIMIT 20;'
```

## Make it stop

| | |
|---|---|
| this PR, for now | put `needs_work_label` on it — the brake gate holds silently |
| this repo | remove its entry, or take the queue label off its PRs |
| everything, gracefully | `docker compose down` — SIGTERM, and the tick in flight finishes |
| everything, now | `docker compose kill` — a review in flight is lost, its row reaped at next boot |

## After changing anything

| changed | do |
|---|---|
| `config/robbie.yaml`, `.env` | `docker compose up -d` |
| `policy/`, `slack-users.tsv`, a repo's review command | nothing |
| `reviewer/entrypoint.sh`, any Dockerfile, `src/` | `./scripts/update` |
| `ROBBIE_UID` / `ROBBIE_GID` | `docker compose build` — they are baked into the reviewer image |
