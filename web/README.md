# web — the Livewall client

The **React client** for Livewall. The agent POSTs a board spec over MCP; this
renders it live over SSE.

React 19, Vite. Bundle: **~274 kB / ~83 kB gzipped**.

> **Why React.** It renders to real DOM, so board text is selectable and devtools
> see a full element tree instead of one `<canvas>`, and the components are plain
> functions with no bespoke runtime.

## Layout

```
src/livewall/
  transport.ts          SSE reader (fetch-based, NOT EventSource)
  useLivewall.ts        holds the latest boards (each with its blocks)
  components.tsx        the 14 components + the renderBlock dispatcher
  components.css        their styling
  freshness.ts          snapshot clock -> freshness / STALE / TTL (pure)
  viewstate.ts          ?board= / ?kiosk= address-bar state (pure)
  auth.ts               ?token= -> localStorage -> Authorization header
  usePullToRefresh.ts   touch pull-to-refresh
  theme.ts              system|light|dark, persisted
  format.ts             exact-by-default number formatting
  types.ts              stream payload shapes
src/App.tsx             app bar, tab bar, board area
src/tokens.css          design tokens (CSS custom properties)
```

> Class names are prefixed `lw-` (`lw-panel`, `lw-gauge`, …) — the component
> prefix, matching the product initialism. `scripts/verify_render.py` asserts on
> these names, so rename them together.

## Transport notes (do not "simplify" these)

`EventSource` is deliberately not used. It cannot detect a **silently dead**
stream — one that stays open but stops delivering — which is exactly what mobile
browsers do to a backgrounded tab. The fetch-based reader implements the three
things that keep a board honest:

- **retry loop** (2s) on any drop or error
- **stall watchdog** — silence is checked every 10s and reconnects once it
  exceeds 45s (server keepalive is 15s, so 45s is three missed keepalives)
- **visibility resume** — reconnect when the tab becomes visible again

## Numbers are exact by default

`6836`, not `6.8K`. Counts must be readable to the unit. Compaction is an
explicit per-value choice by the agent, expressible three ways:

| The agent sends | Renders | Use for |
|---|---|---|
| a **number** | exact — `6836` | counts |
| a **string** | verbatim — `1.5M` | `stat`/`stats` only, pre-formatted |
| `"fmt": "compact"` | label shortened, ratio intact | tokens/bytes on a `progress`/`gauge`/`bar` |

The third exists because those blocks need a REAL number for the ratio — you
cannot pass `1.5M` there.

## Theme

`system` (default) → `light` → `dark`, persisted in `localStorage` under
`daylight-theme`. An inline script in `index.html` applies it before first paint
(no flash). Colours live in CSS custom properties, so dark mode needs no JS.

## Develop

```bash
npm install
npm run dev            # proxies /api to the backend (LIVEWALL_BACKEND to override)
npm run build          # -> dist/, which the backend serves at /
npm run lint
```

The app is **same-origin**: it fetches `/api/*` relative, so it works on the
direct port and behind a reverse proxy with no CORS or base-href concerns.

## Verify

```bash
npm test               # transport + format unit tests (node --test, fake clock)
pip install playwright && playwright install chromium
LIVEWALL_URL=http://127.0.0.1:8470 python scripts/verify_render.py
LIVEWALL_URL=http://127.0.0.1:8470 python scripts/verify_transport.py
LIVEWALL_URL=http://127.0.0.1:8470 python scripts/verify_a11y.py
```

`npm test` runs `test/*.test.ts` against a fake clock and a controllable byte
stream, so the retry loop, the stall watchdog, visibility-resume and the abort
accounting are deterministic (no sleeps). `verify_transport.py` covers the
browser-only paths: multi-tab, pull-to-refresh, theme, and connection
accounting against `/health`'s subscriber count.

`verify_render.py` asserts the client renders all 17 block types with exact
numbers, in both themes. It **seeds its own fixture board** (and deletes it
afterwards), so it passes on a clean checkout with only the backend running.
Because this renders to the DOM it asserts on **text** rather than eyeballing a
screenshot — 59 expected strings, plus 15 required component classes, and a
check that no compacted forms (`6.8K`) appear. `LIVEWALL_CHROME` points it
at a specific browser binary; omit it to use Playwright's bundled Chromium.

`verify_a11y.py` drives the same kind of fixture board through the accessibility
contract: roles and names on every block, keyboard operation of the charts and
the tab strip, focus handling, contrast, reduced motion and reflow at 320px. It
also fails if any element hides text it cannot show — a table cell that clips
instead of wrapping, for instance.
