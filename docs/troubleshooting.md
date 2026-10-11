# Troubleshooting

The failure modes a new user actually hits, and the fix. Every symptom below is
grounded in the code — the exact strings and defaults are quoted from the
source, so you can match what you see.

## "The board never appears"

Work down this list; each step is a different cause.

1. **Wrong URL or port.** The default is <http://127.0.0.1:8470>. If you set
   `LIVEWALL_PORT`, or set a host port for Docker, the client is on *that* port.
   Confirm the backend is up:

   ```bash
   curl http://127.0.0.1:8470/health
   # {"ok":true,"boards":0,...}
   ```

2. **The client was never built.** The backend serves the client from
   `web/dist` at `/`. If that directory is missing, `/` returns a JSON hint
   instead of the app:

   ```json
   {"ok":true,"detail":"Livewall API is running; no web client build found.",
    "build":"cd web && npm install && npm run build",
    "endpoints":["/health","/api/boards","/api/stream"]}
   ```

   Fix it with `make build` (or `cd web && npm install && npm run build`).
   Under Docker the client is built in the image, so this only affects a
   bare-metal run.

3. **A stale `?board=` id.** If you opened a deep-link to a board that is gone,
   the client falls back to following the newest board and shows a quiet notice:

   > board 'old-id' not found — following the newest

   That is not an error; the stale id is dropped from the address bar. Click a
   tab, or drop the `?board=` parameter.

4. **It really is empty.** The empty state is the literal string:

   > waiting for an agent to program a board…

   It means there are **zero** boards, not that something broke. Program one
   yourself (`livewall demo`, or a `POST /api/boards/{id}`) or check that your
   agent's `set_board` actually succeeded. A board can also disappear because it
   expired: a `done`/`error` board lives for its `ttl` (default 1800s), a
   `running` board that stops updating for `stale_ttl` (default 3600s), and past
   `max_boards` (default 12) the least-recently-updated boards are evicted.

## "It says STALE / the numbers froze"

The shell has a **freshness chip** in the top bar that reads `live · updated 3s
ago` while the stream is healthy, and turns to `stale · updated 4m ago` when it
is not. That chip is the **only** staleness indicator — nothing is drawn over
the board, so a board that is already a solid colour (an error board, a status
wall) is never tinted or covered.

"Stale" means one of two things (from `web/src/livewall/freshness.ts`): the
transport is **not connected**, or **no snapshot has arrived for more than 45
seconds** (`STALE_AFTER_SECONDS = 45`). The backend sends a full snapshot frame
after 15 seconds of silence (`LIVEWALL_KEEPALIVE`, default 15), so a healthy
stream never goes 45s without data — 45s is three missed keepalives; the
transport's own stall watchdog uses the same 45s threshold. On any drop it
retries after 2 seconds, and when a backgrounded tab becomes visible again it
forces a fresh connection.

**Is it the backend or the browser?** Ask the backend directly:

```bash
curl http://127.0.0.1:8470/health      # is it up at all?
curl http://127.0.0.1:8470/api/boards  # does it know about the board?
```

- If `curl` works and the board updates there, the **backend is fine** — the
  problem is the browser or the path to it. A backgrounded tab is suspended by
  mobile browsers (bring it to the foreground and it re-syncs), and a
  **buffering reverse proxy** will hold the SSE stream so nothing arrives: the
  proxy must not buffer `/api/stream`. The backend already sends
  `X-Accel-Buffering: no`; configure the proxy to disable response buffering
  too. See [Deploying § Behind a reverse proxy](deploying.md#behind-a-reverse-proxy).
- If `curl` fails too, the **backend is down or unreachable** — restart it, and
  check `LIVEWALL_HOST`/`LIVEWALL_PORT`.

## "401 unauthorized"

This only happens when the backend was started with **`LIVEWALL_TOKEN`** set.
Then every `/api/*` endpoint requires the token, as a header:

```
Authorization: Bearer <token>
```

or equivalently on the query string, `?token=<token>`. Two endpoints are
deliberately left open: **`GET /health`** (and on a token-protected backend an
unauthenticated `/health` returns liveness only, `{"ok":true}`, with no board
ids or config) and the **static client**.

The common trap is **where the token is read from**:

- The **backend** and the **CLI** read `LIVEWALL_TOKEN` from the **process
  environment**. A value in `.env` alone does nothing for them — `export` it, or
  put it in the systemd/compose environment.
- The **MCP server** reads the repo-root `.env` automatically.
- **`docker compose`** substitutes `LIVEWALL_TOKEN` from `.env` into the
  container.
- The **browser** never reads an env var: open the client once with
  `?token=<token>`, and it stores the token and strips it from the URL. If you
  open the plain URL with no token, every `/api/*` fetch is a 401.

## "409 on set"

A board id is a **single, shared, unowned namespace**, so `POST /api/boards/{id}`
to an id that already exists is refused rather than silently overwriting a live
board:

```json
{"ok":false,"error":"board 'migration' already exists; choose a different id, or pass replace=true to overwrite it."}
```

Fix it one of three ways:

- pick a **specific, unique** id that includes the task or run
  (`migrate-users-2026-10-09`, not `progress` — generic ids are exactly what
  collide);
- pass **`?replace=true`** (CLI `--replace`) when you *mean* to replace the live
  board;
- or use `PATCH` / `POST .../update` / `POST .../reset`, which address a board
  the caller already knows and are **not** affected by the collision guard.

A non-boolean `replace` value (`?replace=maybe`) is refused with 400 rather than
silently treated as false.

## "My block was rejected"

A refusal is a 400 with an `error` string that **names the offending field and
the valid set**. Read it; it usually tells you the fix. Real examples:

```json
{"ok":false,"error":"block 0: unknown type 'nope'. Valid types: bar, callout, chart, code, divider, gauge, heading, kv, list, log, progress, sparkline, stat, stats, table, text."}

{"ok":false,"error":"block 0 (progress): unknown field(s) ['nope']. Valid fields: ['fmt', 'id', 'label', 'max', 'type', 'unit', 'value']."}

{"ok":false,"error":"block 0 (progress): `value` must be a finite number (or numeric string), got str."}

{"ok":false,"error":"block 'c' is a 'code' block — `append` only works on log blocks. Use `set` instead."}
```

What they mean:

- **Unknown type** — the type is not in the vocabulary. The valid set is listed.
- **Unknown field** — that type does not declare the field. An unknown field is
  refused (it used to render as a silent `[unknown block]`).
- **Value kind** — structure and value types are both checked. A dict where the
  client iterates, a string where a number is needed (`progress.value`,
  `sparkline.values`), or a nested object in a table cell is refused with the
  field named.
- **`append` on a non-`log` block** — only `log` accepts `append`; a `code`
  block renders from its static `text`, so use `set`.
- **Duplicate / non-string block `id`** — block ids must be unique strings
  (`update_block` addresses blocks by id).
- **Board id** — must already be a canonical slug: lowercase letters, digits,
  `-`, `_`, at most 64 chars. It is **not** silently slugged.

The authoritative vocabulary lives in the shared contract
([`backend/contract.py`](../backend/contract.py)). At runtime an agent should
call **`describe_blocks`** over MCP, which returns the block types, their
fields, the tab spec and the number-formatting rule. For humans it is
[Blocks](blocks.md), and the validation rules are in
[HTTP API § Validation](api.md#validation).

## "Docker compose is up but nothing loads"

- **Check the container is actually up and healthy.** The compose healthcheck
  hits `http://127.0.0.1:8470/health` inside the container every 30s:

  ```bash
  docker compose ps
  docker compose logs livewall
  ```

- **The port is on all interfaces by default.** The compose mapping is
  `"${LIVEWALL_PORT:-8470}:8470"`, and Docker's short syntax with a single port
  binds `0.0.0.0` on the host. If another service already owns the port, the
  container starts but the mapping fails — check `docker compose ps` and the
  logs. To bind host-loopback instead, change the mapping to
  `127.0.0.1:${LIVEWALL_PORT:-8470}:8470`.

- **Inside the container the backend listens on `0.0.0.0`.** That is deliberate
  (a process bound to `127.0.0.1` inside a container is unreachable from the
  host), and is why the compose file hardcodes it rather than substituting
  `LIVEWALL_HOST`.

## "Loopback works, the domain doesn't"

This is the reverse-proxy path. Two Livewall-specific requirements:

1. **Do not buffer the SSE stream.** If the proxy buffers `GET /api/stream`, the
   browser sees nothing (or only a final flush). For nginx set
   `proxy_buffering off;` and `proxy_cache off;`. The backend already sends
   `X-Accel-Buffering: no` and a keepalive every 15s.
2. **Allow a long-lived stream.** Set the proxy's read timeout comfortably
   longer than the 15s keepalive (e.g. `proxy_read_timeout 3600s;`). Caddy
   streams by default and needs no special handling.

Also confirm the proxy forwards to the right address (`127.0.0.1:8470` by
default) and that the backend is bound where the proxy expects — it binds
loopback unless you set `LIVEWALL_HOST`. The full nginx and Caddy configurations,
the exposure model, and the token/access-control options are in
[Deploying](deploying.md).

## "The MCP server won't start"

- **Wrong interpreter.** The server needs `fastmcp` and `httpx`, which
  `make install` puts in `.venv`. A bare `python` often lacks them and fails
  with `ModuleNotFoundError: No module named 'fastmcp'`. Point the client's
  `mcpServers` entry at that interpreter:

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

  If your host uses a different shape, adapt the same command and args — some
  hosts read a YAML `mcp_servers` map:

  ```yaml
  mcp_servers:
    livewall:
      command: /path/to/livewall/.venv/bin/python
      args: ['/path/to/livewall/mcp/server.py']
  ```

  To check by hand, run it over stdio — it prints a FastMCP banner and then
  waits:

  ```bash
  /path/to/livewall/.venv/bin/python mcp/server.py
  # or: .venv/bin/fastmcp run mcp/server.py:mcp
  ```

- **Environment / `.env`.** The server reads `LIVEWALL_URL` (default
  `http://127.0.0.1:8470`) and `LIVEWALL_TOKEN`. It loads a repo-root `.env`
  automatically (override the file with `LIVEWALL_MCP_ENV_FILE`). Remember the
  backend and CLI do **not** read that `.env` — only the MCP server and
  `docker compose` do.

- **The server starts but calls fail.** That is usually the backend: not
  running, the wrong `LIVEWALL_URL`, or a `LIVEWALL_TOKEN` mismatch (the server
  sends the token as a bearer header; a missing/extra token yields
  `401 unauthorized`). Check `curl http://127.0.0.1:8470/health`.

- **Testing note.** Do not use the `fastmcp` **CLI** as a test harness: it strips
  the environment from the spawned server process, so a server that works
  in-process fails under `fastmcp call`. Use `mcp/test_live.py` (the in-memory
  client) instead — `make test-mcp` boots a throwaway backend for it.
