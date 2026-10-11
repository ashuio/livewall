#!/usr/bin/env python3
"""Bearer-token auth gate — enforcement in BOTH directions.

`make test` boots ONE backend without a token; this suite boots its OWN
throwaway backends (one open, one token-protected) so it can assert the gate in
both directions — a protected surface refuses a missing/wrong token AND accepts
the right one — which a single-backend suite cannot.

Cases:
  1. token unset  -> the API is OPEN (a bare clone must run).
  2. token set    -> `/api/*` without a token is 401, and NOTHING was written.
  3. token set    -> the right `Authorization: Bearer` is accepted.
  4. token set    -> a WRONG bearer is 401.
  5. token set    -> the right `?token=` query is accepted.
  6. token set    -> a wrong `?token=` is 401.
  7. token set    -> `/health` is NEVER gated (a probe cannot carry a token).
  8. token set    -> the static client (`/`) is not gated.
  9. token set    -> a non-Bearer scheme (Basic) is refused, not accepted.
 10. token set    -> an empty `?token=` is refused.
 11. token set    -> a non-ASCII token is a clean 401, never a 500.
 12. every method on `/api/*` is gated (GET/POST/PUT/PATCH/DELETE).

Exits non-zero on any gap. Self-contained: no dependency on the runner's server.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
APP = REPO / "backend" / "app.py"

TOKEN = "s3cr3t-token-abc123"

results: list[tuple[str, bool, str]] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:160]}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(base: str, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def call(method: str, base: str, path: str, body=None, headers=None):
    """Return (status, parsed-body-or-{}). Never raises on HTTP errors."""
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, {"_raw": raw[:80]}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"err": str(e)}


class Server:
    """A backend subprocess, optionally token-protected."""

    def __init__(self, token: str | None, web_dir: str | None = None):
        self.token = token
        self.web_dir = web_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        env = dict(os.environ)
        env.update({
            "LIVEWALL_HOST": "127.0.0.1",
            "LIVEWALL_PORT": str(self.port),
            "LIVEWALL_CONFIG_FILE": os.path.join(
                tempfile.mkdtemp(prefix="lw-auth-"), "config.json"),
            "LIVEWALL_WEB_DIR": self.web_dir or str(REPO / "web" / "no-such-dist"),
        })
        # An empty token must behave exactly like unset (the switch is presence).
        if self.token is None:
            env.pop("LIVEWALL_TOKEN", None)
        else:
            env["LIVEWALL_TOKEN"] = self.token
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        return wait_health(self.base)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def run() -> int:
    # ---- 1. open backend (no token): the API must work with no credential ----
    print("== open backend (LIVEWALL_TOKEN unset) ==")
    op = Server(token=None)
    if not op.start():
        case("open backend boots", False, "no /health")
        return 1
    try:
        st, _ = call("GET", op.base, "/api/boards")
        case("no token set: /api/boards is open (200)", st == 200, f"status {st}")
        st, body = call("GET", op.base, "/health")
        case("no token set: /health is open (200)", st == 200, f"status {st}")
        case("no token set: /health returns the FULL payload (auth off)",
             "ids" in body and "config" in body, f"keys {sorted(body)}")
    finally:
        op.stop()

    # ---- 2. protected backend (token set) ----
    print("== protected backend (LIVEWALL_TOKEN set) ==")
    sp = Server(token=TOKEN)
    if not sp.start():
        case("protected backend boots", False, "no /health")
        return 1
    try:
        # 2. reads without a token are refused
        st, _ = call("GET", sp.base, "/api/boards")
        case("no token: GET /api/boards -> 401", st == 401, f"status {st}")

        # 3. a write without a token is refused AND does not take effect
        st, _ = call("POST", sp.base, "/api/boards/probe", {"title": "x"})
        case("no token: POST /api/boards/probe -> 401", st == 401, f"status {st}")
        st, _ = call("GET", sp.base, "/api/boards/probe", headers=bearer(TOKEN))
        case("the refused write did NOT create the board (404)",
             st == 404, f"status {st}")

        # 4. the right bearer is accepted (read + write)
        st, _ = call("GET", sp.base, "/api/boards", headers=bearer(TOKEN))
        case("correct bearer: GET /api/boards -> 200", st == 200, f"status {st}")
        st, _ = call("POST", sp.base, "/api/boards/ok", {"title": "t"},
                     headers=bearer(TOKEN))
        case("correct bearer: POST -> 200", st == 200, f"status {st}")

        # 5. a wrong bearer is refused
        st, _ = call("GET", sp.base, "/api/boards", headers=bearer("wrong"))
        case("wrong bearer -> 401", st == 401, f"status {st}")

        # 6. the query form works, and a wrong one does not
        st, _ = call("GET", sp.base, f"/api/boards?token={TOKEN}")
        case("correct ?token= -> 200", st == 200, f"status {st}")
        st, _ = call("GET", sp.base, "/api/boards?token=nope")
        case("wrong ?token= -> 401", st == 401, f"status {st}")

        # 7. /health is never gated, but an unauthenticated caller gets
        #    LIVENESS ONLY — no board ids, no config (they are the addressing
        #    namespace of every session and global state respectively).
        st, body = call("GET", sp.base, "/health")
        case("/health is never gated (200 without a token)", st == 200, f"status {st}")
        leaked = [k for k in ("boards", "ids", "subscribers", "config") if k in body]
        case("/health unauthenticated leaks no ids/config",
             body == {"ok": True} and not leaked, f"body {body}")
        # ... and an AUTHENTICATED caller still gets the full payload.
        st, body = call("GET", sp.base, "/health", headers=bearer(TOKEN))
        case("/health authenticated returns the full payload",
             st == 200 and "ids" in body and "config" in body,
             f"status {st} keys {sorted(body)}")

        # 8. the static client is not gated (web dir absent -> JSON pointer, 200)
        st, _ = call("GET", sp.base, "/")
        case("static client is not gated (200 without a token)", st == 200, f"status {st}")

        # 9. a non-Bearer scheme is refused
        st, _ = call("GET", sp.base, "/api/boards",
                     headers={"Authorization": f"Basic {TOKEN}"})
        case("non-Bearer scheme -> 401", st == 401, f"status {st}")

        # 10. an empty query token is refused
        st, _ = call("GET", sp.base, "/api/boards?token=")
        case("empty ?token= -> 401", st == 401, f"status {st}")

        # 11. a non-ASCII token is a clean 401, never a 500
        st, _ = call("GET", sp.base, "/api/boards",
                     headers={"Authorization": "Bearer tökén"})
        case("non-ASCII token -> 401 (not 500)", st == 401, f"status {st}")

        # 12. every method is gated
        for method, path in (("GET", "/api/config"),
                             ("PUT", "/api/config"),
                             ("PATCH", "/api/boards/ok"),
                             ("DELETE", "/api/boards/ok")):
            st, _ = call(method, sp.base, path, {"title": "z"} if method != "GET" else None)
            case(f"{method} {path} without a token -> 401", st == 401, f"status {st}")
    finally:
        sp.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    gaps = [n for n, ok, _ in results if not ok]
    print(f"\n== {passed} pass, {len(gaps)} gap(s) ==")
    for n in gaps:
        print(f"  GAP: {n}")
    return 0 if not gaps else 1


if __name__ == "__main__":
    sys.exit(run())
