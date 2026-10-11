# Getting started

**Livewall is a live status board your AI agent writes while it works.** The
agent sends a small JSON *board spec* and a browser renders it live — a progress
bar for a migration, a table of test results, a log tail, a fleet status wall —
with no per-use-case UI code.

This page takes you from nothing to a board you can watch update. It is a
tutorial: every command here was run against this tree. When you want the rules
rather than a walkthrough, the [block reference](blocks.md) and the
[HTTP API](api.md) are the place to look.

## What you need

- **Python 3.10+** for the backend and the MCP server. The backend and MCP
  dependencies (`fastapi`, `fastmcp`) declare `Requires-Python: >=3.10`. The
  standalone CLI is standard-library only and declares `>=3.9`
  (`cli/pyproject.toml`), so a shell-only setup can be older.
- **Node 22+** *only if you build the client yourself.* The Dockerfile uses
  `node:22-alpine`, and the web test runner uses `node --experimental-strip-types`.
- **Docker** if you would rather not install Python or Node at all.

You do not need a database. Boards live in the backend's memory and expire on a
TTL.

## Path A — Docker

From a checkout of the repository:

```bash
cp .env.example .env          # optional: every value has a working default
docker compose up --build     # then open http://127.0.0.1:8470
```

`docker compose up --build` builds the client with Node, starts the backend with
the built client mounted at `/`, and serves both on one port. A successful first
load is the app shell with an empty board area reading:

> waiting for an agent to program a board…

The liveness probe is open and needs no browser:

```bash
curl http://127.0.0.1:8470/health
# {"ok":true,"boards":0,"ids":[],"subscribers":0,"config":{...}}
```

To use a different host port, set `LIVEWALL_PORT` before `up` (it changes the
host side of the mapping only; the container always listens on `8470`):

```bash
LIVEWALL_PORT=9000 docker compose up --build   # http://127.0.0.1:9000
```

> **A non-default port moves the browser only.** The CLI and the MCP server
> do **not** read `LIVEWALL_PORT` or this `.env` file's port: both default to
> `http://127.0.0.1:8470` and will talk to whatever answers there — possibly a
> different Livewall, or nothing. Export `LIVEWALL_URL=http://127.0.0.1:9000`
> in the shell for the CLI (it reads only the process environment), and set
> the same `LIVEWALL_URL` in the MCP `mcpServers` config — or in the repo-root
> `.env`, which the MCP server loads (the CLI does not).

> The compose file publishes the port on **all** host interfaces. If you want
> the loopback-only behaviour the backend itself defaults to, change the mapping
> to `127.0.0.1:${LIVEWALL_PORT:-8470}:8470`. See
> [Deploying](deploying.md#docker).

## Path B — bare metal

One virtualenv holds the backend, the MCP server and the test dependencies:

```bash
make install     # .venv (backend + MCP + tests) and web/node_modules
make build       # client -> web/dist, which the backend serves at /
make backend     # http://127.0.0.1:8470
```

`make install` also downloads Playwright's Chromium for the browser test suites;
if you only want to run the app, that download is the slow part and is optional
for day-to-day use. `make backend` is exactly
`.venv/bin/python backend/app.py`, and the backend binds loopback by default.

Open <http://127.0.0.1:8470> and you get the same shell and the same empty state
as Path A.

<details>
<summary>Client development (Vite)</summary>

`make dev` runs the backend on `:8470` and the Vite dev server on `:5183`
together; open <http://127.0.0.1:5183>. Vite proxies `/api` to the backend. Or
run them separately:

```bash
make backend
cd web && npm run dev
```
</details>

## Program a board by hand (no agent)

Before wiring up an agent, program a board yourself so you can see the whole
loop. Everything the agent does, you can do from a shell.

### The CLI

Install the CLI, or run it in place:

```bash
pipx install ./cli            # puts `livewall` on your PATH
# ...or, with no install:
python3 cli/livewall.py --help
```

See it work in one command:

```bash
livewall demo
```

That creates a sample board (`livewall-demo`) and prints where to look. Refresh
the browser and a **Demo** tab appears with a progress bar, chips and a log.

Now make your own board. `set` creates a board; its blocks come from a JSON file
(`--blocks-file` for the block array, or `--file` for a whole board body). Write
the blocks:

```bash
cat > /tmp/blocks.json <<'JSON'
[
  {"id": "p", "type": "progress", "label": "Rows copied",
   "value": 0, "max": 6950, "unit": "rows"},
  {"id": "l", "type": "log", "title": "recent", "lines": ["batch 1 ok"]}
]
JSON
```

Create the board, then update it — one block at a time:

```bash
livewall set migrate-users-2026-10-09 --title "Migration" \
  --status running --blocks-file /tmp/blocks.json

livewall update migrate-users-2026-10-09 --block p --set '{"value": 6836}'
livewall update migrate-users-2026-10-09 --block l --append "batch 41 ok"
```

The browser tab now reads **6836 / 6950 rows** and shows both log lines. Follow
it from the terminal, and remove it when you are done:

```bash
livewall watch migrate-users-2026-10-09    # Ctrl-C to stop; --once to print once
livewall rm migrate-users-2026-10-09
```

> **Gotcha — `set` with no blocks.** `livewall set <id> --title ...` with no
> `--blocks-file`/`--file` creates a board with **no blocks**, so a later
> `update --block p` fails with `404: no block id 'p'`. Create the blocks up
> front, as above.

`livewall` reads `LIVEWALL_URL` (default `http://127.0.0.1:8470`) and
`LIVEWALL_TOKEN` from the environment. See [`cli/README.md`](../cli/README.md)
for every command.

### The raw HTTP API

The CLI is a thin wrapper over the API; the same thing with `curl` is:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09 \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Migration",
    "status": "running",
    "tab": {"label": "Migration", "accent": "violet",
            "indicator": {"kind": "bar", "value": 0, "max": 6950}},
    "blocks": [
      {"id": "p", "type": "progress", "label": "Rows copied",
       "value": 0, "max": 6950, "unit": "rows"},
      {"id": "l", "type": "log", "title": "recent", "lines": ["batch 1 ok"]}
    ]
  }'
```

Update one block without resending the board, and append to the log:

```bash
curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09/update \
  -H 'Content-Type: application/json' -d '{"id": "p", "set": {"value": 6836}}'

curl -s -X POST http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09/update \
  -H 'Content-Type: application/json' -d '{"id": "l", "append": "batch 41 ok"}'
```

A second `POST` to an id that already exists is refused with **409** rather than
overwriting the live board. To replace it on purpose, add `?replace=true`, or
use `PATCH`/`/update`/`/reset`, which are not affected by the collision guard:

```bash
curl -s -X POST 'http://127.0.0.1:8470/api/boards/migrate-users-2026-10-09?replace=true' \
  -H 'Content-Type: application/json' -d '{"title": "Migration (restarted)", "status": "running"}'
```

See [`docs/api.md`](api.md) for every endpoint and its validation rules.

## Wire it to an agent

The MCP server (`mcp/server.py`) exposes the same API as **11 MCP tools**, so an
MCP host can program a board mid-task. Point your host at the server. Most MCP
clients — Claude Desktop, Cursor, Claude Code (`.mcp.json`), and others — read a
`mcpServers` JSON object:

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

Use the interpreter that has `fastmcp` and `httpx` — the one `make install`
built at `.venv/bin/python` (the file path, not a bare `python`, unless your
`python` resolves there). If your host uses a different config shape, adapt the
same command and args; some hosts read a YAML `mcp_servers` map instead:

```yaml
mcp_servers:
  livewall:
    command: /path/to/livewall/.venv/bin/python
    args: ['/path/to/livewall/mcp/server.py']
```

The server talks to `LIVEWALL_URL`
(default `http://127.0.0.1:8470`) and reads a repo-root `.env` automatically; if
the backend is token-protected, put the same `LIVEWALL_TOKEN` in that `.env` and
the server sends it on every call.

Once wired, a realistic flow looks like this. The agent calls `describe_blocks`
first to learn the vocabulary at runtime, then programs a board and updates it
as it works:

```jsonc
// 1. describe_blocks() -> the block types, their fields, and the number rule.

// 2. set_board(board_id="migrate-users-2026-10-09", title="Migration",
//              status="running", ttl=1800,
//              blocks=[{"id": "p", "type": "progress", "label": "Rows copied",
//                       "value": 0, "max": 6950},
//                      {"id": "l", "type": "log", "lines": ["batch 1 ok"]}])
//    -> {"ok": true, "id": "migrate-users-2026-10-09", "version": 1, ...}

// 3. ...as each batch finishes:
//    update_block(board_id="migrate-users-2026-10-09", block_id="p",
//                 set_fields={"value": 6836})
//    update_block(board_id="migrate-users-2026-10-09", block_id="l",
//                 append="batch 41 ok")

// 4. when it is done, flip the board terminal (this starts its TTL clock):
//    update_board(board_id="migrate-users-2026-10-09",
//                 status="done", subtitle="6,950 rows in 4m")
```

[`docs/mcp.md`](mcp.md) lists all 11 tools, their arguments and their refusals.

## How a board updates

There is no per-field delta on the wire. The lifecycle is:

1. The agent (over MCP) or you (over HTTP) sends a **board spec** — title,
   status, blocks, optional tab and TTL.
2. The backend validates the spec against the shared contract
   (`backend/contract.py`) **before** it mutates anything, and stores the board
   in memory.
3. Every connected browser receives a **complete snapshot** of all boards over
   **SSE** (`GET /api/stream`), and the React client renders the selected
   board's blocks directly.

Because every frame is a full snapshot, a slow or briefly disconnected client
just receives the newest state — it never has to replay deltas. The transport
also retries on a drop, watches for a silently dead stream, and re-syncs when a
backgrounded tab comes back. That design, and the bug stories behind it, are in
[`docs/architecture.md`](architecture.md).

## Where to go next

- **[Cookbook](cookbook.md)** — copy-paste recipes: a migration, test results, a
  log tail, a fleet wall, a CI step, a metric over time.
- **[Blocks](blocks.md)** — every block type, every field, and the
  exact-by-default number rule.
- **[HTTP API](api.md)** — endpoints, status codes, and validation.
- **[MCP](mcp.md)** — every tool the agent gets.
- **[Deploying](deploying.md)** — Docker, bare metal, and behind a reverse
  proxy.
- **[Runnable examples](../examples/README.md)** — small scripts you can copy
  and run against a local backend.
