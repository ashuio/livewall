"""Edge-case sweep over the Livewall API.

Reports PASS/FAIL per case; any FAIL is a gap. Point it at a running backend with
LIVEWALL_URL. The default is a deliberately DEAD port (http://127.0.0.1:1), so a
forgotten LIVEWALL_URL fails loudly instead of driving a real backend.

Hermeticity:
  * The suite snapshots /api/config at start and restores it exactly in a
    `finally`, so a run cannot leave max_boards/default_ttl mutated.
  * It deletes every board at start and in the `finally`, so it leaves no
    leftovers and its scale test starts from a known empty store. That is
    destructive by design: run it against a DEDICATED test backend, not a shared
    one.
  * It exits non-zero if any case is a gap (previously it always exited 0).
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

# Deliberately DEAD default: a bare `python backend/tests/edge_test.py` must not
# drive a real backend on the product port 8470 — this suite deletes every board.
BASE = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:1").rstrip("/")

CONFIG_KEYS = ("default_ttl", "stale_ttl", "max_boards")


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"err": str(e)}


def call_raw(method, path, data, ctype="application/json"):
    """Send a raw body (to exercise malformed / non-object JSON)."""
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"err": str(e)}


results = []


def case(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:110]}")


def delete_all_boards():
    for b in call("GET", "/api/boards")[1].get("boards", []):
        call("DELETE", f"/api/boards/{b['id']}")


def snapshot_config():
    """The server's config BEFORE the suite touches it, or {} if unreadable."""
    s, cfg = call("GET", "/api/config")
    if s != 200 or not isinstance(cfg, dict):
        return {}
    return {k: cfg[k] for k in CONFIG_KEYS if k in cfg}


def main():
    # Fail loudly on a dead/mistyped target before this suite deletes any board.
    s, _ = call("GET", "/health")
    if s != 200:
        print(f"no Livewall backend at {BASE} (GET /health -> {s}).")
        print("  set LIVEWALL_URL to a dedicated test backend, e.g.")
        print(f"  LIVEWALL_URL=http://127.0.0.1:8470 python {sys.argv[0]}")
        print("  (the default is a dead port so a bare run cannot touch a real one)")
        return 2
    # Start state: remember the config so we can restore it exactly, and give the
    # scale test a deterministic cap regardless of how the server was launched.
    orig_config = snapshot_config()
    if orig_config:
        call("PUT", "/api/config", {"max_boards": 12})
    delete_all_boards()

    try:
        print("== A. scale: create 20 boards, max_boards should cap at 12 ==")
        for i in range(20):
            call("POST", f"/api/boards/scale-{i:02d}",
                 {"title": f"S{i}", "status": "running", "ttl": 3600, "blocks": []})
        n = len(call("GET", "/api/boards")[1]["boards"])
        case("scale cap enforced", n <= 12, f"{n} boards after creating 20 (max_boards=12)")

        print("\n== B. invalid input ==")
        s, b = call("POST", "/api/boards/bad", {"title": "x", "blocks": [{"type": "nope"}]})
        case("unknown block type refused at API",
             s == 400 and "unknown type" in b.get("error", "") and "progress" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        s, b = call("POST", "/api/boards/badlist", {"title": "x", "blocks": "nope"})
        case("non-list blocks refused",
             s == 400 and "must be a list" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badblk", {"title": "x", "blocks": [{"type": "text"}, "nope"]})
        case("non-object block refused",
             s == 400 and "must be an object" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/notype", {"title": "x", "blocks": [{"text": "hi"}]})
        case("block without type refused",
             s == 400 and "no `type`" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/dupid", {
            "title": "x", "blocks": [{"id": "a", "type": "text", "text": "1"},
                                     {"id": "a", "type": "text", "text": "2"}]})
        case("duplicate block id refused",
             s == 400 and "duplicate id" in b.get("error", ""), f"status {s}")

        # `id` must be a string: a non-string id used to be str()'d only for the
        # duplicate check and stored raw, so update_block(block_id="123") (what
        # the MCP sends) could never address it.
        s, b = call("POST", "/api/boards/intid",
                    {"title": "x", "blocks": [{"id": 123, "type": "text", "text": "x"}]})
        case("non-string block id refused",
             s == 400 and "id" in b.get("error", "") and "string" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # A field the client does not read must be refused at set_board exactly
        # as update_block refuses an unknown `set` key; otherwise resending the
        # whole board bypasses the stricter gate.
        s, b = call("POST", "/api/boards/bogusfield",
                    {"title": "x", "blocks": [{"id": "a", "type": "text",
                                               "text": "x", "bogus": 1}]})
        case("unknown block field refused",
             s == 400 and "unknown field" in b.get("error", "")
             and "bogus" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")

        many = [{"type": "text", "text": str(i)} for i in range(61)]
        s, b = call("POST", "/api/boards/many", {"title": "x", "blocks": many})
        case("block count cap refused",
             s == 400 and "too many blocks" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badstatus", {"title": "x", "status": "RUNNING", "blocks": []})
        case("unknown status refused",
             s == 400 and "status" in b.get("error", "") and "running" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # SECURITY: a block `color` is interpolated by the client into a CSS
        # `var(--accent-<name>)` token. A scalar-only check let a value like
        # `mint) url(//evil` append a url() to the declaration — valid CSS
        # (`background: <color> <image>`) — so every viewer's browser fetches
        # the attacker's URL. `color` is now accent-checked like `tab.accent`
        # always was.
        s, b = call("POST", "/api/boards/csssink", {
            "title": "x",
            "blocks": [{"id": "b", "type": "bar", "label": "L",
                        "color": "mint) url(http://127.0.0.1:8478/beacon",
                        "data": [["a", 1]]}]})
        case("hostile block color refused at set_board (CSS url() sink)",
             s == 400 and "accent" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")

        s, b = call("POST", "/api/boards/csssink2", {
            "title": "x",
            "blocks": [{"id": "g", "type": "gauge", "label": "G", "value": 1,
                        "max": 2, "color": "not-an-accent"}]})
        case("unknown accent name refused",
             s == 400 and "accent" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")

        s, b = call("POST", "/api/boards/okcolor", {
            "title": "x",
            "blocks": [{"id": "g", "type": "gauge", "label": "G", "value": 1,
                        "max": 2, "color": "Mint"}]})
        case("accent colour case-insensitively accepted",
             s == 200, f"status {s}")
        call("DELETE", "/api/boards/okcolor")

        call("POST", "/api/boards/setcolor", {
            "title": "x",
            "blocks": [{"id": "b", "type": "bar", "label": "L", "data": [["a", 1]]}]})
        s, b = call("POST", "/api/boards/setcolor/update",
                    {"id": "b", "set": {"color": "ok) url(//evil"}})
        case("hostile colour refused on update_block",
             s == 400 and "accent" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/setcolor")

        # A non-string status used to poison every later read (dict.get(list) raises).
        s, b = call("POST", "/api/boards/badstatustype", {"title": "x", "status": [], "blocks": []})
        s2, _ = call("GET", "/api/boards")
        case("non-string status refused without corrupting reads", s == 400 and s2 == 200,
             f"POST {s}, subsequent list {s2}")

        s, b = call("POST", "/api/boards/badtab", {"title": "x", "blocks": [], "tab": {"accent": "chartreuse"}})
        case("bad tab accent refused",
             s == 400 and "accent" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badtab2", {"title": "x", "blocks": [], "tab": {"indicator": {"kind": "sparkle"}}})
        case("bad indicator kind refused",
             s == 400 and "kind" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badtab3", {"title": "x", "blocks": [], "tab": {"indicator": {"kind": "bar"}}})
        case("bar indicator without value refused",
             s == 400 and "value" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badtabtype", {"title": "x", "blocks": [], "tab": "pink"})
        case("non-object tab refused",
             s == 400 and "tab" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/badtitle", {"title": 123, "blocks": []})
        case("non-string title refused",
             s == 400 and "title" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/longtitle", {"title": "x" * 201, "blocks": []})
        case("over-long title refused",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")

        s, b = call_raw("POST", "/api/boards/rawjson", b"{not json")
        case("malformed JSON body refused",
             s == 400 and "JSON" in b.get("error", ""), f"status {s}")

        s, b = call_raw("POST", "/api/boards/rawlist", b'["a"]')
        case("non-object JSON body refused",
             s == 400 and "object" in b.get("error", ""), f"status {s}")

        # Legal-but-undecodable JSON: a deeply nested array is syntactically
        # valid JSON, but json's C scanner raises RecursionError long before
        # MAX_BODY_BYTES (a ~120 kB body nests deep enough). RecursionError is
        # a RuntimeError, not a ValueError, so it escaped the malformed-body
        # handler and 500'd — a client must get the named 400 instead.
        s, b = call_raw("POST", "/api/boards/deepnest",
                        b"[" * 100_000 + b"]" * 100_000)
        case("undecodable (deeply nested) body -> 400, not 500",
             s == 400 and "JSON" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")

        s, b = call("POST", "/api/boards/neg", {"title": "x", "ttl": -5, "blocks": []})
        case("negative ttl refused",
             s == 400 and "positive" in b.get("error", ""), f"status {s}")

        # A fractional ttl was silently int()-truncated (2.9 stored as 2) and a
        # bool int()-coerced (true stored as 1) — both reported ok:true for a
        # DIFFERENT number than the caller sent, the exact silent coercion
        # _config_int refuses for the config keys ("never a different number
        # reported as success").
        for i, bad in enumerate((2.9, True)):
            s, b = call("POST", f"/api/boards/ttlbad{i}",
                        {"title": "x", "ttl": bad, "blocks": []})
            case(f"non-integer ttl {bad!r} refused (no silent coercion)",
                 s == 400 and "integer" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")
            call("DELETE", f"/api/boards/ttlbad{i}")
        # An integer-valued float (2.0) is an exact integer and stays accepted.
        s, b = call("POST", "/api/boards/ttlfloat", {"title": "x", "ttl": 2.0, "blocks": []})
        case("integer-valued float ttl accepted (2.0)", s == 200 and b["summary"]["ttl"] == 2,
             f"status {s}, ttl={b.get('summary',{}).get('ttl')}")
        call("DELETE", "/api/boards/ttlfloat")

        s, b = call("GET", "/api/boards/does-not-exist")
        case("missing board -> 404", s == 404, f"status {s}")
        # Every other refusal on the API carries {"ok": false, "error": ...};
        # this 404's body omitted the ok flag, so a client keying on `ok`
        # saw neither success nor failure.
        case("missing board 404 carries ok:false like every other refusal",
             s == 404 and b.get("ok") is False and "error" in b, f"body {b}")

        # An UNKNOWN /api path must answer in the refusal vocabulary too
        # (docs/api.md promises every refusal carries ok:false). The default
        # router shape ({"detail": "Not Found"}) and the static mount's 405
        # for a mistyped POST left a client keying on `ok` with a body that
        # was neither, and the 405 wrongly implied the endpoint existed with
        # other methods.
        for method, path in (("GET", "/api/nosuchpath"),
                             ("POST", "/api/boards1"),
                             ("GET", "/api/boards/x/update")):
            s, b = call(method, path)
            case(f"unknown endpoint {method} {path!r} -> 404 ok:false",
                 s == 404 and b.get("ok") is False and "no such endpoint" in b.get("error", ""),
                 f"status {s}, body {b}")
        s, b = call("GET", "/api/boards")
        case("real routes still served after the unknown-endpoint fallback",
             s == 200 and "boards" in b, f"status {s}, body keys {list(b)[:4]}")

        s, b = call("POST", "/api/boards/tabtext-dict",
                    {"title": "x", "blocks": [],
                     "tab": {"indicator": {"kind": "text", "text": {"a": 1}}}})
        case("non-string tab.indicator.text refused (a dict is not a label)",
             s == 400 and "text" in b.get("error", ""), f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/tabtext-dict")

        print("\n== C. update semantics ==")
        call("POST", "/api/boards/upd", {"title": "U", "status": "running", "ttl": 600,
             "blocks": [{"id": "p", "type": "progress", "label": "x", "value": 1, "max": 10},
                        {"id": "l", "type": "log", "lines": ["a"]},
                        {"id": "c", "type": "code", "text": "print(1)"},
                        {"id": "st", "type": "stats", "items": [{"label": "a", "value": 1}]}]})
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "set": {"value": 5}})
        case("update_block set", s == 200 and b["block"]["value"] == 5, f"status {s}")

        s, b = call("POST", "/api/boards/upd/update", {"id": "l", "append": "b"})
        case("update_block append", s == 200 and "b" in b["block"]["lines"], f"lines={b.get('block',{}).get('lines')}")

        # `append` is log-only; a non-log target is refused rather than no-oped.
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "append": "junk"})
        case("append to a non-log block refused", s == 400 and "only works" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # `code` renders its static `text`, not `lines`, so `append` would no-op
        # silently. It is refused instead, and this case guards that.
        s, b = call("POST", "/api/boards/upd/update", {"id": "c", "append": "junk"})
        case("append to a code block refused (client ignores lines)", s == 400 and "only works" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # Unknown `set` keys are refused.
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "set": {"bogus_field": 123}})
        case("unknown set field refused", s == 400 and "unknown field" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # `set` as a list used to reach `.items()` and 500.
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "set": ["value"]})
        case("non-object set refused", s == 400 and "set" in b.get("error", ""), f"status {s}")

        # `id` addresses the block; without it, `target` was None and the FIRST
        # anonymous (id-less) block matched `blk.get("id") == None` and was
        # mutated — a write with no target address silently succeeding. A
        # non-string id (e.g. 123) fell through to a misleading 404 instead.
        call("POST", "/api/boards/anonblk",
             {"title": "a", "blocks": [{"type": "text", "text": "orig"}]})
        s, b = call("POST", "/api/boards/anonblk/update", {"set": {"text": "HACKED"}})
        _, full = call("GET", "/api/boards/anonblk")
        anon_text = next((x.get("text") for x in full.get("blocks", [])), None)
        case("update without an id refused (nothing mutated)",
             s == 400 and "id" in b.get("error", "") and anon_text == "orig",
             f"status {s}, {b.get('error','')[:60]}, block text={anon_text!r}")
        s, b = call("POST", "/api/boards/anonblk/update", {"id": None, "set": {"text": "H2"}})
        case("update with id=null refused (400)", s == 400 and "id" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        s, b = call("POST", "/api/boards/anonblk/update", {"id": 123, "set": {"text": "H3"}})
        case("update with a non-string id refused (400)", s == 400 and "id" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/anonblk")

        # A `set` value is type-checked against the target block, so a hostile
        # value cannot be injected after creation. Without that check it reaches
        # the emitter, which 500s on a dict `items` and hands a dict `value` to
        # the client, which then drops the whole board's update silently.
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "set": {"value": {"a": 1}}})
        case("update_block set hostile value refused", s == 400 and "value" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        s, b = call("POST", "/api/boards/upd/update", {"id": "st", "set": {"items": {"a": 1}}})
        case("update_block set hostile items refused", s == 400 and "items" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        s, b = call("POST", "/api/boards/upd/update", {"id": "p", "set": {"max": 0}})
        case("update_block set max=0 refused", s == 400 and "max" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")

        # The unique-string-id invariant and the value contract must hold on the
        # update path too, not just set_board: a `set` that rewrites `id` must not
        # create a duplicate, and one that changes `type` must re-validate the
        # merged block under the NEW type.
        call("POST", "/api/boards/upd2", {"title": "U2", "status": "running", "ttl": 600,
             "blocks": [{"id": "a", "type": "text", "text": "one"},
                        {"id": "b", "type": "text", "text": "two"},
                        {"id": "s", "type": "stat", "label": "n", "value": 1}]})
        s, b = call("POST", "/api/boards/upd2/update", {"id": "b", "set": {"id": "a"}})
        _, full = call("GET", "/api/boards/upd2")
        case("update_block set id to a duplicate refused",
             s == 400 and "unique" in b.get("error", "")
             and [x.get("id") for x in full.get("blocks", [])] == ["a", "b", "s"],
             f"status {s}, ids={[x.get('id') for x in full.get('blocks', [])]}")
        # A no-op `set id` on the same value must NOT be refused.
        s, b = call("POST", "/api/boards/upd2/update", {"id": "a", "set": {"id": "a"}})
        case("update_block set id to itself allowed", s == 200, f"status {s}")
        # stat.value is scalar; progress.value is numeric. Changing the type in
        # the same `set` used to pass the old scalar check and store "abc".
        s, b = call("POST", "/api/boards/upd2/update",
                    {"id": "s", "set": {"type": "progress", "value": "abc"}})
        _, full = call("GET", "/api/boards/upd2")
        sblk = next((x for x in full.get("blocks", []) if x.get("id") == "s"), {})
        case("update_block type change re-validates under the new type",
             s == 400 and "value" in b.get("error", "") and sblk.get("type") == "stat",
             f"status {s}, block={sblk}")
        call("DELETE", "/api/boards/upd2")

        print("\n== D. status transitions + ttl ==")
        s, b = call("PATCH", "/api/boards/upd", {"status": "done", "ttl": 2})
        case("PATCH status+ttl", s == 200 and b["summary"]["status"] == "done", f"expires_in={b.get('summary',{}).get('expires_in')}")
        time.sleep(3)
        ids = [x["id"] for x in call("GET", "/api/boards")[1]["boards"]]
        case("short-ttl board pruned on read", "upd" not in ids, f"present={('upd' in ids)}")

        print("\n== E. reset vs delete ==")
        call("POST", "/api/boards/rst", {"title": "R", "status": "running", "blocks": [{"type": "text", "text": "x"}]})
        s, b = call("POST", "/api/boards/rst/reset")
        _, full = call("GET", "/api/boards/rst")
        case("reset clears blocks but keeps board", s == 200 and full.get("blocks") == [],
             f"blocks={full.get('blocks')}, status={full.get('status')}")

        print("\n== F. config ==")
        s, b = call("PUT", "/api/config", {"max_boards": 3})
        case("set_config max_boards", s == 200 and b["config"]["max_boards"] == 3, str(b.get("config")))
        call("PUT", "/api/config", {"max_boards": 12})

        s, b = call("PUT", "/api/config", {"default_ttl": "abc"})
        case("non-int config rejected", s == 400, f"status {s}")

        # `True` is an int subclass (int(True)==1), `1.5` truncated to 1, and a
        # negative/zero was clamped to 1 by `max(1, ...)`: each silently stored a
        # DIFFERENT number than the caller sent, with ok:true. All are refused.
        for bad in (True, 1.5, 0, -5):
            s, b = call("PUT", "/api/config", {"max_boards": bad})
            case(f"config value {bad!r} refused (no silent coercion)",
                 s == 400 and "positive integer" in b.get("error", ""), f"status {s}")

        s, b = call_raw("PUT", "/api/config", b"{not json")
        case("malformed config body refused", s == 400 and "JSON" in b.get("error", ""), f"status {s}")

        print("\n== G. stream shape ==")
        # Guarantee a board exists so the per-board assertion ALWAYS runs (it used
        # to be skipped silently when the store happened to be empty).
        call("POST", "/api/boards/stream-probe",
             {"title": "stream", "status": "running",
              "blocks": [{"id": "p", "type": "progress", "value": 1, "max": 2}]})
        import urllib.request as u
        try:
            with u.urlopen(BASE + "/api/stream", timeout=4) as r:
                first = r.readline().decode()
            payload = json.loads(first[5:].strip())
            case("stream has config + boards", "config" in payload and "boards" in payload,
                 f"keys={list(payload.keys())}")
            case("stream snapshot contains a board", bool(payload.get("boards")),
                 f"boards={len(payload.get('boards', []))}")
            b0 = payload["boards"][0]
            case("board summary has tab+progress+blocks",
                 all(k in b0 for k in ("tab", "progress", "blocks")),
                 f"keys={list(b0.keys())}")
        except Exception as e:
            case("stream readable", False, str(e))

        print("\n== H. caps ==")
        big = [f"line{i}" for i in range(1000)]
        s, b = call("POST", "/api/boards/logcap",
                    {"title": "x", "status": "running", "blocks": [{"id": "l", "type": "log", "lines": big}]})
        _, full = call("GET", "/api/boards/logcap")
        stored = len((full.get("blocks") or [{}])[0].get("lines", []))
        case("log lines capped at 400 on write", s == 200 and stored == 400,
             f"status {s}, stored {stored}")

        print("\n== I. board-id canonicalisation ==")
        s_space, _ = call("POST", "/api/boards/My%20Board", {"title": "a", "blocks": []})
        s_ok, _ = call("POST", "/api/boards/my-board", {"title": "b", "blocks": []})
        s_upper, _ = call("POST", "/api/boards/MY_BOARD", {"title": "c", "blocks": []})
        s_dot, _ = call("POST", "/api/boards/my.board", {"title": "d", "blocks": []})
        case("non-canonical ids refused (no slug collisions)",
             s_space == 400 and s_ok == 200 and s_upper == 400 and s_dot == 400,
             f"space={s_space} canonical={s_ok} upper={s_upper} dot={s_dot}")

        print("\n== I2. board-id collision (409, loud and non-destructive) ==")
        # A board id is a shared, unowned namespace. A second full set_board on a
        # live id must be refused, not silently clobber the first board.
        call("POST", "/api/boards/collide",
             {"title": "first", "status": "running", "ttl": 600,
              "blocks": [{"id": "a", "type": "text", "text": "one"}]})
        s, b = call("POST", "/api/boards/collide",
                    {"title": "second", "status": "running", "ttl": 600, "blocks": []})
        _, full = call("GET", "/api/boards/collide")
        case("second set_board on an existing id -> 409, not a silent 200",
             s == 409 and b.get("ok") is False
             and full.get("title") == "first" and len(full.get("blocks", [])) == 1,
             f"status {s}, title={full.get('title')!r}")
        case("409 names the collision and the replace opt-in",
             s == 409 and "already exists" in b.get("error", "")
             and "replace" in b.get("error", ""),
             f"{b.get('error','')[:110]}")
        # The update tools address a board the caller already knows: unaffected.
        s, b = call("POST", "/api/boards/collide/update", {"id": "a", "set": {"text": "two"}})
        case("update_block on the existing board still works",
             s == 200 and b.get("block", {}).get("text") == "two", f"status {s}")
        s, b = call("PATCH", "/api/boards/collide", {"subtitle": "touched"})
        case("patch_board on the existing board still works", s == 200, f"status {s}")
        s, b = call("POST", "/api/boards/collide/reset")
        case("reset_board on the existing board still works", s == 200, f"status {s}")
        # An explicit falsy flag keeps the guard armed; garbage is refused loudly.
        s, b = call("POST", "/api/boards/collide?replace=false",
                    {"title": "third", "blocks": []})
        case("replace=false keeps the collision refused (409)", s == 409, f"status {s}")
        s, b = call("POST", "/api/boards/collide?replace=banana",
                    {"title": "third", "blocks": []})
        case("non-boolean replace value refused (400)",
             s == 400 and "boolean" in b.get("error", ""), f"status {s}")
        # The explicit opt-in replaces in place.
        s, b = call("POST", "/api/boards/collide?replace=true",
                    {"title": "second", "status": "running", "ttl": 600, "blocks": []})
        _, full = call("GET", "/api/boards/collide")
        case("replace=true overwrites the existing board",
             s == 200 and full.get("title") == "second" and full.get("blocks") == [],
             f"status {s}, title={full.get('title')!r}")
        # A missing id is the common case: unaffected (creates).
        call("DELETE", "/api/boards/collide")
        s, b = call("POST", "/api/boards/collide", {"title": "fresh", "blocks": []})
        case("set_board on a missing id still creates (200)", s == 200, f"status {s}")
        call("DELETE", "/api/boards/collide")
        s, b = call("DELETE", "/api/boards/collide")
        case("delete_board on a missing id still 404s", s == 404, f"status {s}")

        print("\n== J. concurrency: prune during the request-body await ==")
        # A PATCH/update reads the board, then awaits the request body. If the
        # board is pruned in that window, the pre-fix code mutated a detached
        # dict and _bump KeyError'd -> 500. The slow body below is a
        # deterministic injection: hold the body open past the TTL, force a
        # prune from a read path, then release the body.
        def slow_body(method, path, body):
            host, port = BASE.split("//")[1].split(":")
            s = socket.create_connection((host, int(port)), timeout=8)
            raw = json.dumps(body).encode()
            head = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n").encode()
            s.sendall(head)          # handler reads the board, then waits on body
            time.sleep(1.5)          # board TTL elapses; handler still waiting
            call("GET", "/api/boards")   # read path prunes the expired board
            s.sendall(raw)           # handler resumes against a pruned board
            resp = s.recv(65536).decode(errors="replace")
            s.close()
            try:
                return int(resp.split(" ")[1])
            except (IndexError, ValueError):
                return -1

        call("POST", "/api/boards/race-patch",
             {"title": "R", "status": "done", "ttl": 1, "blocks": []})
        st = slow_body("PATCH", "/api/boards/race-patch", {"title": "changed"})
        case("PATCH pruned mid-await -> 404, not 500", st == 404, f"status {st}")

        call("POST", "/api/boards/race-update",
             {"title": "R", "status": "done", "ttl": 1,
              "blocks": [{"id": "p", "type": "progress", "value": 1, "max": 10}]})
        st = slow_body("POST", "/api/boards/race-update/update", {"id": "p", "set": {"value": 5}})
        case("update_block pruned mid-await -> 404, not 500", st == 404, f"status {st}")

        # set_board reads the body BEFORE the lookup and re-assigns BOARDS[bid],
        # so it must survive the same window by recreating the board.
        call("POST", "/api/boards/race-set",
             {"title": "R", "status": "done", "ttl": 1, "blocks": []})
        st = slow_body("POST", "/api/boards/race-set", {"title": "recreated", "blocks": []})
        case("set_board pruned mid-await -> recreates (200)", st == 200, f"status {st}")

        print("\n== K. field-value types ==")
        # Structure is validated, and so are VALUE types. Each
        # hostile value below either 500'd the emitter or silently rendered
        # nonsense ("None", "{'a': 1}", keys-only). The contract now refuses it
        # and names the field.
        hostile = [
            ("v-stats-dict", {"type": "stats", "items": {"a": 1}}, "items"),
            ("v-stats-strlist", {"type": "stats", "items": ["x"]}, "items"),
            ("v-log-null", {"type": "log", "lines": None}, "lines"),
            ("v-spark-dict", {"type": "sparkline", "values": {"a": 1}}, "values"),
            ("v-progress-dict", {"type": "progress", "value": {"a": 1}, "max": 10}, "value"),
            ("v-progress-max0", {"type": "progress", "value": 5, "max": 0}, "max"),
            ("v-stat-none", {"type": "stat", "label": "x", "value": None}, "value"),
            ("v-stat-dict", {"type": "stat", "label": "x", "value": {"a": 1}}, "value"),
            ("v-bar-notnum", {"type": "bar", "data": [["a", "notnum"]]}, "data"),
            ("v-table-rows", {"type": "table", "columns": ["a"], "rows": {"x": 1}}, "rows"),
            ("v-list-dict", {"type": "list", "items": {"a": 1}}, "items"),
            ("v-kv-dict", {"type": "kv", "items": {"a": 1}}, "items"),
        ]
        for bid, blk, field in hostile:
            call("DELETE", f"/api/boards/{bid}")
            s, b = call("POST", f"/api/boards/{bid}",
                        {"title": "x", "status": "running", "ttl": 600, "blocks": [blk]})
            case(f"hostile `{field}` refused (names field)",
                 s == 400 and field in b.get("error", ""),
                 f"status {s}, {b.get('error','')[:80]}")
            # Pre-fix a 500 still STORED the poison board; clean it so one case
            # cannot contaminate the rest (delete removes before broadcast).
            call("DELETE", f"/api/boards/{bid}")

        # A NEGATIVE value for a positive-kind field is a different break from a
        # wrong TYPE: the kind gate is `float(value) <= 0`, and before this case
        # only 0 and hostile types were exercised — flipping the gate to
        # `== 0` (negatives accepted) sailed through the WHOLE suite.
        # progress.max and gauge.max are the contract's only positive-kind
        # block fields; both must refuse a negative with a named 400.
        for bid, blk in (
            ("neg-progress-max", {"type": "progress", "value": 5, "max": -10}),
            ("neg-gauge-max", {"type": "gauge", "value": 1, "max": -0.5}),
        ):
            call("DELETE", f"/api/boards/{bid}")
            s, b = call("POST", f"/api/boards/{bid}",
                        {"title": "x", "status": "running", "ttl": 600, "blocks": [blk]})
            case(f"negative positive-kind value refused ({bid})",
                 s == 400 and "positive" in b.get("error", ""),
                 f"status {s}, {b.get('error','')[:80]}")
            call("DELETE", f"/api/boards/{bid}")

        # A hostile board must not brick the store: pre-fix the stats-dict board
        # was stored, then every later write 500'd and the SSE stream died.
        s, _ = call("POST", "/api/boards/v-poison",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "stats", "items": {"a": 1}}]})
        sv, _ = call("POST", "/api/boards/v-after",
                    {"title": "ok", "status": "running", "ttl": 600,
                     "blocks": [{"type": "text", "text": "hi"}]})
        import urllib.request as _u
        try:
            with _u.urlopen(BASE + "/api/stream", timeout=4) as _r:
                stream_ok = _r.readline().decode().startswith("data: ")
        except Exception:
            stream_ok = False
        case("hostile board refused without bricking writes/stream",
             s == 400 and sv == 200 and stream_ok,
             f"hostile POST {s}, valid POST {sv}, stream_ok={stream_ok}")
        call("DELETE", "/api/boards/v-poison")
        call("DELETE", "/api/boards/v-after")

        # A tab indicator's value/max are numbers the client divides; a dict
        # passes the old None-only check and reaches tabView as NaN.
        s, b = call("POST", "/api/boards/v-tab",
                    {"title": "x", "blocks": [],
                     "tab": {"indicator": {"kind": "bar", "value": {"a": 1}, "max": 10}}})
        case("non-numeric tab indicator value refused",
             s == 400 and "value" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/v-tab")

        print("\n== K2. line block: multi-series chart with axes ==")
        # A good multi-series line is stored verbatim (the client renders it).
        call("POST", "/api/boards/line-ok", {"title": "x", "status": "running", "ttl": 600,
             "blocks": [{"id": "g", "type": "line", "label": "Tokens over time",
                         "series": [{"name": "p50", "values": [1, 2, 3, 5, 8], "color": "mint"},
                                    {"name": "p95", "values": [4, 6, 9, 14, 22]}],
                         "xLabels": ["00:00", "06:00", "12:00", "18:00", "24:00"],
                         "yMin": 0, "yMax": 100, "legend": True}]})
        s, b = call("GET", "/api/boards/line-ok")
        g = (b.get("blocks") or [{}])[0]
        case("good multi-series line accepted",
             s == 200 and len(g.get("series", [])) == 2 and g.get("series")[0]["name"] == "p50",
             f"status {s}, series={[x.get('name') for x in g.get('series', [])]}")
        # The sugar form: one unnamed series straight from `values`.
        call("POST", "/api/boards/line-sugar", {"title": "x", "status": "running", "ttl": 600,
             "blocks": [{"type": "line", "values": [3, 6, 4, 9, 7],
                         "xLabels": ["a", "b", "c", "d", "e"]}]})
        s, b = call("GET", "/api/boards/line-sugar")
        case("values-sugar line accepted",
             s == 200 and (b.get("blocks") or [{}])[0].get("values") == [3, 6, 4, 9, 7], f"status {s}")
        call("DELETE", "/api/boards/line-sugar")

        # update_block re-validates the MERGED block, so a `set` that breaks a
        # sibling invariant (here: fewer points than `xLabels`) is refused too.
        call("POST", "/api/boards/line-upd", {"title": "x", "status": "running", "ttl": 600,
             "blocks": [{"id": "g", "type": "line", "values": [1, 2, 3, 4, 5],
                         "xLabels": ["a", "b", "c", "d", "e"]}]})
        s, b = call("POST", "/api/boards/line-upd/update",
                    {"id": "g", "set": {"values": [1, 2]}})
        s2, _ = call("POST", "/api/boards/line-upd/update",
                     {"id": "g", "set": {"values": [9, 8, 7, 6, 5]}})
        case("update set keeps cross-field xLabels invariant",
             s == 400 and "xLabels" in b.get("error", "") and s2 == 200,
             f"set-short {s} ({b.get('error','')[:70]}), set-ok {s2}")
        call("DELETE", "/api/boards/line-upd")
        call("DELETE", "/api/boards/line-ok")

        # Hostile lines: each must be a named 400, not a 500 and not a store.
        NaN = float("nan")
        line_hostile = [
            ("h-line-empty", {"type": "line", "series": []}, "series", "at least one"),
            ("h-line-len", {"type": "line", "series": [
                {"name": "p50", "values": [1, 2, 3]},
                {"name": "p95", "values": [4, 6, 9, 14, 22]}]}, "series", "lengths differ"),
            ("h-line-short", {"type": "line", "series": [{"values": [1]}]}, "series", "at least 2"),
            ("h-line-nan", {"type": "line", "series": [{"values": [1, NaN]}]}, "series", "a number"),
            ("h-line-huge", {"type": "line", "series": [{"values": [1, 10 ** 400]}]},
             "series", "a number"),
            ("h-line-both", {"type": "line", "series": [{"values": [1, 2]}], "values": [1, 2]},
             "series", "not both"),
            ("h-line-both2", {"type": "line", "series": [{"values": [1, 2]}], "values": [1, 2]},
             "values", "not both"),
            ("h-line-none", {"type": "line"}, "series", "provide `series` or `values`"),
            ("h-line-none2", {"type": "line"}, "values", "provide `series` or `values`"),
            ("h-line-xlab", {"type": "line", "values": [1, 2, 3], "xLabels": ["a", "b"]},
             "xLabels", "must match"),
            ("h-line-ymax", {"type": "line", "values": [1, 2], "yMin": 5, "yMax": 5},
             "yMax", "greater than"),
            ("h-line-ymin", {"type": "line", "values": [1, 2], "yMin": 10, "yMax": 5},
             "yMin", "greater than"),
            ("h-line-nonobj", {"type": "line", "series": [{"values": [1, 2]}, "junk"]},
             "series", "an object"),
            ("h-line-name", {"type": "line", "series": [{"name": {"a": 1}, "values": [1, 2]}]},
             "series[0].name", "a string"),
            ("h-line-colvals", {"type": "line", "series": [{"color": 5, "values": [1, "xx"]}]},
             "series[0].values[1]", "a number"),
        ]
        for bid, blk, field, frag in line_hostile:
            call("DELETE", f"/api/boards/{bid}")
            s, b = call("POST", f"/api/boards/{bid}",
                        {"title": "x", "status": "running", "ttl": 600, "blocks": [blk]})
            err = b.get("error", "")
            case(f"hostile line refused: {bid[len('h-line-'):]}",
                 s == 400 and field in err and frag in err,
                 f"status {s}, {err[:80]}")
            call("DELETE", f"/api/boards/{bid}")

        # And the boundary: 2 points exactly is the minimum, accepted.
        s, _ = call("POST", "/api/boards/line-2p",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "line", "values": [1, 2]}]})
        case("two-point line accepted (minimum)", s == 200, f"status {s}")
        call("DELETE", "/api/boards/line-2p")


        # S6 round-trip: hostile strings must reach the client byte-for-byte
        # S6 round-trip: hostile strings must reach the client byte-for-byte
        # through the wire. Read the exact value from the streamed board (not the
        # DOM) and compare with what was sent.
        hostile_strings = [
            'he said "hi"',
            "back\\slash",
            "line1\nline2",
            "tab\there",
            "</script><b>x</b>",
            "unicode \u2028 sep \u2029 para",
            "emoji \U0001f600 ok",
            "control \x01\x02\x1f end",
            "null\x00byte",
        ]
        call("POST", "/api/boards/v-strings",
             {"title": hostile_strings[0], "status": "running", "ttl": 600,
              "blocks": [{"id": "l", "type": "log", "title": hostile_strings[0],
                          "lines": hostile_strings}]})
        with _u.urlopen(BASE + "/api/stream", timeout=4) as _r:
            frame = _r.readline().decode()
        payload = json.loads(frame[5:].strip())
        board = next(b for b in payload["boards"] if b["id"] == "v-strings")
        log = next(c for c in board["blocks"] if c.get("type") == "log")
        case("hostile strings survive the wire byte-for-byte",
             log["lines"] == hostile_strings and log["title"] == hostile_strings[0]
             and board["title"] == hostile_strings[0],
             f"lines={log['lines']!r}")
        call("DELETE", "/api/boards/v-strings")

        print("\n== L. resource caps: string lengths and ids ==")
        LONG = "z" * 9000  # > contract.MAX_TEXT_LEN (8192)
        long_blocks = [
            ("cap-text", {"type": "text", "text": LONG}, "text"),
            ("cap-code", {"type": "code", "text": LONG}, "text"),
            ("cap-callout", {"type": "callout", "text": LONG}, "text"),
            ("cap-logline", {"type": "log", "lines": [LONG]}, "lines"),
            ("cap-listitem", {"type": "list", "items": [LONG]}, "items"),
            ("cap-col", {"type": "table", "columns": [LONG], "rows": []}, "columns"),
            ("cap-kv", {"type": "kv", "items": [["k", LONG]]}, "items"),
            ("cap-statval", {"type": "stat", "label": "x", "value": LONG}, "value"),
            ("cap-barlabel", {"type": "bar", "data": [[LONG, 1]]}, "data"),
            ("cap-numstr", {"type": "progress", "value": "1." + "0" * 9000 + "1",
                            "max": 100}, "value"),
        ]
        for bid, blk, field in long_blocks:
            call("DELETE", f"/api/boards/{bid}")
            s, b = call("POST", f"/api/boards/{bid}",
                        {"title": "x", "status": "running", "ttl": 600, "blocks": [blk]})
            case(f"over-long {field} refused ({bid})",
                 s == 400 and "too long" in b.get("error", ""),
                 f"status {s}, {b.get('error','')[:80]}")
            call("DELETE", f"/api/boards/{bid}")

        s, b = call("POST", "/api/boards/cap-tablabel",
                    {"title": "x", "blocks": [], "tab": {"label": LONG}})
        case("over-long tab.label refused",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")
        call("DELETE", "/api/boards/cap-tablabel")

        s, b = call("POST", "/api/boards/cap-tabtext",
                    {"title": "x", "blocks": [],
                     "tab": {"indicator": {"kind": "text", "text": LONG}}})
        case("over-long tab.indicator.text refused",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")
        call("DELETE", "/api/boards/cap-tabtext")

        s, b = call("POST", "/api/boards/" + "a" * 100, {"title": "x", "blocks": []})
        case("over-long board id refused",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")

        s, b = call("POST", "/api/boards/cap-blockid",
                    {"title": "x", "blocks": [{"id": "b" * 100, "type": "text", "text": "x"}]})
        case("over-long block id refused on set_board",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")
        call("POST", "/api/boards/cap-blockid2",
             {"title": "x", "status": "running", "ttl": 600,
              "blocks": [{"id": "ok", "type": "text", "text": "x"}]})
        s, b = call("POST", "/api/boards/cap-blockid2/update",
                    {"id": "ok", "set": {"id": "c" * 100}})
        case("over-long block id refused via update set",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")
        call("DELETE", "/api/boards/cap-blockid2")

        call("POST", "/api/boards/cap-append",
             {"title": "x", "status": "running", "ttl": 600,
              "blocks": [{"id": "l", "type": "log", "lines": ["a"]}]})
        s, b = call("POST", "/api/boards/cap-append/update", {"id": "l", "append": LONG})
        case("over-long append refused",
             s == 400 and "too long" in b.get("error", ""), f"status {s}")
        call("DELETE", "/api/boards/cap-append")

        s, b = call("POST", "/api/boards/cap-ok",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "text", "text": "z" * 8000}]})
        case("under-cap long text still accepted", s == 200, f"status {s}")
        call("DELETE", "/api/boards/cap-ok")

        print("\n== M. resource caps: numeric magnitude ==")
        # JSON integers are arbitrary precision, but the client renderer, the
        # summary and the config arithmetic coerce them to float. `10**400` is a
        # legal JSON
        # number that `float()` cannot represent: pre-fix it escaped validation
        # as a 500, and a scalar field (stat/stats value) was STORED, after which
        # every snapshot/broadcast 500'd — the poison-board class this suite
        # already guards for dicts, missed for huge ints. Each numeric value must
        # be a finite double, refused with a named 400.
        HUGE = 10 ** 400
        for bid, blk, field in [
            ("mag-stat", {"type": "stat", "label": "x", "value": HUGE}, "value"),
            ("mag-stats", {"type": "stats", "items": [{"label": "x", "value": HUGE}]}, "value"),
            ("mag-progress", {"type": "progress", "label": "x", "value": HUGE, "max": 100}, "value"),
            ("mag-max", {"type": "progress", "label": "x", "value": 1, "max": HUGE}, "max"),
            ("mag-gauge", {"type": "gauge", "label": "x", "value": HUGE, "max": 100}, "value"),
        ]:
            call("DELETE", f"/api/boards/{bid}")
            s, b = call("POST", f"/api/boards/{bid}",
                        {"title": "x", "status": "running", "ttl": 600, "blocks": [blk]})
            case(f"huge {field} refused with a named 400 ({bid})",
                 s == 400 and field in b.get("error", ""),
                 f"status {s}, {b.get('error','')[:80]}")
            call("DELETE", f"/api/boards/{bid}")

        # A huge `ttl` is used in `ttl - age` (a float subtraction): it must be
        # refused, not 500 the write and then poison every later read.
        s, b = call("POST", "/api/boards/mag-ttl",
                    {"title": "x", "status": "done", "ttl": HUGE, "blocks": []})
        case("huge ttl refused with a named 400", s == 400 and "ttl" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/mag-ttl")

        # A huge tab indicator value is likewise float-converted by the client.
        s, b = call("POST", "/api/boards/mag-tab",
                    {"title": "x", "blocks": [],
                     "tab": {"indicator": {"kind": "bar", "value": HUGE, "max": 100}}})
        case("huge tab indicator value refused", s == 400 and "value" in b.get("error", ""),
             f"status {s}, {b.get('error','')[:80]}")
        call("DELETE", "/api/boards/mag-tab")

        # Boundary: 2**53 is the largest int a JS number represents exactly, so
        # it is accepted; 2**53+1 is refused (it would silently round on the way
        # to the client). A float is already the client's value, so a large
        # finite float is accepted.
        s, b = call("POST", "/api/boards/mag-safe",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "stat", "label": "x", "value": 2 ** 53}]})
        case("2**53 accepted (exact double)", s == 200, f"status {s}")
        call("DELETE", "/api/boards/mag-safe")
        s, b = call("POST", "/api/boards/mag-over",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "stat", "label": "x", "value": 2 ** 53 + 1}]})
        case("2**53+1 refused (not an exact double)", s == 400, f"status {s}")
        call("DELETE", "/api/boards/mag-over")
        s, b = call("POST", "/api/boards/mag-float",
                    {"title": "x", "status": "running", "ttl": 600,
                     "blocks": [{"type": "stat", "label": "x", "value": 1e17}]})
        case("large finite float accepted (already a double)", s == 200, f"status {s}")
        call("DELETE", "/api/boards/mag-float")

        # The refusals must leave the store usable: a valid write after them
        # still succeeds and the SSE stream still emits a frame.
        call("DELETE", "/api/boards/mag-after")
        s, _ = call("POST", "/api/boards/mag-after",
                    {"title": "ok", "status": "running", "ttl": 600,
                     "blocks": [{"type": "text", "text": "hi"}]})
        import urllib.request as _u2
        try:
            with _u2.urlopen(BASE + "/api/stream", timeout=4) as _r:
                mag_stream_ok = _r.readline().decode().startswith("data: ")
        except Exception:
            mag_stream_ok = False
        case("store/stream survive the huge-number refusals", s == 200 and mag_stream_ok,
             f"valid POST {s}, stream_ok={mag_stream_ok}")
        call("DELETE", "/api/boards/mag-after")
    finally:
        # Restore the server to its pre-run state even if a case raised.
        if orig_config:
            call("PUT", "/api/config", orig_config)
        delete_all_boards()

    gaps = [r for r in results if not r[1]]
    print(f"\n=== {len(results)-len(gaps)} pass, {len(gaps)} gap(s) ===")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
