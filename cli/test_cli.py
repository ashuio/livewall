#!/usr/bin/env python3
"""CLI end-to-end — drives a real backend through the `livewall` command.

Boots its OWN throwaway backends (one open, one token-protected) and runs the
CLI as a subprocess against them, asserting on the exit code AND the output, so
it tests the shipped entry point rather than importing the module.

Exits non-zero on any gap. Self-contained: no dependency on the runner's server.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "backend" / "app.py"
CLI = REPO / "cli" / "livewall.py"
TOKEN = "cli-test-token-xyz"

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


class HtmlServer:
    """A reachable server that answers every GET with text/html and a given
    status (200 by default; a 4xx/5xx exercises the HTTPError path)."""

    def __init__(self, status: int = 200):
        self.status = status
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = None

    def start(self) -> None:
        status = self.status

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>not livewall</html>")

            def log_message(self, format, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class JsonServer:
    """A reachable server that answers every GET with 200 application/json and
    a body that is valid JSON but not the Livewall shape the caller expects."""

    def __init__(self, payload):
        self.payload = payload
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = None

    def start(self) -> None:
        payload = self.payload

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class Server:
    def __init__(self, token: str | None = None):
        self.token = token
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        env = dict(os.environ)
        env.update({
            "LIVEWALL_HOST": "127.0.0.1",
            "LIVEWALL_PORT": str(self.port),
            "LIVEWALL_CONFIG_FILE": os.path.join(
                tempfile.mkdtemp(prefix="lw-cli-"), "config.json"),
            "LIVEWALL_WEB_DIR": str(REPO / "web" / "no-such-dist"),
        })
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


def cli(base: str, *args: str, token: str | None = None, timeout: float = 20.0):
    """Run the CLI; return (exit_code, stdout, stderr)."""
    env = dict(os.environ)
    env["LIVEWALL_URL"] = base
    if token is None:
        env.pop("LIVEWALL_TOKEN", None)
    else:
        env["LIVEWALL_TOKEN"] = token
    p = subprocess.run([sys.executable, str(CLI), *args], env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def run() -> int:
    print("== open backend ==")
    srv = Server(token=None)
    if not srv.start():
        case("backend boots", False, "no /health")
        return 1
    try:
        # health
        rc, out, err = cli(srv.base, "health")
        case("health: exit 0 + ok:true", rc == 0 and '"ok": true' in out,
             f"rc={rc}")

        # set -> get
        rc, out, err = cli(srv.base, "set", "cli1", "--title", "T1", "--status", "running")
        case("set: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "get", "cli1", "--json")
        ok = rc == 0 and json.loads(out).get("title") == "T1"
        case("get --json: round-trips the title", ok, f"rc={rc}")

        # collision -> 409, non-zero, named
        rc, out, err = cli(srv.base, "set", "cli1", "--title", "T2")
        case("set on an existing id: non-zero + names replace",
             rc != 0 and "replace" in (out + err).lower(), f"rc={rc}")
        case("set on an existing id: message names the --replace flag",
             rc != 0 and "--replace" in (out + err), f"rc={rc}")

        # --replace works
        rc, out, err = cli(srv.base, "set", "cli1", "--title", "T2", "--replace")
        case("set --replace: exit 0", rc == 0, f"rc={rc}")

        # update --set
        rc, out, err = cli(srv.base, "set", "cli2", "--title", "U",
                           "--blocks-file", _blocks_file())
        case("set --blocks-file: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "update", "cli2", "--block", "p",
                           "--set", '{"value": 40}')
        case("update --set: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "get", "cli2", "--json")
        blocks = {b["id"]: b for b in json.loads(out).get("blocks", [])}
        case("update --set took effect", blocks.get("p", {}).get("value") == 40,
             f"value={blocks.get('p', {}).get('value')}")

        # update --append on a log block
        rc, out, err = cli(srv.base, "update", "cli2", "--block", "l",
                           "--append", "appended line")
        case("update --append: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")

        # reset clears a board's blocks (the board itself remains)
        rc, out, err = cli(srv.base, "reset", "cli2")
        case("reset: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "get", "cli2", "--json")
        case("reset: cleared the blocks",
             rc == 0 and not json.loads(out).get("blocks"), f"rc={rc}")

        # ls
        rc, out, err = cli(srv.base, "ls")
        case("ls: lists both boards", rc == 0 and "cli1" in out and "cli2" in out,
             f"rc={rc}")

        # bad JSON to --set
        rc, out, err = cli(srv.base, "update", "cli2", "--block", "p", "--set", "{bad")
        case("update --set with bad JSON: non-zero + clear error",
             rc != 0 and "json" in (out + err).lower(), f"rc={rc}")

        # set with nothing
        rc, out, err = cli(srv.base, "set", "cli3")
        case("set with no fields: non-zero + clear error",
             rc != 0 and "nothing to set" in (out + err).lower(), f"rc={rc}")

        # malformed JSON in a --file/--blocks-file/--tab-file -> clear error, no traceback
        badjson = _bad_json_file()
        rc, out, err = cli(srv.base, "set", "clibad", "--file", badjson)
        case("set --file invalid JSON: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "json" in (out + err).lower(), f"rc={rc}")
        rc, out, err = cli(srv.base, "set", "clibad", "--blocks-file", badjson)
        case("set --blocks-file invalid JSON: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "json" in (out + err).lower(), f"rc={rc}")
        rc, out, err = cli(srv.base, "set", "clibad", "--tab-file", badjson)
        case("set --tab-file invalid JSON: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "json" in (out + err).lower(), f"rc={rc}")
        # an unreadable path (a directory) is an OSError, not a traceback
        rc, out, err = cli(srv.base, "set", "clibad", "--file", tempfile.mkdtemp())
        case("set --file is a directory: non-zero + no traceback",
             rc != 0 and "traceback" not in (out + err).lower(), f"rc={rc}")

        # config round-trip
        rc, out, err = cli(srv.base, "config", "--set", "default_ttl=60")
        case("config --set: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "config")
        case("config: shows the new value",
             rc == 0 and json.loads(out).get("default_ttl") == 60, f"rc={rc}")
        rc, out, err = cli(srv.base, "config", "--set", "bogus_key=5")
        case("config --set unknown key: refused, not a silent no-op",
             rc != 0 and "bogus_key" in (out + err), f"rc={rc}")

        # demo
        rc, out, err = cli(srv.base, "demo", "cli-demo")
        case("demo: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")

        # watch --once
        rc, out, err = cli(srv.base, "watch", "cli1", "--once")
        case("watch --once: prints the board", rc == 0 and "T2" in out, f"rc={rc}")

        # contract-legal object form for bar `data` / kv `items` must render
        rc, out, err = cli(srv.base, "set", "cliobj", "--blocks-file", _obj_blocks_file())
        case("set bar/kv object form: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "get", "cliobj")
        case("get bar/kv object form: renders, no traceback",
             rc == 0 and "alpha" in out and "cpu" in out
             and "traceback" not in (out + err).lower(), f"rc={rc}")
        rc, out, err = cli(srv.base, "watch", "cliobj", "--once")
        case("watch bar/kv object form: renders, no traceback",
             rc == 0 and "alpha" in out and "traceback" not in (out + err).lower(),
             f"rc={rc}")

        # a `line` block renders its series, never the unknown-block fallback
        rc, out, err = cli(srv.base, "set", "cliline", "--blocks-file", _line_blocks_file())
        case("set line block: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
        rc, out, err = cli(srv.base, "get", "cliline")
        case("get line block: both series render, no unknown fallback",
             rc == 0 and "p50" in out and "p95" in out and "4 5 6" in out
             and "[unknown block" not in out, f"rc={rc}")
        case("get line block: values-sugar renders",
             rc == 0 and "7 8 9" in out, f"rc={rc}")

        # watch --interval must be a positive, finite number of seconds
        for bad_interval in ("-1", "nan", "inf"):
            rc, out, err = cli(srv.base, "watch", "cli1", "--interval", bad_interval)
            case(f"watch --interval {bad_interval}: refused, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower(), f"rc={rc}")

        # rm -> gone
        rc, out, err = cli(srv.base, "rm", "cli1")
        case("rm: exit 0", rc == 0, f"rc={rc}")
        rc, out, err = cli(srv.base, "get", "cli1")
        case("get after rm: non-zero (404)", rc != 0, f"rc={rc}")

        # watch a missing board exits cleanly (0), per the watcher contract
        rc, out, err = cli(srv.base, "watch", "nope", "--once")
        case("watch a missing board: exits 0 (clean)", rc == 0, f"rc={rc}")

        # unreachable backend -> clear error, not a traceback
        rc, out, err = cli("http://127.0.0.1:1", "ls")
        case("unreachable backend: non-zero + clear error",
             rc != 0 and "cannot reach" in (out + err).lower()
             and "traceback" not in (out + err).lower(), f"rc={rc}")

        # a reachable server that is not Livewall (HTML 200) -> clear error, not a traceback
        html = HtmlServer()
        html.start()
        try:
            rc, out, err = cli(html.base, "ls")
            case("non-JSON 200 response: non-zero + clear, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower()
                 and "json" in (out + err).lower(), f"rc={rc}")
        finally:
            html.stop()

        # a non-JSON ERROR body (5xx HTML) exercises the HTTPError path: the
        # status must still surface as a clean error, not a traceback. (Coverage
        # guard for the narrowed handler; it passes both before and after the
        # narrowing, so it is not bite evidence.)
        html500 = HtmlServer(status=500)
        html500.start()
        try:
            rc, out, err = cli(html500.base, "ls")
            case("non-JSON 500 error body: non-zero + clear, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower()
                 and "500" in (out + err), f"rc={rc}")
        finally:
            html500.stop()

        # a JSON 200 whose board has a non-dict block element -> the good
        # siblings must still render, no traceback (the guard must not swallow
        # the whole board)
        js = JsonServer({"title": "J",
                         "blocks": ["bad", {"type": "text", "text": "good"}]})
        js.start()
        try:
            rc, out, err = cli(js.base, "get", "x")
            case("non-dict block element: renders the good sibling, no traceback",
                 rc == 0 and "good" in out
                 and "traceback" not in (out + err).lower(), f"rc={rc}")
        finally:
            js.stop()

        # valid JSON of the wrong shape (an array) -> clear error, not a traceback
        jl = JsonServer([1, 2, 3])
        jl.start()
        try:
            rc, out, err = cli(jl.base, "ls")
            case("non-object JSON list from ls: non-zero + clear, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower()
                 and "json" in (out + err).lower(), f"rc={rc}")
            rc, out, err = cli(jl.base, "get", "x")
            case("non-object JSON list from get: non-zero + clear, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower()
                 and "json" in (out + err).lower(), f"rc={rc}")
        finally:
            jl.stop()

        # a `boards` value that is not a list -> clear error, not a traceback
        jb = JsonServer({"boards": "nope"})
        jb.start()
        try:
            rc, out, err = cli(jb.base, "ls")
            case("non-list `boards` value: non-zero + clear, no traceback",
                 rc != 0 and "traceback" not in (out + err).lower()
                 and "list" in (out + err).lower(), f"rc={rc}")
        finally:
            jb.stop()

        # a scheme-less / malformed --url -> clear error, not a traceback
        rc, out, err = cli("foo", "ls")
        case("malformed URL (no scheme): non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "url" in (out + err).lower(), f"rc={rc}")

        # a malformed IPv6 URL (unbalanced bracket) makes urlparse itself raise
        rc, out, err = cli("http://[::1", "ls")
        case("malformed IPv6 URL: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "url" in (out + err).lower(), f"rc={rc}")

        # a board id with a space (URL-hostile) -> clear error, not a traceback
        rc, out, err = cli(srv.base, "get", "a b")
        case("board id with a space: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower(), f"rc={rc}")
        rc, out, err = cli(srv.base, "rm", "a b")
        case("rm board id with a space: non-zero + no traceback",
             rc != 0 and "traceback" not in (out + err).lower(), f"rc={rc}")

        # a token containing a control character -> clear error, not a traceback
        rc, out, err = cli(srv.base, "ls", token="abc\ndef")
        case("token with a newline: non-zero + clear, no traceback",
             rc != 0 and "traceback" not in (out + err).lower()
             and "token" in (out + err).lower(), f"rc={rc}")
    finally:
        srv.stop()

    print("== token-protected backend ==")
    sp = Server(token=TOKEN)
    if not sp.start():
        case("protected backend boots", False, "no /health")
        return 1
    try:
        rc, out, err = cli(sp.base, "ls")
        case("no token: non-zero + 'unauthorized'",
             rc != 0 and "unauthorized" in (out + err).lower(), f"rc={rc}")

        rc, out, err = cli(sp.base, "ls", token=TOKEN)
        case("--token via env: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")

        rc, out, err = cli(sp.base, "--token", TOKEN, "ls")
        case("--token flag: exit 0", rc == 0, f"rc={rc} {err.strip()[:80]}")
    finally:
        sp.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    gaps = [n for n, ok, _ in results if not ok]
    print(f"\n== {passed} pass, {len(gaps)} gap(s) ==")
    for n in gaps:
        print(f"  GAP: {n}")
    return 0 if not gaps else 1


def _blocks_file() -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="lw-blocks-")
    with os.fdopen(fd, "w") as f:
        json.dump([
            {"id": "p", "type": "progress", "label": "steps", "value": 1, "max": 5},
            {"id": "l", "type": "log", "title": "log", "lines": ["first"]},
        ], f)
    return path


def _bad_json_file() -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="lw-badjson-")
    with os.fdopen(fd, "w") as f:
        f.write("{not json")
    return path


def _line_blocks_file() -> str:
    """A board with a `line` block, both multi-series and the `values` sugar."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="lw-lineblocks-")
    with os.fdopen(fd, "w") as f:
        json.dump([
            {"id": "t", "type": "line", "label": "Tokens",
             "series": [{"name": "p50", "values": [1, 2, 3]},
                        {"name": "p95", "values": [4, 5, 6]}]},
            {"id": "s", "type": "line", "label": "Burn", "values": [7, 8, 9]},
        ], f)
    return path


def _obj_blocks_file() -> str:
    """A board whose bar/kv rows use the object form the contract also allows."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="lw-objblocks-")
    with os.fdopen(fd, "w") as f:
        json.dump([
            {"id": "b", "type": "bar", "label": "bars",
             "data": [{"label": "alpha", "value": 3}]},
            {"id": "k", "type": "kv",
             "items": [{"k": "cpu", "v": "10%"}]},
        ], f)
    return path


if __name__ == "__main__":
    sys.exit(run())
