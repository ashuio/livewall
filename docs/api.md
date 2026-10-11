# HTTP API

The backend is a plain JSON-over-HTTP service. Everything the MCP server does is
a call to one of these endpoints, so anything an agent can do over MCP you can do
directly.

- **Base URL:** `http://127.0.0.1:8470` by default (`LIVEWALL_HOST` /
  `LIVEWALL_PORT`).
- **Content type:** request bodies are JSON objects (`Content-Type:
  application/json`).
- **Authentication is optional.** The API is open by default; setting
  `LIVEWALL_TOKEN` requires a bearer token on every `/api/*` endpoint (see
  below). See [SECURITY.md](../SECURITY.md) and [`deploying.md`](deploying.md).

## Conventions

A success response carries `"ok": true` (except `GET` responses, which return the
resource directly; `GET /health` is a `GET` that carries `ok: true` alongside its
fields). A refusal carries `"ok": false` and an `"error"` string that
names the offending field and the valid set:

```json
{"ok": false, "error": "block 0: unknown type 'nope'. Valid types: bar, callout, chart, code, divider, gauge, heading, kv, line, list, log, progress, sparkline, stat, stats, table, text."}
```

The one exception is `GET /api/boards/{id}` for a missing board, which returns
`{"ok": false, "error": "no board 'x'"}` with status 404 — it carries the same
`ok: false` shape as every other refusal. A mistyped endpoint path is a refusal
too: an unknown `/api/...` path returns
`404 {"ok": false, "error": "no such endpoint: /api/..."}` rather than a generic
error page.

### Authentication

**Off by default.** When the backend is run with **`LIVEWALL_TOKEN`** set, every
`/api/*` endpoint requires the token and returns **401** (`{"ok": false,
"error": "unauthorized"}`) without it:

```
Authorization: Bearer <token>
```

or, equivalently, `?token=<token>` on the query string (handy for a browser URL
or a bookmark; the bundled client uses it once and then switches to the header).

Two endpoints are deliberately **not** gated: **`GET /health`** (a liveness probe
cannot carry a credential) and the **static client** at `/` (public code, not
data). Unset `LIVEWALL_TOKEN`, the whole API is open — that is the quickstart
default. The comparison is constant-time.

### Board ids

A board id is the URL path segment. It must **already be a canonical slug**:
lowercase letters, digits, `-` and `_` only (`[a-z0-9_-]+`). Anything else is
refused with 400. Ids are not silently slugged, because that would let two
distinct client ids (`"My Board"` and `"my.board"`) collide onto one board and
let one agent overwrite another's.

A board id is a **single, shared, unowned namespace**: two agent sessions can
independently choose the same id. Choose a **specific, unique** id that includes
the task or run (e.g. `migrate-users-2026-10-08`), not a bare generic word like
`progress` — generic ids are exactly what collides. A `POST` to an id that
already exists is refused with **409** (see below) rather than silently
overwriting the live board.

### The board object

`GET /api/boards/{id}` returns the stored board. It has no `id` key (the id is
the path):

```json
{
  "title": "Migration",
  "subtitle": "",
  "status": "running",
  "updated": 1759938000.123,
  "blocks": [ /* block objects */ ],
  "version": 4,
  "ttl": 1800,
  "tab": {"label": "Migration", "accent": "violet",
          "indicator": {"kind": "bar", "value": 6836, "max": 6950}}
}
```

### The board summary

List and snapshot responses use a derived summary:

| Field | Meaning |
|---|---|
| `id` | the board id |
| `title`, `subtitle`, `status` | as stored |
| `updated` | Unix timestamp of the last write |
| `age` | seconds since `updated`, rounded to 0.1 |
| `version` | increments on every write |
| `ttl` | the board's own ttl, or `null` if unset |
| `effective_ttl` | the ttl actually used (board's, else `default_ttl`) |
| `expires_in` | seconds until prune for a `done`/`error` board; `null` otherwise |
| `progress` | the first `progress`/`gauge` block's ratio as a fraction `0..1`, or `null` |
| `tab` | the resolved tab descriptor (see below) |

In the SSE snapshot (and nowhere else) each summary also carries `blocks`: the
board's blocks, exactly as stored.

## Endpoints

### `GET /health`

Liveness plus current state.

```json
{"ok": true, "boards": 2, "ids": ["migration", "tests"],
 "subscribers": 1, "config": {"default_ttl": 1800, "stale_ttl": 3600, "max_boards": 12}}
```

`subscribers` is the live SSE client count; it is exposed so a client leak is
observable from outside the process.

This is the one endpoint the auth gate leaves open, so an **unauthenticated**
caller on a token-protected backend (`LIVEWALL_TOKEN` set) gets **liveness
only** — `{"ok": true}` — with no `ids` or `config`; a caller that presents the
token, and every caller when auth is off, gets the full payload above.

### `GET /api/boards`

List every board, newest-updated first. Expired boards are pruned as part of the
read.

```json
{"boards": [ /* board summaries */ ]}
```

### `GET /api/boards/{id}`

Return one board (the stored object above), or
`404 {"ok": false, "error": "no board 'x'"}`.

### `POST /api/boards/{id}`

Create a board. This is the main write.

A board id is a shared, unowned namespace, so a `POST` to an id that **already
exists** is refused with **409**; it never silently overwrites the live board.
To replace a live board on purpose, opt in with the `replace` query flag:
`POST /api/boards/{id}?replace=true`. The flag accepts `true`/`1`/`yes`/`on`
(and `false`/`0`/`no`/`off`); any other value is refused with `400` rather than
silently treated as false.

Request:

```json
{
  "title": "Migration",
  "subtitle": "optional",
  "status": "running",
  "blocks": [ /* block objects */ ],
  "tab": {"label": "Migration", "accent": "violet",
          "indicator": {"kind": "bar", "value": 6836, "max": 6950}},
  "ttl": 1800
}
```

All fields are optional. Semantics on a **new** board (or on an existing board
when `replace=true` is passed):

- `title` is applied only if present; otherwise the existing title is kept.
- `subtitle` is always set (to `""` if absent).
- `status` defaults to `running` if absent.
- `blocks` is **always replaced** (with `[]` if absent).
- `tab` and `ttl` change only if the key is present in the body.

Response `200`:

```json
{"ok": true, "id": "migration", "version": 4, "summary": { /* board summary */ }}
```

Errors: `409` if the id already exists and `replace=true` was not passed; `400`
for a bad id, a non-boolean `replace`, a malformed body, or any validation
failure. To update a board the caller already knows, prefer `PATCH`, `/update`
or `/reset` — they are not affected by this guard.

> **`replace` is a query parameter — put it in the URL, not the body.** A body
> field named `replace` is ignored, so the first `POST` succeeds and every one
> after it returns `409`. And note that `PATCH` (below) updates board-level
> fields **only** — it cannot rewrite `blocks`, and `/update` edits one block.
> **A full-board refresh is only `POST /api/boards/{id}?replace=true`**, which is
> what a [pusher](pushers.md) does on every tick.

### `PATCH /api/boards/{id}`

Update board-level fields **without touching blocks**.

Request: any subset of `{"title", "subtitle", "status", "tab", "ttl"}`. On this
endpoint `title`, `subtitle` and `status` may not be `null` if present; `tab` and
`ttl` may be `null` (to clear them). A body with no recognised field is an
idempotent no-op that still bumps the version and returns `ok: true`. The MCP
layer refuses an empty update instead.

Response `200`: same shape as `POST`.

Errors: `404` if the board is missing, `400` on a validation failure.

### `POST /api/boards/{id}/update`

Mutate **one** block, cheaply, without resending the board.

Request:

```json
{"id": "p", "set": {"value": 6950}}
```

or, for a `log` block:

```json
{"id": "l", "append": "batch 42 ok"}
```

- `id` is the target block's id.
- `set` is an object of field → value overwrites. Fields must belong to the
  block's type; the merged block is re-validated (so changing `type` re-validates
  under the new type).
- `append` works on **`log` blocks only** (the client renders `code` from its
  static `text`, so appending there would silently do nothing).

Response `200`:

```json
{"ok": true, "id": "migration", "version": 5, "block": { /* the updated block */ }}
```

Errors: `404` if the board is missing; `400` for a bad `set`/`append`, an unknown
field, a duplicate id, or a value that fails validation; `404` if no block has
the target id.

### `POST /api/boards/{id}/reset`

Clear the board's blocks and set it back to `idle` (the board itself is kept).
Response `200`: `{"ok": true, "id": "migration", "version": 6}`. `404` if missing.

### `DELETE /api/boards/{id}`

Remove a board entirely. Response `200`: `{"ok": true, "id": "migration"}`.
`404` if missing.

### `GET /api/config`

Return the global defaults: `{"default_ttl": 1800, "stale_ttl": 3600,
"max_boards": 12}`.

### `PUT /api/config`

Set one or more global defaults. Request: any subset of `{"default_ttl",
"stale_ttl", "max_boards"}`. Each value must be a **positive integer**; a bool
(`true` would coerce to `1`), a fraction, zero, or a negative is refused with
`400`.

The write is persisted to `LIVEWALL_CONFIG_FILE` **before** the running config
changes. If the file cannot be written, the response is `500` with a short
message (the server log has the path and reason), and neither the file nor the
running config changes. Never a false `ok: true`. `max_boards` is additionally
capped by `LIVEWALL_MAX_BOARDS_LIMIT` (default 1000); a larger value is refused
with `400`.

Response `200`: `{"ok": true, "config": { /* the new config */ }}`.

### `GET /api/stream`

Server-Sent Events. On connect the server sends one full snapshot; then it sends
a new full snapshot after every write and after every reaper prune. After
`LIVEWALL_KEEPALIVE` seconds of silence it sends another full snapshot, so an
idle stream never goes quiet long enough for a proxy to close it — and the
client's staleness clock (45s without a snapshot) only trips when the stream is
really dead.

The number of concurrent streams is capped at `LIVEWALL_MAX_SUBSCRIBERS`
(default 100); once the cap is reached, `GET /api/stream` returns `503` and the
bundled client retries rather than wedging.

Each frame is `data: <json>` where `<json>` is:

```json
{
  "config": {"default_ttl": 1800, "stale_ttl": 3600, "max_boards": 12},
  "now": 1759938000.123,
  "boards": [ /* board summaries, each with "blocks" */ ]
}
```

`now` is the server clock (epoch seconds) at the instant the snapshot was
taken. Every board's `age` and `expires_in` in this frame are measured against
this same `now`, so a client can keep advancing each board's age between
snapshots by adding its own elapsed time — no client wall clock is ever
compared to a server timestamp (skew-free).

The snapshot is the unit of consistency: there are no per-field deltas. The
client is expected to tolerate a malformed frame by keeping its last good view
(see [`architecture.md`](architecture.md)).

### `GET /`

Serves the built client when `web/dist` exists. When it does not, the API still
runs and `/` returns a small JSON hint describing how to build the client. The
API is fully usable headless (that is the MCP path).

## Validation

Every write is validated at the API layer against the shared contract
(`backend/contract.py`) **before** anything is mutated. The MCP server runs the
same validators as an earlier guard, so the two cannot disagree. The rules:

**Board**

| Field | Rule |
|---|---|
| id | canonical slug `[a-z0-9_-]+`, ≤ 64 chars (400 otherwise) |
| `title` | string, ≤ 200 chars |
| `subtitle` | string, ≤ 500 chars |
| `status` | one of `idle`, `running`, `done`, `error` |
| `ttl` | positive integer (seconds); it counts only once the board is **terminal** (`done`/`error`) — a `running` board lives until `stale_ttl` of idle instead |
| `blocks` | list of block objects, ≤ 60 |
| `tab` | object or `null` (see below) |

**Tab**

- `accent`: one of `violet`, `pink`, `lime`, `mint`, `ok`, `error`, `warn`,
  `muted`. An unrecognised accent is refused (it used to silently fall back).
- `indicator.kind`: one of `bar`, `pct`, `text`, `none`. An unrecognised kind is
  refused.
- `kind: "bar"` requires a numeric `value` and, if `max` is present, a numeric
  `max`.
- `kind: "pct"` requires a numeric `value`.
- `kind: "text"` requires a non-empty `text`.
- If no `indicator` is given, one is derived: the first `progress`/`gauge` block
  becomes a `bar`, otherwise `none`.

**Blocks**

- Each block is an object with a known `type`; see [`blocks.md`](blocks.md).
- `id` is optional but, if present, must be a **string**, ≤ 64 chars, and unique
  within the board (`update_block` addresses blocks by id).
- Only the fields that type declares are allowed; an unknown field is refused.
- Field **values** are type-checked, not just their structure. A dict where the
  client iterates, or a number where the client expects one, is refused with the
  field named.
- Every stored **string** — block text fields, `list`/`table`/`kv` elements,
  `stat` labels and values, and each `log` line — is capped at 8192 chars; a
  longer value is refused with `400` naming the field.
- Every **number** is bounded to `2**53` (the largest integer a JS number
  represents exactly); a larger integer is refused with `400` naming the field.
  A large finite float is accepted, because it is already the value the client
  receives.
- `log.lines` is capped at 400 lines on write (the oldest are dropped); the
  client renders the most recent 80.

**update_block**

- `set` must be an object; `append` must be a string and only on a `log` block.
- Unknown `set` fields are refused, as is a non-string `id`.
- A `set` that renames a block to an existing id is refused; a `set` that changes
  a block's `type` is re-validated under the new type.

**Config**

- Each value must be a positive integer; bools, fractions, zero and negatives are
  refused rather than coerced. A value above `2**53` is refused too (the
  `max_boards` ceiling is tighter still).

Malformed bodies (not valid JSON, or valid JSON that is not an object) are
refused with `400` rather than 500. A body larger than
`LIVEWALL_MAX_BODY_BYTES` (default 1 MiB) is refused with `413` before it is
parsed, on every write endpoint.
