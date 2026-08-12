# Review standards

These apply to every repo robbie reviews. The repo's own review command adds to
them; where both speak, follow both.

Cite `file:line` from the diff for every finding. If you cannot point at a changed
line, you are guessing — drop it or move it to the summary.

---

## 0. Recon: situate the change before judging it

This section is preparation, not judgment — §1 still decides what becomes a
finding. But a review that skips it can only see one file at a time, and the
defects that reach production live between files.

**Name the shape of the change first.** Third-party integration, form or CRUD
path, background job, migration, money calculation, report, UI pack. The shape
predicts where the bugs are and, more usefully, **which files should have been
touched and were not**.

**Every shape already has siblings in this repo. Find two and read them.** The
question is almost never "is this correct on its own" — it is "does this agree
with the three places that already do this". A vendor client, a form, a
calculator: whichever the diff adds, the repo has precedents, and they carry the
conventions that no linter enforces — where config lives, what a service returns,
which layer owns validation, where constants and copy go, what gets a decorator.
A new file that invents its own answer is a finding even when it works.

**Map the data flow end to end before reading for defects.** Where the value
enters, every layer it crosses, where it is persisted, where it is read back, who
reads it later. Write the chain down. Two failures that a per-file read cannot
see, both observed:

- **Siblings that disagree.** Two calculators for the same quantity, one
  tax-inclusive and one tax-exclusive. Each is self-consistent; the pair is a
  bug waiting for a config flip. When a diff touches one member of a family,
  read the whole family.
- **Deferred work with a gap.** A callback schedules a job minutes out; the state
  it depends on can change in the gap and the cancel path no-ops. Whenever the
  change defers, enumerate what can happen in between — cancel, refund, expire,
  a second attempt.

**Grep callers of everything whose behavior or signature the diff changes; the
callers it did not touch are the risk zone.** List them by `file:line`. A return
value changed from `self` to `nil` on one branch is invisible in its own file and
crashes at a call site three files away, outside the handler that was supposed to
catch it. Same for a renamed key, a narrowed scope, a new nullable column.

**Values crossing a boundary need a bound and a failure path.** Anything arriving
from a vendor, a webhook or a client — amounts, ids, dates, quantities — and
anything leaving toward one. An unbounded amount handed straight to a refund is
Critical however clean the code around it reads. Check what the repo's other
integrations do at the same boundary; usually one of them already has the guard.

**Concept defined twice.** Before relying on a helper, scope or predicate, grep
for the other definitions of the same idea. Preferring the wrong one of two
similarly-named scopes silently widens access.

**Then judge the complexity** — §3. Recon feeds it: a change is only provably
more complex than it needs to be once you have seen how the repo already solves
the same problem.

One consequence worth stating plainly: **a large diff that produces zero findings
is evidence the recon did not happen, not evidence the code is clean.** Say what
you traced. If the budget ran out before the flow was mapped, hold the review and
say so — that is a useful answer. A silent approval is not.

## 1. Reachability comes before everything else

**A mechanism existing is not a bug. A finding is real only once a live code path
is traced to it.** This is the rule that protects the whole review: a fix for an
unreachable state is speculative complexity, and presenting it as a live bug
erodes trust in every other finding you wrote.

Before reporting anything, answer in one sentence: **what concrete user action,
job, webhook or rake task reaches this line?** Name it. Grep callers up to an
entry point — controller, mutation, job, CLI. If the honest answer is "a state
only a test constructs" or "manual SQL", say so and classify it as
defense-in-depth, not a bug.

Three ways this goes wrong, all observed:

- **An input the UI cannot produce.** A backdating bug is not reachable if every
  date picker's `minDate` is today.
- **A guard that runs first.** Verifying that a branch computes the wrong value
  proves nothing if an early return upstream means the branch is never entered.
  Trace the call chain to the line, not just to the file.
- **A count from an incomplete dataset.** Never conclude a path is unreachable
  or a state is absent from a low or zero count in a local, trimmed, or sampled
  database. Check whether the table is trimmed by grouping by year first; a cliff
  means trimming, not absence. Reason from the code path instead.

Domain context from a human overrides code-shaped inference. If a reviewer tells
you a flow is impossible in practice, they are describing production and you are
describing syntax.

## 2. Severity

Spend these carefully. Every Must-fix blocks a merge and pulls the author off
other work, so inflation makes the whole review ignorable.

| | |
|---|---|
| **Critical** | Data loss or corruption; a privilege or authorization bypass; a PR bundling 2+ unrelated major features; a global mutation in tests that can break unrelated tests; a test deleted or skipped in a way that hides a real failure. |
| **Must-fix** | A wrong result on a reachable path; money or destructive operations without a test; a bug fix with no test that fails before the fix; an assertion weakened in the same PR as the code it guarded. |
| **Should-fix** | Avoidable complexity; a stub standing in for logic this codebase owns; an untested new branch; control flow inside a test; a linter-appeasing change with a behavior side effect. |
| **Nitpick** | Naming, formatting, placement. Say it once and move on. |

If you cannot name the concrete failure — the input, the state, the wrong output
— it is not Must-fix. Downgrade it or drop it.

## 3. Engineering judgment: the cheapest change that works

The best code is the code not written. Read a diff asking whether each piece
needs to exist at all:

- **Speculative generality.** An interface with one implementation, a factory for
  one product, a config value that never changes, an abstraction "for when we
  add more". Flag it and name what would justify it later.
- **Hand-rolled over built-in.** A helper the standard library, the framework, or
  an already-installed dependency provides. A new dependency for something a few
  lines cover. A platform feature ignored in favour of application code — a DB
  constraint written as a callback, a native input rebuilt in JS.
- **Deletion missed.** A change that adds a path without removing the one it
  replaces. Dead code left behind after a rename or a migration.
- **Clever over boring.** Optimize for the person paged at 3am, not the person
  writing it today.

**Never simplify away** — and flag when a PR does: validation at trust
boundaries, error handling that prevents data loss, security checks,
accessibility basics, or anything the PR description explicitly asked for.

**A change made only to satisfy a linter can still change behavior.** Check the
option a cop demanded actually matches intent — a `dependent:` added for a cop
fired on soft-destroy and permanently nulled financial foreign keys. Prefer the
explicit no-op form over whatever silences the warning.

**A "dead" reference may have been renamed.** Before agreeing that a route,
endpoint or file can be deleted, grep for the feature under a likely new name.
Deleting a renamed thing silently drops coverage of something that still exists.

## 4. Comments in the diff

Default to zero. A comment is at most two lines, ever.

Flag every comment that narrates: `# Fix for X bug`, `# This test verifies…`,
`# Called from controller Y`, a reference to a task, issue or PR number. The team
reads code, not commentary; that context belongs in the commit message and the PR
description.

A comment earns its place only for a *why* that is invisible in the code: a
hidden constraint, a subtle invariant, a workaround for a specific bug, a
non-intuitive business rule. If a change seems to need a longer comment, the code
is the problem — say that instead.

## 5. PR scope

A PR should carry one major feature or a cohesive set of related changes. **2+
unrelated major features is Critical, with a recommendation to split** — not a
warning in passing. Still review the whole thing, but the verdict is
changes-requested with scope as a blocking reason.

Do not flag: a PR of several unrelated bugfixes, or a small necessary fix bundled
alongside a feature.

## 6. Tests

A PR that changes behavior without touching tests is worth questioning. A PR that
changes both is where the interesting failures hide.

### Did the test get weaker in the same PR as the code it guards?

You can see the diff, so you can see this. Highest-value check here. Flag when a
test changed such that **it would now pass with or without the production
change**:

- an assertion loosened — exact value → `be_present`, `eq(3)` → `be >= 0`, a
  literal → a permissive regex, a specific error class → `StandardError`
- an expectation, assertion or example deleted while the behavior still exists
- `skip`, `xit`, `pending`, `.only`, a commented-out example, or a new exclusion
- setup changed so the branch that used to be exercised no longer is — a factory
  trait dropped, a guard-triggering value replaced with a benign one
- a snapshot or fixture regenerated with no argument for why the new shape is right

The question for each changed test: **would this have failed before the
production change in this PR?** If not, it is decoration. Quote before and after,
and ask for the assertion that pins the new behavior.

### Stubs replacing logic instead of boundaries

The discriminator is ownership, not the mocking library:

- **Legitimate** — what you do not control or cannot run: HTTP, payment
  gateways, mail, the clock, randomness, a third-party SDK.
- **Suspect** — a class in this codebase, stubbed to avoid setting it up. That
  test stops proving the two halves fit, and keeps passing after the real
  collaborator's contract changes.
- **Always wrong** — stubbing the object under test, or the very method the PR
  changed.

Prefer the real flow: drive the service, the resolver, the job end to end with
real records. One test walking the main path is worth several asserting on a
mock's arguments.

Same weight: a stub whose **return shape cannot be traced to what the real method
returns** (invented hashes drift from reality and pass forever); a stub that makes
a branch **unreachable in production** (see §1 — the branch may be dead); and
`allow(…).to receive(…)` with no assertion that the interaction matters.

### Global state that leaks into other tests

**Anything global a test mutates must be restored by the same construct that
mutated it** — the block form, or an `ensure` / `after` / `teardown` hook. A
restore written as a trailing line in the example body is **not** safe: a failing
assertion above it raises, the line never runs, and every later test in that
process is poisoned. Critical, not style: the damage lands on unrelated tests and
the failure looks random.

```ruby
# leaks on failure — the return never runs if the expectation raises
Timecop.freeze(Time.zone.parse("2026-01-01"))
expect(subject.due_on).to eq(...)
Timecop.return

# safe
Timecop.freeze(Time.zone.parse("2026-01-01")) { expect(subject.due_on).to eq(...) }
```

Every one of these has actually shipped and cost days of debugging:

- **the clock** — a bare `Timecop.freeze` with no `return`. Downstream, a service
  that compares `created_at < Time.current` sees both collapse to one instant and
  silently returns empty buckets.
- **request state** — `RequestStore[:current_user]` set in a service test with
  nothing to clear it; an audit log in an unrelated test attributed a guest
  action to the leaked admin.
- **`ENV`** — set in `setup`, no teardown. Every later test in the same container
  took a different code path and hit a live third-party API.
- **browser storage** — a session-persisted store that `reset_sessions!` does not
  clear, rehydrating a previous test's cart into the next one.
- **infrastructure** — cache writes, queues left full, mail deliveries not
  cleared, files outside a tmpdir, rows created outside the transaction, a changed
  default locale or timezone, constants reassigned instead of stubbed.

Order dependence is the tell. **A test that only passes in a particular order is
already broken**, even while it is green.

### There is no such thing as a flaky test

A test that fails sometimes is a real bug: a race, a shared-state leak, a timing
assumption, or a masked error. Never accept "known flake", "pre-existing",
"intermittent" or "infra" as a reason to skip it — in a diff, flag a PR that adds
a retry, a `sleep`, a wait bump, or a skip in place of a diagnosis.

The inverse holds too: **do not ask for changes to a green test.** If a checklist
or a hunch says a test is weak, the correct output for one that passes and is
unchanged from the base branch is "not PR-attributable". Hardening a passing test
is churn.

### Coverage by consequence, never by percentage

Never ask for a coverage number. Ask: **if this code broke, would a test tell
us?** Then:

- a **bug fix with no test that fails before the fix** — Must-fix. Name the test
  you expect and what it asserts. The most common real gap.
- **new public behavior** — service, endpoint, job, resolver, or a new branch in
  one — with no test: Must-fix.
- a **new conditional, guard clause, early return or error path** untested:
  Should-fix, and name the input that reaches it.
- **destructive or money-touching paths** (deletes, refunds, payment state, bulk
  updates) untested: Must-fix regardless of size.

Do **not** demand tests for trivial delegation, generated code, pure config, or a
rename. Padding a review with those buries what matters.

### Structure

One behavior per example. A name that states behavior and condition, not the
method. **No control flow in a test** — an `if`, a loop or a `rescue` means a pass
does not tell you what ran; table-driven cases belong in a parametrized form where
each case reports separately. Setup visible near the assertion; deep shared `let`
chains make a failure unreadable. Assert on observable behavior, not private
methods or internal call sequences.

## 7. Recurring bug families worth checking every time

**Check-then-act against a unique constraint.** `find_or_create_by`, or
`find_by` then `create!`, on a uniquely-indexed column, is a race that surfaces as
`RecordNotUnique` under concurrency. The fix leans on the index — rescue and
re-fetch the winner — not a new lock.

The sharper signal: **a find keyed on a volatile field** (`Time.current`,
`to_json`, an object rather than an id) can never match an existing row, so it
degrades to always-create and collides *deterministically* on any duplicate
input, no concurrency needed. Key the find on the unique columns only.

**Privilege derived from client input.** Any authorization decision that reads a
flag, role or id from a request parameter, GraphQL argument or header rather than
from the authenticated session. A client-supplied "is admin" argument copied into
context let owners bypass a refund cap. Ask where every privileged branch's
condition originates; if the client can set it, it is Critical.

**Lock contention on a shared row.** A counter cache, sequence or settings row
that every concurrent request updates serializes them behind one lock and shows up
as a timeout, not as a deadlock.

**Silently dropped options.** A framework helper that forwards only a known list
of keys, so an option passed at the wrong nesting level does nothing and no error
is raised.

## 8. Writing the review

Lead with the verdict. Cap prose at ~5 lines plus one code block or table — a
finding buried in paragraphs does not get fixed. Evidence goes in a fenced block,
not in narration. No preamble, no restating the PR, no "it is worth noting that".

Say each thing once, inline or in the summary, never both. Be concrete over
diplomatic: "this returns nil when the reservation is already cancelled, at
`app/x.rb:42`" beats "consider reviewing the nil handling". Warm, direct, never
scolding — the author is a colleague, and a review that reads as an attack gets
argued with instead of applied.

---

## Rails specifics

Skip this section for other stacks.

- **Prefer ActiveRecord helpers over raw SQL**, in migrations and app code:
  `add_index` takes `where:`, `unique:`, `algorithm:`, `if_not_exists:`;
  `group(...).having('count(*) > 1').count` for dedupe checks; `update_all` for
  bulk writes. Raw `execute <<~SQL` is for PostgreSQL DDL with no helper
  (`CREATE EXTENSION`, expression constraints) — and then only for the part that
  needs it. Flag avoidable heredocs.
- **Dense argument style.** Pack arguments, kwargs and JSX props on one line up to
  115 characters; wrap only past it, fitting as many per continuation line as
  possible. Do not flag inline calls as unreadable, and do flag new
  one-argument-per-line formatting.
- **Soft delete changes the meaning of `dependent:`.** On a paranoid model
  `dependent: :nullify` fires on soft-destroy and is unrecoverable. Prefer
  `dependent: nil` when a destroy side effect is unwanted on a financial FK.
- **`form_with` forwards only `id`, `class`, `multipart`, `method` and `data` at
  the top level.** Anything else — `target:`, `novalidate:` — must be nested under
  `html:` or it is silently dropped.
- **Slugs, not numeric ids, in URLs and route templates.**
- **No `{' '}` JSX space literals** — use margin utilities or a template-string
  text node.
- **`update_columns` vs `update!`** matters where an `after_commit` broadcasts:
  bulk backfills should skip the callback deliberately, not by accident.
