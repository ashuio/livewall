#!/usr/bin/env python3
"""Push a Livewall board from live sources — the *poller* pattern.

Livewall renders; it never fetches anything. This script is the other half: it
polls sources on an interval and pushes a full board, so the wall keeps updating
with no agent and no browser attached. Run it, walk away, the board stays current.

    python3 examples/pusher.py                      # built-in demo config
    python3 examples/pusher.py examples/pusher.json # your own config
    python3 examples/pusher.py --once               # one sample, then exit

Configuration (flags override the environment):

    LIVEWALL_URL     base URL of the backend  (default http://127.0.0.1:8470)
    LIVEWALL_TOKEN   bearer token, if the backend sets one

A source supplies a VALUE; the block's structure stays yours. That split is the
point: a flaky endpoint can change a number, never the shape of your board.

    {"kind": "http",    "url": "http://127.0.0.1:8470/health",
                        "extract": {"path": "$.ok"}}
    {"kind": "command", "argv": ["df", "-h", "/"],
                        "extract": {"regex": "([0-9]+)%"}}
    {"kind": "file",    "path": "/proc/loadavg",
                        "extract": {"regex": "^([0-9.]+)"}}

`extract` is one of:

    {"path": "$.a.b"}   a dotted path into a JSON body ($ = the root)
    {"regex": "..."}    the first capture group (or the whole match) of the text

The extracted value lands in the block's `field` (default: the type's own field
— see `FIELD_OF`). Scalar fields take a string/number; COMPOSITE fields take a
whole JSON list — `items` for stats/kv/list, `data` for bar, `rows` for table,
`values` for sparkline/chart/line, `series` for line, `lines` for log. Every
block type with at least one field is source-drivable; only `divider` has none.
Blocks with no source are pushed as written, so you can mix static and live
blocks.

One source per block by default. To drive several fields of one block — a
callout's `text` AND its `tone`, say — use `sources`, a field -> source map:

    {"type": "callout", "id": "s", "text": "…",
     "sources": {"text": {...}, "tone": {...}}}

A tab's indicator can be live too — give `tab.indicator` a `source`:

    "tab": {"label": "h", "indicator": {"kind": "bar", "value": 0, "max": 100,
                                        "source": {"kind": "command", ...}}}

Standard library only — the same constraint the bundled CLI keeps.

Two API details this script exists to get right (both are easy to get wrong):

  * `replace` is a QUERY parameter: `POST /api/boards/<id>?replace=true`.
    Putting it in the JSON body is silently ignored and every cycle after the
    first returns 409.
  * `PATCH` cannot write `blocks` (board-level fields only) and `/update` edits
    one block. A full-board refresh is *only* `POST ?replace=true`.

A failing source marks itself in the board (the `poller.log` block) instead of
writing a stale or partial value, so a broken source is visible rather than
silently wrong.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8470"
FETCH_TIMEOUT = 5.0
MAX_OUTPUT = 64 * 1024  # cap a source's output so one bad source cannot OOM us
# The backend refuses any scalar field longer than this (MAX_TEXT_LEN in
# backend/contract.py): an overlong value must be a per-source error, not a
# 400 that dooms the push.
MAX_VALUE = 8192
# The largest number the backend stores exactly (MAX_SAFE_INT in
# backend/contract.py): a bigger integer is legal JSON but the backend refuses
# it, so it must fail this source, not the push.
MAX_SAFE_INT = 2 ** 53

# A runnable default so `python3 examples/pusher.py` does something useful
# without a config file. It reads three different kinds of source.
DEMO = {
    "board": "host-health",
    "title": "Host health",
    "subtitle": "polled from real sources — no agent attached",
    "interval": 5,
    "ttl": 300,
    "tab": {"label": "host", "accent": "mint"},
    "blocks": [
        {"type": "heading", "id": "h", "text": "Polled from real sources",
         "eyebrow": "pusher.py"},
        {"type": "text", "id": "t",
         "text": "Written by examples/pusher.py, not by an agent. Kill the "
                 "pusher and the board freezes."},
        {"type": "progress", "id": "disk", "label": "Disk (/)", "max": 100,
         "unit": "%",
         "source": {"kind": "command", "argv": ["df", "-h", "/"],
                    "extract": {"regex": r"([0-9]+)%"}}},
        {"type": "gauge", "id": "load", "label": "Load 1m", "max": 8.0,
         "color": "mint",
         "source": {"kind": "file", "path": "/proc/loadavg",
                    "extract": {"regex": r"^([0-9.]+)"}}},
        {"type": "stat", "id": "health", "label": "Backend /health",
         "source": {"kind": "http", "url": "http://127.0.0.1:8470/health",
                    "extract": {"path": "$.ok"}}},
        {"type": "log", "id": "log", "title": "poller.log", "lines": []},
    ],
}

# Which block field a source's value lands in, by block type. This is also the
# whitelist of types a source can drive. A source supplies a VALUE into a field
# the CONFIG already declared — it can never add a field or a block type, which
# is what keeps a compromised endpoint from reshaping the board.
#
# Scalar fields (one value) and composite fields (a whole list) are both here:
# `stats`/`kv`/`list` take their `items`, `bar` its `data`, `table` its `rows`,
# the charts their `values`. A composite source must extract a JSON list — a
# scalar there is a named error, never an invented field the backend would 400
# the whole board over.
FIELD_OF = {
    "heading": "text", "text": "text", "code": "text",
    "stat": "value", "progress": "value", "gauge": "value",
    "callout": "text",
    "stats": "items", "kv": "items", "list": "items",
    "bar": "data", "table": "rows",
    "sparkline": "values", "chart": "values",
    "line": "series", "log": "lines",
}
# Fields whose value is a LIST, not a scalar. The entry shape inside it is the
# block's business; the pusher only insists it is a list.
COMPOSITE_FIELDS = {"items", "data", "rows", "values", "series", "columns",
                    "lines", "xLabels"}
# A composite source is bounded so one runaway array cannot blow the board-body
# cap (app.py, 1 MiB) — over this it is a named source error, not a failed push.
MAX_ITEMS = 200
# ...and bounded in bytes: every string is capped at MAX_VALUE, but JSON
# escaping can inflate a source's raw output several-fold (one emoji char
# becomes a 12-char \uXXXX pair), so a bloated array is a named error too.
# A whole BOARD of maxed composites can still outgrow the body cap — the
# backend's 413 refusal is printed by push() — trimming blocks silently would
# hide it, so that residual is left named-at-push rather than auto-trimmed.
MAX_COMPOSITE_BYTES = 256 * 1024
NUMERIC = {"progress", "gauge"}

# Every field each block type may carry — a mirror of BLOCK_FIELDS in
# backend/contract.py. A source may only bind a field on this list: `sources`
# (or a `field` override) naming anything else would push a field the backend
# refuses, 400ing the WHOLE board, so it is named as a source error instead.
TYPE_FIELDS = {
    "heading": {"text", "eyebrow"},
    "text": {"text"},
    "stat": {"label", "value", "unit"},
    "stats": {"items"},
    "progress": {"label", "value", "max", "unit", "fmt"},
    "gauge": {"label", "value", "max", "unit", "color", "fmt"},
    "bar": {"label", "color", "data", "fmt"},
    "sparkline": {"label", "color", "values"},
    "chart": {"label", "color", "values"},
    "line": {"label", "color", "series", "values", "xLabels", "yMin", "yMax",
             "legend"},
    "table": {"columns", "rows", "tagColumn"},
    "log": {"title", "lines"},
    "kv": {"items"},
    "list": {"label", "items"},
    "callout": {"text", "tone"},
    "code": {"text", "title"},
    "divider": set(),
}

# The entry shape each composite field accepts — the same shapes the backend's
# validator (backend/contract.py `_check_kind`) enforces, mirrored here so a
# junky entry (a null, a nested list, an overlong string, a bool) is a named
# source error rather than a 400 that dooms the whole board.
ENTRY_KINDS = {
    ("stats", "items"): "stats_items",
    ("kv", "items"): "kv_items",
    ("list", "items"): "scalar_list",
    ("bar", "data"): "bar_data",
    ("table", "rows"): "rows",
    ("table", "columns"): "scalar_list",
    ("sparkline", "values"): "num_list",
    ("chart", "values"): "num_list",
    ("line", "series"): "line_series",
    ("line", "values"): "num_list",
    ("line", "xLabels"): "scalar_list",
    ("log", "lines"): "scalar_list",
}


# --------------------------------------------------------------------- sources

def _run_command(argv: list[str]) -> str:
    """Run argv directly — never a shell, so there is no injection surface."""
    p = subprocess.run(argv, capture_output=True, text=True,
                       timeout=FETCH_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError(f"exit {p.returncode}: {p.stderr.strip()[:120]}")
    return p.stdout[:MAX_OUTPUT]


def _get_http(url: str, token: str) -> str:
    headers = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return r.read(MAX_OUTPUT).decode("utf-8", "replace")


def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(MAX_OUTPUT)


def _extract(text: str, spec: dict):
    """Pull one scalar out of a source's output. Raises if it cannot."""
    if "regex" in spec:
        m = re.search(spec["regex"], text, re.MULTILINE)
        if not m:
            raise ValueError(f"regex {spec['regex']!r} did not match")
        return m.group(1) if m.groups() else m.group(0)
    if "path" in spec:
        cur = json.loads(text)
        for part in spec["path"].lstrip("$").strip(".").split("."):
            if not part:
                continue
            if isinstance(cur, list):
                cur = cur[int(part)]
            else:
                cur = cur[part]
        return cur
    raise ValueError("source has no `extract` (need `path` or `regex`)")


def _scalar_ok(v) -> bool:
    """A value the backend stores as a scalar: a string within MAX_VALUE, or a
    finite number within MAX_SAFE_INT. A bool is neither (contract `_is_scalar`)."""
    if isinstance(v, str):
        return len(v) <= MAX_VALUE
    # `abs` before isfinite: isfinite converts an int to float, so a giant int
    # would raise inside the check instead of simply being refused.
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and abs(v) <= MAX_SAFE_INT and math.isfinite(v))


def _as_number(v):
    """The number a value renders as, or None when it is not renderably
    numeric — the backend's `numeric` kind: a finite number or a numeric
    string, both within MAX_SAFE_INT."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if abs(v) <= MAX_SAFE_INT and math.isfinite(v) else None
    if isinstance(v, str) and len(v) <= MAX_VALUE:
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _entry_error(field: str, where, want: str, val) -> ValueError:
    return ValueError(
        f"field {field!r}[{where}] must be {want}, got {type(val).__name__}")


def _refuse_scalar(field: str, where, val) -> ValueError:
    """The error for a value that is not a storable scalar — an overlong
    string is named by its length, anything else by its type."""
    if isinstance(val, str):
        return ValueError(
            f"field {field!r}[{where}] is too long ({len(val)} chars); "
            f"the backend accepts {MAX_VALUE}")
    return _entry_error(field, where, "a string or a number", val)


def _check_entries(field: str, kind: str, val: list) -> None:
    """Refuse composite entries the backend's validator would refuse — it 400s
    the WHOLE board over one bad entry, so the pusher names it first. The
    shapes mirror backend/contract.py `_check_kind` (same accepted forms, so
    nothing a legal config produces is refused here)."""
    if kind == "num_list":
        for i, x in enumerate(val):
            if _as_number(x) is None:
                raise _entry_error(field, i, "a number", x)
    elif kind == "scalar_list":
        for i, x in enumerate(val):
            if not _scalar_ok(x):
                raise _refuse_scalar(field, i, x)
    elif kind == "stats_items":
        for i, it in enumerate(val):
            if not isinstance(it, dict):
                raise _entry_error(field, i, "an object", it)
            for k in ("label", "value", "unit"):
                if k in it and not _scalar_ok(it[k]):
                    raise _refuse_scalar(field, f"{i}.{k}", it[k])
    elif kind in ("kv_items", "bar_data"):
        num_second = kind == "bar_data"
        for i, pair in enumerate(val):
            if isinstance(pair, (list, tuple)):
                if len(pair) != 2:
                    raise _entry_error(field, i, "a [key, value] pair", pair)
                k, v = pair
            elif isinstance(pair, dict):
                # The backend defaults a dict form's missing keys ("" / 0) and
                # accepts the result — mirror that exactly, so nothing a legal
                # config produces is refused here.
                kk, vk = ("k", "v") if kind == "kv_items" else ("label", "value")
                k, v = pair.get(kk, ""), pair.get(vk, 0 if num_second else "")
            else:
                raise _entry_error(field, i, "a [key, value] pair", pair)
            if not _scalar_ok(k):
                raise _refuse_scalar(field, f"{i}.key", k)
            if num_second and _as_number(v) is None:
                raise _entry_error(field, f"{i}.value", "a number", v)
            if not num_second and not _scalar_ok(v):
                raise _refuse_scalar(field, f"{i}.value", v)
    elif kind == "rows":
        for i, row in enumerate(val):
            if not isinstance(row, list):
                raise _entry_error(field, i, "a list of cells", row)
            for ci, cell in enumerate(row):
                if not _scalar_ok(cell):
                    raise _refuse_scalar(field, f"{i}[{ci}]", cell)
    elif kind == "line_series":
        if not val:
            raise ValueError(f"field {field!r} must contain at least one series")
        lengths: set[int] = set()
        for i, s in enumerate(val):
            if not isinstance(s, dict):
                raise _entry_error(field, i, "an object", s)
            for k in ("name", "color"):
                if k in s and not _scalar_ok(s[k]):
                    raise _refuse_scalar(field, f"{i}.{k}", s[k])
            pts = s.get("values")
            if not isinstance(pts, list):
                raise _entry_error(field, f"{i}.values", "a list of numbers", pts)
            if len(pts) < 2:
                raise ValueError(
                    f"field {field!r}[{i}].values must have at least 2 points, "
                    f"got {len(pts)}")
            for pi, p in enumerate(pts):
                if _as_number(p) is None:
                    raise _entry_error(field, f"{i}.values[{pi}]", "a number", p)
            lengths.add(len(pts))
        if len(lengths) > 1:
            raise ValueError(
                f"field {field!r} series lengths differ; every series must "
                f"have the same number of points")


def sample(source: dict, field: str, btype: str, token: str):
    """Fetch one source's value. Returns (value, error_or_None).

    `field` is the block field the value lands in, and it — not the block type —
    decides the shape the value must have: a COMPOSITE field takes a whole JSON
    list, any other field a scalar. A mismatch is a named source error, never a
    value smuggled into a field the backend would 400 the whole board over.
    """
    kind = source.get("kind")
    try:
        if kind == "command":
            raw = _run_command(source["argv"])
        elif kind == "http":
            raw = _get_http(source["url"], token)
        elif kind == "file":
            raw = _read_file(source["path"])
        else:
            raise ValueError(f"unknown source kind {kind!r}")
        val = _extract(raw, source.get("extract", {}))

        if field in COMPOSITE_FIELDS:
            # A whole list of entries. A scalar here is an error, not a
            # one-element list; the entries and the serialised size are
            # bounded to the shapes and caps the backend actually accepts.
            if not isinstance(val, list):
                raise ValueError(
                    f"field {field!r} needs a JSON list, got {type(val).__name__}")
            if len(val) > MAX_ITEMS:
                raise ValueError(
                    f"field {field!r} has {len(val)} entries; the pusher cap "
                    f"is {MAX_ITEMS}")
            kind = ENTRY_KINDS.get((btype, field))
            if kind:
                _check_entries(field, kind, val)
            blob = json.dumps(val)
            if len(blob) > MAX_COMPOSITE_BYTES:
                raise ValueError(
                    f"field {field!r} serialises to {len(blob)} bytes; the "
                    f"pusher cap is {MAX_COMPOSITE_BYTES}")
            return val, None

        # A scalar field. The vocabulary takes string | number, so a nested
        # object is coerced rather than pushed raw — but progress/gauge need a
        # real NUMBER: a bool or any non-number there is an error, never a
        # "true" string the backend would 400 the whole board over.
        if btype in NUMERIC:
            if isinstance(val, bool):
                raise ValueError(f"field {field!r} needs a number, got bool")
            f = float(val)  # raises on a non-number, which we surface
            if not math.isfinite(f):
                raise ValueError(f"field {field!r} needs a finite number")
            val = int(f) if f.is_integer() else f
        elif isinstance(val, bool):
            val = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            raise ValueError(
                f"field {field!r} needs a scalar, got {type(val).__name__}")
        elif not isinstance(val, (int, float, str)):
            val = str(val)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            # `abs` first: isfinite converts an int to float, so a giant int
            # would raise inside the check instead of naming the cap.
            if abs(val) > MAX_SAFE_INT or not math.isfinite(val):
                # The backend refuses a number it cannot store exactly
                # (MAX_SAFE_INT) — fail this source, not the push.
                raise ValueError(
                    f"field {field!r} must be a finite number within "
                    f"+-{MAX_SAFE_INT}")
        if isinstance(val, str) and len(val) > MAX_VALUE:
            # The backend would 400 the WHOLE board over one oversized field;
            # fail this source instead, so the rest of the board still lands.
            raise ValueError(
                f"extracted value is too long ({len(val)} chars); "
                f"the backend accepts {MAX_VALUE}")
        return val, None
    except Exception as e:  # a bad source must not kill the loop
        return None, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------- board

def _block_sources(block: dict) -> dict[str, dict]:
    """The field -> source map for one block, from `source` and/or `sources`.

    `source` binds the block type's default field (overridable with `field`);
    `sources` binds any field by name and wins on a field both forms name.
    Every bound field must be one the block's type actually allows (per
    TYPE_FIELDS — the backend's own vocabulary): anything else would push an
    invented field the backend refuses, 400ing the WHOLE board, so it is a
    named source error instead. Raises ValueError when a `source` sits on a
    type with no default field and none is named.
    """
    out: dict[str, dict] = {}
    btype = block.get("type") or ""
    if isinstance(block.get("source"), dict):
        field = block.get("field") or FIELD_OF.get(btype)
        if not field:
            raise ValueError(
                f"block type {btype!r} has no field for a source; name one "
                f"with `field`, or use `sources`")
        out[field] = block["source"]
    elif block.get("source") is not None:
        raise ValueError("`source` must be a source object")
    sources = block.get("sources")
    if sources is not None and not isinstance(sources, dict):
        raise ValueError("`sources` must be a field -> source map")
    for field, src in (sources or {}).items():
        if not isinstance(src, dict):
            raise ValueError(f"`sources.{field}` must be a source object")
        out[field] = src
    allowed = TYPE_FIELDS.get(btype)
    for field in out:
        if allowed is None:
            raise ValueError(f"block type {btype!r} is not a known block type")
        if field not in allowed:
            raise ValueError(
                f"block type {btype!r} has no field {field!r}; valid fields: "
                f"{sorted(allowed)}")
    return out


def _resolve_tab(cfg_tab, token: str, log: list[str]):
    """A tab descriptor with any live `indicator.source` resolved to a value."""
    if not cfg_tab:
        return None
    tab = json.loads(json.dumps(cfg_tab))  # deep copy — never mutate the config
    ind = tab.get("indicator")
    if isinstance(ind, dict) and isinstance(ind.get("source"), dict):
        src = ind.pop("source")
        # The indicator value is numeric (a bar/pct needs a ratio); a bool or
        # any non-number is a named error. If there is no usable placeholder
        # either, the indicator is dropped rather than pushed in a shape the
        # backend would 400 the whole board over.
        val, e = sample(src, "value", "gauge", token)
        if e is None and _as_number(val) is None:
            e = f"indicator value must be a number, got {type(val).__name__}"
        if e is None:
            ind["value"] = val
        else:
            log.append(f"[{time.strftime('%H:%M:%S')}] tab.indicator: {e}")
            if _as_number(ind.get("value")) is None:
                del tab["indicator"]
    return tab


def build_board(cfg: dict, token: str, log: list[str]) -> tuple[dict, int, int]:
    """Resolve every block's sources. Returns (board, ok_count, error_count)."""
    ok = err = 0
    blocks: list[dict] = []
    for block in cfg["blocks"]:
        b = {k: v for k, v in block.items()
             if k not in ("source", "sources", "_last")}
        if "source" in block or "sources" in block:
            # `field` (if any) steers the source binding below; it is not a
            # contract field, and left on the block it would 400 the WHOLE
            # board over.
            b.pop("field", None)
        try:
            srcs = _block_sources(block)
        except ValueError as e:
            err += 1
            log.append(f"[{time.strftime('%H:%M:%S')}] {block.get('id')}: {e}")
            blocks.append(b)  # pushed as written; the source is simply unused
            continue
        if not srcs:
            blocks.append(b)
            continue
        btype = block.get("type") or ""
        last = block.get("_last") or {}
        for field, src in srcs.items():
            val, e = sample(src, field, btype, token)
            if e is None:
                b[field] = val
                last[field] = val
                ok += 1
            else:
                err += 1
                log.append(
                    f"[{time.strftime('%H:%M:%S')}] {block.get('id')}.{field}: {e}")
                # Keep the last good value if we have one; never write a blank.
                if field in last:
                    b[field] = last[field]
        block["_last"] = last
        blocks.append(b)

    # The tab is resolved BEFORE the log block below takes its lines, so a dead
    # indicator source is visible in the same tick that failed.
    tab = _resolve_tab(cfg.get("tab"), token, log)
    for b, block in zip(blocks, cfg["blocks"]):
        # the log block, if the config has one, gets our lines — but a log
        # block whose own `lines` a source drives keeps those, not ours
        if (b.get("type") == "log" and "source" not in block
                and "sources" not in block):
            b["lines"] = log[-12:]

    board = {
        "title": cfg.get("title", cfg["board"]),
        "subtitle": cfg.get("subtitle", ""),
        "status": "error" if err and not ok else "running",
        "ttl": cfg.get("ttl", 300),
        "blocks": blocks,
    }
    if tab:
        board["tab"] = tab
    return board, ok, err


def push(base: str, token: str, board_id: str, board: dict) -> bool:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}/api/boards/{board_id}?replace=true",   # replace is a QUERY param
        data=json.dumps(board).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        print(f"  push failed: HTTP {e.code} {e.read()[:160].decode(errors='replace')}")
        return False
    except urllib.error.URLError as e:
        print(f"  push failed: cannot reach {base} ({e.reason})")
        return False


# ------------------------------------------------------------------------ main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Push a Livewall board from live sources.")
    ap.add_argument("config", nargs="?", help="JSON config file (default: built-in demo)")
    ap.add_argument("--url", default=os.environ.get("LIVEWALL_URL", DEFAULT_URL))
    ap.add_argument("--token", default=os.environ.get("LIVEWALL_TOKEN", ""))
    ap.add_argument("--once", action="store_true", help="push one sample and exit")
    args = ap.parse_args(argv)

    cfg = json.load(open(args.config)) if args.config else DEMO
    board_id = cfg["board"]
    interval = float(cfg.get("interval", 5))
    log: list[str] = []

    print(f"pushing {args.url}/api/boards/{board_id} every {interval:g}s "
          f"({'once' if args.once else 'Ctrl-C to stop'})")
    n = 0
    try:
        while True:
            n += 1
            board, ok, err = build_board(cfg, args.token, log)
            good = push(args.url, args.token, board_id, board)
            print(f"  sample {n}: pushed={good} ok={ok} errors={err}")
            if args.once:
                return 0 if good else 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
