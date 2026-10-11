# Contributing to Livewall

Thanks for looking. This is a small, focused project; the notes below are what
you need to make a change that lands.

## Setup

```bash
git clone https://github.com/ashuio/livewall livewall
cd livewall
make install          # client deps + a .venv with backend, mcp and playwright deps
```

`make install` builds ONE virtualenv at `.venv/` (backend, MCP and test deps)
and runs `npm install` in `web/`. `make test` uses that venv.

Common tasks:

```bash
make build            # build the client into web/dist
make backend          # run the backend alone (loopback:8470)
make dev              # backend + Vite dev server (client on :5183)
make test             # the whole suite (see below)
```

## Running each piece locally

- **Backend** — `make backend` (or `make dev` for the client too). It serves the
  built client at `/` and the API at `/api/*` on `http://127.0.0.1:8470`. Config
  is environment variables; every one has a working default (see
  [`.env.example`](.env.example)).
- **Web client** — `make dev`, or `cd web && npm run dev` for the client alone.
  Vite serves it on `http://127.0.0.1:5183` and proxies `/api` and `/health` to
  the backend; `LIVEWALL_BACKEND` overrides the proxy target.
- **MCP server** — wire `mcp/server.py` into an MCP host (Claude Code, Cursor,
  …); the snippets are in [`mcp/README.md`](mcp/README.md). To run it directly:
  `.venv/bin/python mcp/server.py`. It reads `LIVEWALL_URL` and
  `LIVEWALL_TOKEN`, and loads the repo-root `.env` if present.
- **CLI** — `pipx install ./cli` (stdlib only), or run it in place with
  `python3 cli/livewall.py`. `livewall demo` creates a sample board you can open
  in the client; [`cli/README.md`](cli/README.md) lists every command.

## Tests and the gate

Two commands are the gate. Both must pass before a change is done:

```bash
make test                    # backend, cli, mcp and web suites
cd web && npm run lint       # oxlint (only needed if the client changed)
```

`make test` needs **no running server**. `scripts/run_tests.py` picks a free
port, writes a throwaway config file, boots the backend against it, runs every
suite, tears the server down, and **exits non-zero if anything fails**. It builds
the client first, so a broken build fails the gate.

You can run a subset or a single suite:

```bash
make test-backend     # backend suites only
make test-cli
make test-mcp
make test-web
python3 backend/tests/edge_test.py   # one suite (against a dedicated backend; see backend/README.md)
```

### Testing rules

These are strict on purpose. A suite that cannot fail is worse than no suite,
because it looks like coverage.

- Every suite exits non-zero on failure. A suite that always exits `0` is a bug.
- Each browser suite seeds its own fixtures and deletes them afterwards, so it
  passes on a clean checkout with only the backend running. Because it counts
  boards, it **refuses to start against a backend that already holds boards** —
  point it at a throwaway one, never a live instance.
- The render suite asserts on rendered text, not a screenshot, and fails if a
  compacted form such as `6.8K` shows up where an exact count belongs.
- Transport unit tests run on a fake clock and a fake byte stream, so the retry
  loop, stall watchdog and visibility-resume are deterministic. No sleeps decide
  the result.

The suites are: the backend (edge cases, SSE/stream, config persistence, auth,
store concurrency, the example pusher, and vocabulary parity), the MCP contract,
the CLI, and the web tests — Node unit tests plus three browser suites (render,
transport and accessibility). The current counts are listed in the
[README](README.md#tests).

### Choosing the interpreter

`make test` resolves its Python as `LIVEWALL_TEST_PYTHON` → `./.venv/bin/python`
→ the interpreter running it, and fails loudly naming `make install` if the
chosen one lacks the dependencies. If your environment lives somewhere else, set
the override rather than relying on a fallback:

```bash
LIVEWALL_TEST_PYTHON=/path/to/python make test
```

`LIVEWALL_MCP_PYTHON` and `LIVEWALL_PW_PYTHON` override the interpreter for the
MCP and browser suites alone. `LIVEWALL_CHROME` points the browser suites at a
specific Chromium binary; without it they use the one Playwright installed.

## Screenshots

The images in the README are generated, not hand-taken. If you change a block's
rendering or add a block type, regenerate them:

```bash
LIVEWALL_URL=http://127.0.0.1:8470 .venv/bin/python scripts/make_screenshot.py
```

It seeds a fixture board covering every block type plus two decorative tabs,
writes `docs/screenshot{,-dark,-mobile,-mobile-dark}.png`, and deletes the
boards again. `make test` does not run it (it needs a browser and a live
backend), so a stale image is a review catch, not a gate failure — which is
exactly why it is scripted rather than manual.

## What a PR is held to

- **No silent failure.** A write either takes effect or returns a named refusal;
  a config write that cannot persist must not report success. If something is
  swallowed, it must be logged.
- **Validate before you mutate.** Every write path builds and validates all of
  its changes first, then applies them, so a later refusal cannot leave an
  earlier field half-applied.
- **One contract, both layers.** The HTTP API (`backend/app.py`) and the MCP
  server (`mcp/server.py`) share `backend/contract.py`. If you change the
  vocabulary, limits, or validators, change it once there; both consumers must
  stay in agreement.
- **Numbers stay exact by default.** `web/src/livewall/format.ts` is the single
  source of the rule (a number exact, a string verbatim); its unit test checks it.
- **No bare `except:` / `except Exception` in product code.** Narrow every clause
  to a named exception.
- **Tests must be able to fail.** Add a test for your change, and prove it can
  fail: revert the source, watch your test go red, restore the source. A suite
  that was green before your fix and green after proves nothing.
- **Docs track the source.** If a number, name, path, flag or field changes,
  update the doc that states it. A doc that is *almost* right is worse than none,
  because it makes the reader distrust the rest.

## Style

- Match the surrounding code. The Python is `from __future__ import annotations`,
  typed, and commented where the *why* is non-obvious.
- The client is TypeScript + React; run `cd web && npm run lint` before pushing.
- Keep commits focused and write a message that says what changed and why.

## Scope

Documentation, tests, and bug fixes are all welcome. For a behaviour change,
open an issue first so the design can be discussed. Livewall is deliberately
small, and the "no silent failure / validate first" rules above are not
negotiable. Security reports go to the process in [SECURITY.md](SECURITY.md), not
a public issue.

## Community and license

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

Livewall is released under the [Apache License 2.0](LICENSE). By contributing,
you agree that your contributions are licensed under the same terms.
