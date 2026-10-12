#!/usr/bin/env python3
"""examples/pusher.py value-resolution — pure functions, no backend needed.

The pusher is shipped tooling agents copy, and it once had three real defects
with zero gate coverage: a source on a block type with no single-value field
invented a `value` field the backend 400'd the WHOLE board over; an overlong
extracted value did the same; and a failed source silently blanked a field that
had a last good value. Its value-resolution path is pure (`FIELD_OF`,
`COMPOSITE_FIELDS`, `MAX_VALUE`, `MAX_ITEMS`, `_extract`, `sample`, `_block_sources`,
`_resolve_tab`, `build_board` — the only I/O a `file`/`command` source needs is a
temp file and `/bin/sh`-free argv), so it is pinned here without a running Livewall.

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


def json_source(path: str, path_expr: str = "$") -> dict:
    """A source whose value is parsed JSON — how a composite (list) source is
    pulled: a `path` extract, not a regex (a regex yields the raw string)."""
    return {"kind": "file", "path": path, "extract": {"path": path_expr}}


def write_tmp(text: str) -> str:
    fd, p = tempfile.mkstemp(prefix="lw-pusher-")
    with open(fd, "w") as f:
        f.write(text)
    return p


def test_field_of_mapping() -> None:
    # EVERY block type with a field is drivable; only `divider` has none.
    expected = {
        "heading": "text", "text": "text", "code": "text",
        "stat": "value", "progress": "value", "gauge": "value",
        "callout": "text",
        "stats": "items", "kv": "items", "list": "items",
        "bar": "data", "table": "rows",
        "sparkline": "values", "chart": "values",
        "line": "series", "log": "lines",
    }
    case("FIELD_OF: every type with a field is source-drivable",
         pusher.FIELD_OF == expected,
         f"missing={sorted(set(expected) - set(pusher.FIELD_OF))} "
         f"extra={sorted(set(pusher.FIELD_OF) - set(expected))}")
    case("FIELD_OF: divider (no fields) is absent",
         "divider" not in pusher.FIELD_OF, f"{pusher.FIELD_OF}")
    # Composite targets must be exactly the list-valued fields we advertise.
    composite = {f for f in pusher.FIELD_OF.values()
                 if f in pusher.COMPOSITE_FIELDS}
    case("COMPOSITE_FIELDS: covers items/data/rows/values/series/lines",
         {"items", "data", "rows", "values", "series", "lines"}
         <= pusher.COMPOSITE_FIELDS, f"{sorted(pusher.COMPOSITE_FIELDS)}")


def test_extract() -> None:
    v = pusher._extract('{"ok": true, "n": 3}', {"path": "$.ok"})
    case("_extract dotted path", v is True, f"got {v!r}")
    v = pusher._extract("used 73% of /", {"regex": "([0-9]+)%"})
    case("_extract regex first group", v == "73", f"got {v!r}")
    v = pusher._extract("plain", {"regex": "plain"})
    case("_extract regex without groups -> whole match", v == "plain", f"got {v!r}")
    # A path into a list is how a composite source is pulled.
    v = pusher._extract('{"items": [1, 2, 3]}', {"path": "$.items"})
    case("_extract path into a list", v == [1, 2, 3], f"got {v!r}")
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
    val, err = pusher.sample(file_source(write_tmp("42.0")), "value", "progress", "")
    case("sample: progress coerces '42.0' -> int 42", err is None and val == 42,
         f"val={val!r} err={err}")
    val, err = pusher.sample(file_source(write_tmp("42.5")), "value", "progress", "")
    case("sample: progress keeps a fractional value", err is None and val == 42.5,
         f"val={val!r} err={err}")
    # A non-numeric value on a numeric block is a source error, not a crash.
    val, err = pusher.sample(file_source(write_tmp("junk")), "value", "progress", "")
    case("sample: non-numeric on a numeric block is a source error",
         val is None and err is not None and "float" in err, f"err={err!r}")
    # A bool is coerced to a string, never pushed as JSON true/false.
    val, err = pusher.sample(
        file_source(write_tmp("true"), "^(true)$"), "value", "stat", "")
    case("sample: bool -> 'true' string", err is None and val == "true", f"val={val!r}")
    # ...but on a NUMERIC block a bool is an error, not a "true" string the
    # backend's `numeric` kind would 400 the WHOLE board over. (A JSON bool
    # only arrives via a `path` extract; a regex always yields a string.)
    val, err = pusher.sample(json_source(write_tmp("true")), "value", "progress", "")
    case("sample: a JSON bool on a numeric block is a source error",
         val is None and err is not None and "bool" in err, f"err={err!r}")

    # The backend refuses a number it cannot store exactly (MAX_SAFE_INT, 2**53)
    # — a legal-JSON giant must fail THIS source, not the push.
    val, err = pusher.sample(json_source(write_tmp(str(10**400))), "value", "stat", "")
    case("sample: a 10**400 scalar is a source error, not a board-killer",
         val is None and err is not None and str(pusher.MAX_SAFE_INT) in err,
         f"err={err!r}")
    # An unquoted 1e400 parses to float inf via a `path` extract — not a
    # renderable number, so a source error. (A quoted "1e400" is a legal
    # scalar STRING the backend accepts; only the float is refused.)
    val, err = pusher.sample(json_source(write_tmp("1e400")), "value", "stat", "")
    case("sample: a non-finite float scalar is a source error",
         val is None and err is not None and "finite" in err, f"err={err!r}")

    # The MAX_VALUE bound: the backend refuses any scalar over 8192 chars and
    # would 400 the WHOLE board — an overlong extraction must fail THIS source,
    # not the push.
    overlong = pusher.MAX_VALUE + 1
    val, err = pusher.sample(file_source(write_tmp("x" * overlong)), "text", "text", "")
    case(f"sample: a {overlong}-char value is a source error, not a board-killer",
         val is None and err is not None and str(pusher.MAX_VALUE) in err, f"err={err!r}")

    # An unknown kind is a source error naming the kind.
    val, err = pusher.sample({"kind": "carrier-pigeon"}, "value", "stat", "")
    case("sample: unknown kind is a source error", val is None and "carrier-pigeon" in (err or ""),
         f"err={err!r}")

    # A scalar field refuses a list; a composite field refuses a scalar. Both are
    # named errors, so a shape mismatch can never reach the backend.
    val, err = pusher.sample(json_source(write_tmp('["a", "b"]')), "value", "stat", "")
    case("sample: a list into a scalar field is a source error",
         val is None and err is not None and "scalar" in err, f"err={err!r}")
    val, err = pusher.sample(file_source(write_tmp("just text")), "items", "stats", "")
    case("sample: a scalar into a composite field is a source error",
         val is None and err is not None and "JSON list" in err, f"err={err!r}")

    # A composite source takes a whole list of objects — the shape a stats/bar
    # source actually produces.
    body = json.dumps([{"label": "Memories", "value": 5371}])
    val, err = pusher.sample(json_source(write_tmp(body)), "items", "stats", "")
    case("sample: a composite field takes a list of objects",
         err is None and val == [{"label": "Memories", "value": 5371}],
         f"val={val!r} err={err}")

    # The MAX_ITEMS bound: a runaway array is a source error, not a failed push.
    big = json.dumps(list(range(pusher.MAX_ITEMS + 1)))
    val, err = pusher.sample(json_source(write_tmp(big)), "values", "sparkline", "")
    case(f"sample: a {pusher.MAX_ITEMS + 1}-entry list is a source error",
         val is None and err is not None and str(pusher.MAX_ITEMS) in err, f"err={err!r}")


def test_entry_validation() -> None:
    """Composite entries are checked against the shapes the backend's
    validator enforces — it 400s the WHOLE board over one junky entry, so each
    of these must be a named source error, never a pushed board."""
    def sampled(text: str, field: str, btype: str):
        return pusher.sample(json_source(write_tmp(text)), field, btype, "")

    # legal shapes pass: pairs into kv/bar, objects into stats, scalars into a
    # list, numbers (or numeric strings, which the backend accepts) into values
    val, err = sampled('[["k", "v"], {"k": "k2", "v": 2}]', "items", "kv")
    case("entry: kv pairs and {k,v} objects pass", err is None, f"err={err}")
    val, err = sampled('[["mem", 5], {"label": "n", "value": 0}]', "data", "bar")
    case("entry: bar pairs pass, value must be numeric-ok", err is None, f"err={err}")
    val, err = sampled('["a", "b", 3]', "items", "list")
    case("entry: scalar items pass", err is None, f"err={err}")
    val, err = sampled('["1", 2.5, 3]', "values", "sparkline")
    case("entry: numeric strings pass in a num list (backend accepts them)",
         err is None, f"err={err}")
    val, err = sampled('[{"name": "p50", "values": [1, 2]}, {"values": [3, 4]}]',
                       "series", "line")
    case("entry: line series pass", err is None, f"err={err}")
    # xLabels is a LIST field too (scalar_list) — a bare string there would be
    # pushed as a string the backend's scalar_list kind refuses.
    val, err = sampled('["mon", "tue"]', "xLabels", "line")
    case("entry: xLabels takes a list of scalars", err is None, f"err={err}")
    val, err = sampled('"mon"', "xLabels", "line")
    case("entry: a bare string into xLabels is a source error",
         val is None and err is not None and "JSON list" in err, f"err={err!r}")

    # junk entries are named errors, each one a shape the backend would 400
    # the WHOLE board over.
    for name, text, field, btype, want in [
        ("a list where an object belongs", "[[1, 2]]", "items", "stats", "object"),
        ("a null entry", '[{"label": "a"}, null]', "items", "stats", "object"),
        ("a bool inside a kv pair", '[["k", true]]', "items", "kv", "bool"),
        ("a string where a number belongs", '["abc", "def"]', "values",
         "sparkline", "number"),
        ("dicts where cell lists belong", '[{"a": 1}]', "rows", "table", "list"),
    ]:
        val, err = sampled(text, field, btype)
        case(f"entry: {name} is a source error",
             val is None and err is not None and want in err, f"err={err!r}")

    # overlong entries, part 2: the backend caps EVERY string (MAX_TEXT_LEN),
    # including ones inside entries — an 8193-char label must fail the source.
    overlong = json.dumps([{"label": "x" * (pusher.MAX_VALUE + 1), "value": 1}])
    val, err = sampled(overlong, "items", "stats")
    case("entry: an overlong label inside a stats item is a source error",
         val is None and err is not None and str(pusher.MAX_VALUE) in err,
         f"err={err!r}")

    # line series: at least one series, every series >= 2 points, all equal.
    val, err = sampled('[{"values": [1]}]', "series", "line")
    case("entry: a 1-point series is a source error",
         val is None and err is not None and "2 points" in err, f"err={err!r}")
    val, err = sampled('[{"values": [1, 2]}, {"values": [1, 2, 3]}]', "series", "line")
    case("entry: series of unequal length are a source error",
         val is None and err is not None and "lengths differ" in err, f"err={err!r}")
    val, err = sampled("[]", "series", "line")
    case("entry: an empty series list is a source error",
         val is None and err is not None and "at least one series" in err,
         f"err={err!r}")

    # The composite byte cap: 7 astral-char strings x 8192 chars stay under the
    # per-string cap but json.dumps inflates each char to a 12-char surrogate
    # escape (~688 KB) — over MAX_COMPOSITE_BYTES, still under the 64 KiB raw
    # read cap, so the named byte-cap error itself is what fires.
    emoji = "\U0001F680" * pusher.MAX_VALUE
    raw = "[" + ",".join('"' + emoji + '"' for _ in range(7)) + "]"
    val, err = pusher.sample(
        {"kind": "file", "path": write_tmp(raw), "extract": {"path": "$"}},
        "items", "list", "")
    case("sample: an escaped-inflation composite is a byte-cap source error",
         val is None and err is not None and str(pusher.MAX_COMPOSITE_BYTES) in err,
         f"err={err!r}")

    # The 64 KiB raw read cap is load-bearing for every composite bound above:
    # a bigger body must be a named source error (truncated JSON), never a
    # partial value silently pushed.
    val, err = pusher.sample(
        file_source(write_tmp("x" * (pusher.MAX_OUTPUT + 1))),
        "text", "text", "")
    case("sample: an over-MAX_OUTPUT source body is a source error",
         val is None and err is not None, f"err={err!r}")


def test_block_sources() -> None:
    # `source` binds the type's default field; `field` overrides it.
    src = file_source("/tmp/x")
    got = pusher._block_sources({"type": "stat", "source": src})
    case("_block_sources: source -> the type's default field",
         got == {"value": src}, f"{got}")
    got = pusher._block_sources({"type": "stat", "field": "unit", "source": src})
    case("_block_sources: an explicit `field` overrides the default",
         got == {"unit": src}, f"{got}")
    # `sources` drives any field by name; both forms can coexist.
    got = pusher._block_sources({"type": "callout", "source": src,
                                 "sources": {"tone": src}})
    case("_block_sources: `sources` binds named fields alongside `source`",
         got == {"text": src, "tone": src}, f"{got}")
    # A source on a type with no field at all is an error, not a silent skip.
    try:
        pusher._block_sources({"type": "divider", "source": src})
        case("_block_sources: a source on a field-less type raises", False, "no error")
    except ValueError as e:
        case("_block_sources: a source on a field-less type raises",
             "no field" in str(e), str(e))
    # A field the block's type does not allow would be an INVENTED field the
    # backend refuses, 400ing the WHOLE board — a named error instead. This
    # covers both an invented `sources` key and a bogus `field` override.
    for bad in [{"type": "stat", "sources": {"bogus": src}},
                {"type": "text", "field": "value", "source": src}]:
        try:
            pusher._block_sources(bad)
            case("_block_sources: an invented field raises", False, "no error")
        except ValueError as e:
            case("_block_sources: an invented field raises",
                 "has no field" in str(e), str(e))
    # A malformed `sources`/`source` is a named error, never a silent skip.
    for bad in [{"type": "stat", "sources": {"value": "warn"}},
                {"type": "stat", "sources": "value"},
                {"type": "stat", "source": "value"}]:
        try:
            pusher._block_sources(bad)
            case("_block_sources: a malformed source raises", False, "no error")
        except ValueError as e:
            case("_block_sources: a malformed source raises",
                 "must be a source" in str(e) or "map" in str(e), str(e))


def test_resolve_tab() -> None:
    log: list[str] = []
    tab = pusher._resolve_tab(
        {"label": "h", "accent": "violet",
         "indicator": {"kind": "bar", "value": 0, "max": 100,
                       "source": file_source(write_tmp("37"))}},
        "", log)
    case("_resolve_tab: a live indicator source becomes a value",
         tab["indicator"]["value"] == 37 and "source" not in tab["indicator"],
         f"{tab}")
    # The config's own tab dict must not be mutated (the pusher reuses cfg each tick).
    cfg_tab = {"label": "h", "indicator": {"kind": "bar", "value": 0, "max": 100,
                                           "source": file_source(write_tmp("1"))}}
    pusher._resolve_tab(cfg_tab, "", [])
    case("_resolve_tab: the config tab is not mutated in place",
         "source" in cfg_tab["indicator"], f"cfg tab after call: {cfg_tab}")
    # A dead indicator source leaves the placeholder and names the error.
    log2: list[str] = []
    tab2 = pusher._resolve_tab(
        {"label": "h", "indicator": {"kind": "bar", "value": 5, "max": 10,
                                     "source": file_source("/no/such/file")}},
        "", log2)
    case("_resolve_tab: a dead indicator source keeps the placeholder",
         tab2["indicator"]["value"] == 5 and any("tab.indicator" in l for l in log2),
         f"tab={tab2} log={log2}")
    # A JSON bool resolves to the string "true", which the backend's tab
    # validator refuses — it must be a named error, not a pushed value.
    log3: list[str] = []
    tab3 = pusher._resolve_tab(
        {"label": "h", "indicator": {"kind": "bar", "value": 0, "max": 100,
                                     "source": json_source(write_tmp("true"))}},
        "", log3)
    case("_resolve_tab: a non-numeric resolved value keeps the placeholder",
         tab3["indicator"]["value"] == 0 and any("tab.indicator" in l for l in log3),
         f"tab={tab3} log={log3}")
    # A dead source with NO numeric placeholder would push a value-less bar
    # indicator the backend refuses — the indicator is dropped instead.
    tab4 = pusher._resolve_tab(
        {"label": "h", "indicator": {"kind": "bar", "max": 100,
                                     "source": file_source("/no/such/file")}},
        "", [])
    case("_resolve_tab: a dead source with no placeholder drops the indicator",
         "indicator" not in tab4 and tab4.get("label") == "h", f"tab={tab4}")
    case("_resolve_tab: no tab -> None", pusher._resolve_tab(None, "", []) is None)


def test_build_board() -> None:
    # (1) a composite source drives a whole list field (stats/bar/table), which
    # the OLD pusher refused outright — this is the "every object drivable" rule.
    good_path = write_tmp("37")
    items_body = write_tmp(json.dumps([{"label": "Memories", "value": 5371},
                                       {"label": "Nodes", "value": 79123}]))
    cfg = {"board": "b", "title": "T", "blocks": [
        {"id": "ok", "type": "progress", "label": "ok", "max": 100,
         "source": file_source(good_path)},
        {"id": "tiles", "type": "stats", "items": [],
         "source": json_source(items_body)},
    ]}
    log: list[str] = []
    board, ok_count, err_count = pusher.build_board(cfg, "", log)
    tiles = next(b for b in board["blocks"] if b.get("id") == "tiles")
    case("build_board: a composite source drives a stats block",
         err_count == 0 and ok_count == 2
         and tiles["items"] == [{"label": "Memories", "value": 5371},
                                {"label": "Nodes", "value": 79123}],
         f"ok={ok_count} err={err_count} items={tiles.get('items')}")

    # (2) `sources` drives several fields of one block at once.
    cfg2 = {"board": "b", "title": "T", "blocks": [
        {"id": "c", "type": "callout", "text": "…", "tone": "info",
         "sources": {"text": file_source(write_tmp("all good")),
                     "tone": file_source(write_tmp("ok"))}},
    ]}
    log2: list[str] = []
    board2, ok2, err2 = pusher.build_board(cfg2, "", log2)
    c = board2["blocks"][0]
    case("build_board: `sources` drives several fields of one block",
         err2 == 0 and ok2 == 2 and c["text"] == "all good" and c["tone"] == "ok",
         f"ok={ok2} err={err2} block={c}")

    # (3) a dead source keeps the LAST GOOD value per field (never a blank), and
    # a partial failure (one source dies, another lives) leaves the board running.
    blk = {"id": "p", "type": "progress", "label": "p", "max": 100,
           "source": file_source(good_path)}
    cfg3 = {"board": "b", "title": "T", "blocks": [
        blk,
        {"id": "fine", "type": "text", "source": file_source(write_tmp("alive"))},
        {"id": "log", "type": "log", "title": "poller.log", "lines": []},
    ]}
    board3, ok3, _ = pusher.build_board(cfg3, "", [])
    blk["source"] = file_source("/no/such/file/anywhere")  # the source dies
    board4, ok4, err4 = pusher.build_board(cfg3, "", [])
    kept = next(b for b in board4["blocks"] if b.get("id") == "p")
    case("build_board: a dead source keeps the last good value",
         ok3 == 2 and ok4 == 1 and err4 == 1 and kept.get("value") == 37,
         f"first ok={ok3}, then ok={ok4} err={err4}, kept={kept.get('value')!r}")
    case("build_board: a partial failure leaves the board running",
         board4["status"] == "running", f"status={board4['status']!r}")

    # ...and the board goes to `error` only when EVERY source failed.
    cfg_all_dead = {"board": "b", "title": "T", "blocks": [
        {"id": "d", "type": "progress", "label": "d", "max": 100,
         "source": file_source("/no/such/file/anywhere")},
    ]}
    board_dead, ok_dead, err_dead = pusher.build_board(cfg_all_dead, "", [])
    case("build_board: every source failed -> board status error",
         ok_dead == 0 and err_dead == 1 and board_dead["status"] == "error",
         f"ok={ok_dead} err={err_dead} status={board_dead['status']!r}")

    # (4) the poller.log block receives the run's log lines.
    case("build_board: a log block carries the poller log",
         any(b.get("type") == "log" and b.get("lines") for b in board4["blocks"]),
         f"log lines={[b.get('lines') for b in board4['blocks'] if b.get('type') == 'log']}")

    # (5) a source on a field-less type is a NAMED error and does not doom the board.
    cfg5 = {"board": "b", "title": "T", "blocks": [
        {"id": "fine", "type": "text", "source": file_source(write_tmp("hi"))},
        {"id": "bad", "type": "divider", "source": file_source(write_tmp("hi"))},
    ]}
    log5: list[str] = []
    board5, ok5, err5 = pusher.build_board(cfg5, "", log5)
    case("build_board: a field-less source is a NAMED error, board still running",
         err5 == 1 and ok5 == 1 and any("divider" in l for l in log5)
         and board5["status"] == "running",
         f"ok={ok5} err={err5} log={log5} status={board5['status']!r}")

    # (6) the documented `field` override steers the source but must NOT leak
    # into the pushed block — no type allows a `field` key, so a leaked one
    # would 400 the WHOLE board.
    cfg6 = {"board": "b", "title": "T", "blocks": [
        {"id": "s", "type": "stat", "label": "L", "field": "unit",
         "source": file_source(write_tmp("ms"))},
    ]}
    board6, ok6, err6 = pusher.build_board(cfg6, "", [])
    s6 = board6["blocks"][0]
    case("build_board: a `field` override does not leak into the block",
         err6 == 0 and s6.get("unit") == "ms" and "field" not in s6, f"block={s6}")

    # (7) `sources` naming a field the type does not allow is a NAMED error;
    # the invented field never reaches the block.
    cfg7 = {"board": "b", "title": "T", "blocks": [
        {"id": "s", "type": "stat", "label": "L",
         "sources": {"bogus": file_source(write_tmp("x"))}},
        {"id": "log", "type": "log", "lines": []},
    ]}
    log7: list[str] = []
    board7, ok7, err7 = pusher.build_board(cfg7, "", log7)
    s7 = next(b for b in board7["blocks"] if b.get("id") == "s")
    case("build_board: an invented `sources` field is a NAMED error, never pushed",
         err7 == 1 and "bogus" not in s7 and board7["status"] == "error"
         and any("bogus" in l for l in log7), f"block={s7} log={log7}")

    # (8) a junky composite (entries the backend's validator would refuse) is a
    # named error: the block keeps its static placeholder, the board stays up.
    cfg8 = {"board": "b", "title": "T", "blocks": [
        {"id": "tiles", "type": "stats", "items": [{"label": "static", "value": 1}],
         "source": json_source(write_tmp("[[1, 2], null]"))},
        {"id": "log", "type": "log", "lines": []},
    ]}
    log8: list[str] = []
    board8, ok8, err8 = pusher.build_board(cfg8, "", log8)
    t8 = next(b for b in board8["blocks"] if b.get("id") == "tiles")
    case("build_board: junky composite entries are a NAMED error, placeholder kept",
         err8 == 1 and t8["items"] == [{"label": "static", "value": 1}]
         and any("must be an object" in l for l in log8),
         f"items={t8.get('items')} log={log8}")

    # (9) a log block whose own `lines` a source drives keeps those — the
    # poller log must not clobber a value a source just successfully wrote.
    cfg9 = {"board": "b", "title": "T", "blocks": [
        {"id": "log", "type": "log", "title": "x", "lines": [],
         "source": json_source(write_tmp('["line one", "line two"]'))},
    ]}
    board9, ok9, err9 = pusher.build_board(cfg9, "", ["[00:00:00] poller line"])
    log9 = board9["blocks"][0]
    case("build_board: a source-driven log block keeps its driven lines",
         err9 == 0 and log9.get("lines") == ["line one", "line two"],
         f"lines={log9.get('lines')!r}")

    # (10) a dead tab-indicator source with no numeric placeholder still
    # yields a contract-legal tab (the indicator is dropped, the error named).
    cfg10 = {"board": "b", "title": "T",
             "tab": {"label": "h", "accent": "violet",
                     "indicator": {"kind": "bar", "max": 100,
                                   "source": file_source("/no/such/file")}},
             "blocks": [{"id": "log", "type": "log", "lines": []}]}
    log10: list[str] = []
    board10, _, _ = pusher.build_board(cfg10, "", log10)
    case("build_board: a dead indicator with no placeholder drops the indicator",
         "indicator" not in board10.get("tab", {})
         and any("tab.indicator" in l for l in log10),
         f"tab={board10.get('tab')} log={log10}")

    # (11) the pusher's board payload is contract-legal: the backend rejects
    # an invented field or a junky entry with a 400 that dooms the WHOLE
    # board, so every board this test builds is run through the REAL
    # validator — blocks and tab — not just a key-set sketch.
    sys.path.insert(0, str(REPO / "backend"))
    import contract
    legal = True
    detail = ""
    for bi, board in enumerate((board, board2, board4, board5, board6, board7,
                                board8, board9, board10)):
        try:
            contract.validate_blocks(board["blocks"])
            contract.validate_tab(board.get("tab"))
        except contract.ContractError as e:
            legal, detail = False, f"board {bi}: {e}"
            break
    case("build_board: every emitted board passes the backend's own validator",
         legal, detail)

    # (12) the pusher's mirrors of the backend's vocabulary cannot drift: a
    # field the backend gains (or loses) must change the pusher's whitelists
    # in the same change, or a source path re-opens a 400 hole.
    case("TYPE_FIELDS mirrors the backend's BLOCK_FIELDS",
         pusher.TYPE_FIELDS == {t: set(f) for t, f in contract.BLOCK_FIELDS.items()},
         "differs" if pusher.TYPE_FIELDS != {
             t: set(f) for t, f in contract.BLOCK_FIELDS.items()} else "")
    list_kinds = {"stats_items", "bar_data", "line_series", "kv_items",
                  "scalar_list", "num_list", "rows"}
    missing = [(t, f) for t, fields in contract._FIELD_KINDS.items()
               for f, k in fields.items()
               if k in list_kinds and (t, f) not in pusher.ENTRY_KINDS]
    case("ENTRY_KINDS covers every list-valued field the contract defines",
         not missing, f"missing={missing}")


def main() -> int:
    test_field_of_mapping()
    test_extract()
    test_sample_coercion_and_bound()
    test_entry_validation()
    test_block_sources()
    test_resolve_tab()
    test_build_board()
    gaps = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ==")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
