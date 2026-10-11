# Blocks

A board is a list of **block** objects. Each block is:

```json
{"id": "p", "type": "progress", "label": "Rows copied", "value": 6836, "max": 6950}
```

- `type` is required and must be one of the 17 types below.
- `id` is optional, but if present it must be a **string** and **unique** within
  the board. `update_block` addresses blocks by id, so a block without an id can
  never be updated individually.
- Only the fields a type declares are accepted. An unknown field is refused with
  a 400 naming it.

There are **17 block types** across **15 components**: `chart` is an
alias of `sparkline`, and `stat` and `stats` share one chip component. `text`
renders as a plain card.

> **Required fields.** No field is *strictly* required by the validator. A
> missing field falls back to a default (`""`, `0`, or `100`). The tables mark
> the fields a block needs to be *useful*. The API will happily accept
> `{"type": "progress"}`, and it renders as an empty 0% bar; that is almost never
> what you want.

Field kinds used below:

| Kind | Accepts |
|---|---|
| `scalar` | a string or a number |
| `numeric` | a finite number, or a string that parses to one |
| `positive` | a `numeric` greater than 0 |
| `scalar list` | a list of `scalar` |
| `number list` | a list of `numeric` |
| `series list` (`line`) | a list of `{name?, values: number list, color?}` series |

A **number** here is bounded to `2**53` (the largest integer a JS number
represents exactly). A larger integer is refused with a `400` rather than being
silently rounded on the way to the client; a large finite *float* is accepted
because it is already the value the client will see.

## The types

### `heading`

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `text` | scalar | yes | the heading text |
| `eyebrow` | scalar | no | a small uppercase label above the heading |

### `text`

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `text` | scalar | yes | a paragraph inside a card |

### `stat`

A single chip.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | yes | the chip's uppercase label |
| `value` | scalar | yes | the chip's value (see [Numbers](#numbers)) |
| `unit` | scalar | no | a small unit suffix after the value |

### `stats`

A row of chips.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `items` | list of `{label, value, unit?}` objects | yes | one chip per item |

Each item's `label`, `value` and `unit` are `scalar`.

### `progress`

A labelled horizontal bar.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | yes | the label on the left |
| `value` | numeric | yes | the filled amount |
| `max` | positive | yes | the denominator |
| `unit` | scalar | no | a unit suffix after `value / max` |
| `fmt` | scalar | no | `"compact"` shortens the `value / max` label (see [Numbers](#numbers)) |

The percentage is `value / max`, clamped to `0..100`.

### `gauge`

An SVG ring.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | yes | the legend label |
| `value` | numeric | yes | the filled amount |
| `max` | positive | yes | the denominator |
| `unit` | scalar | no | a unit suffix after `value / max` |
| `color` | accent | no | accent colour (see [Colours](#colours)) |
| `fmt` | scalar | no | `"compact"` shortens the label |

The ring's centre shows the percentage.

### `bar`

One or more horizontal bars, scaled against the largest value in the set.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | no | a label above the bars |
| `color` | accent | no | accent colour |
| `data` | list of `[label, value]` pairs or `{label, value}` objects | yes | one row per entry: label, bar, value |
| `fmt` | scalar | no | `"compact"` shortens each value label |

Bar widths are relative to the **largest value in `data`** (minimum denominator
1), not a fixed `max`.

### `sparkline` / `chart`

An SVG sparkline (line + area). `chart` is a true alias of `sparkline`.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | no | a label above the chart |
| `color` | accent | no | accent colour |
| `values` | number list | yes | the series; fewer than 2 values renders `(no data)` |

**Point values are on hover.** The sparkline has no axes, so the individual
numbers are not printed; move a pointer (or drag a finger) across it and a
readout shows the value at the nearest point, with a dot on the curve. The chart
is measured to its container so the curve and the dot are never distorted.

### `line`

A multi-series line chart with axes — the trend view for "tokens over time"
or "p95 vs p50". One SVG with a y-axis (gridlines with min/max labels), an
x-axis (the `xLabels`, or the point indices when they are absent), one polyline
per series in its accent colour, and a legend.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | no | the card heading |
| `color` | accent | no | the **default series accent token** (see [Colours](#colours)) |
| `series` | series list | yes* | one polyline per series: `{name?, values: number list, color?}` |
| `values` | number list | yes* | sugar for one unnamed series |
| `xLabels` | scalar list | no | x-axis labels; **length must equal the point count** |
| `yMin` / `yMax` | numeric | no | the y-axis range; auto-scaled from the data when absent |
| `legend` | bool (or string) | no | force the legend on/off; default = shown when there is more than one series |

\* Exactly **one** of `series` and `values` — sending both is refused with a
400 (``provide `series` or `values`, not both.``), and so is sending neither.

Rules the contract enforces, each a named `400`, and like every block a
malformed `line` still degrades to a visible value rather than throwing:

- At least one series; every series must have the **same length**; each needs
  **at least 2 points** (`must have at least 2 points`).
- `xLabels`, when present, must match the per-series point count.
- If both `yMin` and `yMax` are given, `yMax` must be greater than `yMin`.
- `legend` is boolish: the strings `"true"` / `"false"` coerce like
  `tagColumn` above.

Example:

```jsonc
{"id": "t", "type": "line", "label": "Tokens over time",
 "series": [
   {"name": "p50", "values": [2, 14, 40, 77, 8], "color": "mint"},
   {"name": "p95", "values": [10, 30, 50, 95, 22]}],
 "xLabels": ["00:00", "06:00", "12:00", "18:00", "24:00"],
 "yMin": 0, "yMax": 100}

// sugar: `values` alone renders a single unnamed series
{"id": "s", "type": "line", "label": "Burn-down", "values": [3, 6, 4, 9, 7], "color": "pink"}
```

A degenerate chart is degraded, never broken: the `values` sugar may hold a
single point (the contract accepts a short `values`), which renders the
visible `(no data)` state instead of a broken axis. Inside a `series`,
though, each entry needs its own **at least 2 points** — a shorter series is
a named `400`, not a graceful degradation.

**Values are on hover.** Move a pointer (or drag a finger) across the chart and
a crosshair snaps to the nearest x, marking the point on every series and
reading out each one's value — so a chart shows its numbers without you having
to eyeball the axis. The chart fills its card at any width.

Only the charts with hidden values are interactive: `bar` already prints each
value beside its bar, so it needs no hover.

### `table`

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `columns` | scalar list | no | the header row (uppercased) |
| `rows` | list of lists of `scalar` | yes | the body rows |
| `tagColumn` | bool (or string) | no | when true, the **last** cell of each row renders as a status pill |

Pill colours are inferred from the last cell's text:

| Text (case-insensitive) | Pill |
|---|---|
| `done`, `ok`, `success`, `complete`, `completed` | ok (green) |
| `running`, `active`, `in progress` | running |
| `failed`, `error`, `err` | error (red) |
| `warn`, `warning`, `pending` | warning |
| anything else | muted |

### `log`

A monospace log panel.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `title` | scalar | no | a label above the log |
| `lines` | scalar list | yes | one line per entry; an empty log renders `(no output)` |

`log` is the **only** appendable block: `update_block`'s `append` adds a line.
The server stores at most **400** lines; the client renders the most recent
**80**.

### `kv`

Key/value rows.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `items` | list of `[key, value]` pairs or `{k, v}` objects | yes | one row per entry, key muted on the left, value mono on the right |

### `list`

A bulleted list.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `label` | scalar | no | a label above the list |
| `items` | scalar list | yes | one bulleted line per item |

### `callout`

A tinted callout.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `text` | scalar | yes | the callout text |
| `tone` | scalar | no | one of `ok`, `warn`, `error`, `info` (default `info`) |

Tone aliases are accepted: `success` → ok, `warning` → warn, `err`/`fail` →
error.

### `code`

A monospace block, split on newlines, with a **copy button** — a code block is
usually something you want to take away, so the browser client renders one
alongside the text. The button copies the block's `text` verbatim and reports
the result in words ("Copied" / "Copy failed"), so a blocked clipboard says so
instead of pretending.

| Field | Kind | Required | Renders as |
|---|---|---|---|
| `text` | scalar | yes | the code text; `\n` starts a new line |
| `title` | scalar | no | a label above the block |

`code` is **not** appendable. It is rendered from its static `text`, so
`update_block`'s `append` on a `code` block is refused rather than silently
ignored.

### `divider`

A horizontal rule. No fields.

## Colours

`gauge`, `bar`, `sparkline`/`chart` and `line` accept an optional `color` (for
`line`, it sets the default series colour). It maps to a CSS accent variable,
so these eight values resolve:

`violet` (default), `pink`, `lime`, `mint`, `ok`, `warn`, `error`, `muted`

`ok`, `warn`, `error`, `violet` and `muted` re-tint with the theme (they map to
theme-aware variables); `pink`, `lime` and `mint` deliberately stay pastel in
both themes — they are dots and badges, not backgrounds.

A `color` is validated against this accent set (case-insensitively), the same
rule `tab.accent` always had. Anything else is refused with `400`, naming the
field: the value is interpolated into a CSS `var(--accent-<name>)` token by the
client, so an arbitrary string there is an injection sink (a crafted value can
append a `url()` to the declaration and make every viewer's browser fetch it).

## Numbers

The rule is simple: **numbers are exact by default, and compaction is a
per-value choice the agent makes.** The reason is that a count a human wants to
read off or diff (`6836`) must not become a lossy approximation (`6.8K`), while a
quantity only meaningful at scale (a token total) may be shown short.

There are three ways to express it:

| The agent sends | Renders | Use for |
|---|---|---|
| a **number** | exact, `6836` | counts, anything you might diff |
| a **string** | verbatim, `"1.5M"` | `stat`/`stats` values only, pre-formatted by the agent |
| `"fmt": "compact"` | the label shortened, the ratio intact | `progress`/`gauge`/`bar` |

The third exists because `progress`/`gauge`/`bar` need a **real number** to
compute the ratio, so you cannot pass `"1.5M"` there. `fmt: "compact"` shortens
only the displayed label; the bar or ring still uses the true value.

Compaction uses one decimal place at these thresholds:

| Range | Suffix | Example |
|---|---|---|
| ≥ 1,000,000,000 | `B` | `2500000000` → `2.5B` |
| ≥ 1,000,000 | `M` | `1521227` → `1.5M` |
| ≥ 1,000 | `K` | `9410` → `9.4K` |
| < 1,000 | none | `999` → `999` |

Examples:

```jsonc
// exact number: a count stays readable to the unit
{"type": "stats", "items": [{"label": "Memories", "value": 5371}]}
//   -> 5371

// string: pre-formatted, rendered verbatim
{"type": "stats", "items": [{"label": "Tokens", "value": "1.5M"}]}
//   -> 1.5M

// fmt:compact - the ratio uses 1521227, the label shows 1.5M
{"type": "gauge", "label": "Tokens", "value": 1521227, "max": 2000000, "fmt": "compact"}
//   -> ring at 76%, label "1.5M / 2.0M"

// exact ratio: "3 / 64"
{"type": "progress", "label": "Shards", "value": 3, "max": 64}
//   -> "3 / 64", 5%
```

The formatting rule lives in one place, `web/src/livewall/format.ts`; the server
does no number formatting — it stores and streams the values exactly as the
agent wrote them. `web/test/format.test.ts` asserts the exact rendered values,
and the browser render suite additionally asserts the exact values and the
**absence** of compacted forms where an exact count belongs.
