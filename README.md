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
`SENTRY_ORG_SLUG`; `SENTRY_PROJECTS` optionally narrows it. To drop it, delete
the service from `docker-compose.yml` and clear `review_mcp`.

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

Dedup key is `repo:pr:head_sha:last_review_requested_at`, so a PR is reviewed
once and re-reviewed when new commits land. A re-request without new commits
changes the key too, but gate 4 turns that into one DM instead of a second
review of the same diff.

## Verdicts

| verdict | what gets posted | effect on the queue |
|---|---|---|
| `needs-work` | changes-requested review + inline comments + label | clears the review request; the PR leaves the human's queue until a re-request |
| `comment` | plain comment + inline comments (one call each) + label | request untouched; the human still gets the briefing for their own pass |
| `ok` | nothing; the label is cleared | request untouched; briefing only |

`comment` posts its inline notes individually on purpose: a submitted review
would fulfil the human's pending request, and it shouldn't.

## Setup

```bash
git clone <this repo> && cd robbie
cp .env.example .env                       # tokens; chmod 600
cp config/robbie.yaml.example config/robbie.yaml
cp config/slack-users.tsv.example config/slack-users.tsv

./scripts/mirror-sync owner/repo           # create the bare mirror
docker compose build
docker compose up -d
docker compose logs -f
```

Put `./scripts/mirror-sync` on a timer (systemd, cron). A stale mirror costs a
slower clone, not a wrong review — `gh` fetches the PR head itself.

**The one thing that will bite you:** the orchestrator spawns *sibling*
containers, so every bind mount it builds is resolved by the host docker daemon.
`repos[].bare` in the config must be a **host** path, and the compose file mounts
the mirror directory at that same path inside the orchestrator so both sides
agree. Change one, change the other.

## Commands

```bash
docker compose exec robbie robbie status                        # queue overview
docker compose exec robbie robbie once --repo o/r --pr 123      # force one review
docker compose exec robbie robbie poll --once                   # a single tick
docker compose exec robbie robbie digest --days 7               # stuck-in-review digest
docker compose exec robbie robbie --dry-run poll --once         # decide, write nothing
```

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
