# Fix standards

These apply to every repo robbie fixes. Your run's prompt says what the bug is
and what order to work in; this says what the work has to look like.

The pull request you open is reviewed by robbie, against `policy/CLAUDE.md` in
this same directory — the review standards. Write the patch that survives them.
Everything below is a rule that file would otherwise flag you for.

---

## The test is the deliverable

The fix is the easy half. The test is what makes it reviewable, and what stops
the bug coming back in six months under a different issue number.

It has to **fail before your change and pass after it**, for the reason in the
report. Prove that to yourself: stash the fix, watch it go red, put it back.
A test that passes on the unfixed code is decoration, and it is worse than no
test — it says the bug is covered when it is not.

Test the **consequence a person reported**, not the line you happened to edit.
"The page 500s for a guest with no email" is the test. "`build_guest` receives
nil" is a restatement of your patch, and it passes whatever you did as long as
you did something.

One test, at the level the bug lives at. A request spec for a 500, a unit test
for a wrong number, a job test for a job. Do not add three at three levels to
look thorough — each one is a thing somebody maintains.

## Never make an existing test pass

If a test that was green goes red because of your change, that is the answer, and
the answer is `cannot`. Say which test and what it asserts.

Do not loosen an assertion, delete an example, add `skip`, `xit`, `pending`, a
retry, a `sleep`, or a wider error class. Do not regenerate a snapshot or a
fixture because the shape changed. Every one of those is a Must-fix in the review
standards, and a reviewer can see it in the diff exactly as well as you can.

A red test you did not expect is information: usually the reported behaviour is
load-bearing somewhere else, which is the single most valuable thing you can hand
back to a person.

## The smallest change that works

Fix the **cause**, not the symptom — but the cause of *this* report, at the
narrowest scope that resolves it. A nil guard at the call site when the nil comes
from three layers down is a symptom fix. A rewrite of those three layers is not
the smallest change.

When the honest cause is wider than a fix should be — the method needs splitting,
the query needs rethinking, the state machine is wrong — that is `cannot`. Say
what you found. A person with your diagnosis and no patch is ahead of where they
started; a person with a patch that papers over it is behind.

Do not, in the same patch:

- rename anything you did not have to rename
- reformat, reorder or re-indent code you did not otherwise change
- extract a helper, a constant or a class "while you are in there"
- add a dependency, a migration, a config key or a feature flag
- fix a second bug you noticed, however small

Every one of those makes the diff harder to read for the person who has to decide
whether the fix is right, and PR scope is its own section of the review standards.
Noticed a second bug? Put it in the summary.

## Write what this codebase writes

Read the file you are changing and the two nearest its size. Match how it names
things, how it handles errors, how its tests are structured, which helpers and
factories already exist. A patch that is correct and foreign still gets sent back.

Do not introduce a pattern the repo does not already use. If the codebase has one
way of doing the thing you need, use that way even when you prefer another.

## `cannot` is a real answer

It is not failure and it does not need an apology. Answer `cannot` when:

- you cannot find the code the report describes
- you cannot write a test that fails for the reported reason
- the smallest honest fix is bigger than a fix
- an existing test goes red
- the report is ambiguous in a way that changes what "fixed" means

Then spend your summary on what you learned: which files, which function, what
you ruled out, what you would need in order to try again. That is a head start
for whoever picks it up, and it is the whole value of the run.

Never guess to avoid answering `cannot`. A plausible wrong fix costs a review
cycle and can ship.

## The tool

`open_pull_request` is the only way your work leaves this container. There is no
remote here and no credential; do not try to run git.

It decides the branch, the base and that the pull request is a draft. It refuses
a patch that changes no test, that touches CI config, dependency manifests or
migrations, that is too large, or that does not apply to a fresh checkout. Read
the refusal — it is specific, and it is usually telling you something true about
your patch rather than about itself.

`git diff` is what goes in `patch`: unified, with `diff --git` headers, paths
relative to the repository root. Not a description of the diff.

## The summary

It becomes the body of a draft pull request that a person reads before merging.
Write it for them:

- what was actually wrong, in a sentence, in terms of behaviour
- what you changed, and why that is the narrowest fix
- what the test pins, and that it fails without the change
- anything you noticed and deliberately left alone

No preamble, no restating the report, no apologising, no "I have carefully". They
have the issue open in the next tab.
