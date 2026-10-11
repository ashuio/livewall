# Architecture

Livewall is three pieces around a small shared contract. This page explains how
they fit and, where it matters, **why they are shaped that way**. Several of the
shapes are load-bearing and have a bug story behind them.

```
   your agent
       │   MCP tool calls (set_board / update_board / update_block / …)
       ▼
  mcp/server.py ───────────────► backend/app.py
  (agent-facing tools;           (boards, config,
   an earlier guard over          TTL/pruning,
   the same contract)             SSE broadcast)
                                        │
                                        │  GET /api/stream   (SSE: board blocks)
                                        ▼
                              web/ (React: renders blocks directly)
```

## The three pieces

### 1. `backend/app.py`: the service

A FastAPI app that owns all state. There is no database: **boards live in a dict
in process memory** (`BOARDS`). It:

- accepts writes over plain JSON endpoints ([`docs/api.md`](api.md)),
- keeps a `BOARDS` map keyed by board id and a `SUBS` set of connected SSE
  queues,
- **prunes** expired boards on every list/snapshot read *and* on a 5-second
  reaper loop, so a board disappears on time even when nothing else is
  happening,
- **broadcasts** a complete snapshot of all boards after every write and after
  each reaper prune,
- persists global config to `LIVEWALL_CONFIG_FILE` so a restart does not revert
  it,
- serves the built client at `/` when `web/dist` exists.

The config write path is deliberately ordered **persist, then commit**: a config
change that cannot be written to disk returns an error and leaves the running
config unchanged. It never reports a save that did not happen.

### 2. `backend/contract.py`: one vocabulary, both layers

`contract.py` is the single source of truth for the block vocabulary, limits, and
validators. The HTTP API (`backend/app.py`) and the MCP server
(`mcp/server.py`) both import it, so **the two can never disagree about what is
legal**. Before it existed, the MCP validated everything and the HTTP API
validated almost nothing, and the HTTP API is the thing actually exposed.

Every refusal names the offending field and the valid set, so a caller can fix
the request instead of guessing. The rules themselves are in
[`docs/api.md#validation`](api.md#validation).

### 3. `web/`: the React client

A React app that renders the boards live. It is same-origin: it fetches `/api/*`
relative, so it works on the direct port and behind a reverse proxy with no CORS
or base-href concerns.

- `transport.ts` streams the snapshot over SSE and turns it into board objects.
- `useLivewall.ts` holds the latest boards in state; each board carries its own
  `blocks`, so there is no client-side model to keep in sync.
- `components.tsx` renders the blocks — the 14 components plus the
  `renderBlock` dispatcher.
- `App.tsx` is the shell: app bar, tab bar, selected board.

The **wire format is the board spec itself**: the server streams each board's
blocks exactly as stored and the client renders them directly, with no
intermediate UI protocol. The renderer is deliberately *total*: for every field
it either renders the value or degrades it to something visible and honest
(`""`, `0`, a positive default), but never raises and never shows a Python-style
repr. The contract refuses hostile values at the API; the renderer's totality is
the second line of defence for a board already in memory.

The small behaviours that used to be applied while translating a board are now
the client's, and are specified in [`docs/protocol.md`](protocol.md): the exact
number rule, the log render cap, the empty state, the unknown-block fallback, and
totality. [`docs/blocks.md`](blocks.md) has the vocabulary.

## Why the transport is shaped the way it is

The client **deliberately does not use `EventSource`.** `EventSource`'s reconnect
is opaque. More importantly, it cannot detect a **silently dead**
stream: a connection that stays open but stops delivering bytes. Backgrounded
mobile tabs produce exactly that. `EventSource` also gives no hook for "the tab
just came back, re-sync now."

So the transport is a `fetch`-based SSE reader with three mechanisms:

- **Retry.** Any drop or error schedules a reconnect after 2 seconds. The retry
  is single-shot (not a spin): a failing backend cannot make the client hammer it.
- **Stall watchdog.** A timer runs every 10 seconds; if no line has arrived for
  45 seconds, the client treats the stream as dead and reconnects. The server's
  keepalive snapshot is 15 seconds, so 45 seconds is three missed keepalives.
- **Visibility resume.** When the tab becomes visible again, the client forces a
  fresh connection and re-syncs.

A subtle but important detail is the **epoch guard**. Every connection teardown
bumps an epoch; a callback from a superseded connection is ignored. Without it, a
self-initiated abort (a reconnect, the watchdog, or a pull-to-refresh) surfaced
as a fake "cannot reach backend" error and then tore down the *fresh* connection.
One visibility resume cost three connections, two phantom errors, and a two
second delay.

The server side matches this. Every broadcast is a **complete snapshot**, so a
slow subscriber only ever needs the most recent one: each subscriber's queue is
bounded at one pending snapshot and a newer broadcast replaces the unread older
one (**newest-wins coalescing**). A stalled client can therefore never make the
server grow without bound. The queue is removed from `SUBS` in a `finally` when
the client disconnects.

## Why the wire is the board spec

The server does not compile a board into a renderer-specific protocol. It stores
the spec the agent sent (after validation) and streams it back verbatim, so what
is on the wire is exactly what the contract accepted. That keeps the server
small, makes the stream inspectable, and means any client that can render the
block vocabulary can draw the board — Livewall's React client is one such
renderer, not a privileged one. The components live in
`web/src/livewall/components.tsx` rather than being wired into the shell for that
reason.

The components are ordinary React: a gauge, a sparkline and a line chart drawn
as SVG, chips,
bars, a table, a log, and so on. Colours come from CSS custom properties, so
light/dark needs no plumbing through props.

## Why boards are ephemeral

Boards are **one-off surfaces by design**. An agent programs a board for a
running operation; it lives on a TTL and then it is gone. This is not a missing
persistence feature:

- There is nothing to migrate, back up, or clean up between runs.
- A board's value is its *current* state. A stale board from last week is
  misleading, not useful.
- In-memory storage keeps the whole service small and dependency-free.

A finished board (`status: done`/`error`) survives for its `ttl` (default
`1800s`); a `running` board that stops updating is pruned after `stale_ttl`
(default `3600s`), which is how a crashed agent's board eventually goes away. The
board cap (`max_boards`, default `12`) evicts the least-recently-updated boards.

## The life of one update

1. An agent calls `update_block` (MCP) or `POST /api/boards/{id}/update`.
2. The contract validates the merged block **before** mutating it.
3. `_bump` increments the board's `version`, sets `updated`, and calls
   `_broadcast`.
4. `_broadcast` serializes one snapshot of all boards and hands it to every
   subscriber queue (coalescing if a subscriber has not read yet).
5. Each browser's transport parses the frame and re-renders the selected board
   from its blocks.

There is no per-field delta on the wire; the snapshot is the unit of
consistency. That is what keeps the client simple and the server bounded.
