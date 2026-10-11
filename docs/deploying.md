# Deploying

Read this first: **Livewall ships open by default — with an optional bearer
token — and the backend binds loopback by default.** That combination is the
design, not an oversight. The intended production shape is:

> **Bind it locally, then put your own access control in front of it.**

A real deployment is therefore two decisions, not one: how to run the process,
and what sits between the network and that process. This page covers both, and is
honest that the defaults are not a production front door.

See also [SECURITY.md](../SECURITY.md) for why the open default is not a
vulnerability report.

## The exposure model

- `LIVEWALL_HOST` defaults to `127.0.0.1`, so a bare process is reachable only
  from the machine it runs on.
- Any client that can reach the port can read and write every board: with the
  default (no `LIVEWALL_TOKEN`) there is no login, no token and no rate limit.
- Boards are in memory and short-lived; there is nothing sensitive to protect
  *inside* the service. The exposure concern is **who can program or read the
  boards**, and that is answered by the network path, not by Livewall.

## Docker

The included `docker-compose.yml` builds the client, starts the backend, and
serves both on one port:

```bash
cp .env.example .env          # optional: every value has a working default
docker compose up --build     # then http://127.0.0.1:8470
```

The `Dockerfile` is a two-stage build: Node builds `web/dist`, then a Python
image installs the backend and copies the built client in. Inside the container
`LIVEWALL_HOST=0.0.0.0`: the process must listen beyond loopback for the port
mapping to reach it. That is not a contradiction of the loopback default; the
container boundary is what keeps it local, *if* you map the port to loopback.

> **The compose file publishes the port on all host interfaces.** The mapping is
> `"${LIVEWALL_PORT:-8470}:8470"`, and Docker's short syntax with a single port
> binds `0.0.0.0` on the host. If you want the host-local behaviour the backend
> defaults to, change it to:
>
> ```yaml
> ports:
>   - "127.0.0.1:${LIVEWALL_PORT:-8470}:8470"
> ```
>
> Then put a reverse proxy (below) in front of `127.0.0.1:8470`.

Other compose details:

- **Config volume.** `LIVEWALL_CONFIG_FILE=/data/config.json` on a named volume
  (`livewall-data`), so runtime config survives container recreation. The
  container runs as a non-root user (uid 10001), and `/data` is owned by that
  user inside the image — a first-use volume inherits the ownership through
  Docker's copy-up. A volume created by an older, root-running image keeps
  root's ownership; migrating it once is a one-liner:

  ```bash
  docker run --rm -v livewall-data:/data alpine chown -R 10001:10001 /data
  ```

  (Without it the upgraded container can read the saved config but cannot
  rewrite it — a config change would fail with `could not persist config`.)
- **Healthcheck.** Hits `http://127.0.0.1:8470/health` every 30s.
- **`restart: unless-stopped`.**

To change the host port, set `LIVEWALL_PORT` before `docker compose up` (it
affects the host side of the mapping).

## Bare metal

```bash
# 1. build the client (the backend serves it from web/dist)
cd web && npm ci && npm run build && cd ..

# 2. install and run the backend
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/python backend/app.py          # http://127.0.0.1:8470
```

The backend serves `web/dist` at `/` when it exists (`LIVEWALL_WEB_DIR` overrides
the location). With no client build present the API still runs headless and `/`
returns a hint on how to build one. The MCP path needs no browser.

`npm run build` compiles into a staging directory and then swaps it into
`web/dist` with renames, so a running instance keeps serving the previous build
until the new one is complete. Build straight into `web/dist` by hand and you
lose that: Vite empties the output directory before it compiles, so `/` answers
404 for the length of the build — and stays 404 if the build fails.

To keep it running, a minimal systemd unit:

```ini
[Unit]
Description=Livewall
After=network.target

[Service]
WorkingDirectory=/opt/livewall
Environment=LIVEWALL_HOST=127.0.0.1
Environment=LIVEWALL_PORT=8470
Environment=LIVEWALL_CONFIG_FILE=/var/lib/livewall/config.json
# Uncomment the next line to require bearer-token auth:
# Environment=LIVEWALL_TOKEN=<openssl rand -hex 32>
ExecStart=/opt/livewall/.venv/bin/python backend/app.py
Restart=on-failure
# An SSE stream only ends when the client disconnects, so a plain `stop`
# would wait on open streams forever; uvicorn cancels them after
# LIVEWALL_GRACEFUL_SHUTDOWN_TIMEOUT (3s) and exits. Keep this bound
# comfortably above that so SIGKILL stays a last resort, not the norm.
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

## Behind a reverse proxy

This is the real deployment. The backend speaks plain HTTP on loopback; the proxy
terminates TLS, enforces access control, and forwards. Two things are specific to
Livewall:

1. **The SSE stream must not be buffered.** If the proxy buffers
   `GET /api/stream`, the browser sees nothing until the buffer flushes (or
   never). The backend sends `X-Accel-Buffering: no` and a keepalive snapshot
   every 15 seconds; configure the proxy to disable response buffering anyway.
2. **The stream is long-lived.** Set a read timeout comfortably longer than the
   15s keepalive (or longer than the whole session).

### nginx

```nginx
server {
    listen 443 ssl;
    server_name livewall.example.com;

    ssl_certificate     /etc/letsencrypt/live/livewall.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/livewall.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8470;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # SSE: never buffer or cache the stream.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

### Caddy

Caddy streams responses by default, so SSE needs no special handling:

```
livewall.example.com {
    reverse_proxy 127.0.0.1:8470
}
```

### Access control

Livewall ships **open by default**, and has an optional built-in **bearer
token**. Two layers, pick what you need:

**1. The built-in token** — set `LIVEWALL_TOKEN` on the backend and every
`/api/*` endpoint requires `Authorization: Bearer <token>` (`/health` stays
open). Generate one with `openssl rand -hex 32`. This protects the *data*; put
the proxy in front as well for TLS.

Where the value has to go: the backend and the CLI read the **process
environment**, not `.env`, so for a bare-metal run `export LIVEWALL_TOKEN=...`
(or add it to the systemd unit) before starting the process. The MCP server
reads the repo-root `.env` automatically, and `docker compose` substitutes
`LIVEWALL_TOKEN` from `.env` into the container. The browser client never reads
an env var — open the client once with `?token=<token>` and it stores it.

**2. The proxy**, when you want a login instead of a shared token — pick one:

- **Basic auth** (simplest):

  ```
  livewall.example.com {
      basicauth {
          you $2a$14$<bcrypt-hash>
      }
      reverse_proxy 127.0.0.1:8470
  }
  ```

  For nginx, `auth_basic` + `auth_basic_user_file`.

- **An SSO/OAuth gateway** in front of the proxy, if you have one.
- **A private network** (Tailnet, VPN, or a firewall rule) instead of the public
  internet. If only your own devices can reach the port, that *is* the access
  control.

If you expose it publicly with no token and no proxy access control, you have
published a board any visitor can write to. That is a deliberate operator
choice, and Livewall will not stop you.

## Configuration in a deployment

Every knob is an environment variable; see [`.env.example`](../.env.example).
The backend and the CLI read them from the **process environment** (they do not
load `.env`), so `cp .env.example .env` alone does not configure a bare-metal
backend — `export` the values or put them in the unit file. A repo-root `.env`
is loaded by the MCP server and read by `docker compose` for the container.
The ones that matter most when deploying:

| Variable | Default | Note |
|---|---|---|
| `LIVEWALL_HOST` | `127.0.0.1` | keep loopback behind a proxy; `0.0.0.0` inside a container |
| `LIVEWALL_PORT` | `8470` | |
| `LIVEWALL_CONFIG_FILE` | `./backend/config.json` | point at a persistent volume so runtime config survives restarts |
| `LIVEWALL_WEB_DIR` | `./web/dist` | built client to serve at `/` |
| `LIVEWALL_DEFAULT_TTL` | `1800` | finished-board lifetime, seconds |
| `LIVEWALL_STALE_TTL` | `3600` | running-board idle lifetime, seconds |
| `LIVEWALL_MAX_BOARDS` | `12` | cap; oldest-updated evicted past it |
| `LIVEWALL_MAX_BOARDS_LIMIT` | `1000` | ceiling for the runtime `max_boards` knob — `PUT /api/config` is refused above it, so a huge value cannot disable eviction |
| `LIVEWALL_MAX_BODY_BYTES` | `1048576` | max bytes per request body; a larger body is refused with `413` |
| `LIVEWALL_MAX_SUBSCRIBERS` | `100` | max concurrent `GET /api/stream` connections; the next is refused with `503` (the client retries) |
| `LIVEWALL_KEEPALIVE` | `15` | SSE keepalive interval, seconds |
| `LIVEWALL_GRACEFUL_SHUTDOWN_TIMEOUT` | `3` | uvicorn's graceful-shutdown bound, seconds; open SSE streams are cancelled after it. Keep well under the unit's `TimeoutStopSec` |
| `LIVEWALL_TOKEN` | *(unset)* | when set, every `/api/*` endpoint requires `Authorization: Bearer <token>` (or `?token=`); `/health` and the static client stay open |
| `LIVEWALL_LOG_LEVEL` | `warning` | uvicorn log level |

Runtime changes via `PUT /api/config` (or MCP `set_config`) are persisted to
`LIVEWALL_CONFIG_FILE` before they take effect.
