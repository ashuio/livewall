# MCP

`mcp/server.py` exposes the Livewall API as **11 MCP tools**, so any MCP client
(Claude Code, Cursor, …) can program boards. The MCP layer is an **earlier,
friendlier guard** in front of the same HTTP API: it validates against the shared
contract (`backend/contract.py`) and turns the API's silent failure modes into
named refusals before the request is sent. The API enforces the same rules for
callers that skip MCP.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `LIVEWALL_URL` | `http://127.0.0.1:8470` | base URL of the backend |
| `LIVEWALL_TIMEOUT` | `15` | HTTP timeout, seconds |
| `LIVEWALL_TOKEN` | *(unset)* | bearer token, when the backend gates `/api/*` |
| `LIVEWALL_MCP_ENV_FILE` | repo-root `.env` | env file to load, if present |

The repo-root `.env` is read automatically when it exists. If the backend is run
with `LIVEWALL_TOKEN` set, set the same value here and the server sends it as an
`Authorization: Bearer` header on every call — no other change is needed (see
[`deploying.md`](deploying.md)). Note this `.env` is loaded by the MCP server
only: the backend and the CLI read the process environment, so give them the
token with `export` (or in the unit/compose environment), not only in `.env`.

Run it over stdio:

```bash
python mcp/server.py
# or
fastmcp run mcp/server.py:mcp
```

> The `fastmcp` **CLI is not a valid test harness** here: it strips the
> environment from the spawned server process, so a server that works in-process
> fails under `fastmcp call`. Use `mcp/test_live.py` (the in-memory client)
> instead.

## Wiring

Most MCP clients — Claude Desktop, Cursor, Claude Code (`.mcp.json`), and
others — read a `mcpServers` JSON object:

```json
{
  "mcpServers": {
    "livewall": {
      "command": "/path/to/livewall/.venv/bin/python",
      "args": ["/path/to/livewall/mcp/server.py"],
      "env": { "LIVEWALL_URL": "http://127.0.0.1:8470" }
    }
  }
}
```

Use the interpreter that has `fastmcp` and `httpx` (the one `make install`
builds at `.venv/bin/python`). If your host uses a different config shape, adapt
the same command and args; some hosts read a YAML `mcp_servers` map instead:

```yaml
mcp_servers:
  livewall:
    command: /path/to/livewall/.venv/bin/python
    args: ['/path/to/livewall/mcp/server.py']
```

New tool schemas register at the next client restart.

## The tools

### Discovery

| Tool | Arguments | Returns |
|---|---|---|
| `livewall_ping` | none | backend health + current config |
| `describe_blocks` | none | the block vocabulary, board fields, tab spec, statuses, and the number-formatting rule |

`describe_blocks` is how an agent learns the vocabulary **at runtime**. It is the
authoritative list of block types and their fields; call it first.

### Read

| Tool | Arguments | Returns |
|---|---|---|
| `list_boards` | none | `{count, boards:[…]}`: every board summary (status, ttl, age, progress, resolved tab) |
| `get_board` | `board_id` | one board's full spec (title, status, blocks, tab, ttl) |
| `get_config` | none | `{default_ttl, stale_ttl, max_boards}` |

### Write

| Tool | Arguments | Returns |
|---|---|---|
| `set_board` | `board_id`, `title`, `blocks`, `subtitle=""`, `status="running"`, `tab=None`, `ttl=None`, `replace=False` | `{ok, id, version, summary}` |
| `update_board` | `board_id`, `title?`, `subtitle?`, `status?`, `tab?`, `ttl?` | `{ok, id, version, summary}` |
| `update_block` | `board_id`, `block_id`, `set_fields?`, `append?` | `{ok, id, version, block}` |
| `reset_board` | `board_id` | `{ok, id, version}` |
| `delete_board` | `board_id` | `{ok, id}` |
| `set_config` | `default_ttl?`, `stale_ttl?`, `max_boards?` | `{ok, config}` |

Notes:

- `set_board` **creates a board**. A board id is a shared, unowned namespace, so
  calling it on an id that already exists is refused with **HTTP 409** (surfaced
  as an error naming the id) — it does **not** silently overwrite another
  session's board. To replace a live board on purpose, pass `replace=True`. To
  update a board you already know, use `update_board` / `update_block` /
  `reset_board` instead; they never need the flag. Choose a **specific, unique**
  id that includes the task or run (e.g. `migrate-users-2026-10-08`), not a bare
  generic word like `progress`.
- `update_board` changes board-level fields without touching blocks. It refuses
  an empty update (`nothing to update — pass at least one field.`).
- `update_block` changes **one** block. `set_fields` maps field → value (e.g.
  `{"value": 9410}`); `append` adds a line to a `log` block. At least one of the
  two is required. On a missing `block_id` the error **lists the ids that do
  exist** instead of a bare 404.
- `set_config` values must be integers; its schema is **strict** (a JSON bool or
  a numeric string is refused). This is deliberately tighter than the HTTP API,
  which accepts a numeric string.
- Writes read the resulting state back, so the return value is the authoritative
  version and summary, not just an ack.

## A multi-step example

An agent watching a migration. First it learns the vocabulary (abbreviated):

```jsonc
// describe_blocks() ->
{
  "block_types": {
    "progress": "label, value, max, unit?",
    "log": "title?, lines:[...]",
    "callout": "text, tone? (ok|warn|error|info)",
    /* … */
  },
  "number_format": {"rule": "Numbers are EXACT by default. YOU choose when to compact, per value."}
}
```

Then it programs the board:

```jsonc
// set_board(
//   board_id="migration",
//   title="Migration",
//   status="running",
//   ttl=1800,
//   tab={"label": "Migration", "accent": "violet",
//        "indicator": {"kind": "bar", "value": 0, "max": 6950}},
//   blocks=[
//     {"id": "p", "type": "progress", "label": "Rows copied",
//      "value": 0, "max": 6950, "unit": "rows"},
//     {"id": "l", "type": "log", "title": "recent", "lines": ["batch 1 ok"]},
//     {"id": "c", "type": "callout", "tone": "info", "text": "starting"}
//   ])
// ->
{"ok": true, "id": "migration", "version": 1, "summary": { /* … */ }}
```

As it works, it updates the block and appends to the log, with no full-board
resend:

```jsonc
// update_block(board_id="migration", block_id="p", set_fields={"value": 6836})
// -> {"ok": true, "id": "migration", "version": 2, "block": {…, "value": 6836}}

// update_block(board_id="migration", block_id="l", append="batch 41 ok")
// -> {"ok": true, "id": "migration", "version": 3, "block": {…}}
```

When it is done, it flips the board to `done` (which starts the TTL clock):

```jsonc
// update_board(board_id="migration", status="done", subtitle="6,950 rows in 4m")
// -> {"ok": true, "id": "migration", "version": 4, "summary": {…, "status": "done"}}
```

Later, the board expires on its own (`ttl`, default `1800s`). To clear it
immediately instead, `delete_board(board_id="migration")` removes the tab
entirely; `reset_board` keeps the board but clears its blocks and returns it to
`idle`.

## Refusals

Every refusal names the fix. A few examples:

- unknown block type → the valid set is listed;
- duplicate or non-string block id → refused up front;
- unknown `set` field, or `append` on a non-`log` block → refused;
- bad `tab.accent` / `indicator.kind` → refused (they used to silently degrade);
- non-canonical `board_id` → refused (no silent slugging);
- `set_board` on an id that already exists → refused with `409` (pass
  `replace=True` to overwrite on purpose, or pick another id);
- a write to a missing board → an error naming the board.

`mcp/test_live.py` asserts these refusals name the fix, and runs a full write
lifecycle with cleanup. From the repo root, `make test-mcp` boots a throwaway
backend for it.
