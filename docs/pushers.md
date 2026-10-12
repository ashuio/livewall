# Pushers — boards that update themselves

Livewall is a **renderer**: it never fetches anything. Something pushes a board,
it draws it. That is a deliberate design choice — the server makes no outbound
requests, runs no commands, and reads no files, so its attack surface stays tiny.

A **pusher** is the other half. It is a small program *you* run, next to the data,
that polls whatever you point it at and pushes a board on an interval. Start it
and walk away: the wall keeps updating with no agent and no browser attached.

```
┌────────────────────┐   poll every Ns   ┌──────────────────┐   push   ┌─────────────┐
│  pusher (yours)    │ ────────────────► │  your sources    │ ───────► │  Livewall   │
│  examples/pusher.py│                   │  http / cmd / file│          │  renders it │
└────────────────────┘                   └──────────────────┘          └─────────────┘
```

**Why a separate process, and not a feature in the server?** Because "the server
fetches a URL you gave it" is SSRF and "the server runs a command you gave it" is
remote code execution. Keeping the fetch and the exec in *your* process, with
*your* permissions, on *your* machine, means Livewall never needs a threat model
for either. The pusher is ordinary code you can read; the server stays inert.

If you want an agent to push directly instead, that is the MCP server
([MCP](mcp.md)) or the CLI. Pushers are for the unattended case.

## Quickstart

```bash
# a backend, and the bundled example pusher
make install && make build && make backend &     # or: docker compose up --build
python3 examples/pusher.py                       # built-in demo, Ctrl-C to stop
```

Open <http://127.0.0.1:8470> and watch it change. The demo polls three different
kinds of source — a shell command, a file, and an HTTP endpoint — and pushes every
five seconds. Kill the pusher and the board stops changing; that is the whole
demonstration.

With your own config:

```bash
python3 examples/pusher.py examples/pusher.json
python3 examples/pusher.py examples/pusher.json --once    # one sample, then exit
```

`LIVEWALL_URL` (default `http://127.0.0.1:8470`) and `LIVEWALL_TOKEN` are read from
the environment, or overridden with `--url` / `--token`.

## Config

A pusher config is a board, where blocks may carry a `source`:

```jsonc
{
  "board": "host-health",          // the board id
  "title": "Host health",
  "subtitle": "optional",
  "interval": 10,                  // seconds between samples
  "ttl": 300,                      // terminal-prune TTL; while the board is
                                   // running, the global stale_ttl (3600s) applies
  "tab": { "label": "host", "accent": "mint" },

  "blocks": [
    { "type": "progress", "id": "disk", "label": "Disk (/)", "max": 100, "unit": "%",
      "source": { "kind": "command", "argv": ["df", "-h", "/"],
                  "extract": { "regex": "([0-9]+)%" } } },

    { "type": "gauge", "id": "load", "label": "Load 1m", "max": 8.0,
      "source": { "kind": "file", "path": "/proc/loadavg",
                  "extract": { "regex": "^([0-9.]+)" } } },

    { "type": "stat", "id": "health", "label": "Backend /health",
      "source": { "kind": "http", "url": "http://127.0.0.1:8470/health",
                  "extract": { "path": "$.ok" } } },

    { "type": "log", "id": "log", "title": "poller.log", "lines": [] }
  ]
}
```

Blocks **without** a `source` are pushed exactly as written, so static labels and
live values mix freely.

### `source.kind`

| Kind | Fields | Notes |
|---|---|---|
| `command` | `argv` (a list) | Run **directly** — never through a shell, so there is no injection surface. A non-zero exit is an error. |
| `http` | `url` | `GET`, 5 s timeout, 64 KiB read cap. `Authorization: Bearer $LIVEWALL_TOKEN` is sent when a token is set. |
| `file` | `path` | Read as UTF-8 (invalid bytes replaced). |

### `source.extract`

One of:

| Form | Meaning |
|---|---|
| `{"path": "$.a.b"}` | A dotted path into a JSON body. `$` is the root; a numeric segment indexes a list. |
| `{"regex": "..."}` | The first capture group (or the whole match) of the text, matched with `re.MULTILINE`. |

An extracted **string longer than 8192 characters** is a source error (the
backend refuses any scalar longer than that), not a failed push: it is logged by
name and the block keeps its last good value.

### Where the value lands

By default a source writes the block's own field for its type:

| Block type | Default field | Shape |
|---|---|---|
| `progress`, `gauge`, `stat` | `value` | scalar |
| `text`, `callout`, `heading`, `code` | `text` | scalar |
| `stats`, `kv`, `list` | `items` | **list** |
| `bar` | `data` | **list** |
| `table` | `rows` | **list** |
| `sparkline`, `chart` | `values` | **list** |
| `line` | `series` | **list** |
| `log` | `lines` | **list** |

Override the field with `"field"`. **Every block type with a field is
source-drivable** — only `divider` has none.

A source supplies a **value** into a field the config already declared; it can
never add a field or a block type. That split is what keeps a compromised
endpoint from reshaping the board.

**Scalars** (`value`/`text`) take a string or number. `progress` and `gauge` need
a **number** — a non-numeric sample (a string that will not parse, a JSON bool,
anything nested) is an error, not a silent zero or a `"true"` string the backend
would 400 the whole board over. A JSON `true`/`false` on other types is coerced
to the strings `"true"`/`"false"`, because the block vocabulary takes scalars and
a boolean is not one. A number the backend cannot store exactly (beyond ±2⁵³) or
that is not finite is an error too.

**Composite fields** (`items`/`data`/`rows`/`values`/`series`/`lines`/`xLabels`)
take a
whole **JSON list**. The pusher checks the entries against the same shapes the
backend's validator enforces — objects for `stats`, `[key, value]` pairs for
`kv`/`bar`, cell lists for `table` rows, numbers for the charts — so a null, a
nested list, a bool or an overlong string inside an entry is a named source
error, never a 400 that kills the whole board. This is how a `stats` block gets
a live set of tiles from one endpoint:

```jsonc
{ "type": "stats", "id": "tiles", "items": [],
  "source": { "kind": "http", "url": "http://127.0.0.1:9000/stats",
              "extract": { "path": "$.tiles" } } }
```

A scalar into a composite field (or a list into a scalar field) is a named
source error, never a value smuggled into a field the backend would 400 the whole
board over. A composite list is capped at **200 entries** and **256 KiB
serialised** for the same reason — one runaway array must fail its own source,
not the push.

There is a runnable config that shows all of this:
[`examples/composite_sources.json`](../examples/composite_sources.json) with its
probe [`examples/composite_probe.py`](../examples/composite_probe.py) — one
source fills a `stats`, a `bar` and a `kv`, a `sources` map drives two fields of
a callout, and the tab indicator is live.

### Driving several fields of one block — `sources`

A block's `source` binds one field. To drive more than one — a callout's `text`
*and* its `tone`, say — use `sources`, a field -> source map:

```jsonc
{ "type": "callout", "id": "status", "text": "…", "tone": "info",
  "sources": {
    "text": { "kind": "command", "argv": ["…"], "extract": { "path": "$.line" } },
    "tone": { "kind": "command", "argv": ["…"], "extract": { "path": "$.tone" } } } }
```

Both forms may be present (`sources` wins on a field both name). Every bound
field must be one the block's type actually allows — `sources` naming anything
else would push an invented field the backend refuses, 400ing the whole board,
so it is a named source error instead. Each field keeps its **own** last good
value, so one dead source blanks only its own field.

### A live tab indicator

A board's `tab.indicator` can carry a `source` of its own, so the tab's mini-bar
tracks a real ratio instead of a hardcoded number:

```jsonc
"tab": { "label": "h", "accent": "violet",
         "indicator": { "kind": "bar", "value": 0, "max": 100,
                        "source": { "kind": "command", "argv": ["…"],
                                    "extract": { "path": "$.indicator" } } } }
```

The resolved value must be numeric (a bar/pct needs a ratio); a dead or
non-numeric source leaves the placeholder value and names the error in the log —
and if there is no numeric placeholder to keep, the indicator is dropped rather
than pushed in a shape the backend would refuse. **Note:** the client
only draws the tab bar when a board has **more than one** board to switch
between, so on a single-board wall the indicator is not visible — put anything
you need seen in a block.

## When a source fails

A failing source is **visible, not silent**:

- The block keeps its **last good value** (or is omitted on the very first sample —
  never written as a blank or a zero).
- The error is appended to the config's `log` block, named:
  `bad: FileNotFoundError: [Errno 2] No such file or directory: '/nope/missing'`.
- The board is marked `error` only if **every** source failed; a partial failure
  leaves it `running` with the error line visible.
- A bad source never kills the loop — the next tick tries again.

That matters more than it sounds: a board showing a stale number that *looks*
live is worse than a board that says the source is broken.

## Running it as a service

A pusher is a long-lived process; give it a unit so it survives a reboot:

```ini
# /etc/systemd/system/livewall-pusher.service
[Unit]
Description=Livewall pusher — host-health board
After=network-online.target livewall.service

[Service]
ExecStart=/usr/bin/python3 /opt/livewall/examples/pusher.py /etc/livewall/pusher.json
Environment=LIVEWALL_URL=http://127.0.0.1:8470
Restart=always
RestartSec=5
User=livewall

[Install]
WantedBy=multi-user.target
```

Run it as an **unprivileged user** that can read only what its sources need. That
is the point of the split: the pusher's permissions are the ceiling on what a
source can reach, and Livewall's are irrelevant.

## Two API details worth knowing

Both are easy to get wrong when you write your own pusher.

1. **`replace` is a query parameter**, not a body field:
   `POST /api/boards/<id>?replace=true`. Putting `"replace": true` in the JSON body
   is *silently ignored* — the first push succeeds and every one after it returns
   **409**.
2. **`PATCH` cannot write `blocks`.** It updates board-level fields only
   (`title`, `subtitle`, `status`, `tab`, `ttl`), and `/update` mutates a single
   block by id. A **full-board refresh is only `POST ?replace=true`**.

The symptom of getting either wrong is the same and looks like success: the
board's `version` counter advances while the rendered content stays frozen at the
first push.

## Writing your own

The bundled `examples/pusher.py` is under 300 lines of standard library and is meant to
be copied. If you would rather write it in another language, it is just:

1. Poll your source on an interval.
2. Build the board JSON.
3. `POST /api/boards/<id>?replace=true` with it.

That is the entire contract — see [HTTP API](api.md) for the board shape and
[Blocks](blocks.md) for the vocabulary.
