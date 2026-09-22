#!/usr/bin/env bash
# One review, then die. Reads the block contract on stdin, writes the run
# envelope to stdout, touches nothing on the host.
set -euo pipefail

: "${REPO_SLUG:?}" "${PR_URL:?}" "${REVIEW_COMMAND:?}" "${BASE_REF:?}"
# empty on an issue run: there is no PR, and the checkout below is skipped
: "${PR_NUMBER?}"
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
#
# A fix run has no token to set up. It never reaches GitHub: it reads the mirror
# and hands its patch to the PR tool, which is the only thing here holding a
# credential that can write.
[[ "$MODE" == "fix" ]] || gh auth setup-git

# --shared keeps the objects in the read-only mirror instead of copying them:
# on a large repo that is the difference between a 2s and a 40s start.
git clone --quiet --shared /bare /work/repo
cd /work/repo
git remote set-url origin "https://github.com/$REPO_SLUG.git"
if [ -n "$PR_NUMBER" ]; then
  # the mirror is only an object cache; gh fetches the head itself, so this works
  # even for a fork or a branch the mirror has never seen
  gh pr checkout "$PR_NUMBER" >/dev/null
elif [[ "$MODE" == "fix" ]]; then
  # offline by construction: the mirror is all a fix run gets, and the PR tool
  # rebases onto a fresh trunk anyway when it applies the patch
  git checkout --quiet "origin/$CRITERIA_REF"
else
  # an issue is asked about trunk, and about a trunk fresher than the mirror:
  # the answer is which code a report lands in, and the mirror lags every push
  git fetch --quiet origin "$CRITERIA_REF"
  git checkout --quiet FETCH_HEAD
fi

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

# A fix image carries a runtime and a database; the review image does not, and a
# fix run on it still works — it just cannot run what it writes. Everything here
# is skipped when the pieces are absent, so one entrypoint serves both.
if [[ "$MODE" == "fix" && -n "${FIX_DB_URL:-}" ]] && command -v pg_ctl >/dev/null 2>&1; then
  db_user="${FIX_DB_URL#*://}"; db_user="${db_user%%@*}"; db_user="${db_user%%:*}"
  db_name="${FIX_DB_URL##*/}"; db_name="${db_name%%\?*}"
  # -k /tmp below for the same reason: the default socket directory belongs to
  # root and this runs unprivileged
  export PGDATA=/work/pgdata PGHOST=127.0.0.1 PGPORT=5432 PGUSER="$db_user"
  export DATABASE_URL="$FIX_DB_URL" RAILS_ENV=test
  # a preloader is for a developer's second run; here every run is the first, and
  # spring's fork dance fails outright in a container with no writable tmp of its own
  export DISABLE_SPRING=1
  # thrown away with the container, so trust auth on loopback is the whole of it
  initdb --username="$db_user" --auth=trust --encoding=UTF8 >&2 \
    && pg_ctl start -w -o "-h 127.0.0.1 -p 5432 -k /tmp" -l /work/pg.log >&2 \
    && createdb --username="$db_user" "$db_name" >&2 \
    || echo "entrypoint: no database this run; the fix cannot run its test" >&2
  # Both of these live outside the checkout on purpose: anything written inside
  # it lands in the patch the model hands over.
  if [ -d /packs ] && [ -n "$(ls -A /packs 2>/dev/null)" ] && [ -f config/shakapacker.yml ]; then
    out="$(ruby -ryaml -e 'print(YAML.unsafe_load_file(ARGV[0]).dig("test","public_output_path").to_s)' \
            config/shakapacker.yml 2>/dev/null)"
    if [ -n "$out" ]; then
      mkdir -p "public/$out" && cp -r /packs/. "public/$out/" 2>/dev/null
      ruby -ryaml -e 'c=YAML.unsafe_load_file(ARGV[0]); c["test"]["compile"]=false; File.write(ARGV[1], c.to_yaml)' \
        config/shakapacker.yml /tmp/shakapacker.yml 2>/dev/null \
        && export SHAKAPACKER_CONFIG=/tmp/shakapacker.yml
    fi
  fi

  if [ -n "$(ls -A /node_modules 2>/dev/null)" ]; then
    mkdir -p node_modules && cp -r /node_modules/. node_modules/ 2>/dev/null
  fi

  if [ -x bin/rails ]; then
    # the schema the checkout carries, not a dump: a migration in trunk that has
    # not been loaded is a test failing for a reason the fix did not cause
    bin/rails db:test:prepare >&2 2>/dev/null \
      || echo "entrypoint: db:test:prepare did not finish; the suite may not run" >&2
  fi
fi

mcp="${REVIEW_MCP:-}"
[[ -n "$mcp" ]] || mcp='{"mcpServers":{}}'

# only `robbie once --model` sets this; everything else runs the account default
model=()
[[ -n "${REVIEW_MODEL:-}" ]] && model=(--model "$REVIEW_MODEL")

cmd=(claude -p
  --output-format json
  --permission-mode bypassPermissions
  --setting-sources user
  --strict-mcp-config --mcp-config "$mcp"
  --effort "${REVIEW_EFFORT:-high}"
  --no-session-persistence
  "${model[@]}")

exec "${cmd[@]}" <<<"$preamble$body"
