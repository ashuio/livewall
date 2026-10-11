# Protocol

The **board spec is the wire format**. There is no intermediate UI protocol and
no compiled catalog: the server streams each board's own `blocks`, exactly as
stored, and the client renders them directly. This page describes the wire
shape and the render contract that the client is expected to honour.

## The snapshot

`GET /api/stream` is a Server-Sent Events stream. Each frame is a **complete
snapshot** of every board:

```json
{
  "config": {"default_ttl": 1800, "stale_ttl": 3600, "max_boards": 12},
  "now": 1759938000.5,
  "boards": [
    {
      "id": "migration",
      "title": "Migration",
      "subtitle": "",
      "status": "running",
      "updated": 1759937990.0,
      "age": 10.5,
      "version": 3,
      "ttl": null,
      "effective_ttl": 1800,
      "expires_in": null,
      "progress": 0.98,
      "tab": {"label": "Migration", "accent": "violet",
              "indicator": {"kind": "bar", "value": 6836, "max": 6950}},
      "blocks": [
        {"id": "p", "type": "progress", "label": "Rows copied",
         "value": 6836, "max": 6950, "unit": "rows"},
        {"id": "l", "type": "log", "title": "recent", "lines": ["batch 41 ok"]}
      ]
    }
  ]
}
```

- Every field except `blocks` is the **summary** also returned by
  `GET /api/boards` (see [`api.md`](api.md)).
- `blocks` is the board's block list **exactly as stored** — the same objects
  that were validated on write. The server does not transform them.
- `now` is the server clock the frame was measured against; the client advances
  ages on its own clock between frames.

There is no per-field delta on the wire: the snapshot is the unit of
consistency.

## The block vocabulary

A block is an object with a required `type` and the fields that type declares;
the full list is in [`blocks.md`](blocks.md) and the single source of truth is
`backend/contract.py`. There are **17 block types** rendered by **14
components**: `chart` is an alias of `sparkline`, `stat`/`stats` share one chip
component, and `log`/`code` share one log component.

## The render contract

Because the raw blocks are the wire, the client owns the small amount of
behaviour that used to be applied server-side. It must be preserved exactly:

- **Numbers are exact by default.** A `stat`/`stats` value that is a **number**
  renders exact (`6836`, never `6.8K`); a **string** renders verbatim (the agent
  opts into compaction by pre-formatting it, e.g. `"1.5M"`). `progress`/`gauge`/
  `bar` values are coerced to real numbers (a ratio needs one). The rule lives
  in `web/src/livewall/format.ts`.
- **Log cap.** A `log` block is stored with up to `MAX_LOG_LINES` (400) lines
  but rendered with the most recent `RENDER_LOG_LINES` (80). The `code` block
  renders through the log component but is not subject to that cap: it renders
  from its static `text`, which the contract caps at `MAX_TEXT_LEN` (8192
  chars).
- **Empty state.** A board with no title, no subtitle and no blocks shows
  `waiting for the agent to program the board…`.
- **Unknown block.** An unrecognised `type` renders `[unknown block: <type>]`
  rather than crashing.
- **Totality.** The renderer never throws on hostile input: a malformed block
  degrades to a visible value (an empty string, `0`, a default), so one bad
  block cannot blank a board.

The contract validates on write, so a well-behaved agent never hits these
paths; the renderer's totality is the second line of defence for a board already
in memory.

See [`architecture.md`](architecture.md) for how the pieces fit and
[`blocks.md`](blocks.md) for the component each block renders as.
