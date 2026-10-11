#!/usr/bin/env python3
"""examples/pusher.py value-resolution — pure functions, no backend needed.

The pusher is shipped tooling agents copy, and it once had three real defects
with zero gate coverage: a source on a block type with no single-value field
invented a `value` field the backend 400'd the WHOLE board over; an overlong
extracted value did the same; and a failed source silently blanked a field that
had a last good value. Its value-resolution path is pure (`FIELD_OF`,
`MAX_VALUE`, `_extract`, `sample`, `build_board` — the only I/O a `file`/`command`
source needs is a temp file and `/bin/sh`-free argv), so it is pinned here
without a running Livewall.

Exits non-zero on any gap.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location("pusher", REPO / "examples" / "pusher.py")
pusher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pusher)

results: list[tuple[str, bool, str]] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:150]}")


def file_source(path: str, regex: str = "^(.*)$") -> dict:
    return {"kind": "file", "path": path, "extract": {"regex": regex}}


def write_tmp(text: str) -> str:
    fd, p = tempfile.mkstemp(prefix="lw-pusher-")
    with open(fd, "w") as f:
        f.write(text)
    return p


def test_field_of_mapping() -> None:
    case("FIELD_OF: single-value types map to their field",
         pusher.FIELD_OF.get("progress") == "value"
         and pusher.FIELD_OF.get("gauge") == "value"
         and pusher.FIELD_OF.get("stat") == "value"
         and pusher.FIELD_OF.get("text") == "text"
         and pusher.FIELD_OF.get("callout") == "text"
         and pusher.FIELD_OF.get("heading") == "text",
         f"{pusher.FIELD_OF}")
    # `stats` (and any structured type) must NOT be source-drivable: the backend
    # would refuse the invented `value` field and reject the whole board.
    case("FIELD_OF: a type with no single-value field is absent",
         "stats" not in pusher.FIELD_OF and "log" not in pusher.FIELD_OF
         and "bar" not in pusher.FIELD_OF and "table" not in pusher.FIELD_OF,
         f"leaked: {[t for t in ('stats', 'log', 'bar', 'table') if t in pusher.FIELD_OF]}")


def test_extract() -> None:
    v = pusher._extract('{"ok": true, "n": 3}', {"path": "$.ok"})
    case("_extract dotted path", v is True, f"got {v!r}")
    v = pusher._extract("used 73% of /", {"regex": "([0-9]+)%"})
    case("_extract regex first group", v == "73", f"got {v!r}")
    v = pusher._extract("plain", {"regex": "plain"})
    case("_extract regex without groups -> whole match", v == "plain", f"got {v!r}")
    try:
        pusher._extract("nope", {"regex": "zzz"})
        case("_extract unmatched regex raises", False, "no error raised")
    except ValueError as e:
        case("_extract unmatched regex raises", "did not match" in str(e), str(e))
    try:
        pusher._extract("x", {})
        case("_extract without path/regex raises", False, "no error raised")
    except ValueError:
        case("_extract without path/regex raises", True)


def test_sample_coercion_and_bound() -> None:
    # Numeric types coerce through float; integral floats become ints.
    val, err = pusher.sample({"type": "progress", "source": file_source(write_tmp("42.0"))}, "")
    case("sample: progress coerces '42.0' -> int 42", err is None and val == 42,
         f"val={val!r} err={err}")
    val, err = pusher.sample({"type": "progress", "source": file_source(write_tmp("42.5"))}, "")
    case("sample: progress keeps a fractional value", err is None and val == 42.5,
         f"val={val!r} err={err}")
    # A non-numeric value on a numeric block is a source error, not a crash.
    val, err = pusher.sample({"type": "progress", "source": file_source(write_tmp("junk"))}, "")
    case("sample: non-numeric on a numeric block is a source error",
         val is None and err is not None and "float" in err, f"err={err!r}")
    # A bool is coerced to a string, never pushed as JSON true/false.
    val, err = pusher.sample(
        {"type": "stat", "source": file_source(write_tmp("true"), "^(true)$")}, "")
    case("sample: bool -> 'true' string", err is None and val == "true", f"val={val!r}")

    # The MAX_VALUE bound: the backend refuses any scalar over 8192 chars and
    # would 400 the WHOLE board — an overlong extraction must fail THIS source,
    # not the push.
    overlong = pusher.MAX_VALUE + 1
    val, err = pusher.sample(
        {"type": "text", "source": file_source(write_tmp("x" * overlong))}, "")
    case(f"sample: a {overlong}-char value is a source error, not a board-killer",
         val is None and err is not None and str(pusher.MAX_VALUE) in err, f"err={err!r}")

    # An unknown kind is a source error naming the kind.
    val, err = pusher.sample({"type": "stat", "source": {"kind": "carrier-pigeon"}}, "")
    case("sample: unknown kind is a source error", val is None and "carrier-pigeon" in (err or ""),
         f"err={err!r}")


def test_build_board() -> None:
    # (1) the invented-field defect: a source on `stats` must NOT invent a
    # `value` field — the block is pushed as written and the failure is named.
    good_path = write_tmp("37")
    cfg = {"board": "b", "title": "T", "blocks": [
        {"id": "ok", "type": "progress", "label": "ok", "max": 100,
         "source": file_source(good_path)},
        {"id": "bad", "type": "stats",
         "items": [{"label": "n", "value": 1}],
         "source": file_source(good_path)},
    ]}
    log: list[str] = []
    board, ok_count, err_count = pusher.build_board(cfg, "", log)
    stats = next(b for b in board["blocks"] if b.get("id") == "bad")
    case("build_board: a source on `stats` invents NO field",
         "value" not in stats and "items" in stats
         and stats["items"] == [{"label": "n", "value": 1}], f"block={stats}")
    case("build_board: the no-single-value-field source is a NAMED error",
         err_count == 1 and ok_count == 1 and any("stats" in line for line in log),
         f"ok={ok_count} err={err_count} log={log}")
    case("build_board: a named source error does not doom the board status",
         board["status"] == "running", f"status={board['status']!r}")

    # (2) the overlong defect: one oversized source must fail ALONE.
    cfg2 = {"board": "b", "title": "T", "blocks": [
        {"id": "fine", "type": "text", "source": file_source(write_tmp("short"))},
        {"id": "huge", "type": "text", "source": file_source(write_tmp("y" * 9000))},
    ]}
    log2: list[str] = []
    board2, ok2, err2 = pusher.build_board(cfg2, "", log2)
    by_id = {b.get("id"): b for b in board2["blocks"]}
    case("build_board: an overlong value fails its source, not the board",
         err2 == 1 and ok2 == 1
         and by_id["fine"].get("text") == "short"
         and by_id["huge"].get("text") != "y" * 9000,
         f"ok={ok2} err={err2} fine={by_id['fine'].get('text')!r}")

    # (3) a failing source keeps the last good value instead of writing a blank.
    blk = {"id": "p", "type": "progress", "label": "p", "max": 100,
           "source": file_source(good_path)}
    cfg3 = {"board": "b", "title": "T", "blocks": [
        blk,
        {"id": "log", "type": "log", "title": "poller.log", "lines": []},
    ]}
    board3, ok3, _ = pusher.build_board(cfg3, "", [])
    blk["source"] = file_source("/no/such/file/anywhere")  # the source dies
    board4, ok4, err4 = pusher.build_board(cfg3, "", [])
    kept = next(b for b in board4["blocks"] if b.get("id") == "p")
    case("build_board: a dead source keeps the last good value",
         ok3 == 1 and ok4 == 0 and err4 == 1 and kept.get("value") == 37,
         f"first ok={ok3}, then ok={ok4} err={err4}, kept={kept.get('value')!r}")

    # (4) the poller.log block receives the run's log lines.
    case("build_board: a log block carries the poller log",
         any(b.get("type") == "log" and b.get("lines") for b in board4["blocks"]),
         f"log lines={[b.get('lines') for b in board4['blocks'] if b.get('type') == 'log']}")

    # (5) the pusher's board payload is contract-legal: the backend rejects an
    # invented field, so the shape here is exactly what set_board accepts
    # (verified structurally — every key of every block is in the contract).
    sys.path.insert(0, str(REPO / "backend"))
    import contract
    legal = True
    detail = ""
    for b in board4["blocks"] + board["blocks"]:
        t = b.get("type")
        if t not in contract.BLOCK_FIELDS:
            legal, detail = False, f"unknown type {t!r}"
            break
        extra = set(b) - contract.BLOCK_FIELDS[t] - {"id", "type"}
        if extra:
            legal, detail = False, f"{t}: invented fields {sorted(extra)}"
            break
    case("build_board: every emitted block carries only contract fields", legal, detail)


def main() -> int:
    test_field_of_mapping()
    test_extract()
    test_sample_coercion_and_bound()
    test_build_board()
    gaps = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ==")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
