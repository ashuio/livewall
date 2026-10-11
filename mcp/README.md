# mcp — Livewall as MCP tools

MCP server exposing [Livewall](../) — an agent-programmable live status
surface — as MCP tools. Any MCP client (Claude Code, Cursor, …) can create,
update and tear down live boards that the web client renders in real time.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `livewall_ping` | read | health check + current config |
| `describe_blocks` | read | **the block vocabulary** — call this first |
| `list_boards` | read | every board with status, ttl, age, progress, resolved tab |
| `get_board` | read | one board's full spec |
| `get_config` | read | global defaults |
| `set_board` | write | create a board (409 if the id exists; `replace=True` to overwrite) |
| `update_board` | write | partial board fields (no block changes) |
| `update_block` | write | mutate ONE block (cheap, no full resend) |
| `reset_board` | write | clear a board's blocks, back to idle |
| `delete_board` | write | remove a board entirely |
| `set_config` | write | set default_ttl / stale_ttl / max_boards |

## Contract hardening

The upstream API's silent failure modes are turned into server-side refusals:

- **Block vocabulary validated** — an unknown `type` renders as `[unknown block]`
  client-side (silent, ugly). Refused here, naming the valid set.
- **Tab spec validated** — an unrecognised `accent` silently falls back and a bad
  `indicator.kind` silently degrades; both are refused.
- **Block ids** must be unique **strings** (update_block addresses blocks by id)
  — refused up front.
- **Block fields** are validated against the type's vocabulary; an unknown field
  or a non-string `id` is refused, so an agent cannot bypass `update_block`'s
  stricter gate by resending the whole board.
- **Board ids** must be canonical lowercase slugs (`[a-z0-9_-]+`) — refused, so
  two distinct ids can never collide onto one board. And a `set_board` on an id
  that already exists is refused with **409** (the board is left intact), unless
  the caller passes `replace=True`; the tool tells the agent to choose a
  specific, unique id rather than reuse a generic one.
- **Writes read back** and return the resulting version + summary.
- **`update_block` on a missing id lists the ids that DO exist** instead of a bare 404.
- **`set_config` values validated** — each must be a positive integer, and the
  schema is **strict** (JSON integers only). A bool would silently become `1`
  via `int(True)`, so it is refused rather than accepted. (The HTTP API is
  looser — it still accepts numeric strings like `"777"` — because it is a
  JSON-over-the-wire surface; MCP declares `int`, so it enforces `int`.)
- Status / ttl / title / subtitle / tab validated against the shared contract,
  including the block-count (60) and log-line (400) caps. The HTTP API enforces
  the same rules, so a caller that skips MCP still gets a named refusal.

## Setup

From the repo root, `make install` builds a `.venv` holding the MCP server's
dependencies (`mcp/requirements.txt`) alongside the backend's. To do it by
hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.txt
```

Env: `LIVEWALL_URL` (default `http://127.0.0.1:8470`) and `LIVEWALL_TOKEN` (the
bearer token, when the backend gates `/api/*`). The repo-root `.env` is read
automatically if present — override with `LIVEWALL_MCP_ENV_FILE`.

Livewall is open by default. When the backend sets `LIVEWALL_TOKEN`, this server
sends it as an `Authorization: Bearer` header on every call; otherwise bind to
loopback or a private network.

## Test

```bash
.venv/bin/python mcp/test_live.py     # or: make test-mcp
```

Runs the fastmcp **in-memory client** against the real module and the live
backend: every read path, every contract refusal (asserting the message names the
fix), a full write lifecycle with cleanup, and a baseline-restoration check that
restores the captured `default_ttl`/`stale_ttl`/`max_boards` exactly and asserts
it. From the repo root, `make test-mcp` boots a throwaway backend for you.

> The `fastmcp` CLI is NOT a valid harness here — it strips the environment from
> the spawned server process, so a server that works in-process fails under
> `fastmcp call`.

## Client wiring

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

## Pitfall

Do **not** wrap a `raise NewError(...) from err` inside another `except` that
re-raises `err` — that sets `err.__context__` to the new error while the new
error's `__cause__` points back at `err`, forming a traceback cycle that Rich
renders forever. The MCP call hangs with no output. Raise once, `from err`, and
let it propagate.
