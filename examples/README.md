# Examples

Runnable, self-contained examples. Each one programs a board against a **local
Livewall backend** and cleans up after itself, so you can copy the directory and
run any script without leaving state behind.

Start a backend first (see [Getting started](../docs/getting-started.md)):

```bash
docker compose up --build        # then http://127.0.0.1:8470
# ...or bare metal: make install && make build && make backend
```

All examples read `LIVEWALL_URL` (default `http://127.0.0.1:8470`) and
`LIVEWALL_TOKEN` (when the backend is token-protected).

| Example | What it is | How to run |
|---|---|---|
| [`program_a_board.py`](program_a_board.py) | Python, **standard library only** — creates a board, updates a block, appends to its log, prints the URL, deletes it | `python3 examples/program_a_board.py` |
| [`program_a_board.sh`](program_a_board.sh) | The same flow via `curl` (plus the CLI, if installed) | `./examples/program_a_board.sh` |
| [`ci_progress.sh`](ci_progress.sh) | A CI-style build board: updates progress + log per step, then marks it `done` | `./examples/ci_progress.sh` |
| [`pusher.py`](pusher.py) | **The poller pattern** — polls HTTP / command / file sources on an interval and pushes a board, so it updates with no agent attached. Takes an optional JSON config ([`pusher.json`](pusher.json)); runs a built-in demo without one | `python3 examples/pusher.py` |
| [`composite_sources.json`](composite_sources.json) + [`composite_probe.py`](composite_probe.py) | A pusher config showing the richer source shapes: one **composite** source drives a whole `stats` tile set, a `bar` and a `kv` in a single call; a **`sources`** map drives two fields of one callout; the tab indicator is live. `composite_probe.py` is the one-JSON-object probe they all read | `python3 examples/pusher.py examples/composite_sources.json` (from the repo root) |

The two shell scripts **delete the board they created when they exit**, via a
`trap ... EXIT`, so re-running is safe and nothing accumulates.
`program_a_board.py` deletes the board on its success path; if a request fails
mid-run it exits without cleaning up, so remove the board yourself with
`livewall rm <id>`. To leave the board up and watch it in the web client, pass
`--keep` (Python) or set `LIVEWALL_KEEP=1` (shell):

```bash
python3 examples/program_a_board.py --keep
LIVEWALL_KEEP=1 ./examples/ci_progress.sh
```

`pusher.py` is the exception: it is a **long-running process**, not a one-shot, so
it leaves its board up on purpose — that is the point. Ctrl-C stops the pusher and
the board then goes stale: it stays `running`, so it is pruned after the global
`stale_ttl` (3600s) of no updates, not after its own `ttl`.

The board id is overridable — `--board <id>` for the Python example,
`LIVEWALL_BOARD=<id>` for the shell ones.

`ci_progress.sh` uses a run-specific default id (`ci-<user>-<pid>`), so parallel
runs do not collide. All three create with `?replace=true`, so they overwrite
only their own board id rather than failing with a 409.

> These exist for an adopter to read and run. `pusher.py` is also exercised by
> the suite (`backend/tests/pusher_test.py` pins its value-resolution logic);
> the rest of the API suite lives in `backend/tests/`, `cli/test_cli.py`,
> `mcp/test_live.py` and `web/`.
