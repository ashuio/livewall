# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities **privately** to the maintainer at
**livewall@ashu.io**. Please do not open a public issue for a security report.

You can also report through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**.

Include what you can: affected version/commit, a description, a reproduction
(requests, a board spec, or a script), and the impact you believe it has. If you
are not sure whether something is in scope, report it and let the maintainer
decide.

There is no bug-bounty programme and no formal SLA. Reports are handled on a
best-effort basis; the maintainer will acknowledge a report and tell you whether
it is in scope.

## Supported versions

Livewall's current version, **`0.0.1`**, is the supported version. Fixes land
on the default branch; there are no backports.

## Authentication: optional bearer token

**Livewall ships open by default and gains a bearer token when you set one.**

- **Unset `LIVEWALL_TOKEN`** (the default): the API is open. The backend binds
  loopback, and the intended deployment is "bind it, then put your own access
  control in front" (see [`docs/deploying.md`](docs/deploying.md)).
- **Set `LIVEWALL_TOKEN`**: every `/api/*` endpoint requires
  `Authorization: Bearer <token>` (or `?token=<token>`) and returns `401`
  otherwise. `/health` and the static client stay open, so liveness probes and
  the browser bundle need no credential — but an unauthenticated `/health` on a
  token-protected backend returns **liveness only** (`{"ok": true}`), with no
  board ids or config.

The comparison is constant-time (`hmac.compare_digest`). The backend and the CLI
read the token from the **process environment** (a bare-metal backend with the
token only in a repo-root `.env` stays open); the MCP server also loads that
`.env`, and `docker compose` substitutes it into the container. The browser
client gets the token from `?token=<token>` once and stores it — it never reads
an environment variable.

Therefore the following are **not vulnerabilities**:

- "There is no authentication / no login" *when the operator chose to leave
  `LIVEWALL_TOKEN` unset* — that is the documented default.
- "An unauthenticated request can create, read, update or delete a board" when
  auth is off — the documented design of that mode.
- "Anyone who can reach the port can read the boards" when auth is off.
- "There is no rate limiting, so a client with network access can spam writes."
- A deployment that is reachable from the internet because the operator bound it
  publicly or published it through a proxy without access control.

A defect **in** the auth gate — a request that bypasses it while
`LIVEWALL_TOKEN` is set, a comparison that leaks the token, or a gated endpoint
that answers without a credential — **is** in scope.

## What a remote client can drive (resource bounds)

"No auth" is only safe to ship because every resource a remote client can drive
is bounded. Each ceiling below is a deliberate choice, overridable by the
operator where noted:

| Resource | Bound | On breach |
|---|---|---|
| Request body | `LIVEWALL_MAX_BODY_BYTES` (default 1 MiB) | `413`; an oversize `Content-Length` is refused up front and a chunked body is capped while streaming |
| Concurrent SSE subscribers | `LIVEWALL_MAX_SUBSCRIBERS` (default 100) | `503`; the client retries, so it is throttled, not wedged |
| A single stored string | `MAX_TEXT_LEN` (8192 chars) — every block text field, list/table/kv element, stat label and value, log line, and tab label/indicator text | `400`, naming the field |
| A numeric value | `2**53` (the largest integer a JS number represents exactly); a larger integer, or a non-finite value, is refused | `400`, naming the field |
| Board id / block id | 64 chars each | `400` |
| Blocks per board | 60 | `400` |
| Log lines stored / rendered | 400 / 80 | oldest dropped |
| Title / subtitle | 200 / 500 chars | `400` |
| Boards | `max_boards`, ceiling `LIVEWALL_MAX_BOARDS_LIMIT` (default 1000) | oldest-updated evicted |
| Per-subscriber queue | one pending snapshot, newest-wins | the stale frame is dropped |

List, row and value *counts* have no separate cap: they are bounded by the
request-body cap, because a board is written in a single request.

What is **not** bounded is request **volume**. A client with network access can
send many requests that are each within these bounds; there is no rate limiting
(see above). That is a documented residual, not a defect: the per-request bounds
keep any *single* request from exhausting the process, and rate limiting belongs
to the access-control layer you are expected to put in front.

## Output encoding (XSS)

Board text is rendered by React as text, not HTML: every Livewall component
(`web/src/livewall/components.tsx`) passes strings as children (`{s(...)}`), and
no component uses `dangerouslySetInnerHTML`. A text field containing `<script>`
renders as the literal characters. A path that turns board text into executable
markup would be in scope (see the renderer bullet below).

## In scope

Reports are welcome for defects in the parts that process untrusted input:

- **The HTTP API's input validation** (`backend/app.py`, `backend/contract.py`):
  a request that bypasses the shared contract, crashes or wedges the server,
  corrupts state, causes a 500 where a refusal is expected, or otherwise escapes
  the documented validation rules. The intended invariant is that every write is
  validated *before* it mutates, and that a refusal is named rather than silent.
- **The auth gate** (`backend/auth.py`, the middleware in `backend/app.py`):
  a request that reaches a `/api/*` handler while `LIVEWALL_TOKEN` is set without
  presenting the token, a comparison that is not constant-time, or a path that
  should be gated but is not (or vice versa).
- **The block renderer** (`web/src/livewall/components.tsx`): a block that is
  legal under the contract but crashes the renderer, blanks the board, or renders
  attacker-controlled text as executable HTML rather than text. The renderer is
  intended to be total — a malformed block degrades to a visible value.
- **The transport** (`web/src/livewall/transport.ts`, `backend/app.py`'s SSE
  stream): a stream that leaks server resources, a client that can make the
  server grow without bound, or a malformed frame that silently wedges the
  client.
- **Dependencies**: a known-vulnerable dependency used in a way that is
  reachable from Livewall's own code. Report it with the path that reaches it.
  At this release `npm audit` is clean: the client's only dependencies are
  `react` and `react-dom`, and Livewall renders no markdown or raw HTML, so
  there is no sanitizer path to reach.

## Out of scope

- Missing authentication (when `LIVEWALL_TOKEN` is unset), missing TLS, missing
  rate limiting, or any other consequence of exposing the service without a
  front door (see above).
- The configuration of your reverse proxy, firewall, or network.
- Denial of service from request **volume**: a client that already has network
  access and is allowed to write boards sending many requests. Each request is
  bounded (see "What a remote client can drive"); there is no rate limiting, by
  design. Livewall is a LAN/Tailnet tool, and the board cap and the other
  ceilings are resource limits, not an access-control boundary.
- A *single* request that exceeds one of those bounds is not out of scope: the
  contract refuses it (`413` / `400` / `503`) and does not mutate state.
- Social engineering, physical access, and issues in third-party services.

## Silent failures

A failure nobody can see is the kind that hurts, so the project works to make
them visible: writes validate before they mutate, a config write that cannot
persist returns an error instead of a false success, and the client surfaces a
malformed frame rather than blanking the board.

If you find a case that still fails quietly, report it. It does not have to be
exploitable to be worth knowing about.
