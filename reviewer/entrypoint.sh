#!/usr/bin/env bash
# One review, then die. Reads the block contract on stdin, writes the run
# envelope to stdout, touches nothing on the host.
set -euo pipefail

: "${REPO_SLUG:?}" "${PR_NUMBER:?}" "${PR_URL:?}" "${REVIEW_COMMAND:?}" "${BASE_REF:?}"
# unset means an orchestrator too old to pin it, which is the bug this closes
: "${CRITERIA_REF:?}"

preamble="$(cat)"
MODE="${REVIEW_MODE:-review}"

# robbie's cross-repo standards go in the user scope, which the CLI loads on its
# own. Copied rather than mounted so it cannot collide with the credentials mount.
if [ -d /policy ]; then
  mkdir -p "$HOME/.claude"
  cp -r /policy/. "$HOME/.claude/"
fi

# `gh` authenticates from GH_TOKEN; plain git over https does not. The review
# command fetches the base ref to get one fresher than the mirror's, and without
# this that asks for a username on a terminal nobody is holding.
gh auth setup-git

# --shared keeps the objects in the read-only mirror instead of copying them:
# on a large repo that is the difference between a 2s and a 40s start.
git clone --quiet --shared /bare /work/repo
cd /work/repo
git remote set-url origin "https://github.com/$REPO_SLUG.git"
# the mirror is only an object cache; gh fetches the head itself, so this works
# even for a fork or a branch the mirror has never seen
gh pr checkout "$PR_NUMBER" >/dev/null

# The review criteria come from CRITERIA_REF, never from the checkout and never
# from BASE_REF: a PR must not be able to rewrite the rules it is judged by, and
# it chooses its own base branch. Same reason the CLI runs with
# --setting-sources user below, which keeps a .claude/ added by this PR from being
# loaded as instructions. Fetched over the API rather than with git, which has no
# credentials of its own here, and to avoid pulling a whole branch for one file.
body=""
if [[ "$MODE" == "review" ]]; then
  if ! body="$(gh api "repos/$REPO_SLUG/contents/$REVIEW_COMMAND?ref=$CRITERIA_REF" \
                -H "Accept: application/vnd.github.raw" 2>/dev/null)"; then
    echo "entrypoint: $REVIEW_COMMAND not found on $CRITERIA_REF" >&2
    exit 3
  fi
  # frontmatter out, $ARGUMENTS in. Variable expansion, never eval: backticks
  # inside the command file stay literal data. Keep it that way.
  body="$(awk 'NR==1&&/^---/{f=1;next} f&&/^---/{f=0;next} !f' <<<"$body")"
  body="${body//\$ARGUMENTS/$PR_URL}"
  body="

--- review command follows (\$ARGUMENTS = $PR_URL) ---
$body"
fi

mcp="${REVIEW_MCP:-}"
[[ -n "$mcp" ]] || mcp='{"mcpServers":{}}'

# only `robbie once --model` sets this; everything else runs the account default
model=()
[[ -n "${REVIEW_MODEL:-}" ]] && model=(--model "$REVIEW_MODEL")

exec claude -p \
  --output-format json \
  --permission-mode bypassPermissions \
  --setting-sources user \
  --strict-mcp-config --mcp-config "$mcp" \
  --effort "${REVIEW_EFFORT:-high}" \
  --no-session-persistence \
  "${model[@]}" \
  <<<"$preamble$body"
