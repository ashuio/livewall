---
name: livewall
description: Show a long-running job on a live board instead of going silent or spamming status messages. Use when work will run for minutes and someone should be able to watch it — migrations, batch jobs, fleet sweeps, builds, backfills, audits — or when the user mentions Livewall, a status board, a dashboard, or "watch it live".
license: Apache-2.0
compatibility: Needs a running Livewall backend (default http://127.0.0.1:8470) and, for the MCP path, an MCP client with the livewall server configured. The HTTP API and the CLI need no MCP host.
metadata:
  homepage: https://github.com/ashuio/livewall
---

# Livewall

Livewall is a live status surface: you send a small JSON **board spec**, a browser
renders it, and it updates over SSE as you push changes. It is the third option
between going silent during a long job and spamming status messages.

**The point is the MIDDLE of a long operation.** A board lets someone watch
progress in real time instead of asking "how's it going?". Program one when work
runs long enough that a person would otherwise ask — not for a sub-30-second
command, which just buries the signal.

## Before you write a board

**Call `describe_blocks` first.** It is the authoritative, runtime vocabulary:
every block `type`, its fields, the tab spec, the valid statuses, and the
number-formatting rule. Do **not** work from memory or from an example you saw
elsewhere — the vocabulary grows, and a field name that looks right but is not
makes the whole call fail (nothing is created).

Then:

1. **Pick a specific board id** — include the task or run, e.g.
   `migrate-users-2026-10-11`, never a bare `progress`. Ids are a shared,
   unowned namespace: a generic id is exactly what collides with another session.
2. **`set_board`** with a first block or two and `status: "running"`.
3. **`update_block`** as the work advances — bump a `progress` value, append a
   line to a `log`. One block at a time, no full-board resend.
4. **Flip it terminal** — `update_board(status="done")` (or `"error"`), with a
   closing block.
5. **Set a long `ttl` at creation** if the board should outlive the default — you
   only need to set it once (see the traps below).

A typical board is a `heading`, a `progress`, a `stats`, and a `log`. Keep it
scannable — it is read at a glance, not studied.

## Traps (each of these is real and costs a cycle)

- **`ttl` is not what keeps a running board alive.** `ttl` governs only a board
  that has reached a terminal status (`done`/`error`). A board still `running` is
  pruned after `stale_ttl` of **no updates** (default 3600s = 1h) — regardless of
  its `ttl`. So a long job must keep pushing (a heartbeat `update_block` is
  enough); a long `ttl` will not save a board you stopped updating. `ttl` set at
  creation is **preserved** across `set_board(replace=True)` and `update_board`, so
  set it once — you do not need to re-apply it. Confirm from `list_boards` with
  `ttl`/`effective_ttl`; `expires_in` stays `null` while the board is `running`.
- **An existing id is refused with 409.** The remedy is **`replace=true`**, not a
  new id — a new id fragments into a duplicate tab instead of re-claiming the
  board. `list_boards` first: if the id is still live, `set_board(..., replace=True)`
  re-claims it; if it expired, a plain `set_board` creates it fresh. (`replace`
  must be a real boolean.)
- **Boards are in-memory and deliberately short-lived.** A backend restart wipes
  every board. Never assume a board you created earlier still exists — re-check
  with `list_boards`. Do not try to make a board persistent; that is by design.
- **`list` items are scalars, not pairs.** `list` takes `items: ["a", "b"]`. An
  array-of-pairs is refused — for key/value rows use the **`kv`** block.
- **MCP argument names differ from the HTTP body.** The MCP tool is
  `update_block(board_id, block_id, set_fields={...}, append="...")`; the HTTP
  endpoint takes `{"id": ..., "set": {...}}`. And `update_board` has **no `blocks`
  field** — use `set_board` to replace blocks.
- **A full-board refresh has exactly one shape** (relevant when you drive the
  HTTP API directly): `POST /api/boards/{id}?replace=true`. `replace` is a
  **query parameter, not a body field** — in the body it is ignored and every
  call after the first 409s. And `PATCH /api/boards/{id}` updates board-level
  fields **only**; it cannot rewrite `blocks`. Symptom of getting this wrong:
  the board's `version` climbs while the content stays frozen. **A version bump
  is not proof the board updated — assert on a value.**
- **Numbers are exact by default.** Pass a number for a count a human reads off
  (`6836`, not `6.8K`). Compact only scale-only quantities, and only explicitly —
  `fmt: "compact"` on a `progress`/`gauge`/`bar`, or a pre-formatted string in a
  `stat`. See `describe_blocks` for the full rule.
- **After a vocabulary change, the MCP server is stale until restarted.** If a
  brand-new block type is refused as `unknown type`, the MCP process is holding an
  older contract than the backend — restart the MCP server (a gateway-level
  action), do not rework the board spec.

## Unattended updates (no agent in the loop)

Livewall never fetches anything itself — it is a renderer. To drive a board from a
health endpoint, a command, or a file **with no agent attached**, run an external
**pusher**: a small program that polls the source and pushes on an interval.

A ready-made one ships with the project (`examples/pusher.py`, standard library
only) and the pattern is documented in `docs/pushers.md`. A source supplies a
**value**, never structure — it fills one declared block field, so a flaky
endpoint can change a number but never the shape of the board.

## The other surfaces

The same API is available three ways; pick the one that fits:

| Surface | Use when |
|---|---|
| **MCP** (`set_board`, `update_block`, …) | you are an agent with the MCP server configured — the friendliest, it validates before sending |
| **HTTP** (`POST /api/boards/{id}`) | a script, a CI step, or any language |
| **CLI** (`livewall set/update/watch`) | a shell, cron, or a terminal (`pipx install ./cli`) |

Full reference: `docs/mcp.md`, `docs/api.md`, `docs/blocks.md`, `docs/pushers.md`.
