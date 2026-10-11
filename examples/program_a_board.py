#!/usr/bin/env python3
"""Program a Livewall board end to end with the standard library only.

Creates a board (progress, a multi-series `line` chart, a `values`-sugared
`line`, and a log), updates one block, appends to its log, prints the URL, and
then deletes the board on the way out — so it is safe to run repeatedly and
leaves nothing behind. Pass ``--keep`` to leave it up and watch it in the web
client instead.

    python3 examples/program_a_board.py
    python3 examples/program_a_board.py --keep

Configuration (flags override the environment):

    LIVEWALL_URL     base URL of the backend  (default http://127.0.0.1:8470)
    LIVEWALL_TOKEN   bearer token, if the backend sets one

This mirrors the bundled CLI (``cli/livewall.py``): it talks to the same REST
API with ``urllib`` and no third-party imports.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8470"


def request(base: str, token: str, method: str, path: str, body=None):
    """Call the backend, returning parsed JSON. Exits 1 with a message on error."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        sys.exit(f"error: HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach Livewall at {base}: {e.reason}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("LIVEWALL_URL", DEFAULT_URL),
                   help="backend base URL (env LIVEWALL_URL)")
    p.add_argument("--token", default=os.environ.get("LIVEWALL_TOKEN", ""),
                   help="bearer token (env LIVEWALL_TOKEN)")
    p.add_argument("--board", default="example-program-a-board",
                   help="board id — a lowercase slug")
    p.add_argument("--keep", action="store_true",
                   help="leave the board up instead of deleting it")
    args = p.parse_args(argv)
    base = args.url.rstrip("/")
    bid = urllib.parse.quote(args.board, safe="")

    board = {
        "title": "Example board",
        "status": "running",
        "tab": {"label": "Example", "accent": "violet",
                "indicator": {"kind": "bar", "value": 0, "max": 5}},
        "blocks": [
            {"id": "p", "type": "progress", "label": "Steps", "value": 0, "max": 5},
            {"id": "t", "type": "line", "label": "Tokens over time", "series": [
                {"name": "p50", "values": [120, 180, 240, 320], "color": "mint"},
                {"name": "p95", "values": [300, 420, 540, 610]},
            ], "xLabels": ["09:00", "09:05", "09:10", "09:15"]},
            {"id": "v", "type": "line", "label": "Queue depth", "color": "pink",
             "values": [12, 18, 9, 24, 15]},
            {"id": "l", "type": "log", "title": "activity", "lines": []},
        ],
    }

    created = request(base, args.token, "POST",
                      f"/api/boards/{bid}?replace=true", board) or {}
    print(f"created board {args.board!r} (version {created.get('version')})")

    request(base, args.token, "POST", f"/api/boards/{bid}/update",
            {"id": "p", "set": {"value": 3}})
    request(base, args.token, "POST", f"/api/boards/{bid}/update",
            {"id": "l", "append": "updated block p to 3"})
    print("updated block 'p' and appended to log 'l'")
    print(f"open {base}/?board={args.board} to watch it")

    if args.keep:
        print(f"--keep: leaving it up (remove it with: livewall rm {args.board})")
        return 0

    request(base, args.token, "DELETE", f"/api/boards/{bid}")
    print("deleted the board (pass --keep to leave it up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
