# robbie

Pre-reviews pull requests before a human does. Polls GitHub for PRs that request
a given reviewer, runs one throwaway container per PR to review it, and publishes
the findings as inline comments on the diff.

The model never writes anywhere. It emits delimited blocks; robbie publishes
them. So the comment, the inline notes and the label are deterministic rather
than something a model has to remember to do.

```
┌─ orchestrator (long-lived) ──────────────────────────┐
│  poll → gates → Semaphore(N) → parse → publish → 📣  │
└──────────────── docker.sock ─────────────────────────┘
                        ▼
┌─ reviewer × N (--rm, capped, read-only mirror) ──────┐
│  clone, gh pr checkout, claude -p, print blocks, die │
└──────────────────────────────────────────────────────┘
```

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
