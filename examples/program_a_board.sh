#!/usr/bin/env bash
# Program a Livewall board with curl, then clean up.
#
#   ./examples/program_a_board.sh                    # create, update, delete
#   LIVEWALL_KEEP=1 ./examples/program_a_board.sh    # leave it up to watch
#
# Configuration:
#   LIVEWALL_URL     base URL of the backend  (default http://127.0.0.1:8470)
#   LIVEWALL_TOKEN   bearer token, if the backend sets one
#   LIVEWALL_BOARD   board id to use          (default example-program-a-board)
set -euo pipefail

BASE="${LIVEWALL_URL:-http://127.0.0.1:8470}"
BASE="${BASE%/}"
ID="${LIVEWALL_BOARD:-example-program-a-board}"
TOKEN="${LIVEWALL_TOKEN:-}"

auth=()
if [ -n "$TOKEN" ]; then
  auth=(-H "Authorization: Bearer $TOKEN")
fi

cleanup() {
  if [ -z "${LIVEWALL_KEEP:-}" ]; then
    curl -s -X DELETE ${auth[@]+"${auth[@]}"} "$BASE/api/boards/$ID" >/dev/null || true
    echo "deleted the board (set LIVEWALL_KEEP=1 to leave it up)"
  else
    echo "LIVEWALL_KEEP set: leaving the board up"
  fi
}
trap cleanup EXIT

curl -s -X POST ${auth[@]+"${auth[@]}"} -H 'Content-Type: application/json' \
  "$BASE/api/boards/$ID?replace=true" -d '{
    "title": "Example board",
    "status": "running",
    "tab": {"label": "Example", "accent": "violet",
            "indicator": {"kind": "bar", "value": 0, "max": 5}},
    "blocks": [
      {"id": "p", "type": "progress", "label": "Steps", "value": 0, "max": 5},
      {"id": "l", "type": "log", "title": "activity", "lines": []}
    ]
  }' >/dev/null
echo "created board '$ID'"

curl -s -X POST ${auth[@]+"${auth[@]}"} -H 'Content-Type: application/json' \
  "$BASE/api/boards/$ID/update" -d '{"id": "p", "set": {"value": 3}}' >/dev/null
curl -s -X POST ${auth[@]+"${auth[@]}"} -H 'Content-Type: application/json' \
  "$BASE/api/boards/$ID/update" -d '{"id": "l", "append": "updated block p to 3"}' >/dev/null
echo "updated block 'p' and appended to log 'l'"

# Show the result with the CLI, if it is installed.
if command -v livewall >/dev/null 2>&1; then
  livewall get "$ID"
fi

echo "open $BASE/?board=$ID to watch it"
