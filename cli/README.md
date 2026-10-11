# livewall CLI

A command-line client for a Livewall backend — program a board from a shell,
a cron job, or a CI step, with no MCP host required. Standard library only, so
installation resolves no dependencies.

## Install

```bash
pipx install ./cli          # from a clone
# or run it in place, no install:
python3 cli/livewall.py --help
```

## Configure

Two environment variables (flags override them):

| Variable | Meaning | Default |
|---|---|---|
| `LIVEWALL_URL` | backend base URL | `http://127.0.0.1:8470` |
| `LIVEWALL_TOKEN` | bearer token, if the backend sets `LIVEWALL_TOKEN` | *(none)* |

## Commands

```
livewall health                      # GET /health
livewall ls                          # list boards
livewall get <id> [--json]           # show one board (text view, or raw JSON)
livewall set <id> [--file f.json] [--title T] [--subtitle S] [--status ST]
                  [--ttl N] [--blocks-file f.json] [--tab-file f.json] [--replace]
livewall update <id> --block B --set '{"value":40}' [--append "a line"]
livewall reset <id>                  # clear a board's blocks
livewall rm <id>                     # delete a board
livewall config [--set k=v ...]      # show or set global defaults
livewall watch <id> [--interval 2] [--once]
livewall demo [id]                   # create a sample board
```

## Examples

```bash
# A board that a colleague can watch live in the web client:
# (--blocks-file is a JSON array of block objects; without it the board
#  has no blocks and `update --block p` has nothing to address)
livewall set build --title "Nightly build" --status running \
  --blocks-file blocks.json
livewall update build --block p --set '{"value": 40}'

# From a full spec on disk (the same JSON the HTTP API and MCP take):
livewall set release --file board.json --replace

# Follow it from the terminal until it expires:
livewall watch build

# See it working in one command:
livewall demo
```

## Notes

- A board id is required and specific; `set` on an id that already exists
  returns `409` unless you pass `--replace`, so two callers cannot silently
  overwrite each other's tab.
- If the backend requires a token and none is supplied, the CLI exits non-zero
  with `unauthorized (401)` rather than a bare stack trace.
- `watch` exits cleanly (code 0) when the board disappears, so it is safe in a
  script.
