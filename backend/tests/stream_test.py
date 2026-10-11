#!/usr/bin/env python3
"""SSE stream & concurrency suite for Livewall.

Boots its OWN backend in-process (uvicorn in a thread) so it can inspect the
shared `SUBS` set directly — the HTTP edge suite cannot see it. Uses real TCP
sockets for the stream, so client-disconnect and coalescing behaviour is
exercised, not simulated.

Covers:
  S2  queue is bounded; newest-wins coalescing
  S3  broadcast tolerates a subscriber disappearing
  S4  disconnect cancels the generator and the queue leaves SUBS
  S5  keepalive fires and cannot terminate the stream
  S6  reaper prunes + broadcasts on its timer (bounded staleness)
  S7  version is monotonic under concurrent writers
  S8  snapshot is internally consistent (every board carries blocks)
  plus two cases worth covering: a live push after an update, and
  the reaper's broadcast branch.

Run standalone:  python3 backend/tests/stream_test.py
Exit code is non-zero if any case fails.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
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

# Configure the app before import: temp config, own process, default keepalive.
_TMP = tempfile.mkdtemp(prefix="livewall-stream-")
os.environ["LIVEWALL_CONFIG_FILE"] = str(Path(_TMP) / "config.json")
os.environ["LIVEWALL_HOST"] = "127.0.0.1"
os.environ["LIVEWALL_KEEPALIVE"] = "15"

import uvicorn  # noqa: E402

import app as lw  # noqa: E402

results = []


def case(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:120]}", flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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


def call(base, method, path, body=None, timeout=8):
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


class SSEConn:
    """A raw SSE client over a real socket.

    Parses the HTTP response itself so the transport does not matter: it
    de-chunks a `Transfer-Encoding: chunked` body (uvicorn uses chunked for
    HTTP/1.1, `Connection: close` for HTTP/1.0) and copes with a frame split
    across recvs and several frames arriving in one recv. HTTP/1.1 is used
    because that is what a browser sends.
    """

    def __init__(self, base, timeout=15, version="HTTP/1.1"):
        host, port = base.split("//")[1].split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.sendall(
            f"GET /api/stream {version}\r\nHost: {host}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode())
        self.raw = b""            # received bytes, not yet decoded
        self.body = b""           # decoded body, not yet split into events
        self.chunked = False
        self.headers_parsed = False
        self.eof = False

    def _recv(self, timeout):
        self.sock.settimeout(max(0.05, timeout))
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not data:
            self.eof = True
            return False
        self.raw += data
        return True

    def _decode(self):
        """Move as much complete data as possible from `raw` into `body`."""
        if not self.chunked:
            self.body += self.raw
            self.raw = b""
            return
        while True:
            nl = self.raw.find(b"\r\n")
            if nl == -1:
                return  # need the chunk-size line
            size_field = self.raw[:nl].split(b";", 1)[0].strip()
            try:
                size = int(size_field, 16)
            except ValueError:
                self.body += self.raw  # not chunk framing; treat as raw
                self.raw = b""
                return
            if size == 0:
                self.eof = True
                self.raw = b""
                return
            end = nl + 2 + size
            if len(self.raw) < end + 2:
                return  # chunk body (or its trailing CRLF) not complete yet
            self.body += self.raw[nl + 2:end]
            self.raw = self.raw[end + 2:]

    def next_event(self, timeout=15):
        """Return the next SSE event's text, or None on timeout/EOF."""
        deadline = time.time() + timeout
        while True:
            if not self.headers_parsed:
                while b"\r\n\r\n" not in self.raw:
                    rem = deadline - time.time()
                    if rem <= 0:
                        return None
                    if not self._recv(rem) and (self.eof or deadline - time.time() <= 0):
                        return None
                head, self.raw = self.raw.split(b"\r\n\r\n", 1)
                self.chunked = b"transfer-encoding: chunked" in head.lower()
                self.headers_parsed = True
            self._decode()
            idx = self.body.find(b"\n\n")
            if idx != -1:
                event = self.body[:idx]
                self.body = self.body[idx + 2:]
                return event.decode(errors="replace")
            if self.eof:
                if self.body.strip():
                    event, self.body = self.body, b""
                    return event.decode(errors="replace")
                return None
            rem = deadline - time.time()
            if rem <= 0:
                return None
            self._recv(rem)

    def next_data(self, timeout=15):
        """Next `data:` frame parsed as JSON, skipping keepalive comments."""
        deadline = time.time() + timeout
        while True:
            rem = deadline - time.time()
            if rem <= 0:
                return None
            ev = self.next_event(rem)
            if ev is None:
                return None
            if ev.startswith("data: "):
                return json.loads(ev[6:])
            # keepalive comment or other field: keep reading

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def board_ids(snap):
    return [b["id"] for b in snap.get("boards", [])]


def wait_subs(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred(len(lw.SUBS)):
            return len(lw.SUBS)
        time.sleep(0.05)
    return len(lw.SUBS)


def delete_all(base):
    for b in call(base, "GET", "/api/boards")[1].get("boards", []):
        call(base, "DELETE", f"/api/boards/{b['id']}")


# --------------------------------------------------------------------- cases
def test_queue_bound_and_coalescing(base):
    # Unit-level: the enqueue policy itself (post-fix symbol).
    async def run():
        q = asyncio.Queue(maxsize=getattr(lw, "STREAM_QUEUE_MAX", 1))
        for i in range(100):
            lw._enqueue(q, f"m{i}")
        size = q.qsize()
        newest = q.get_nowait()
        return size, newest

    try:
        size, newest = asyncio.run(run())
        case("S2 enqueue bounded + newest-wins",
             size <= getattr(lw, "STREAM_QUEUE_MAX", 1) and newest == "m99",
             f"qsize={size}, newest={newest}")
    except AttributeError as e:
        case("S2 enqueue bounded + newest-wins", False, f"missing symbol: {e}")

    # Live: a real subscriber's queue must be bounded (0 == unbounded).
    conn = SSEConn(base)
    try:
        conn.next_data()  # initial snapshot -> subscriber is registered
        wait_subs(lambda n: n >= 1, 3)
        q = next(iter(lw.SUBS))
        case("S2 live subscriber queue is bounded",
             q.maxsize == 1,
             f"maxsize={q.maxsize} (0 means unbounded)")
    finally:
        conn.close()


def test_live_push(base):
    """Coverage gap 1: a push after an update reaches a connected client."""
    conn = SSEConn(base)
    try:
        conn.next_data()
        st1, _ = call(base, "POST", "/api/boards/push-test",
                      {"title": "P", "status": "running", "blocks":
                       [{"id": "p", "type": "progress", "label": "x", "value": 1, "max": 10}]})
        snap = conn.next_data(timeout=8)
        v1 = next((b["version"] for b in snap["boards"] if b["id"] == "push-test"), None)
        st2, _ = call(base, "POST", "/api/boards/push-test/update", {"id": "p", "set": {"value": 7}})
        snap = conn.next_data(timeout=8)
        v2 = next((b["version"] for b in snap["boards"] if b["id"] == "push-test"), None)
        blk = next((b for b in snap["boards"] if b["id"] == "push-test"), {})
        val = None
        for b in blk.get("blocks", []):
            if b.get("type") == "progress":
                val = b.get("value")
        case("live push after update reaches client",
             st1 == 200 and st2 == 200 and v1 == 1 and v2 == 2 and val == 7,
             f"create={st1} update={st2}, versions={v1}->{v2}, pushed value={val}")
    finally:
        conn.close()


def test_reaper_prunes_and_broadcasts(base):
    """Coverage gap 2: the 5s reaper prunes AND broadcasts on its timer."""
    conn = SSEConn(base)
    try:
        conn.next_data()
        st, _ = call(base, "POST", "/api/boards/reap-test",
                     {"title": "R", "status": "done", "ttl": 1, "blocks": []})
        snap = conn.next_data(timeout=8)
        seen = st == 200 and snap is not None and "reap-test" in board_ids(snap)
        t0 = time.time()
        deadline = t0 + 12
        gone_at = None
        while time.time() < deadline:
            snap = conn.next_data(timeout=max(0.1, deadline - time.time()))
            if snap is None:
                break
            if "reap-test" not in board_ids(snap):
                gone_at = time.time() - t0
                break
        case("reaper prunes + broadcasts on its timer",
             seen and gone_at is not None and gone_at < 12,
             f"create={st}, board seen={seen}, pruned broadcast after "
             f"{gone_at if gone_at is None else round(gone_at, 1)}s")
    finally:
        conn.close()


def test_disconnect_and_leak(base):
    # Let any subscriber left by an earlier test finish disconnecting so the
    # baseline is stable, then measure the leak.
    wait_subs(lambda n: n == 0, 12)
    before = len(lw.SUBS)
    conns = []
    for _ in range(20):
        conn = SSEConn(base)
        conn.next_data()  # ensure the subscriber is live
        conns.append(conn)
    peak = wait_subs(lambda n: n >= before + 20, 8)
    case("20 clients subscribe", peak >= before + 20, f"SUBS {before} -> {peak}")

    for conn in conns:
        conn.close()
    after = wait_subs(lambda n: n <= before, 12)
    case("SUBS returns to baseline after 20 disconnects",
         after == before, f"before={before}, peak={peak}, after={after}")

    # Sequential connect/read/close loop (table B: 20 in a loop).
    for _ in range(20):
        conn = SSEConn(base)
        conn.next_data()
        conn.close()
    after_loop = wait_subs(lambda n: n <= before, 12)
    case("SUBS leak-free after 20 connect/close loops",
         after_loop == before, f"before={before}, after={after_loop}")


def test_keepalive(base):
    """S5: silence must produce a keepalive SNAPSHOT — a `data:` frame, not a
    bare `: keepalive` comment — and it must not end the stream.

    The client measures data age by snapshots only (comments are skipped in the
    transport), so a bare comment left a healthy, connected wall flip its chip
    to "stale" after 45s of no board changes and announce "Not receiving live
    updates" while connected.
    The keepalive now carries the full snapshot, so "no snapshot for 45s"
    really means the stream is dead (three missed keepalives).
    """
    old = getattr(lw, "KEEPALIVE", 15)
    lw.KEEPALIVE = 0.5
    conn = SSEConn(base)
    try:
        conn.next_data()  # initial snapshot
        # The silence window itself must deliver a data frame (the keepalive
        # snapshot). Pre-fix this returned None: only a comment arrived, which
        # next_data skips, and the client's data age grew without bound.
        silence_snap = conn.next_data(timeout=4)
        got_snapshot = silence_snap is not None
        call(base, "POST", "/api/boards/ka-test",
             {"title": "K", "status": "running", "blocks": []})
        snap = conn.next_data(timeout=4)
        alive = snap is not None and "ka-test" in board_ids(snap)
        case("keepalive fires as a snapshot and stream survives TimeoutError",
             got_snapshot and alive,
             f"silence-snapshot={got_snapshot}, post-keepalive frame={alive}")
    finally:
        lw.KEEPALIVE = old
        conn.close()
        lw.KEEPALIVE = old


def test_pre_iteration_disconnect_leak():
    """A disconnect BEFORE the generator's first iteration must not leak the queue.

    Starlette's StreamingResponse sends `http.response.start` (the headers)
    BEFORE the first `async for` over the body iterator (starlette/
    responses.py:251). On the ASGI <2.4 path uvicorn h11 actually takes
    (spec_version "2.3"), `stream_response` runs concurrently with
    `listen_for_disconnect`; a client that disconnects inside that header
    window cancels the send before the generator is ever started. A Python
    generator that was never started does NOT run its `finally` on close()
    (verified: `g = gen(); g.close()` skips the finally), so
    `_stream_gen`'s `SUBS.discard(q)` never executes and `aclose()` in
    _StreamResponse cannot rescue it — the queue stays in SUBS forever. Each
    such leak consumes one slot of MAX_SUBSCRIBERS, so enough early
    disconnects wedge the stream endpoint at its 503 cap until restart.

    The injection is synthetic by design: a `send` that fails immediately,
    driven through the real `_StreamResponse.__call__` on the real installed
    Starlette (scope without `asgi.spec_version` -> the collapsing-task-group
    path, same branch uvicorn takes).
    """
    async def run():
        q = asyncio.Queue(maxsize=lw.STREAM_QUEUE_MAX)
        lw.SUBS.add(q)
        resp = lw._StreamResponse(
            lw._stream_gen(q), queue=q, media_type="text/event-stream")
        sends = 0

        async def failing_send(msg):
            nonlocal sends
            sends += 1
            raise OSError("client gone before headers flushed")

        async def recv():
            await asyncio.sleep(3600)  # cancelled by the task group on failure

        scope = {"type": "http", "method": "GET", "path": "/api/stream"}
        try:
            await resp(scope, recv, failing_send)
        except BaseException:
            pass  # the disconnect surfaces; the accounting is what matters
        return q, sends

    before = len(lw.SUBS)
    q, sends = asyncio.run(run())
    leaked = q in lw.SUBS
    after = len(lw.SUBS)
    case("disconnect before the generator starts: SUBS drains (no leak)",
         not leaked and after == before and sends >= 1,
         f"sends attempted={sends}, leaked={leaked}, SUBS {before} -> {after}")


def test_version_monotonic(base):
    """S7: concurrent writers cannot collide on a version."""
    call(base, "POST", "/api/boards/ver-test",
         {"title": "V", "status": "running",
          "blocks": [{"id": "p", "type": "progress", "value": 0, "max": 100}]})
    got = []
    lock = threading.Lock()

    def worker():
        st, body = call(base, "POST", "/api/boards/ver-test/update",
                        {"id": "p", "set": {"value": 1}})
        if st == 200:
            with lock:
                got.append(body["version"])

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    unique = len(set(got)) == len(got)
    case("S7 versions unique under 12 concurrent writers",
         len(got) == 12 and unique and min(got) >= 1,
         f"versions={sorted(got)}")


def test_snapshot_consistency(base):
    """S8: every board in a snapshot carries its own blocks."""
    conn = SSEConn(base)
    try:
        frame = conn.next_data()
        ok = bool(frame["boards"])
        for b in frame["boards"]:
            if not isinstance(b.get("blocks"), list):
                ok = False
        case("S8 every snapshot board has blocks",
             ok, f"boards={len(frame['boards'])}")
    finally:
        conn.close()


def test_transport_agnostic(base):
    """The parser must decode both chunked and raw bodies.

    Measured on this box (uvicorn/h11): an SSE response is `transfer-encoding:
    chunked` for BOTH HTTP/1.1 and HTTP/1.0 — HTTP/1.0 does NOT fall back to
    `Connection: close`, contrary to the assumption the first version of this
    harness made. Both versions are still exercised explicitly, because a parser
    that handles only one framing silently times out on the other, and the
    framing is a property of the server, not of the request version.
    """
    outcomes = {}
    for version in ("HTTP/1.1", "HTTP/1.0"):
        conn = SSEConn(base, version=version)
        try:
            conn.next_data()  # headers + initial snapshot
            bid = "xport-" + version.replace("/", "").replace(".", "").lower()
            st, _ = call(base, "POST", f"/api/boards/{bid}",
                         {"title": "X", "status": "running", "blocks": []})
            snap = conn.next_data(timeout=8)
            outcomes[version] = (st == 200, conn.chunked,
                                 snap is not None and bid in board_ids(snap))
        finally:
            conn.close()
    ok = all(created and seen for created, _, seen in outcomes.values())
    case("parser handles chunked and raw transports",
         ok, f"HTTP/1.1 create/chunked/seen={outcomes['HTTP/1.1']}, "
             f"HTTP/1.0 create/chunked/seen={outcomes['HTTP/1.0']}")


def test_body_cap(base):
    """A request body over MAX_BODY_BYTES is refused with 413, not read whole.

    The cap is a module global so it can be lowered here without sending
    megabytes; the assertion is that both an advertised `Content-Length` and a
    `Transfer-Encoding: chunked` body (which has no Content-Length to trust) are
    refused once they exceed it.
    """
    old = getattr(lw, "MAX_BODY_BYTES", None)
    lw.MAX_BODY_BYTES = 1024
    host, port = base.split("//")[1].split(":")
    port = int(port)

    def raw(req_bytes):
        s = socket.create_connection((host, port), timeout=8)
        try:
            s.sendall(req_bytes)
            return _read_all(s)
        finally:
            s.close()

    try:
        body = b'{"blocks":[{"type":"text","text":"' + b"x" * 2048 + b'"}]}'
        resp = raw(
            b"POST /api/boards/bodycap HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
        case("oversize Content-Length -> 413",
             b" 413 " in resp.split(b"\r\n", 1)[0], resp[:120].decode(errors="replace"))

        chunk = b'{"blocks":[{"type":"text","text":"' + b"y" * 2048 + b'"}]}'
        framed = b"%x\r\n%s\r\n0\r\n\r\n" % (len(chunk), chunk)
        resp = raw(
            b"POST /api/boards/bodycap2 HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + framed)
        case("oversize chunked body -> 413",
             b" 413 " in resp.split(b"\r\n", 1)[0], resp[:120].decode(errors="replace"))
    finally:
        if old is not None:
            lw.MAX_BODY_BYTES = old


def _read_all(sock, timeout=8):
    sock.settimeout(timeout)
    out = b""
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            out += data
    except OSError:
        pass
    return out


def _status_of(base):
    """Open a raw GET /api/stream and return its HTTP status line."""
    host, port = base.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=8)
    try:
        s.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n"
                  b"Accept: text/event-stream\r\nConnection: close\r\n\r\n")
        data = b""
        s.settimeout(8)
        try:
            while b"\r\n\r\n" not in data:
                d = s.recv(65536)
                if not d:
                    break
                data += d
        except OSError:
            pass
        return data.split(b"\r\n", 1)[0].decode(errors="replace")
    finally:
        s.close()


def test_subscriber_cap(base):
    """Concurrent SSE subscribers are capped; N+1 is refused with 503."""
    old = getattr(lw, "MAX_SUBSCRIBERS", None)
    wait_subs(lambda n: n == 0, 12)
    baseline = len(lw.SUBS)
    if old is not None:
        lw.MAX_SUBSCRIBERS = baseline + 2
    conns = []
    try:
        for _ in range(2):
            c = SSEConn(base)
            c.next_data()
            conns.append(c)
        live = wait_subs(lambda n: n >= baseline + 2, 8)
        case("subscriber cap: N subscribers register",
             live == baseline + 2, f"baseline={baseline}, SUBS={live}")
        status = _status_of(base)
        case("subscriber cap: N+1 -> 503", "503" in status, status)
        still = wait_subs(lambda n: n <= baseline + 2, 3)
        case("subscriber cap: SUBS stays capped",
             still <= baseline + 2, f"SUBS={still}")
    finally:
        for c in conns:
            c.close()
        wait_subs(lambda n: n <= baseline, 12)
        if old is not None:
            lw.MAX_SUBSCRIBERS = old


def test_nonreader_disconnect_drains(base):
    """S9: a client that stops reading mid-stream must not leak its queue.

    Once the client stops reading, asyncio flow control parks the response
    task's send; on disconnect Starlette (ASGI 2.3 path) cancels the
    response task mid-send and does not close the body iterator — the
    generator suspended at its yield was ABANDONED with its `finally`
    unexecuted, and SUBS kept the queue until cyclic GC happened to run
    (never, on an idle server). _StreamResponse closes the iterator on
    every exit path; SUBS must return to baseline promptly.

    Eight boards with max-length text blocks make each snapshot ~65 kB, so
    ~60 broadcasts overflow the client's kernel receive buffer and the
    server's write buffer — the flow-control parking the abandonment needs.
    Every storm POST is status-checked: a refused POST (contract violation,
    board cap) would otherwise make the case vacuously pass.
    """
    wait_subs(lambda n: n == 0, 12)
    before = len(lw.SUBS)
    conns = []
    try:
        for _ in range(5):
            c = SSEConn(base)
            c.next_data()  # initial snapshot; the client then stops reading
            conns.append(c)
        live = wait_subs(lambda n: n >= before + 5, 8)
        case("non-reader: 5 clients subscribe", live == before + 5,
             f"SUBS={live} (before={before})")
        ok = True
        for i in range(8):
            st, _ = call(base, "POST", f"/api/boards/fat{i}",
                         {"title": "F", "status": "running", "blocks":
                          [{"id": "t", "type": "text", "text": "x" * 8192}]})
            ok = ok and st == 200
        for i in range(60):
            st, _ = call(base, "POST", f"/api/boards/fat{i % 8}/update",
                         {"id": "t", "set": {"text": "y" * 8192}})
            ok = ok and st == 200
        case("non-reader: backlog posts accepted", ok,
             "every storm POST returned 200" if ok else "a storm POST was refused")
        still = wait_subs(lambda n: n >= before + 5, 3)
        case("non-reader: streams stay subscribed under backlog",
             still == before + 5, f"SUBS={still}")
    finally:
        for c in conns:
            c.close()
        after = wait_subs(lambda n: n <= before, 5)
        case("S9 non-reading disconnect drains SUBS", after == before,
             f"before={before}, after={after}")
        delete_all(base)
        wait_subs(lambda n: n == 0, 12)


def test_sigterm_exits_promptly_with_stream():
    """S10: SIGTERM with a connected stream must exit promptly.

    Boots `python3 app.py` exactly the way the systemd unit does (a real
    process, a real signal), opens one stream, then SIGTERMs it. uvicorn's
    graceful shutdown waits for in-flight connections, and an SSE stream
    ends only when the CLIENT disconnects — so without
    GRACEFUL_SHUTDOWN_TIMEOUT the process stays up until systemd's stop
    times out and SIGKILLs it (the "State 'stop-sigterm' timed out.
    Killing." journal entries; measured pre-fix: still alive after 60s).
    """
    port = free_port()
    env = dict(os.environ)
    env.update(LIVEWALL_HOST="127.0.0.1", LIVEWALL_PORT=str(port),
               LIVEWALL_CONFIG_FILE=str(Path(_TMP) / "sigterm-config.json"),
               LIVEWALL_LOG_LEVEL="warning")
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "app.py")],
        env=env, cwd=str(BACKEND),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    conn = None
    try:
        base = f"http://127.0.0.1:{port}"
        # `call` returns (-1, ...) instead of raising, so gate on the status.
        for _ in range(100):
            st, _ = call(base, "GET", "/health")
            if st == 200:
                break
            time.sleep(0.1)
        conn = SSEConn(base)
        conn.next_data()
        t0 = time.time()
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            case("S10 SIGTERM with stream exits promptly", False,
                 "process still alive after 10s — graceful shutdown unbounded")
        else:
            elapsed = time.time() - t0
            case("S10 SIGTERM with stream exits promptly", elapsed < 8,
                 f"exited in {elapsed:.1f}s (rc={rc})")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if conn is not None:
            conn.close()


def main():
    srv = Server()
    if not srv.start():
        print("  GAP   backend failed to start")
        return 1
    try:
        delete_all(srv.base)
        print("== stream & concurrency ==")
        test_queue_bound_and_coalescing(srv.base)
        test_live_push(srv.base)
        test_reaper_prunes_and_broadcasts(srv.base)
        test_disconnect_and_leak(srv.base)
        test_keepalive(srv.base)
        test_version_monotonic(srv.base)
        test_snapshot_consistency(srv.base)
        test_transport_agnostic(srv.base)
        test_body_cap(srv.base)
        test_subscriber_cap(srv.base)
        test_nonreader_disconnect_drains(srv.base)
        test_sigterm_exits_promptly_with_stream()
        test_pre_iteration_disconnect_leak()
    finally:
        srv.stop()

    gaps = [r for r in results if not r[1]]
    print(f"\n=== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ===")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
