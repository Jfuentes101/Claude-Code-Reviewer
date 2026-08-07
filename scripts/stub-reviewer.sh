#!/usr/bin/env bash
# Everything the real reviewer does except call the model: clone from the mirror,
# check the PR out, read the criteria off CRITERIA_REF, load the policy into the
# user scope, then emit a valid run envelope. Used by scripts/fleet-check to
# exercise container spawning and isolation without paying for a review.
#
# STUB_HOLD controls how long it sits still, so containers overlap observably.
set -euo pipefail

: "${REPO_SLUG:?}" "${PR_NUMBER:?}" "${BASE_REF:?}" "${CRITERIA_REF:?}" "${REVIEW_COMMAND:?}"

cat > /dev/null   # drain the preamble the orchestrator pipes in

if [ -d /policy ]; then
  mkdir -p "$HOME/.claude"
  cp -r /policy/. "$HOME/.claude/"
fi
policy_bytes=$(wc -c < "$HOME/.claude/CLAUDE.md" 2>/dev/null || echo 0)

git clone --quiet --shared /bare /work/repo
cd /work/repo
git remote set-url origin "https://github.com/$REPO_SLUG.git"
gh pr checkout "$PR_NUMBER" >/dev/null
branch="$(git branch --show-current)"
head="$(git rev-parse --short HEAD)"

criteria_bytes=$(gh api "repos/$REPO_SLUG/contents/$REVIEW_COMMAND?ref=$CRITERIA_REF" \
  -H "Accept: application/vnd.github.raw" | wc -c)

sleep "${STUB_HOLD:-8}"

# the markers contract.MARKERS actually parses, so persistence runs unchanged
result="$(printf '<<<VERDICT>>>\nok\n<<<END>>>\n<<<GITHUB>>>\nstub: pr=%s branch=%s head=%s criteria=%sB policy=%sB\n<<<END>>>\n<<<INLINE>>>\n[]\n<<<END>>>' \
  "$PR_NUMBER" "$branch" "$head" "$criteria_bytes" "$policy_bytes")"
jq -n --arg r "$result" \
  '{result:$r, total_cost_usd:0, usage:{input_tokens:1,output_tokens:1},
    subtype:"success", num_turns:1}'
