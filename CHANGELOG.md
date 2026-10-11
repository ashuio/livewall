# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.1] - 2026-10-11

The first release. Livewall is an agent-programmable live status surface: an
agent sends a JSON *board spec* over MCP (or HTTP, or the CLI), and a browser
renders it live over SSE. Many agents can each own a named board; the client
shows them as tabs.

### Added

- **Backend** (`backend/`): a FastAPI service owning boards, TTL/pruning, config
  persistence and the SSE snapshot broadcast. No database; boards are in-process
  by design.
- **Board renderer**: turns a board spec into the components the client draws.
  Deliberately total, so a hostile or malformed field degrades to a visible
  value instead of raising.
- **Shared contract** (`backend/contract.py`): one source of truth for the block
  vocabulary, limits and validators, imported by **both** the HTTP API and the
  MCP server, so the two cannot disagree about what is legal.
- **React client** (`web/`): renders the boards live. `fetch`-based SSE
  transport with a retry loop, a stall watchdog and visibility-resume, plus an
  epoch guard so a self-initiated abort cannot tear down the fresh connection.
- **MCP server** (`mcp/`): 11 tools — `livewall_ping`, `describe_blocks`,
  `list_boards`, `get_board`, `get_config`, `set_board`, `update_board`,
  `update_block`, `reset_board`, `delete_board`, `set_config`.
- **Command-line client** (`cli/`): a stdlib-only `livewall` command that drives
  the same API as the MCP server, so a shell script, cron job or CI step can
  program a board without an MCP host. Commands: `ls`, `get`, `set`, `update`,
  `reset`, `rm`, `config`, `watch`, `demo`, `health`. Install with
  `pipx install ./cli`; it reads `LIVEWALL_URL` and `LIVEWALL_TOKEN`. The client
  renders a board as plain text in the terminal, and `watch` exits cleanly when
  the board disappears.
- **17 block types** across **14 custom components**: `heading`, `text`, `stat`,
  `stats`, `progress`, `gauge`, `bar`, `sparkline`/`chart`, `line`, `table`,
  `log`, `kv`, `list`, `callout`, `code`, `divider`.
- **The `line` block**: a multi-series line chart with axes — one polyline per
  series drawn as hand-rolled SVG (no chart library). `series` carries
  `{name?, values, color?}` entries (all the same length, each at least 2
  points), a top-level `values` is sugar for one unnamed series, `xLabels`
  labels the x-axis, `yMin`/`yMax` set the y-range (auto-scaled when absent),
  and the legend shows automatically when there is more than one series. A
  degenerate series (fewer than 2 points) degrades to a visible `(no data)`
  state. Its `color` accepts the accent tokens, including `ok`/`warn`/`error`,
  which were previously undefined and now resolve for the other
  colour-taking blocks too.
- **Interactive charts.** The `line` chart and the `sparkline`/`chart` block
  read out their values on hover: a crosshair snaps to the nearest x, marks the
  point on each series and shows a small readout of the values there. A chart
  with hidden numbers (`line`, `sparkline`) is now readable without eyeballing
  the axis; `bar` already prints its values, so it is unchanged. Both charts are
  measured to their container, so the line chart fills its card instead of
  letterboxing to a fixed strip, and neither is distorted at any width.
- **Optional bearer-token auth (`LIVEWALL_TOKEN`).** Unset (the default), the
  API is open. Set, every `/api/*` endpoint requires
  `Authorization: Bearer <token>` (or `?token=<token>`) and returns `401`
  otherwise; `/health` and the static client stay open so probes and the browser
  bundle need no credential. The comparison is constant-time. The same variable
  configures the backend, the CLI and the MCP server. The web client learns the
  token from `?token=` once, persists it to `localStorage`, strips it from the
  address bar, and sends it as a header on every fetch.
- **Freshness chip and update pulse.** The SSE snapshot carries the
  server clock (`now`); the client uses it to show how fresh the selected board
  is — a freshness chip that turns from `live` to `stale` when the stream stops
  advancing, and a pulse when a board updates. The chip is the *only* staleness
  indicator: nothing is drawn over the board, so a board that is already a solid
  colour (an error board, a status wall) is never tinted or covered. Ages advance
  on the client clock between snapshots and are never compared to a server
  timestamp, so clock skew cannot fake staleness.
- **Deep-linkable boards (`?board=<id>`).** The client reads a board id from the
  URL at load, selects and pins it, and writes the selection back to the address
  bar when a tab is clicked, so a board view is shareable. An unknown id falls
  back to following the newest board and shows a quiet notice. `?token=` and
  `?board=` compose: one writer owns the address bar, so neither clobbers the
  other.
- **Error boards are unmissable.** A board with `status: "error"` carries a red,
  alarmed tab (regardless of its agent-chosen accent, and keeping its bar/pct/text
  indicator) and a full-width `ERROR` banner in the board view. It is never
  auto-hidden or de-selected when it is the newest.
- **Kiosk mode (`?kiosk=1`).** Hides the app-bar actions and the tab bar, shows
  the one selected board enlarged, and keeps the freshness chip (which reports
  staleness). Exit with **Esc** or the exit button; `?kiosk=0` is normal mode.
  Composes with `?board=`.
- **Responsive layout (phone / tablet / desktop).** Real breakpoints — phone
  `<=600px`, tablet `601–1024px`, desktop `>=1025px`. On the phone band the app
  bar and its action group wrap instead of overflowing horizontally, and app-bar
  buttons and tabs become 42px touch targets; the freshness chip and error alarm
  stay visible, and kiosk still works at every width.
- **Board overview for many boards.** A grid button in the app bar (or the `g`
  key) opens an overlay listing every board with its title, status, indicator,
  age and error alarm; selecting one selects it and writes `?board=` through the
  single URL writer, and **Esc** closes it. It is hidden when there is one board
  and in kiosk (`?kiosk=1`).
- **The wall is fully keyboard-operable.** Tab / Shift+Tab move through the
  app-bar controls and the tab strip; the arrow keys move between tabs, Home /
  End jump to the first / last, and Enter or Space selects the focused tab. `g`
  toggles the board overview, whose arrow keys move between cards, Enter selects,
  Esc closes, and focus is trapped in the overlay and returned to the trigger on
  close. Esc exits kiosk.
- **Motion honours `prefers-reduced-motion: reduce`.** The update pulse, the
  breathing live dot, the blinking error dot and the pull spinner are all
  disabled under the reduced-motion preference.
- **Accessible shell chrome.** The app-bar controls, the tab strip and the
  overlays expose correct roles, names and labels to assistive technology.
- **A ticking TTL countdown for terminal boards.** A `done` / `error` board shows
  its remaining lifetime (for example `expires in 29m`) and ticks down on the
  client clock between snapshots.
- **Clone-and-run surface**: `Dockerfile`, `docker-compose.yml`, `Makefile`,
  `.env.example`. A fresh clone with no `.env` runs on defaults.
- **Documentation**: `README.md` plus `docs/` (architecture, API, blocks, MCP,
  deployment, protocol, getting started, cookbook, pushers, troubleshooting),
  `examples/` (runnable scripts), `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`.
- **Pushers**: `examples/pusher.py` and [`docs/pushers.md`](docs/pushers.md) — the
  pattern for a board that updates **itself**. A pusher is a small program you run
  next to the data: it polls HTTP endpoints, commands or files on an interval and
  pushes a board, so the wall stays current with no agent and no browser attached.
  Livewall itself still fetches nothing, which keeps the server's attack surface
  where it was.
- **An Agent Skill**: [`skills/livewall/SKILL.md`](skills/livewall/SKILL.md) — an
  [Agent Skill](https://agentskills.io) that teaches an agent *when* to program a
  board, the shape of the workflow, and the traps that are easy to hit. It
  deliberately carries no block vocabulary (that stays with `describe_blocks`), so
  it cannot drift out of date.

### Changed

- **The board spec is now the wire format.** The SSE snapshot carries each
  board's own `blocks`, exactly as stored, and the client renders them directly.
  The translation layer that compiled a board into a separate UI protocol has
  been removed, together with its dependency; the number rule (exact by default)
  now lives only in the client (`web/src/livewall/format.ts`).
- **Board-id collisions are refused, not silent.** `POST /api/boards/{id}` on an
  id that already exists returns **409** and leaves the live board intact,
  instead of silently replacing it. Pass `?replace=true` to overwrite on purpose
  (a non-boolean value is refused with 400). `PATCH`, `/update`, `/reset` and
  `DELETE` address a board the caller already knows and are unaffected. The MCP
  `set_board` tool gains a `replace=False` parameter and its description requires
  a specific, unique board id — there is no default, and reusing a generic id is
  exactly what collides across sessions.

### Fixed

- **`/health` no longer discloses board ids and config to unauthenticated
  callers.** `/health` is the one endpoint the auth gate leaves open (a liveness
  probe cannot carry a credential). It used to return `boards`, `ids`,
  `subscribers` and `config` to everyone, so with `LIVEWALL_TOKEN` set an
  unauthenticated caller could still enumerate every board id. On a
  token-protected backend an unauthenticated request now gets `{"ok": true}`
  only; an authenticated caller — and every caller when auth is off — still gets
  the full payload.
- **`docker-compose.yml` passes `LIVEWALL_TOKEN` through to the container.**
  Compose reads `.env` only to substitute `${...}` in the compose file and does
  not inject it into the container on its own, so the token was silently dropped
  and a `docker compose up` API stayed open even with `LIVEWALL_TOKEN` set in
  `.env`. The service `environment:` now carries
  `LIVEWALL_TOKEN: ${LIVEWALL_TOKEN:-}` (empty default preserves the open
  quickstart).
- **Token-wiring docs corrected.** The docs said one `.env` value configured the
  backend, CLI, MCP and browser. In fact the backend and the CLI read only the
  **process environment** (a bare-metal backend with the token only in `.env`
  stays open), the MCP server reads the repo-root `.env`, `docker compose`
  substitutes it from `.env` into the container, and the browser client takes the
  token from `?token=`. Each surface now says where the token actually comes
  from.
- **Huge integers no longer 500 or poison the store.** JSON integers are
  arbitrary precision, but the emitter, the summary and the config arithmetic
  convert them to a float. `10**400` (a legal JSON number) raised `OverflowError`
  — a `500` where the contract promises a named refusal — and a huge
  `stat`/`stats` value was *stored*, after which every snapshot and broadcast
  `500`d. Every numeric value is now required to be a finite double bounded to
  `2**53` (the largest integer a JS number represents exactly); a larger integer
  is refused with `400`, and the read/emit paths stay total for a board already
  in memory. Applies to block values, `ttl`, and `default_ttl`/`stale_ttl`.

### Security

- **The container runs as a non-root user.** The shipped image used to run its
  backend as root, so any future code-execution bug in the backend would land
  on root inside the container. The image now creates a dedicated user
  (uid 10001), owns `/data` (the config-volume mount point) with it, and runs
  as it. A named volume first used by the new image inherits the ownership;
  a volume created by an older image needs a one-time `chown` — see
  [`docs/deploying.md`](docs/deploying.md).
- **Input validation at the API layer.** Every board write is validated against
  the shared contract; refusals name the offending field and the valid set.
- **Config writes are persist-then-commit.** A config change that cannot be
  written to disk returns an error and leaves the running config unchanged. It
  never reports a save that did not happen.
- **No bare `except:` or `except Exception`** remains in product code; every
  handler is narrowed to a named exception.
- **Open by default; lock it down with a token.** Livewall binds loopback by
  default and is meant to run behind a reverse proxy with the operator's own
  access control. Set `LIVEWALL_TOKEN` to require a bearer token on every
  `/api/*` endpoint. See [`SECURITY.md`](SECURITY.md).

### Notes

- Boards are **ephemeral by design**: one-off surfaces that expire on a TTL.
  That is a design decision, not a missing persistence feature.
- Numbers render **exact by default**; compaction is an explicit per-value choice
  the agent makes.
- CI runs on every push to `main` and every pull request: the full test suite,
  the client lint and a Docker build (`.github/workflows/ci.yml`).
