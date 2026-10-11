#!/usr/bin/env python3
"""Store concurrency: route handlers must not mutate the store off the event loop.

The defect this suite pins: the store (`BOARDS`, `SUBS`, `CONFIG`) lives on the
asyncio event loop, but FastAPI dispatches a NON-coroutine endpoint through
`run_in_threadpool` (fastapi/routing.py:138 -> starlette/concurrency.py:32,
`anyio.to_thread.run_sync`). A `def` handler therefore performs its
check-then-act sequences on a WORKER THREAD while the loop-side reaper
(`_reaper` -> `_prune`) and the async handlers mutate the same dicts
concurrently. The GIL serializes single bytecodes, not the sequence
`if bid not in BOARDS: ... del BOARDS[bid]`, so two threads (or a thread and
the reaper) can interleave inside one handler:

  * `delete_board`: both threads pass the membership check, the second `del`
    raises KeyError -> an unhandled 500 (reproduced deterministically below).
  * `reset_board`: `BOARDS.get` succeeds, the reaper prunes the board, `_bump`
    hits `BOARDS[bid]` -> KeyError -> 500 (same class; not deterministically
    reproducible post-fix because the window only exists off-loop).
  * `list_boards`/`get_config`: read paths that can observe a half-mutated
    `CONFIG` (`clear()` then `update()`) or double-prune with the reaper.

The fix is structural: every route handler is a coroutine, so each handler's
read-modify-write section runs within ONE event-loop tick — atomic against the
reaper and every other handler without a lock.

The RED/GREEN proof mirrors the framework's own dispatch rule exactly
(`run_in_threadpool` for sync, direct await for async), and forces both threads
to meet INSIDE the membership check via a barrier-carrying `BOARDS` double, so
the race is deterministic rather than lucky.

Run standalone:  python3 backend/tests/store_concurrency_test.py
Exit code is non-zero if any case fails.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# Configure the app before import: temp config, own process.
_TMP = tempfile.mkdtemp(prefix="livewall-storecc-")
os.environ["LIVEWALL_CONFIG_FILE"] = str(Path(_TMP) / "config.json")
os.environ["LIVEWALL_HOST"] = "127.0.0.1"

import uvicorn  # noqa: E402

import app as lw  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

results = []


def case(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:140]}", flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MeetingBoards(dict):
    """A BOARDS double that makes the delete race deterministic.

    The real sequence is `if bid not in BOARDS: ... del BOARDS[bid]` — the
    decision and the mutation are two separate steps. The double performs the
    REAL membership test untouched, then, when the caller is OFF the event
    loop (a worker thread — exactly the condition under which the race
    exists), it meets the other thread inside `__delitem__`: both threads have
    already passed the genuine check, so both proceed to delete, and the
    second `del` raises KeyError — the live 500. On the loop the barrier is
    inert (the handler is serialized; a losing request never reaches
    `__delitem__`), so the GREEN run is not stalled by it.
    """

    barrier: threading.Barrier | None = None

    def __delitem__(self, key):
        barrier = self.barrier
        if barrier is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # a worker thread: the race window is real
                try:
                    barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
        super().__delitem__(key)


def dispatch(handler, *args):
    """Await `handler` exactly the way FastAPI's request_response does.

    fastapi/routing.py:138: a non-coroutine endpoint is wrapped in
    `run_in_threadpool` (starlette/concurrency.py:32 -> anyio worker thread);
    a coroutine endpoint is awaited on the loop directly.
    """
    if inspect.iscoroutinefunction(handler):
        return handler(*args)
    return run_in_threadpool(handler, *args)


def test_delete_toctou():
    """Two concurrent DELETEs of one board must never 500.

    RED (pre-fix): both worker threads meet inside `bid not in BOARDS`, both
    pass it, the second `del BOARDS[bid]` raises KeyError — in the real server
    that is an unhandled exception -> HTTP 500.
    GREEN (post-fix): the handler is a coroutine; `asyncio.gather` on one loop
    serializes the two sync sections, so the loser observes the winner's
    deletion and returns a clean 404.
    """
    real = lw.BOARDS

    def board():
        return {"title": "x", "subtitle": "", "status": "running", "updated": 0.0,
                "blocks": [], "version": 1, "ttl": None, "tab": None}

    async def run():
        rb = MeetingBoards()
        rb.barrier = threading.Barrier(2)
        rb["x"] = board()
        lw.BOARDS = rb
        try:
            return await asyncio.gather(dispatch(lw.delete_board, "x"),
                                        dispatch(lw.delete_board, "x"))
        finally:
            lw.BOARDS = real

    try:
        r1, r2 = asyncio.run(run())
        # delete_board returns a plain dict on success (app.py: "return {\"ok\":
        # True, ...}") and a JSONResponse on the named 404.
        def status_of(r):
            return 200 if isinstance(r, dict) else r.status_code
        statuses = sorted(status_of(r) for r in (r1, r2))
        case("concurrent DELETE of one board: no 500, one wins one 404s",
             statuses == [200, 404],
             f"statuses={statuses} (the 404 proves the sync sections were "
             "serialized on the loop)")
    except KeyError as e:
        case("concurrent DELETE of one board: no 500, one wins one 404s",
             False, f"KeyError escaped the handler (a live 500): {e!r}")


def test_every_route_handler_is_a_coroutine():
    """Fence: no route handler may be dispatched to a worker thread.

    A new `def` endpoint would silently re-open the off-loop race class this
    suite exists for. The assertion is structural by design: the behavioural
    proof is `test_delete_toctou`; this fence keeps the invariant from
    regressing one added route at a time.
    """
    from fastapi.routing import APIRoute
    offenders = []
    for route in lw.app.routes:
        if isinstance(route, APIRoute) and not inspect.iscoroutinefunction(route.endpoint):
            offenders.append(f"{','.join(sorted(route.methods))} {route.path}")
    case("every route handler is a coroutine (no threadpool dispatch)",
         not offenders, f"offenders={offenders}")


class Server:
    def __init__(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(lw.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(400):
            if self.server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def call(base, method, path, body=None, timeout=10):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"err": str(e)}


def test_mixed_op_stress(base):
    """Real-server guard: concurrent mixed writes/reads never 500.

    60 rounds of 24 concurrent operations over 4 boards (create, delete,
    reset, patch, update, list). A 500 or a transport error is a gap; the
    legitimate statuses under contention are 200/404/400/409.
    """
    ok_statuses = {200, 404, 400, 409}
    bad = []
    for rnd in range(60):
        bar = threading.Barrier(24)
        out = []
        lock = threading.Lock()

        def go(i):
            bid = f"mx{rnd % 4}"
            ops = [
                ("POST", f"/api/boards/{bid}", {"title": "m"}),
                ("DELETE", f"/api/boards/{bid}", None),
                ("POST", f"/api/boards/{bid}/reset", None),
                ("PATCH", f"/api/boards/{bid}", {"title": "p"}),
                ("POST", f"/api/boards/{bid}/update", {"id": "z", "set": {}}),
                ("GET", "/api/boards", None),
                ("GET", f"/api/boards/{bid}", None),
            ]
            m, p, b = ops[i % len(ops)]
            bar.wait()
            st, _ = call(base, m, p, b)
            with lock:
                out.append(st)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(24)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        for st in out:
            if st not in ok_statuses:
                bad.append((rnd, st))
    case("mixed-op stress: 60 rounds x 24 concurrent ops, no 500/transport error",
         not bad, f"offenders={bad[:6]}")

    left = call(base, "GET", "/api/boards")[1].get("boards", [])
    for b in left:
        call(base, "DELETE", f"/api/boards/{b['id']}")


def main():
    test_delete_toctou()
    test_every_route_handler_is_a_coroutine()
    srv = Server()
    if not srv.start():
        print("  GAP   backend failed to start")
        return 1
    try:
        test_mixed_op_stress(srv.base)
    finally:
        srv.stop()
    gaps = [r for r in results if not r[1]]
    print(f"\n=== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ===")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
