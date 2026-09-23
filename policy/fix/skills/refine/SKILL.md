---
name: refine
description: Before opening the pull request, have review_patch read your patch, triage each finding, fix what holds, and go once more with the rejections carried forward. Use whenever a review_patch tool is available.
---

# refine — review, triage, fix, once more

`review_patch` hands your patch to a fresh agent on a clean checkout and blocks
until it answers, which takes around ten minutes. Two calls at most, then open the
pull request whatever they said.

## The reviewer has no memory

Every call is a new agent that has never seen the last review or your reasons. The
`context` argument is the memory. Round 1 gets the intent block; round 2 gets the
intent block plus every finding you rejected, with the reason, or it raises them
again.

**Intent block** — what the diff cannot tell a stranger: the reported behaviour,
what is actually wrong, how this code is reached in production and what can never
reach it (the one caller, the validation upstream, the boot check). Facts about the
system.

Write about the **system, never about the reviewer**. "A group booking notifies every
reservation in it, not only the parent" narrows what counts as a defect. "Ignore the
notification changes" narrows what gets read and buries real bugs with it; the
reviewer is told to read past it anyway. The test: could the sentence have been in
the bug report or a spec? Then it is context.

**Rejected block**, round 2 only:

```
Findings from the earlier pass that were considered and deliberately not changed.
- <file:line> — <the finding> — not changed because <reason>.
```

## Triage every finding

Two questions first, both cheaper than a wasted round:

- **Can it happen?** Name the entry point, the caller and the values that reach it.
  The guard that makes it impossible is usually not in the file the finding points
  at. If you cannot draw the path, it is a reject, and the reason is the thing that
  blocks it — `refused at config/initializers/x.rb:12`, never "looks unreachable".
- **What does the fix touch?** The other callers, the tests that pin the behaviour.
  A one-line change in a place with three callers is a three-caller change.

Then one of:

- **fix** — it holds. Apply it.
- **reject** — wrong, deliberate, or cannot happen. One line, with the reason.
- **decide** — a design call. Nobody is watching this run, so you make it: if the
  answer means the code is wrong it is a fix, if it means the code is right it is a
  reject and your answer is the reason.

Judge the body, not the severity label. A Must-fix whose scenario does not survive
reading the caller is a reject.

## Apply

Find out why the code is shaped the way it is before changing it — `git log -S`,
`git blame`, the test that pins it. Fix so that reason survives. Your change must stay
the smallest one that fixes the report: a finding about code you did not touch is a
reject ("outside this fix"), not an invitation to widen the patch, and the patch
still has to pass `open_pull_request`'s limits.

Run the test you wrote, and the ones next to it, before the second call.

## Stop

Open the pull request when any of these holds:

- the verdict is `ok`
- every finding was a reject
- two calls have run
- `review_unavailable` — there is no review to be had; that is never a reason for
  `cannot`

A round-2 finding on code you changed in round 1 is your patch being wrong, not a new
bug: revert that change and make the one that satisfies both, rather than patching
the patch.

## In the summary

Add what the review changed and every rejected finding with its reason. That list is
what stops the human reviewer raising them a third time.
