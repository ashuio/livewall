# Cookbook

Task recipes. Each one is **the job**, **the board spec** (or the CLI commands),
and **the gotcha** — the thing that bites if you skip it. Every block field here
is valid against [`backend/contract.py`](../backend/contract.py); the full
vocabulary is in [Blocks](blocks.md).

Unless a recipe says otherwise, it talks to a backend at
`http://127.0.0.1:8470` (see [Getting started](getting-started.md) to start
one), and `curl` posts JSON. To keep an existing board id from colliding, the
recipes create with `?replace=true`; drop the flag to get a **409** instead of
overwriting a live board.

## A long migration / batch job

**The job.** A batch process runs for minutes. You want a progress bar, a few
live stats, and a log tail — and you want to update one thing at a time, not
re-send the whole board every batch.

**The board spec.** Create it once, up front:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09 \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Users migration",
    "status": "running",
    "ttl": 1800,
    "tab": {"label": "Migration", "accent": "violet",
            "indicator": {"kind": "bar", "value": 0, "max": 6950}},
    "blocks": [
      {"id": "h", "type": "heading", "text": "Users migration", "eyebrow": "batch 0"},
      {"id": "p", "type": "progress", "label": "Rows copied",
       "value": 0, "max": 6950, "unit": "rows"},
      {"id": "s", "type": "stats", "items": [
        {"label": "batches", "value": 0},
        {"label": "errors", "value": 0},
        {"label": "elapsed", "value": "0s"}
      ]},
      {"id": "c", "type": "callout", "tone": "info", "text": "starting"},
      {"id": "l", "type": "log", "title": "recent batches", "lines": []}
    ]
  }'
```

Then each batch touches **one** block (and the tab indicator, if you want it to
track progress):

```bash
# one block at a time — no full-board resend
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09/update \
  -H 'Content-Type: application/json' -d '{"id": "p", "set": {"value": 6836}}'

# a stats block is one field: replace its whole `items` list
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09/update \
  -H 'Content-Type: application/json' \
  -d '{"id": "s", "set": {"items": [
        {"label": "batches", "value": 41},
        {"label": "errors", "value": 0},
        {"label": "elapsed", "value": "4m12s"}]}}'

# append one line to the log (see the log-tail recipe)
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09/update \
  -H 'Content-Type: application/json' -d '{"id": "l", "append": "batch 41 ok"}'
```

When it finishes, flip the board terminal so its TTL clock starts:

```bash
curl -s -X PATCH http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09 \
  -H 'Content-Type: application/json' \
  -d '{"status": "done", "subtitle": "6,950 rows in 4m18s"}'
```

**The gotcha — the board id is explicit and shared.** A board id is a single,
unowned namespace: two sessions can independently pick the same one, and a
generic id like `progress` is exactly what collides. Use a **specific, unique**
id that names the task or run (`migrate-users-2026-10-09`), not a bare word.
The second `POST` to that id is refused with **409** rather than clobbering the
live board; only `?replace=true` overwrites on purpose. Every later write
addresses the board by that id, so an agent that forgets it cannot update its
own board. `update` also needs the **block** id (`p`, `l`) — a block with no
`id` can never be updated individually.

## Test / build results

**The job.** Show a table of test cases, a verdict, and a terminal board status.

**The board spec.**

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/nightly-build \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Nightly build",
    "status": "running",
    "blocks": [
      {"id": "t", "type": "table",
       "columns": ["case", "duration", "result"],
       "rows": [
         ["auth", "0.4s", "done"],
         ["api",  "2.1s", "done"],
         ["web",  "5.7s", "failed"]
       ],
       "tagColumn": true},
      {"id": "c", "type": "callout", "tone": "error",
       "text": "1 of 3 cases failed — web"}
    ]
  }'
```

`tagColumn: true` renders the **last** cell of each row as a status pill,
colour-coded from its text (`done`/`ok`/`success`/`complete`/`completed` →
green, `running`/`active`/`in progress` → running, `failed`/`error`/`err` →
red, `warn`/`warning`/`pending` → warning, anything else → muted).
Then set the terminal status:

```bash
curl -s -X PATCH http://127.0.0.1:8470/api/boards/nightly-build \
  -H 'Content-Type: application/json' -d '{"status": "error"}'
```

Use `"status": "done"` for a green build, `"error"` for a red one. A terminal
board shows an error banner when it is `error`, and its TTL countdown starts.

**The gotcha.** `tagColumn` colours the **last** cell, so the pill column must
be last in each row. And a `table` cell is a `scalar` (string or number) — a
nested object is refused. If a case has no verdict, put a placeholder string in
the last cell (`"n/a"` renders as a muted pill).

## A log tail

**The job.** Stream output into a panel as it happens.

**The board spec.**

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/build-log \
  -H 'Content-Type: application/json' \
  -d '{"title": "Build log", "status": "running",
       "blocks": [{"id": "l", "type": "log", "title": "output", "lines": []}]}'
```

Append a line per event — cheap, one line on the wire:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/build-log/update \
  -H 'Content-Type: application/json' -d '{"id": "l", "append": "compiling src/app.ts"}'
```

**The gotcha — `append` vs re-send.** `append` adds **one** line to a `log`
block. To rewrite the log, send the whole array with `set`:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/build-log/update \
  -H 'Content-Type: application/json' \
  -d '{"id": "l", "set": {"lines": ["fresh start", "second line"]}}'
```

`append` works on **`log` blocks only**. A `code` block renders from its static
`text`, so appending to it is refused with a 400 naming the block — use `set`.
The server stores at most **400** lines (`MAX_LOG_LINES`); past that the oldest
are dropped, so an unbounded tail cannot grow the snapshot forever. The client
renders the most recent **80** (`RENDER_LOG_LINES`), which keeps the visible
window full during rapid appends.

## A fleet / status wall

**The job.** Several jobs, each its own tab, on one screen — and a kiosk mode
for a wall display.

**The board spec.** One board per job, each with a `tab` descriptor. The
indicator is what the tab shows when it is not selected:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/fleet-api \
  -H 'Content-Type: application/json' \
  -d '{"title": "api", "status": "running",
       "tab": {"label": "api", "accent": "violet",
               "indicator": {"kind": "bar", "value": 9410, "max": 12800}},
       "blocks": [{"id": "s", "type": "stat", "label": "status", "value": "ok"}]}'

curl -s -X POST http://127.0.0.1:8470/api/boards/fleet-web \
  -H 'Content-Type: application/json' \
  -d '{"title": "web", "status": "running",
       "tab": {"label": "web", "accent": "pink",
               "indicator": {"kind": "pct", "value": 72}},
       "blocks": [{"id": "s", "type": "stat", "label": "status", "value": "ok"}]}'

curl -s -X POST http://127.0.0.1:8470/api/boards/fleet-worker \
  -H 'Content-Type: application/json' \
  -d '{"title": "worker", "status": "running",
       "tab": {"label": "worker", "accent": "lime",
               "indicator": {"kind": "text", "text": "3/8"}},
       "blocks": [{"id": "s", "type": "stat", "label": "status", "value": "ok"}]}'
```

`indicator.kind` is one of `bar` (needs `value`; an optional `max`), `pct`
(needs `value`), `text` (needs a non-empty `text`), or `none`. If you omit the
indicator, a board with a `progress`/`gauge` block gets a derived `bar`,
otherwise `none`.

**The gotcha — the wall is driven by the URL.** These compose:

- **`?board=fleet-api`** deep-links a board: it is selected and pinned on load,
  and clicking a tab writes the selection back to the address bar, so the URL
  is shareable. An unknown id falls back to following the newest board.
- **`?kiosk=1`** is wall mode: the app-bar actions and the tab bar are hidden
  and the one selected board is enlarged. It still shows the freshness chip,
  which is where staleness is reported. Exit with **Esc** or the exit button.
  Combine as `?kiosk=1&board=fleet-api`.

With many boards the tab strip scrolls; press **`g`** (or the grid button) for
the board overview, which shows every board at once. It is hidden in kiosk
mode, so a passer-by cannot navigate away from the wall. See the
[README](../README.md#configuration) for the full URL and keyboard story.

## A CI step

**The job.** Program a board from a shell script and let its exit code fail the
step.

**The commands.** The CLI (`livewall`) wraps the same API, so a CI job needs no
MCP host:

```bash
#!/usr/bin/env bash
set -euo pipefail

# LIVEWALL_URL / LIVEWALL_TOKEN are read from the environment.
export LIVEWALL_URL="${LIVEWALL_URL:-http://127.0.0.1:8470}"
ID="ci-${CI_PIPELINE_ID:-local}"

cat > blocks.json <<'JSON'
[
  {"id": "p", "type": "progress", "label": "Steps", "value": 0, "max": 3},
  {"id": "l", "type": "log", "title": "output", "lines": []}
]
JSON

livewall set "$ID" --title "CI ${CI_PIPELINE_ID:-local}" --status running \
  --blocks-file blocks.json

livewall update "$ID" --block p --set '{"value": 1}'
livewall update "$ID" --block l --append "tests passed"
livewall update "$ID" --block p --set '{"value": 2}'

# flip the board terminal without resending its blocks (see the gotcha):
curl -s -X PATCH "$LIVEWALL_URL/api/boards/$ID" \
  -H 'Content-Type: application/json' -d '{"status": "done"}'
```

`livewall` exits **0** on success and **1** on any refusal — a 409 collision, a
bad field, a missing block, or an unreachable backend — so `set -e` fails the
step. (`watch` is the exception: it exits 0 when a board disappears, so it is
safe to leave running in a script.)

**The gotcha — the CLI has no board-level patch.** `livewall set` (even with
`--replace`) always **replaces the blocks** from your `--blocks-file`/`--file`,
so using it to flip `status` would reset the progress and log you just built.
Flip `status` (or `title`/`subtitle`/`tab`/`ttl`) with a `PATCH` as above, with
the MCP `update_board`, or by re-sending a complete final board body via
`livewall set --file final.json --replace`.

**The gotcha — the token lives in the process environment.** When the backend
is run with `LIVEWALL_TOKEN` set, every `/api/*` call needs
`Authorization: Bearer <token>` (or `?token=`). The CLI reads `LIVEWALL_TOKEN`
from the **environment**, not from a `.env` file, so `export` it in the CI job
(or pass `--token`). Without it the CLI exits non-zero with
`unauthorized (401): the backend requires a token...`. The CLI posts to a board
it does not own, so use a **run-specific id** (`ci-$CI_PIPELINE_ID`) — a reused
id is refused with 409 on the second run unless you pass `--replace`.

A runnable version of this pattern is
[`examples/ci_progress.sh`](../examples/ci_progress.sh).

## A metric over time

**The job.** Plot a series — throughput, queue depth, memory — and keep the
numbers honest.

**The board spec.**

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/metrics-demo \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Metrics", "status": "running",
    "blocks": [
      {"id": "m", "type": "sparkline", "label": "req/s", "color": "lime",
       "values": [120, 132, 128, 150, 143, 160, 171]},
      {"id": "g", "type": "gauge", "label": "Tokens",
       "value": 1521227, "max": 2000000, "fmt": "compact"}
    ]
  }'
```

Push a new point by replacing the series (there is no append on a sparkline):

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/metrics-demo/update \
  -H 'Content-Type: application/json' \
  -d '{"id": "m", "set": {"values": [132, 128, 150, 143, 160, 171, 158]}}'
```

`chart` is an alias of `sparkline` — same fields, same rendering. `values` is a
list of numbers; fewer than 2 values renders `(no data)`. `color` is one of
`violet` (default), `pink`, `lime`, `mint`, `ok`, `warn`, `error`, `muted`.

For a trend with axes, an optional legend and multiple series ("p95 vs p50"),
use the **`line`** block instead of a `sparkline` — it takes
`series:[{name?, values, color?}]`, an `xLabels` list and optional
`yMin`/`yMax`; see [Blocks § line](blocks.md#line).

**The gotcha — numbers are exact by default.** `values` must be **numbers**, not
strings: `[120, 132]` is right, `["1.5M"]` is refused
(`values[0] must be a number`). A `gauge`/`progress`/`bar` also needs a real
number for its ratio, so you cannot pass `"1.5M"` there either — set
`"fmt": "compact"` to shorten only the *label* while the ratio uses the true
value. Pre-formatted strings (`"1.5M"`, `"6,836"`, `"n/a"`) are accepted on
`stat`/`stats` values only. The rule and the thresholds are in
[Blocks § Numbers](blocks.md#numbers).

## Cleanup / lifecycle

**The job.** Make sure boards do not pile up.

**The knobs.** Boards are **ephemeral by design** — there is nothing to migrate,
back up, or clean up between runs. A board is removed when:

- it is **terminal** (`status: done`/`error`) and its TTL elapses — the board's
  own `ttl`, else the global `default_ttl` (1800s);
- it is `running` and stops updating for `stale_ttl` (3600s) — how a crashed
  agent's board eventually goes away;
- the board cap is exceeded: past `max_boards` (12), the least-recently-updated
  boards are evicted.

```bash
# end it now (starts the TTL clock):
curl -s -X PATCH http://127.0.0.1:8470/api/boards/nightly-build \
  -H 'Content-Type: application/json' -d '{"status": "done"}'

# remove it immediately:
curl -s -X DELETE http://127.0.0.1:8470/api/boards/nightly-build

# keep the board but clear its blocks and return it to idle:
curl -s -X POST http://127.0.0.1:8470/api/boards/nightly-build/reset
```

**The gotcha — a terminal board is not gone until its TTL.** `done`/`error`
starts the countdown; the summary's `expires_in` is set only for terminal
boards (it is `null` while running). Global defaults are readable and settable:

```bash
curl -s http://127.0.0.1:8470/api/config
curl -s -X PUT http://127.0.0.1:8470/api/config \
  -H 'Content-Type: application/json' -d '{"default_ttl": 600}'
```

Config values must be positive integers (a bool, fraction, zero or negative is
refused), and a change is persisted to `LIVEWALL_CONFIG_FILE` **before** it
takes effect — a save that cannot be written returns `500` rather than a false
`ok: true`. For a per-board lifetime, pass `ttl` on the board itself; it wins
over the global default.
