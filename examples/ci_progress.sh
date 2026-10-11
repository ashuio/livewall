#!/usr/bin/env bash
# A CI-style build board: create it, run a few steps updating progress and the
# log, then mark the board done. The pattern is the "A CI step" recipe in
# docs/cookbook.md.
#
#   ./examples/ci_progress.sh                    # run it, then delete the board
#   LIVEWALL_KEEP=1 ./examples/ci_progress.sh    # leave it up to watch
#
# Configuration:
#   LIVEWALL_URL     base URL of the backend  (default http://127.0.0.1:8470)
#   LIVEWALL_TOKEN   bearer token, if the backend sets one
#   LIVEWALL_BOARD   board id to use          (default ci-<user>-<pid>)
set -euo pipefail

BASE="${LIVEWALL_URL:-http://127.0.0.1:8470}"
BASE="${BASE%/}"
ID="${LIVEWALL_BOARD:-ci-${USER:-local}-$$}"
TOKEN="${LIVEWALL_TOKEN:-}"

auth=()
if [ -n "$TOKEN" ]; then
  auth=(-H "Authorization: Bearer $TOKEN")
fi

api() {
  curl -s -f ${auth[@]+"${auth[@]}"} -H 'Content-Type: application/json' "$@"
}

cleanup() {
  if [ -z "${LIVEWALL_KEEP:-}" ]; then
    curl -s -X DELETE ${auth[@]+"${auth[@]}"} "$BASE/api/boards/$ID" >/dev/null || true
    echo "deleted board '$ID' (set LIVEWALL_KEEP=1 to leave it up)"
  else
    echo "LIVEWALL_KEEP set: leaving board '$ID' up"
  fi
}
trap cleanup EXIT

# 1. Program the board once, up front.
api -X POST "$BASE/api/boards/$ID?replace=true" -d '{
  "title": "Build",
  "status": "running",
  "tab": {"label": "Build", "accent": "violet",
          "indicator": {"kind": "bar", "value": 0, "max": 4}},
  "blocks": [
    {"id": "p", "type": "progress", "label": "Steps", "value": 0, "max": 4},
    {"id": "l", "type": "log", "title": "output", "lines": []}
  ]
}' >/dev/null
echo "programmed board '$ID' -> $BASE/?board=$ID"

# 2. Update ONE block per step — no full-board resend.
run_step() {
  local n="$1" label="$2"
  api -X POST "$BASE/api/boards/$ID/update" \
    -d "{\"id\": \"p\", \"set\": {\"value\": $n}}" >/dev/null
  api -X POST "$BASE/api/boards/$ID/update" \
    -d "{\"id\": \"l\", \"append\": \"$label ok\"}" >/dev/null
  echo "  step $n: $label"
}

run_step 1 "install"
run_step 2 "lint"
run_step 3 "test"
run_step 4 "build"

# 3. Flip the board terminal. PATCH changes only board fields, so the blocks
#    and log above are preserved.
api -X PATCH "$BASE/api/boards/$ID" \
  -d '{"status": "done", "subtitle": "all steps passed"}' >/dev/null
echo "board '$ID' -> done"
