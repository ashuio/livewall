#!/usr/bin/env python3
"""Emit ONE JSON snapshot of host state, for a pusher to sample.

Why this exists: a pusher resolves one value per source, and a `command` source
runs **once per block per tick** — so a heavy command repeated for every field of
a board is wasteful. This probe does the expensive part once, prints a single
JSON object, and every block's source then reads one field out of it with a
`{"path": "$.field"}` extract. Point several blocks at the same probe and only
the first call per tick does the work.

It also shows the two shapes a source can supply:

  * a SCALAR (`used_pct`, `tone`, `line`) — one number or string;
  * a COMPOSITE (`tiles`, `bars`, `detail`) — a whole JSON **list**, which one
    source can push into a `stats`, `bar` or `kv` block in a single call.

Read-only: it reads /proc and stats the filesystem; it writes nothing.

    python3 examples/composite_probe.py

Pair it with `examples/composite_sources.json`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

# The filesystem to report on. Overridable so the probe is not pinned to one box.
TARGET = os.environ.get("PROBE_PATH", "/")
# The load-average threshold at which the status turns to `warn`.
LOAD_WARN = float(os.environ.get("PROBE_LOAD_WARN", "4.0"))


def _load1() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:  # noqa: BLE001 — a probe must never raise
        return -1.0


def _mem_used_pct() -> float:
    """Used memory as a percentage, from /proc/meminfo (Linux)."""
    try:
        vals: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                vals[k.strip()] = int(rest.strip().split()[0])
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable", vals.get("MemFree", 0))
        if total <= 0:
            return -1.0
        return round(100.0 * (total - avail) / total, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def snapshot() -> dict:
    try:
        du = shutil.disk_usage(TARGET)
        used_pct = round(100.0 * du.used / du.total, 1)
        free_gib = round(du.free / 1024**3, 1)
        total_gib = round(du.total / 1024**3, 1)
    except Exception:  # noqa: BLE001
        used_pct, free_gib, total_gib = -1.0, -1.0, -1.0

    load = _load1()
    mem = _mem_used_pct()

    if used_pct >= 90 or (mem >= 90):
        tone, line = "error", f"{TARGET} is {used_pct}% full — act now"
    elif used_pct >= 75 or load >= LOAD_WARN or mem >= 75:
        tone, line = "warn", f"{TARGET} {used_pct}% full · load {load:.2f} · mem {mem}%"
    else:
        tone, line = "ok", f"{TARGET} {used_pct}% full · load {load:.2f} · mem {mem}%"

    return {
        # --- scalars (a source writes one of these into one field) ---
        "used_pct": used_pct,
        "tone": tone,
        "line": line,
        # --- composites (a source writes the whole list into one field) ---
        "tiles": [
            {"label": "Disk used", "value": f"{used_pct}%"},
            {"label": "Free", "value": f"{free_gib} GiB"},
            {"label": "Total", "value": f"{total_gib} GiB"},
            {"label": "Load 1m", "value": f"{load:.2f}"},
            {"label": "Mem used", "value": f"{mem}%"},
        ],
        "bars": [
            ["disk", used_pct],
            ["memory", mem],
        ],
        "detail": [
            ["Path", TARGET],
            ["Load 1m", f"{load:.2f}"],
            ["Memory used", f"{mem}%"],
        ],
    }


def main() -> int:
    print(json.dumps(snapshot()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
