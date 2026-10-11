#!/usr/bin/env python3
"""Live test harness for livewall-mcp.

Uses the fastmcp in-memory Client against the real server module and a live
Livewall backend. Covers every read path, every contract refusal (asserting the
refusal names the fix), and a full write lifecycle with cleanup + baseline
restoration.

Run: python test_live.py   (needs fastmcp + httpx, and a Livewall backend)

LIVEWALL_URL selects the backend. Its default here is a deliberately DEAD port
(http://127.0.0.1:1) so a bare run fails loudly instead of driving a real
backend; the server module reads the variable at import, so it is set first.
"""

import asyncio
import importlib.util
import os
import sys

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
# Dead-port default BEFORE importing server.py: it computes BASE_URL at import,
# and this suite writes boards + config, so a bare run must not hit a real 8470.
os.environ.setdefault("LIVEWALL_URL", "http://127.0.0.1:1")
spec = importlib.util.spec_from_file_location("srv", os.path.join(HERE, "server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from fastmcp import Client  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {str(detail)[:200]}")


async def call(c, tool, args=None):
    """Return (ok, payload_text_or_error)."""
    try:
        res = await c.call_tool(tool, args or {})
        data = getattr(res, "data", None)
        if data is None:
            data = res.content[0].text if res.content else None
        return True, data
    except Exception as e:  # fastmcp raises ToolError on our LivewallError
        return False, str(e)


def probe_request_branches() -> None:
    """Exercise the httpx paths unreachable from the live backend plus the
    best-effort enrichment swallow, with a mock
    transport. These are synchronous `_request`/tool calls, not MCP round-trips.
    """
    print("\n-- _request branches (mock transport) --")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/204":
            return httpx.Response(204)
        if path == "/empty":
            return httpx.Response(200, content=b"")
        if path == "/json":
            return httpx.Response(200, json={"ok": True})
        if path == "/text-error":
            return httpx.Response(500, text="upstream boom")
        if path == "/api/boards/x/update":
            return httpx.Response(404, json={"ok": False, "error": "no block id 'nope'"})
        if path == "/api/boards/x":
            raise httpx.ConnectError("enrichment lookup down")
        return httpx.Response(404, json={"error": "unrouted"})

    transport = httpx.MockTransport(handler)
    orig = mod._client
    mod._client = lambda: httpx.Client(transport=transport, base_url="http://mock")
    try:
        check("_request 204 -> None", mod._request("DELETE", "/204") is None)
        check("_request empty body -> None", mod._request("GET", "/empty") is None)
        check("_request JSON body parsed", mod._request("GET", "/json") == {"ok": True})
        try:
            mod._request("GET", "/text-error")
            check("_request non-JSON error body surfaces", False, "no error raised")
        except mod.LivewallError as e:
            check("_request non-JSON error body surfaces",
                  "500" in str(e) and "upstream boom" in str(e), str(e)[:160])
        # S4: the enrichment GET fails, but the refusal must still be raised
        # (ids "unknown"), never swallowed into a silent no-op.
        try:
            mod.update_block(board_id="x", block_id="nope", set_fields={"value": 1})
            check("update_block still refuses when enrichment fails", False, "no error raised")
        except mod.LivewallError as e:
            check("update_block still refuses when enrichment fails",
                  "no block id" in str(e) and "unknown" in str(e), str(e)[:200])
    finally:
        mod._client = orig


async def main():
    # Fail loudly on a dead/mistyped target before this suite writes anything.
    try:
        httpx.get(mod.BASE_URL + "/health", timeout=4)
    except Exception as e:
        print(f"no Livewall backend at {mod.BASE_URL} (/health: {e}).")
        print("  set LIVEWALL_URL to a running backend, e.g.")
        print("  LIVEWALL_URL=http://127.0.0.1:8470 python mcp/test_live.py")
        print("  (the default is a dead port so a bare run cannot touch a real one)")
        return 2
    async with Client(mod.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        print(f"tools discovered: {len(tools)}")
        expected = {
            "livewall_ping", "describe_blocks", "list_boards", "get_board",
            "get_config", "set_board", "update_board", "update_block",
            "reset_board", "delete_board", "set_config",
        }
        check("all expected tools registered", expected <= tools,
              f"missing={expected - tools}")

        print("\n-- read paths --")
        # Clear any leftover from an interrupted prior run so the baseline is clean.
        try:
            await c.call_tool("delete_board", {"board_id": "mcp-selftest"})
        except Exception:
            pass

        ok, h = await call(c, "livewall_ping")
        check("livewall_ping", ok and isinstance(h, dict) and h.get("ok") is True, h)

        ok, d = await call(c, "describe_blocks")
        check("describe_blocks has vocabulary",
              ok and "progress" in d.get("block_types", {}), str(d)[:120])

        # The machine vocabulary must not under-report: every writable field the
        # contract declares for a type must appear in that type's describe_blocks
        # doc string. (A field the contract accepts but the doc omits is how an
        # agent concludes it is not allowed — the `fmt` omission this pins.)
        import re as _re
        doc_fields = d.get("block_types", {}) if (ok and isinstance(d, dict)) else {}
        missing_docs = []
        for t in sorted(mod.contract.BLOCK_TYPES):
            doc = str(doc_fields.get(t, ""))
            if "alias" in doc:  # `chart` documents itself as an alias
                continue
            for f in sorted(mod.contract.BLOCK_FIELDS.get(t, set())):
                if not _re.search(r"\b" + _re.escape(f) + r"\b", doc):
                    missing_docs.append(f"{t}.{f}")
        check("describe_blocks documents every contract field",
              ok and not missing_docs, f"undocumented: {missing_docs}")

        ok, lw = await call(c, "list_boards")
        check("list_boards returns count+boards",
              ok and "count" in lw and "boards" in lw, str(lw)[:120])
        baseline = lw["count"]

        ok, cf = await call(c, "get_config")
        check("get_config", ok and "default_ttl" in cf, cf)
        # Remember the FULL config so the suite can restore it exactly, rather
        # than guessing a value (it used to hardcode default_ttl=900).
        baseline_cfg = dict(cf) if ok and isinstance(cf, dict) else {}

        print("\n-- contract refusals (each must name the fix) --")
        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "blocks": [{"type": "wat"}]})
        check("unknown block type refused + lists valid",
              (not ok) and "unknown type" in str(e) and "progress" in str(e), e)

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "blocks": [{"text": "no type field"}]})
        check("block without type refused",
              (not ok) and "no `type`" in str(e), e)

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x",
            "blocks": [{"id": "a", "type": "text", "text": "1"},
                       {"id": "a", "type": "text", "text": "2"}]})
        check("duplicate block id refused", (not ok) and "duplicate id" in e, e[:160])

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "status": "nope", "blocks": []})
        check("bad status refused + lists valid",
              (not ok) and "status" in e and "running" in e, e[:160])

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "ttl": -5, "blocks": []})
        check("non-positive ttl refused", (not ok) and "positive" in e, e[:160])

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "blocks": [],
            "tab": {"accent": "chartreuse"}})
        check("bad tab accent refused + lists valid",
              (not ok) and "accent" in e and "violet" in e, e[:160])

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "blocks": [],
            "tab": {"indicator": {"kind": "sparkle"}}})
        check("bad indicator kind refused", (not ok) and "kind" in e, e[:160])

        ok, e = await call(c, "set_board", {
            "board_id": "x", "title": "x", "blocks": [],
            "tab": {"indicator": {"kind": "bar"}}})
        check("bar indicator without value refused",
              (not ok) and "value" in e, e[:160])

        ok, e = await call(c, "get_board", {"board_id": "ghost-board"})
        check("get missing board -> error", not ok and "ghost-board" in e, e[:160])

        print("\n-- write lifecycle (throwaway board) --")
        bid = "mcp-selftest"
        ok, sb = await call(c, "set_board", {
            "board_id": bid, "title": "Self test", "subtitle": "harness",
            "status": "running", "ttl": 120,
            "tab": {"label": "SelfTest", "accent": "pink",
                    "indicator": {"kind": "bar", "value": 1, "max": 4}},
            "blocks": [
                {"id": "p", "type": "progress", "label": "step", "value": 1, "max": 4},
                {"id": "l", "type": "log", "lines": ["start"]},
                {"id": "s", "type": "stats", "items": [{"label": "n", "value": 1}]},
            ]})
        check("set_board create", ok and sb.get("version", 0) >= 1, sb)
        check("set_board reports summary+tab",
              ok and sb["summary"]["tab"]["label"] == "SelfTest", str(sb)[:200])

        ok, gb = await call(c, "get_board", {"board_id": bid})
        check("get_board after create", ok and len(gb.get("blocks", [])) == 3, str(gb)[:160])

        ok, ub = await call(c, "update_block", {
            "board_id": bid, "block_id": "p", "set_fields": {"value": 3}})
        check("update_block set", ok and ub["block"]["value"] == 3, str(ub)[:160])

        ok, ub = await call(c, "update_block", {
            "board_id": bid, "block_id": "l", "append": "more"})
        check("update_block append to log",
              ok and "more" in ub["block"]["lines"], str(ub)[:160])

        ok, e = await call(c, "update_block", {
            "board_id": bid, "block_id": "nope", "set_fields": {"value": 1}})
        check("update_block missing id lists existing ids",
              (not ok) and "nope" in e and "'p'" in e, e[:200])

        ok, pb = await call(c, "update_board", {"board_id": bid, "status": "done", "ttl": 60})
        check("update_board partial (status+ttl)",
              ok and pb["summary"]["status"] == "done"
              and pb["summary"]["effective_ttl"] == 60, str(pb)[:200])

        ok, rb = await call(c, "reset_board", {"board_id": bid})
        check("reset_board", ok and rb.get("ok") is True, rb)

        ok, db = await call(c, "delete_board", {"board_id": bid})
        check("delete_board", ok and db.get("ok") is True, db)

        ok, e = await call(c, "delete_board", {"board_id": bid})
        check("delete again -> error", not ok, e[:120])

        print("\n-- board-id collision (loud, non-destructive) --")
        cid = "mcp-collide"
        try:
            await c.call_tool("delete_board", {"board_id": cid})
        except Exception:
            pass
        ok, first = await call(c, "set_board", {
            "board_id": cid, "title": "first",
            "blocks": [{"id": "a", "type": "text", "text": "one"}]})
        check("collision setup: first set_board", ok and first.get("version", 0) >= 1, first)
        ok, e = await call(c, "set_board", {
            "board_id": cid, "title": "second", "blocks": []})
        check("second set_board on an existing id refused (not silent)",
              (not ok) and "already exists" in str(e) and "replace" in str(e), e[:200])
        ok, gb = await call(c, "get_board", {"board_id": cid})
        check("first board intact after the refused collision",
              ok and gb.get("title") == "first", str(gb)[:160])
        ok, ub = await call(c, "update_block", {
            "board_id": cid, "block_id": "a", "set_fields": {"text": "two"}})
        check("update_block unaffected by the guard",
              ok and ub["block"]["text"] == "two", str(ub)[:160])
        ok, rep = await call(c, "set_board", {
            "board_id": cid, "title": "second", "blocks": [], "replace": True})
        ok2, gb2 = await call(c, "get_board", {"board_id": cid})
        check("replace=True overwrites the existing board",
              ok and ok2 and gb2.get("title") == "second", str(gb2)[:160])
        await call(c, "delete_board", {"board_id": cid})

        print("\n-- config lifecycle --")
        ok, sc = await call(c, "set_config", {"default_ttl": 777})
        check("set_config", ok and sc["config"]["default_ttl"] == 777, sc)
        # Restore the FULL captured baseline (not a hardcoded 900), so a run
        # against a server configured differently is left as it was found.
        if baseline_cfg:
            restore = {k: baseline_cfg[k] for k in
                       ("default_ttl", "stale_ttl", "max_boards") if k in baseline_cfg}
            await call(c, "set_config", restore)

        ok, e = await call(c, "set_config", {})
        check("set_config with nothing refused", (not ok) and "nothing" in e, e[:120])

        # The non-int / non-positive branch is unreachable from a valid call, so
        # exercise it directly.
        ok, e = await call(c, "set_config", {"default_ttl": "abc"})
        check("set_config non-int refused", (not ok) and "integer" in str(e), str(e)[:160])
        ok, e = await call(c, "set_config", {"max_boards": 0})
        check("set_config zero refused", (not ok) and "positive" in str(e), str(e)[:160])
        ok, e = await call(c, "set_config", {"stale_ttl": -3})
        check("set_config negative refused", (not ok) and "positive" in str(e), str(e)[:160])
        # A bool passes pydantic's int schema (bool is an int subclass) and
        # `int(True)` is 1 — so this is the one non-int that reaches the guard.
        ok, e = await call(c, "set_config", {"default_ttl": True})
        check("set_config bool refused (not silently 1)",
              (not ok) and "integer" in str(e), str(e)[:160])

        print("\n-- baseline restoration --")
        ok, lw2 = await call(c, "list_boards")
        check("board count restored to baseline",
              ok and lw2["count"] == baseline,
              f"baseline={baseline} now={lw2['count']}")

        ok, cf2 = await call(c, "get_config")
        check("config restored to baseline",
              ok and bool(baseline_cfg)
              and all(cf2.get(k) == baseline_cfg[k] for k in baseline_cfg),
              f"baseline={baseline_cfg} now={cf2}")

    probe_request_branches()

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
