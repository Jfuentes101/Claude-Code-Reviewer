# robbie

Pre-reviews pull requests before a human does. Polls GitHub for PRs that request
a given reviewer, runs one throwaway container per PR to review it, and publishes
the findings as inline comments on the diff.

The model never writes anywhere. It emits delimited blocks; robbie publishes
them. So the comment, the inline notes and the label are deterministic rather
than something a model has to remember to do.

```
┌─ orchestrator ──────┐   docker.sock    ┌─ reviewer × N (--rm) ─┐
│ polling, gates, DB  │ ───────────────▶ │ clone, claude -p       │
│ publish, slack.py   │                  │ MCP over http ─────┐   │
└─────────┬───────────┘                  └────────────────────┼───┘
          │            network "robbie"                       │
          └──────────────┬────────────────────────────────────┘
                         ▼
              ┌─ mcp-sentry ─┐ ┌─ mcp-asana ─┐   optional sidecars;
              │ read-only    │ │ read-only   │   tokens live here
              └──────────────┘ └─────────────┘
```

## Why reviewers are spawned, not pooled

A warm pool of N workers would save the container start (~0.5s) and the clone
from the local mirror (~2s) on a job that takes 5–30 minutes. It would cost a
queue broker, worker lifecycle handling, and state surviving between jobs —
which is the one thing a review must not have, since every pass starts from a
clean checkout. `max_concurrent_reviews` is the knob; there is no pool to size.

## Where MCP belongs, and where it doesn't

MCP exposes tools **to a model**, so the only thing that should get an MCP
server is the review model, and only for reads that inform a review: is this
file throwing in prod, is there a ticket describing this feature, what does the
call graph look like. Configure those in `review_mcp`.

Two things deliberately do not use it:

- **robbie's own Slack notifications.** The orchestrator is deterministic Python
  making one HTTP POST. An MCP service there would add a handshake and a process
  to babysit and buy nothing. That is `slack.py`.
- **anything that writes.** The review model emits blocks and nothing else. Give
  it a `post_message` tool and the property that makes the output trustworthy —
  that a model cannot act, only report — is gone.

A sidecar beats stdio-inside-the-image for one concrete reason: the token stays
in the sidecar, and the container running a model with `bypassPermissions` never
sees it. Same reasoning as `GH_TOKEN_REVIEWER`.

### mcp-sentry

Ships enabled. It answers one question that changes review outcomes: **is the
code this PR touches already failing in production?** A dropped nil guard on a
line throwing 4k times a day is not a nitpick.

| tool | for |
|---|---|
| `issues_for_paths` | the changed files → unresolved prod errors in their stack traces |
| `search_issues` | Sentry query syntax, when a diff or PR body names an error |
| `issue_detail` | one issue + the in-app frames of its latest event |

`issues_for_paths` tries each path as an exact stack filename first, then as a
wildcard on the basename, because Sentry indexes filenames as the stack trace
spells them — which for bundled or relocated code is not the repo-relative path.
Without the fallback it answers "nothing" for most real PRs. The response carries
`match` so the model knows a wildcard hit may be a different file with the same
name, and `truncated` when a PR changed more files than the cap.

Needs `SENTRY_TOKEN` (read-only: `event:read`, `project:read`) and
`SENTRY_ORG_SLUG`; `SENTRY_PROJECTS` optionally narrows it. Opt in with
`docker compose --profile sentry up -d` and point `review_mcp` at it; leaving
both alone is how you don't run it.

`MCP_ALLOWED_HOSTS` is load-bearing, not decoration: the SDK's DNS rebinding
protection validates the `Host` header, and a reviewer connecting to
`http://mcp-sentry:8080/mcp` sends `Host: mcp-sentry:8080`. Unlisted hosts get a
421 and every tool call fails. Rename the service, update this too.

No auth between reviewer and sidecar: it's a private compose network exposing
read-only tools, which is exactly what the reviewer is allowed to read anyway.

## When a PR gets reviewed

All of these have to hold:

1. it carries the queue label (`Code Review` by default)
2. the configured reviewer's review is actually requested on it
3. it does **not** carry the needs-work label
4. it changes something, and something new since the last pass
5. no comment of robbie's is still open there without a reply, a fix or a resolve
6. CI on the head commit is not red — still running is fine, judged as it stands

**Gate 3 is the brake.** While robbie's last pass stands unaddressed, commits
land without triggering anything. Taking the label off is how the author says
"ready for another pass" — every review that sets it says so — and an `ok`
verdict clears it. A blocked PR holds silently: no DM, every tick.

Gates 4–6 differ in whether they leave a trace. 4 is recorded (there is no new
code to judge, so it is not looked at again); 5 and 6 are not, so a reply, a
resolve or a green build brings the PR back on its own.

**A tick answers before it reviews.** Gate 5 is only honest if conceded findings
get closed, so every tick first works through the threads somebody replied to —
judging each reply against the code and then resolving, answering or leaving it —
and only then reads the queue. That order is not cosmetic: one `my_threads` read
per PR feeds both gate 5 and the prompt's prior-conversation block, so closing
afterwards would leave the gate ruling on threads the same tick is about to close,
and a re-review re-raising findings it conceded seconds later. `robbie threads
[--pr N]` runs that half alone.

Dedup key is `repo:pr:head_sha:last_review_requested_at`, so a PR is reviewed
once and re-reviewed when new commits land. A re-request without new commits
changes the key too, but gate 4 turns that into one DM instead of a second
review of the same diff.

## Verdicts

| verdict | what gets posted | effect on the queue |
|---|---|---|
| `needs-work` | changes-requested review + inline comments + label | clears the review request; the PR leaves the human's queue until a re-request |
| `comment` | plain comment + inline comments (one call each) + label | request untouched; the human still gets their own pass |
| `ok` | the label comes off and a `ci_phrase` comment goes on | request untouched |

`comment` posts its inline notes individually on purpose: a submitted review
would fulfil the human's pending request, and it shouldn't.

An `ok` is the only verdict that leaves no trace on the PR, so it is the only one
that DMs — one line, to everyone in `slack.approved_ids`, since the PR just left
all of their queues. The others announce themselves in the channel, on the PR, and
to the author.

That is the whole of Slack: the author hears that their review is ready, the team
channel hears that a review was posted, and an approval is DMed to the humans who
would otherwise never know it happened. The model writes none of it — the copy is
in `slack.py` and the numbers come from the findings.

That comment is also how CI starts. Where a build no longer runs on push because
the push volume made it too expensive, the review becomes the gate in front of
it: `ci_phrase` (default `run-ci`) is posted as the **entire** comment body — no
signature, no hidden marker — because whatever listens for it may be matching the
whole comment. It is posted at most once per head sha, so forcing a re-review of
an approved commit cannot buy a second build.

## Setup

### What has to exist before `compose up`

Nothing here is created for you. `up` is the last step, not the first.

| | |
|---|---|
| **docker + the compose plugin** | and access to `/var/run/docker.sock`: reviewers are *sibling* containers spawned over it, so there is no docker-in-docker to install |
| **`git`, and disk for a mirror** | one bare clone per repo, the size of that repo — ~1 GB for a monolith. `./scripts/mirror-sync` makes it; compose never can, because the mirror is mounted read-only |
| **a GitHub account for the reviewer** | its *pending review requests are the queue*. `GH_TOKEN` (scope `repo`) must belong to it, because reviews are posted as that account. Put a second, **read-only** token in `GH_TOKEN_REVIEWER` — that is the one the model gets |
| **a Slack bot token** | `chat:write`, invited to `slack_channel`. Author DMs need a `slack-users.tsv` you fill in by hand; the shipped example maps nobody |
| **a model to review with** | `ANTHROPIC_API_KEY` for `backend: api`, or a `.credentials.json` from a machine where `claude login` ran for `backend: oauth`. Any endpoint speaking the Anthropic Messages API can take some or all of the reviews instead — see [Which model reviews](#which-model-reviews) |

Everything else — the database, the state volume, the compose network, the
images — is made by `up` itself.

```bash
git clone <this repo> && cd robbie
cp .env.example .env                       # tokens; chmod 600
cp config/robbie.yaml.example config/robbie.yaml
cp config/slack-users.tsv.example config/slack-users.tsv
# edit all three

./scripts/setup
```

`setup` checks everything above, clones any mirror the config names and does not
have, builds both images and starts the daemon. `scripts/setup --check` stops
after the checks and changes nothing. By hand it is the same four commands:

```bash
./scripts/mirror-sync owner/repo           # ~1 GB clone, and it must come first
docker compose build                       # ROBBIE_UID/GID are read HERE, not at up
docker compose up -d
docker compose logs -f
```

Later, on a new commit:

```bash
./scripts/update      # pull, rebuild both images, recreate what changed
```

Rebuilding the **reviewer** image is the part that gets forgotten by hand:
`reviewer/entrypoint.sh` lives in it, and a change there is invisible until
something rebuilds it. A restart is not abrupt — compose sends SIGTERM and robbie
finishes the tick in flight, so an update can take as long as its longest review.

### If you skip one

`robbie` validates its paths at boot and exits rather than start half-configured,
and `restart: unless-stopped` then loops it. So a skipped step is a repeating line
in `docker compose logs`, not a daemon that quietly reviews nothing:

| skipped | what you see |
|---|---|
| the mirror | docker creates the bind path as an **empty root-owned directory**, then `mirror … is not a directory here; ./scripts/mirror-sync creates it` |
| a host path (`ROBBIE_MIRRORS`, `ROBBIE_POLICY`, `CLAUDE_CREDENTIALS`) that only exists inside the container | same shape: the host gets an empty directory where a file or a repo should be. These three are handed to the *host* daemon, so they must be host paths mounted at the same path on both sides |
| a token | `GH_TOKEN is not set`, `backend=api needs ANTHROPIC_API_KEY`, or `CLAUDE_CREDENTIALS not readable` |
| `ROBBIE_UID`/`ROBBIE_GID` on `backend: oauth` | nothing at boot — every *review* fails on an unreadable token instead. They are baked into the reviewer image at build time, so changing them means `compose build` again |

Then prove it before it reviews anything:

```bash
docker compose exec robbie robbie --dry-run poll --once
```

That reads the queue, runs every gate, and writes nothing — so a bad token, an
unreadable mirror or a path that means something different inside the container
surfaces there instead of halfway through a paid review.

Put `./scripts/mirror-sync` on a timer (systemd, cron). A stale mirror costs a
slower clone, not a wrong review — `gh` fetches the PR head itself.

### What each file wants from you

**`.env`** — every secret, plus the paths and ids compose interpolates. Start by
choosing `backend`, because it decides which of them matter: `api` needs
`ANTHROPIC_API_KEY`, `oauth` needs `CLAUDE_CREDENTIALS` pointing at a copy of
`~/.claude/.credentials.json` from a machine where `claude login` has run. Put a
**read-only** `GH_TOKEN_REVIEWER` next to the writing `GH_TOKEN`: the reviewer
runs a model with `bypassPermissions`, and publishing is not its job.

**`config/robbie.yaml`** — the identity that matters is `reviewer_login`, whose
pending review requests *are* the queue; `GH_TOKEN` must belong to it, because
its reviews are posted as that account.

**`config/slack-users.tsv`** — GitHub login → Slack id, for the DM to a PR's
author. The shipped example maps nobody, so until you fill it every author DM
comes back unmapped (once per author, as a warning to the owner).

### Four things that will bite you

**Host paths.** The orchestrator spawns *sibling* containers, so every bind mount
it builds is resolved by the host docker daemon. `repos[].bare`, `policy_dir` and
`CLAUDE_CREDENTIALS` must be **host** paths, and the compose file mounts them at
those same paths inside the orchestrator so both sides agree. Change one, change
the other. A path that only exists inside the orchestrator gets silently created
on the host as an empty directory when a reviewer spawns.

**The reviewer's uid, on `backend=oauth`.** The credentials file is mounted mode
600 and the CLI refreshes it in place, so the container's user has to be the host
user that owns it: set `ROBBIE_UID`/`ROBBIE_GID` to `id -u`/`id -g` before
building. Get it wrong and *every* review fails on an unreadable token — the base
image already occupies uid 1000, so this is not the theoretical kind of mismatch.

**`backend=oauth` spends a human's plan.** Reviews come out of the same 5-hour
window as that person's own interactive work, and a fleet of them empties it fast.
`stop_pct` is the ceiling, `reserve_pct` is what each in-flight review holds back
so a free slot cannot start on a reading that three unfinished containers are
about to invalidate, and `max_concurrent_reviews` is how many can be wrong at
once. On a shared plan, 1–2 is a kinder cap than 3.

**AppArmor hosts.** Ubuntu and Pop!_OS ship a `docker-default` profile that treats
the profile transition on `exec` as gaining privileges, so with
`no_new_privileges` even `exec /usr/bin/bash` returns EPERM. Set
`docker.no_new_privileges: false` there. The default `true` is right on a plain
Debian VPS. Dropping capabilities is not part of that trade and always happens.

## Commands

```bash
docker compose exec robbie robbie status                        # queue overview
docker compose exec robbie robbie once --repo o/r --pr 123      # force one review
docker compose exec robbie robbie poll --once                   # a single tick
docker compose exec robbie robbie digest --days 7               # stuck-in-review digest
docker compose exec robbie robbie threads --pr 123              # just the replies half
docker compose exec robbie robbie --dry-run poll --once         # decide, write nothing
```

## The metrics panel

Off unless asked for:

```bash
robbie dashboard                                   # http://127.0.0.1:4020
docker compose --profile dashboard up -d           # same, in compose
```

One page, server-rendered, no JavaScript and no new dependency — stdlib
`http.server` over the SQLite the daemon already writes. It shows the system at a
glance (reviews in flight, spend billed to the account, disk, schema), both spend
meters with their live readings, the model arms with their configured share and
what each has actually found, the PRs held or failed with the reason, the recent
reviews with findings and timings, and the transcripts, which are the closest thing
to a log of a review — each one readable in the browser.

Two things it deliberately does not do. It never shows a per-review dollar figure
for a third-party model, because the CLI prices those off its own table and that is
not the provider's bill. And it holds a **read-only** database connection, so a bug
in the panel cannot touch a review's row — which is also why its volume is mounted
writable: SQLite needs the `-shm` file to read a WAL database at all.

**It has no auth**, and it shows PR titles, diffs quoted in transcripts and spend.
It binds to `127.0.0.1` unless told otherwise and the compose service publishes on
the loopback only. Reaching it from elsewhere means an authenticating proxy in
front, not `--host 0.0.0.0`.

`digest` is the nag for PRs parked on a standing changes-requested review that
nobody ever re-requests — they fall out of every queue otherwise. It clocks on
the age of the review, not on last activity, because those authors keep pushing.
Run it weekly.

## Where the review criteria come from

Three layers, and which one owns a rule is not arbitrary:

| layer | lives in | scope | changed by |
|---|---|---|---|
| output contract + CI facts | `contract.py`, stdin | the prompt | a robbie release |
| cross-repo standards | `policy/` → reviewer's `~/.claude/` | **user** | editing a file |
| this repo's criteria | `.claude/commands/…` on the **base branch** | read explicitly | a merged PR |

`policy/` is mounted read-only into every reviewer and copied to its user scope,
which the CLI loads on its own. That means it costs no prompt tokens, the model
cannot forget to read it, and editing a file changes the next review — no
rebuild, no restart.

`policy/CLAUDE.md` is distilled from review corrections a human actually made,
which is why it leads with the rule that kills false positives rather than with
style:

1. **Reachability first** — a mechanism existing is not a bug; trace a live path
   or classify it as defense-in-depth. A fix for an unreachable state is
   speculative complexity, and shipping one as a live bug discredits every other
   finding in the review. Includes the trap of concluding absence from a count in
   a trimmed or stale dataset.
2. **Severity discipline** — a table plus the test "if you cannot name the input,
   the state and the wrong output, it is not Must-fix". Inflation makes the whole
   review ignorable, and it jams the needs-work label brake.
3. **Cheapest change that works** — speculative generality, hand-rolled over
   built-in, deletion missed, and the note that a change made only to satisfy a
   linter can still change behavior.
4. **Comment hygiene** — zero by default, two lines maximum, and no narration of
   fixes, tests, callers or ticket numbers.
5. **PR scope** — 2+ unrelated major features is Critical with a split
   recommendation, and the exceptions that must not be flagged.
6. **Tests** — weakened assertions in the same PR as the code they guard, stubs
   standing in for owned logic rather than boundaries, global state restored by a
   trailing line a raised assertion skips, "no such thing as a flaky test" and its
   inverse "do not touch a green test", coverage judged by consequence.
7. **Recurring bug families** — check-then-act against a unique constraint
   (including the volatile-key variant that collides deterministically),
   privilege derived from client input, shared-row lock contention, silently
   dropped options.
8. **How to write it** — lead with the verdict, cap prose, evidence in a block.

**The criteria are read from the base branch, never from the checkout.** A PR is
under review; the rules it is judged by are not up for negotiation by it.
Otherwise a PR could rewrite `.claude/commands/code-review.md` to say "emit verdict ok",
or add a `.claude/CLAUDE.md` that instructs the reviewer to find nothing. For the
same reason the reviewer runs with `--setting-sources user`, so a `.claude/`
appearing in the PR is never loaded as instructions.

To add a standard: edit a file under `policy/`. To make it language-specific,
add another file — everything in that directory reaches the user scope.

## CI and linters

**robbie does not lint. CI does, and gate 6 turns a lint failure into a blocker
instead of something robbie rediscovers.** If the team's CI runs rubocop and
eslint, then by the time a PR reaches a review its linters are already green —
running them again in the reviewer to learn that is pure cost, and it would need
the whole language toolchain in the image plus a `bundle install` per branch.

What the reviewer gets instead is the check state, injected into its prompt as
data by the wrapper — from the same `statusCheckRollup` gate 6 judged:

```
CI state on the head commit, as the wrapper read it moments ago:
  passing (2): ci/circleci: build, rubocop · STILL RUNNING: rspec · FAILING: CodeRabbit
```

Three things follow, and each closes a failure mode:

- The model's `*CI & linters:*` line cannot contradict the gate that let the
  review happen, because both read the same bytes.
- It never shells out to discover CI state and never waits for a running check.
  It has no CI-provider credentials anyway.
- A linter that is not installed in the image is declared **expected**, not a
  gap. Without that, a review command asking for `bundle exec rubocop` produces
  "Linters: skipped, unavailable" — noise that reads like the review was
  incomplete.

Checks that gate 6 ignores (`ignore_checks`, CodeRabbit by default) still appear
as failing, since the model should know a reviewer bot is unhappy even though it
isn't a broken build.

**If you do want local linting** for a repo, the escape hatch needs no config:
put the toolchain in a child image, point that repo's `image:` at it, and say so
in that repo's own review command file. The review criteria live in the repo
being reviewed, which is where a per-repo lint policy belongs.

## Which model reviews

Two separate questions: **who is billed**, and **which model runs it**.

`backend` answers the first — `api` (an `ANTHROPIC_API_KEY`) or `oauth` (a
`claude login` session). That account's default model reviews everything unless
you say otherwise.

`review_models` answers the second. It splits reviews between arms by weight:

```yaml
review_models:
  - {model: glm-5.2:cloud, via: endpoint}
  - {model: qwen3.5:397b-cloud, via: endpoint}
  - {model: sonnet, weight: 2}          # via: backend, the default
```

`via: endpoint` sends the run to `REVIEW_BASE_URL` with `REVIEW_API_TOKEN`
instead — **any endpoint that speaks the Anthropic Messages API** (`/v1/messages`).
`https://ollama.com` serves one, so a token from its settings page is the entire
setup: no local `ollama serve`, no sidecar, no OAuth, nothing mounted. Same
container, same policy, same prompt, same block contract — the model is the only
thing that changes, which is what makes comparing them mean anything.

That endpoint is undocumented by ollama (the published API is `/api/chat`), so it
can move without notice. If it does, a shared `ollama serve` sidecar is the
fallback and the URL is config, not code.

- **Routing is a hash of the dedup key**, not a counter: no state to keep, and the
  same commit always lands on the same arm, so a re-review compares like with like
  instead of moving the variable being measured.
- **An arm whose meter is spent is covered by the other side** — the heaviest arm
  across the endpoint divide, and only if *its* meter allows. Coverage over an
  exact ratio, deliberately. `once --model` is never substituted: that is a
  request, not a preference.
- **The meter follows where the run is billed**, not `backend`. A review sent to a
  third party spends nothing on the account, and holding it against the account's
  window would refuse it for a reason that does not apply to it.

`scripts/model-compare` prints the table from rows robbie already writes — verdict,
findings, blocking, inline, summary findings, tokens, seconds, per model per commit.
Three things in it do **not** compare across providers, and the script will not
pretend otherwise:

| column | why |
|---|---|
| `cost_usd` | fiction for endpoint runs: the CLI prices tokens from its own table for a model it does not price. The real number is the provider's own meter |
| `tokens_in` | Anthropic's excludes cache reads and a third party has no cache, so the same review reads as 39 tokens on one side and 761k on the other |
| `findings` | counts the INLINE block only, and a re-review is asked *not* to repeat inline what is already posted. Read it next to `summary_findings`, never alone |

## Cost

`backend: api` (default) uses `ANTHROPIC_API_KEY` and reads the per-run cost the
CLI reports, so `budget.daily_usd` is enforced against measured spend. Every
review's cost, token counts and duration land in SQLite:

```sql
SELECT repo, pr, verdict, cost_usd, duration_s FROM reviews ORDER BY created_at DESC LIMIT 20;
```

`backend: oauth` reuses a `claude login` session instead and gates on the plan's
5-hour window (`budget.stop_pct`). It works, but the token refresh writes back to
the credentials file, which concurrent reviewers can race on. Prefer `api` unless
you have a reason.

An unreadable budget is never treated as unlimited: robbie warns once and keeps
going, so a broken endpoint degrades loudly.

Either backend can only read what has already been **spent**, and a container
halfway through a review has spent nothing yet. So the gate is asked again right
before a container starts rather than only in the gate phase minutes earlier, and
every review in flight — plus the one asking — holds back `reserve_pct` (oauth) or
`reserve_usd` (api). Without that, every free slot reads the same safe number at
the same moment and they all start.

One reserve, not an estimate per PR: across 50 recorded reviews the same PR
re-reviewed six times ranged from $1.57 to $6.23, so within-PR spread is as wide
as the population's and a size heuristic would be false precision. Size the number
from your own table instead — `SELECT cost_usd FROM reviews` against the spend of
one window is the whole calibration. A consequence to expect: the budget can now
cap the fleet below `max_concurrent_reviews`, because what the money covers is the
real limit.

## Adding a repo

Append to `repos:` in the config. Each entry carries its own label, needs-work
label, review command path, Slack channel and image. The review command is a
file **in the repo** (`.claude/commands/code-review.md` by default) — the reviewer
checks the PR out and reads it there, so the review criteria are versioned with
the code they judge.

If a repo wants its linters run locally, build a child image `FROM
robbie-reviewer` with that toolchain and point its `image:` at it. Keeping
language runtimes out of the base image is what keeps it small.

## Security posture

- The reviewer gets `GH_TOKEN_REVIEWER` when set — put a **read-only** token
  there. It runs a model with `bypassPermissions`; publishing is not its job.
- The reviewer sees exactly one host path, the mirror, read-only. Everything
  else it touches dies with the container (`--rm`, `--cap-drop ALL`,
  `no-new-privileges`, cpu/memory/pids caps).
- `publish.py` is the only module that writes to GitHub: one review or one
  comment, plus the needs-work label. No approve, no merge, no close, no
  arbitrary API.
- **The accepted risk is exfiltration, not writes.** The reviewer runs arbitrary
  code from the PR with `bypassPermissions`, and nothing restricts its outbound
  network. A read-only token means the worst it can do with `GH_TOKEN_REVIEWER`
  is read and send it somewhere — that is the bet, and it only holds while that
  token is read-only. The container joins `docker.network` only when
  `review_mcp` is set, so with no sidecars there is nothing internal to reach.
- The orchestrator holds the docker socket, which is root on the host. That is
  the accepted trade for a single-tenant VPS. If the host is shared, run the
  orchestrator under systemd on the host instead and keep only the workers in
  docker — same argv, no socket mount.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The gates, the block parser, the anchoring and the state semantics are pure and
covered. That is deliberate: the policy is the part that must not be
"observable only in production".
