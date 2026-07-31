# Review standards

These apply to every repo robbie reviews. The repo's own review command adds to
them; where both speak, follow both.

Cite `file:line` from the diff for every finding. If you cannot point at a
changed line, you are guessing — drop the finding or put it in the summary.

---

## Tests

A PR that changes behavior and does not change its tests is the normal case worth
questioning. A PR that changes both is where the interesting failures hide.

### 1. Did the test get weaker in the same PR as the code it guards?

You can see the diff, so you can see this. It is the highest-value check here.

Flag when a test changed such that **it would now pass with or without the
production change**:

- an assertion loosened — exact value → `be_present` / `not_to be_nil`, `eq(3)`
  → `be >= 0`, a literal string → a permissive regex, a specific error class →
  `StandardError`
- an expectation, assertion or whole example deleted while the behavior it
  covered still exists
- `skip`, `xit`, `pending`, `.only`, a commented-out example, or an exclusion
  added to a test that was previously running
- setup changed so the branch that used to be exercised no longer is — a factory
  trait dropped, a guard-triggering value replaced with a benign one
- a snapshot or fixture regenerated with no explanation of why the new shape is
  correct

The question to ask each changed test: **would this have failed before the
production change in this PR?** If not, the test is decoration. Say so, quote the
before and after, and ask for the assertion that actually pins the new behavior.

### 2. Stubs replacing logic instead of boundaries

The discriminator is ownership, not the mocking library:

- **Legitimate**: things you do not control or cannot run — HTTP calls, payment
  gateways, mail delivery, the clock, randomness, a third-party SDK, another
  service's API.
- **Suspect**: a class in this codebase, stubbed to avoid setting it up. That
  test no longer proves the two halves fit together, and it keeps passing after
  the real collaborator's contract changes.
- **Always wrong**: stubbing the object under test, or stubbing the very method
  the PR changed.

Prefer the real flow: exercise the service, the graph resolver, the job, the
interactor end to end with real records from factories. A test that walks the
main path is worth several that assert on a mock's arguments.

Also flag, with the same weight:

- a stub whose **return shape cannot be traced to what the real method actually
  returns**. Invented hashes drift from reality silently and the test passes
  forever. Check the real method's return before accepting the stub's shape.
- a stub that makes a branch **unreachable in production** — if the only way to
  reach that code is a value the real collaborator never returns, the test is
  proving nothing and the branch may be dead.
- `allow(...).to receive(...)` with no corresponding assertion that the
  interaction matters.

### 3. Global state that leaks into other tests

The sharp rule: **anything global a test mutates must be restored by the same
construct that mutated it** — the block form, or an `ensure` / `after` hook.
A restore written as a trailing line in the example body is **not** safe: a
failing assertion above it raises, the line never runs, and every later test in
that process is poisoned. That is a Critical, not a style note, because the
damage lands on unrelated tests and the failure looks random.

```ruby
# leaks on failure — the return never runs if the expectation raises
Timecop.freeze(Time.zone.parse("2026-01-01"))
expect(subject.due_on).to eq(...)
Timecop.return

# safe
Timecop.freeze(Time.zone.parse("2026-01-01")) do
  expect(subject.due_on).to eq(...)
end
```

Watch for all of these, in any language:

- time: `Timecop.freeze/travel`, `travel_to`, `freeze_time`, fake timers
- constants and config: raw reassignment instead of `stub_const`, mutated
  `ENV`, feature flags left enabled, changed class-level attributes or
  `mattr_accessor`
- request state: `session`, `cookies`, headers, `Current.*`, `Thread.current`,
  `RequestStore`
- infrastructure: `Rails.cache` writes, Sidekiq/ActiveJob queues left full,
  `ActionMailer::Base.deliveries` not cleared, files written outside a tmpdir,
  rows created outside the transaction, a changed default locale or timezone
- test doubles that outlive the example, and `before(:all)` / `before(:context)`
  setup that later examples then mutate

A related signal: if a test only passes in a particular order, it is already
broken. Flag anything that reads as order-dependent even when it currently
passes.

### 4. Structure

- one behavior per example; an example asserting eight unrelated things cannot
  tell you which one broke
- the name states the behavior and the condition, not the method name — "returns
  nil when the reservation is already cancelled", not "test cancel"
- no control flow in a test. An `if`, a loop or a `rescue` in an example means
  you cannot tell from a pass what actually ran. Loops over cases belong in a
  parametrized/table form where each case reports separately.
- setup visible near the assertion. Deep shared `let` chains and far-away
  `before` blocks make a failure unreadable; prefer explicit setup even when it
  repeats.
- assert on behavior and observable output, not on private methods or internal
  call sequences — those tests break on every refactor and prove nothing.

### 5. Coverage, judged by consequence and not by percentage

Never ask for a coverage number. Ask: **if this code broke, would a test tell
us?** Then:

- a **bug fix with no test that fails before the fix** — Must-fix. Name the test
  you would expect and what it should assert. This is the single most common real
  gap.
- **new public behavior** — a service, endpoint, job, resolver, or a new branch
  in one — with no test at all: Must-fix.
- a **new conditional, guard clause, early return or error path** with no case
  covering it: Should-fix, and name the input that reaches it.
- **destructive or money-touching paths** (deletes, refunds, payment state,
  bulk updates) with no test: Must-fix regardless of size.

Do **not** demand tests for trivial delegation, generated code, pure config, or
a rename. Padding a review with those buries the findings that matter.

### Severity for test findings

- **Critical** — a global mutation that can break unrelated tests; a test
  deleted or skipped in a way that hides a real failure.
- **Must-fix** — a bug fix with no failing-first regression test; new public or
  destructive behavior untested; an assertion weakened in the same PR as the code
  it guarded.
- **Should-fix** — a stub standing in for logic this codebase owns; a stub whose
  shape does not match reality; an untested new branch; control flow inside a
  test.
- **Nitpick** — naming, structure and placement preferences that do not change
  what the test proves.

Do not inflate. Every Must-fix blocks a merge and pulls the author away from
other work, so spend them where the test genuinely fails to protect the code.
