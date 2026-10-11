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

The extracted value lands in the block's `field` (default `"value"`): `value`
for progress/gauge/stat, `text` for text/callout. Blocks with no `source` are
pushed as written, so you can mix static and live blocks.

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
# whitelist of types a source can drive: a `stats` block (or any other type with
# no single-value field) cannot take a `source` — the backend would 400 the
# whole board over the invented `value` field, so we name it as a source error
# instead and push the block as written.
FIELD_OF = {
    "progress": "value", "gauge": "value", "stat": "value",
    "text": "text", "callout": "text", "heading": "text",
}
NUMERIC = {"progress", "gauge"}


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


def sample(block: dict, token: str):
    """Fetch one block's source. Returns (value, error_or_None)."""
    src = block["source"]
    kind = src.get("kind")
    try:
        if kind == "command":
            raw = _run_command(src["argv"])
        elif kind == "http":
            raw = _get_http(src["url"], token)
        elif kind == "file":
            raw = _read_file(src["path"])
        else:
            raise ValueError(f"unknown source kind {kind!r}")
        val = _extract(raw, src.get("extract", {}))
        # The block vocabulary takes scalars (string | number) — a JSON bool or a
        # nested object is not one, so coerce rather than let the push 400.
        if isinstance(val, bool):
            val = "true" if val else "false"
        elif block.get("type") in NUMERIC:
            f = float(val)  # raises on a non-number, which we surface
            val = int(f) if f.is_integer() else f
        elif not isinstance(val, (int, float, str)):
            val = json.dumps(val) if not isinstance(val, list) else ", ".join(map(str, val))
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

def build_board(cfg: dict, token: str, log: list[str]) -> tuple[dict, int, int]:
    """Resolve every block's source. Returns (board, ok_count, error_count)."""
    ok = err = 0
    blocks: list[dict] = []
    for block in cfg["blocks"]:
        b = {k: v for k, v in block.items() if k != "source" and k != "_last"}
        if "source" not in block:
            blocks.append(b)
            continue
        btype = block.get("type") or ""
        if "source" in block and btype not in FIELD_OF and "field" not in block:
            err += 1
            log.append(f"[{time.strftime('%H:%M:%S')}] {b.get('id')}: "
                       f"block type {btype!r} has no single-value field for a source")
            blocks.append(b)  # pushed as written; the source is simply unused
            continue
        field = block.get("field") or FIELD_OF.get(btype, "value")
        val, e = sample(block, token)
        if e is None:
            b[field] = val
            block["_last"] = val
            ok += 1
        else:
            err += 1
            log.append(f"[{time.strftime('%H:%M:%S')}] {b.get('id')}: {e}")
            # Keep the last good value if we have one; never write a blank.
            if "_last" in block:
                b[field] = block["_last"]
        blocks.append(b)

    for b in blocks:  # the log block, if the config has one, gets our lines
        if b.get("type") == "log":
            b["lines"] = log[-12:]

    board = {
        "title": cfg.get("title", cfg["board"]),
        "subtitle": cfg.get("subtitle", ""),
        "status": "error" if err and not ok else "running",
        "ttl": cfg.get("ttl", 300),
        "blocks": blocks,
    }
    if cfg.get("tab"):
        board["tab"] = cfg["tab"]
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
