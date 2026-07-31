#!/usr/bin/env bash
# One review, then die. Reads the block contract on stdin, writes the run
# envelope to stdout, touches nothing on the host.
set -euo pipefail

: "${REPO_SLUG:?}" "${PR_NUMBER:?}" "${PR_URL:?}" "${REVIEW_COMMAND:?}"

preamble="$(cat)"

# --shared keeps the objects in the read-only mirror instead of copying them:
# on a large repo that is the difference between a 2s and a 40s start.
git clone --quiet --shared /bare /work/repo
cd /work/repo
git remote set-url origin "https://github.com/$REPO_SLUG.git"
# the mirror is only an object cache; gh fetches the head itself, so this works
# even for a fork or a branch the mirror has never seen
gh pr checkout "$PR_NUMBER" >/dev/null

[[ -f "$REVIEW_COMMAND" ]] || {
  echo "entrypoint: $REVIEW_COMMAND not found in $REPO_SLUG@$PR_NUMBER" >&2
  exit 3
}

# frontmatter out, $ARGUMENTS in. Variable expansion, never eval: backticks
# inside the command file stay literal data. Keep it that way.
body="$(awk 'NR==1&&/^---/{f=1;next} f&&/^---/{f=0;next} !f' "$REVIEW_COMMAND")"
body="${body//\$ARGUMENTS/$PR_URL}"

mcp="${REVIEW_MCP:-}"
[[ -n "$mcp" ]] || mcp='{"mcpServers":{}}'

exec claude -p \
  --output-format json \
  --permission-mode bypassPermissions \
  --strict-mcp-config --mcp-config "$mcp" \
  --effort "${REVIEW_EFFORT:-high}" \
  --no-session-persistence \
  <<<"$preamble

--- review command follows (\$ARGUMENTS = $PR_URL) ---
$body"
