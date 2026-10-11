# backend

FastAPI service. Owns boards, the SSE stream, TTL/pruning, and serves the built
client at `/`. It streams each board's blocks to the client, exactly as stored.

## Layout

| File | Role |
|---|---|
| `app.py` | FastAPI app — boards, config, SSE, serves `web/dist` at `/` |
| `tests/edge_test.py` | API edge-case suite (scale, validation, TTL, stream shape, prune-during-await race, hostile field-value types) |
| `tests/stream_test.py` | SSE/concurrency suite (bounded queue, disconnect/leak, keepalive, reaper, live push) |
| `tests/config_persist_test.py` | config persistence across a restart (round-trip, missing directory, corrupt file) |
| `tests/auth_test.py` | auth gate (open, 401, accepted, `/health` liveness-only, static exempt) |
| `tests/store_concurrency_test.py` | board store under concurrent writes (lost-update and prune-during-await races) |
| `tests/pusher_test.py` | the example pusher's extraction and field-selection logic |
| `tests/vocab_parity_test.py` | block vocabulary parity between `contract.py` and every consumer |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + config |
| GET | `/api/boards` | board summaries (status, ttl, age, progress, tab) |
| GET | `/api/boards/{id}` | full board |
| POST | `/api/boards/{id}` | create a board; 409 if the id exists (`?replace=true` to overwrite) |
| PATCH | `/api/boards/{id}` | partial board fields |
| POST | `/api/boards/{id}/update` | mutate one block `{id,set:{...},append:"..."}` |
| POST | `/api/boards/{id}/reset` | clear blocks |
| DELETE | `/api/boards/{id}` | remove board |
| GET/PUT | `/api/config` | `{default_ttl, stale_ttl, max_boards}` |
| GET | `/api/stream` | SSE snapshot of all boards (client) |

## Block vocabulary

`heading` · `text` · `stat` · `stats` · `progress` · `gauge` · `bar` · `line` ·
`sparkline`/`chart` · `table` · `log` · `kv` · `list` · `callout` · `code` ·
`divider`. Blocks carry an optional `id`; `update_block` addresses blocks by id.

## Tab spec (agent-authored)

```json
{"tab": {"label": "Deploy", "accent": "violet",
         "indicator": {"kind": "bar", "value": 9410, "max": 12800}}}
```

`accent`: violet|pink|lime|mint|ok|error|warn|muted · `indicator.kind`:
bar|pct|text|none. Defaults derive from status / the first progress block.

## TTL / pruning

- Per-board `ttl` (seconds) — how long a FINISHED board survives; `null` → the
  global `default_ttl`. Overridable per tab at create (`POST`) and later (`PATCH`).
- Global config: `default_ttl` (1800), `stale_ttl` (3600, kills a `running` board
  that stopped updating), `max_boards` (12, evicts oldest).
- **Config persists to disk** (`LIVEWALL_CONFIG_FILE`) — otherwise a restart
  silently reverts it.
- Pruning runs on every read AND on a 5s reaper.

## Stream

`GET /api/stream` is an SSE stream: one initial snapshot, then a full snapshot
after every write (and after the reaper prunes). Each frame is a COMPLETE
snapshot, so per-subscriber queues are bounded at one pending snapshot
(`STREAM_QUEUE_MAX`): a slow reader gets the newest state and skips stale
intermediate frames (newest-wins coalescing) instead of growing without bound.
After `LIVEWALL_KEEPALIVE` seconds of silence a fresh `data:` snapshot is sent —
the liveness pulse doubles as a data frame, so the client's freshness chip stays
honest while the connection is healthy; on disconnect the generator is cancelled
and its queue is removed from `SUBS`.

## Validation (loud failures, not silent ones)

Every write is validated at the API layer against `contract.py` — the same
vocabulary and limits the MCP server uses, so the two cannot disagree. Refusals
are 400s naming the offending field and the valid set:

- **`POST`/`PATCH` board**: `status` must be one of `idle|running|done|error`;
  `blocks` must be a list of objects, each with a known `type`, a **string** `id`
  (unique per board), only the fields that type declares, at most `MAX_BLOCKS`
  (60) per board; `tab` must be a valid tab object (accent/indicator checked);
  `ttl` must be a positive integer; `title`/`subtitle` must be strings within
  `MAX_TITLE`/`MAX_SUBTITLE`.
- **Malformed bodies**: a body that is not a JSON object (bad JSON, a list,
  `null`) is refused instead of 500ing.
- **Board ids** must already be canonical lowercase slugs
  (`[a-z0-9_-]+`) — no silent slugging, so two distinct client ids can never
  collide onto one board.
- **Board-id collisions are loud.** A `POST` to an id that already exists is
  refused with **409** and the live board is left intact; the caller opts into
  replacing it with `?replace=true` (a non-boolean value is refused with 400).
  `PATCH`, `/update`, `/reset` and `DELETE` address a board the caller already
  knows and are unaffected.
- **`update_block`**: `append` only works on `log` blocks (the client renders
  `code` from its static `text`, so appending there would no-op); unknown `set`
  fields for the block's type are refused; `set` must be an object. A `set` that
  renames a block must not collide with another block's `id`, and one that
  changes a block's `type` is re-validated under the new type.
- **`PUT /api/config`**: each value must be a positive integer — a bool
  (`int(True)==1`), a fraction, zero, or a negative is refused, never silently
  coerced. The write is persisted to `LIVEWALL_CONFIG_FILE` **before** the
  running config changes, and a persistence failure returns **500** with the
  path and reason (never a false `ok:true`). On startup, an absent config file
  is normal; a corrupt/unreadable one — or one whose value is not a positive
  integer — falls back to the seeded defaults and logs a warning naming the key,
  so the discard is visible.

Log history is capped at `MAX_LOG_LINES` (400) on write/append; the client
renders the most recent `RENDER_LOG_LINES` (80). Both live in `contract.py`.

## Configuration

Every knob is an environment variable, read from the **process environment** —
the backend does **not** load a `.env` file, so `export` the values (or put them
in the unit/compose environment) before starting it. See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `LIVEWALL_HOST` | `127.0.0.1` | bind address |
| `LIVEWALL_PORT` | `8470` | port |
| `LIVEWALL_DEFAULT_TTL` | `1800` | terminal-board lifetime (s) |
| `LIVEWALL_STALE_TTL` | `3600` | running-board idle lifetime (s) |
| `LIVEWALL_MAX_BOARDS` | `12` | board cap |
| `LIVEWALL_MAX_BOARDS_LIMIT` | `1000` | ceiling for the runtime `max_boards` knob |
| `LIVEWALL_MAX_BODY_BYTES` | `1048576` | max bytes per request body (413 over) |
| `LIVEWALL_MAX_SUBSCRIBERS` | `100` | max concurrent SSE subscribers (503 over) |
| `LIVEWALL_KEEPALIVE` | `15` | SSE keepalive interval (s) |
| `LIVEWALL_CONFIG_FILE` | `./backend/config.json` | where runtime config persists |
| `LIVEWALL_WEB_DIR` | `../web/dist` | built client to serve at `/` |
| `LIVEWALL_LOG_LEVEL` | `warning` | uvicorn log level |

`LIVEWALL_HOST` defaults to **loopback**: exposing a dashboard to the network is
a deliberate act, not an accident.

## Run

```bash
pip install -r requirements.txt
python app.py                          # loopback:8470
LIVEWALL_URL=http://127.0.0.1:8470 python tests/edge_test.py
```

The edge suite snapshots `/api/config` and restores it exactly in a `finally`,
and deletes every board at start and end, so it leaves no leftovers and cannot
leak `max_boards`/`default_ttl` between runs. It is destructive to boards, so
point it at a dedicated backend. It exits non-zero if any case is a gap. From the
repo root, `make test-backend` boots a throwaway backend for you.

With no client build present the API still runs and `/` explains how to build
one — the API is fully usable headless (that is the MCP path).
