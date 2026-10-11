#!/usr/bin/env python3
"""Config persistence across a restart.

`make test` boots ONE backend for the suite; this suite boots its OWN throwaway
backends so it can stop and restart the process against the same
`LIVEWALL_CONFIG_FILE` and observe what actually survives. It covers:

  1. round-trip: `PUT /api/config`, stop, restart -> the value survived.
  2. missing directory: the config path's parent does not exist -> the PUT is
     REFUSED (500, `ok:false` naming the path) rather than reporting a write
     that could not happen, and memory is left unchanged.
  3. corrupt file: a file that is not valid JSON, and one that is valid JSON but
     not an object -> the server still boots on the seeded defaults and LOGS the
     discard, so the operator can see their saved config was thrown away.
  4. invalid env value: a non-numeric `LIVEWALL_MAX_BOARDS` falls back to the
     default AND logs it, rather than silently ignoring the operator's setting.

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

# Seed the env so the assertions on "defaults" are hermetic regardless of the
# ambient environment the suite is launched from.
SEED = {
    "LIVEWALL_DEFAULT_TTL": "1800",
    "LIVEWALL_STALE_TTL": "3600",
    "LIVEWALL_MAX_BOARDS": "12",
}

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


def call(method: str, base: str, path: str, body=None):
    req = urllib.request.Request(
        base + path, method=method,
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


class Server:
    """A backend subprocess pinned to one config-file path."""

    def __init__(self, cfg_path, extra_env=None):
        self.cfg_path = str(cfg_path)
        self.extra_env = extra_env or {}
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        env = dict(os.environ)
        env.update(SEED)
        env.update(self.extra_env)
        env.update({
            "LIVEWALL_HOST": "127.0.0.1",
            "LIVEWALL_PORT": str(self.port),
            "LIVEWALL_CONFIG_FILE": self.cfg_path,
            "LIVEWALL_WEB_DIR": str(REPO / "web" / "no-such-dist"),
        })
        self.proc = subprocess.Popen(
            [sys.executable, str(APP)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return wait_health(self.base)

    def stop(self) -> str:
        if self.proc is None:
            return ""
        self.proc.terminate()
        try:
            _, err = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        self.proc = None
        return (err or b"").decode(errors="replace")


def test_round_trip() -> None:
    print("== 1. config survives a restart ==")
    tmp = tempfile.mkdtemp(prefix="lw-cfg-rt-")
    cfg = Path(tmp) / "config.json"

    s1 = Server(cfg)
    try:
        case("server boots (round-trip)", s1.start())
        st, body = call("PUT", s1.base, "/api/config", {"max_boards": 7, "default_ttl": 123})
        case("PUT /api/config succeeds when the directory exists",
             st == 200 and body.get("ok") is True, f"status {st} body {body}")
        case("PUT /api/config wrote the file", cfg.exists(), f"path {cfg}")
    finally:
        s1.stop()

    s2 = Server(cfg)
    try:
        case("server restarts on the same config file", s2.start())
        st, body = call("GET", s2.base, "/api/config")
        case("config survived the restart",
             st == 200 and body.get("max_boards") == 7 and body.get("default_ttl") == 123,
             f"status {st} body {body}")
    finally:
        s2.stop()


def test_missing_directory() -> None:
    print("\n== 2. config path whose parent directory does not exist ==")
    tmp = tempfile.mkdtemp(prefix="lw-cfg-miss-")
    cfg = Path(tmp) / "no-such-dir" / "config.json"

    s = Server(cfg)
    try:
        case("server boots (missing dir)", s.start())
        st, body = call("PUT", s.base, "/api/config", {"max_boards": 9})
        case("missing dir -> PUT refused, not a false ok",
             st >= 400 and body.get("ok") is False
             and "persist" in str(body.get("error", "")),
             f"status {st} body {body}")
        err_text = str(body.get("error", ""))
        case("missing dir -> 500 body leaks no server path or exception text",
             "no-such-dir" not in err_text and str(cfg) not in err_text
             and "Errno" not in err_text,
             f"error={err_text!r}")
        case("missing dir -> no file created", not cfg.exists(), f"path {cfg}")
        st2, body2 = call("GET", s.base, "/api/config")
        case("missing dir -> memory unchanged after the failed write",
             st2 == 200 and body2.get("max_boards") == 12,
             f"status {st2} body {body2}")
    finally:
        s.stop()


def _corrupt_case(label: str, content: str) -> None:
    print(f"\n== 3. corrupt config file: {label} ==")
    tmp = tempfile.mkdtemp(prefix="lw-cfg-bad-")
    cfg = Path(tmp) / "config.json"
    cfg.write_text(content)

    s = Server(cfg)
    started = s.start()
    try:
        case(f"{label}: server still boots on defaults", started, f"health on {s.base}")
        if started:
            st, body = call("GET", s.base, "/api/config")
            case(f"{label}: defaults served",
                 st == 200 and body.get("max_boards") == 12
                 and body.get("default_ttl") == 1800,
                 f"status {st} body {body}")
    finally:
        err = s.stop()
    case(f"{label}: the discard is LOGGED",
         "could not load config" in err, err[-300:])


def _coerced_case(label: str, value) -> None:
    """A valid-JSON config file whose VALUE is illegal must not be coerced.

    `max(1, int(v))` used to boot `true` as 1, `7.9` as 7, `0`/`-5` as 1 with no
    warning — the same silent coercion the HTTP write path refuses. The load
    layer must apply the same rule and say so.
    """
    print(f"\n== 5. coerced config value: {label} ==")
    tmp = tempfile.mkdtemp(prefix="lw-cfg-coerce-")
    cfg = Path(tmp) / "config.json"
    cfg.write_text(json.dumps({"max_boards": value}))

    s = Server(cfg)
    started = s.start()
    try:
        case(f"{label}: server still boots on defaults", started, f"health on {s.base}")
        if started:
            st, body = call("GET", s.base, "/api/config")
            case(f"{label}: value NOT silently coerced (default served)",
                 st == 200 and body.get("max_boards") == 12,
                 f"status {st} body {body}")
    finally:
        err = s.stop()
    case(f"{label}: the refusal is LOGGED and names the key",
         "could not load config" in err and "max_boards" in err, err[-300:])


def test_invalid_env_logged() -> None:
    print("\n== 4. invalid env value is logged, not silently ignored ==")
    tmp = tempfile.mkdtemp(prefix="lw-cfg-env-")
    cfg = Path(tmp) / "config.json"
    s = Server(cfg, extra_env={"LIVEWALL_MAX_BOARDS": "abc"})
    started = s.start()
    try:
        case("invalid env: server still boots", started, f"health on {s.base}")
        if started:
            st, body = call("GET", s.base, "/api/config")
            case("invalid env: default is used",
                 st == 200 and body.get("max_boards") == 12,
                 f"status {st} body {body}")
    finally:
        err = s.stop()
    case("invalid env: the fallback is LOGGED",
         "invalid LIVEWALL_MAX_BOARDS" in err, err[-300:])


def test_ttl_env_seed_ceiling() -> None:
    """The env-seeded ttl keys are bounded like the write path.

    `PUT /api/config` refuses a ttl above 2**53 (`_config_value`) because the
    summary uses ttl in float arithmetic; the ENV seed bypassed that bound, so
    `LIVEWALL_DEFAULT_TTL=10**400` was silently accepted and every terminal
    board's expiry silently disabled. `_seed_max_boards` already clamps with a
    warning; the ttl seeds now do the same.
    """
    print("\n== 9. ttl env seed above the safe bound is clamped and logged ==")
    tmp = tempfile.mkdtemp(prefix="lw-ttl-seed-")
    s = Server(Path(tmp) / "config.json",
               extra_env={"LIVEWALL_DEFAULT_TTL": str(10 ** 400),
                          "LIVEWALL_STALE_TTL": str(10 ** 400)})
    started = s.start()
    try:
        case("ttl seed above bound: server boots", started)
        if started:
            st, body = call("GET", s.base, "/api/config")
            case("ttl seed above bound: clamped to 2**53",
                 st == 200 and body.get("default_ttl") == 2 ** 53
                 and body.get("stale_ttl") == 2 ** 53, f"body {body}")
    finally:
        err = s.stop()
    case("ttl seed above bound: the clamp is LOGGED",
         "exceeds the safe bound" in err and "LIVEWALL_DEFAULT_TTL" in err, err[-300:])


def test_invalid_keepalive_env() -> None:
    """A bad `LIVEWALL_KEEPALIVE` must not kill the boot.

    Every other knob is parsed by `_env_int` with a logged fallback; KEEPALIVE
    was a bare `float(os.environ.get(...))`, so `LIVEWALL_KEEPALIVE=abc`
    (an operator typo in a unit file) raised ValueError at import and the
    process never bound — while `0` or a negative value would make
    `asyncio.wait_for(..., timeout=0)` time out instantly and emit a
    keepalive comment in a hot loop.
    """
    print("\n== 8. invalid LIVEWALL_KEEPALIVE falls back and is logged ==")
    for bad, expect_default in (("abc", 15.0), ("0", 15.0), ("-3", 15.0)):
        tmp = tempfile.mkdtemp(prefix="lw-ka-")
        s = Server(Path(tmp) / "config.json", extra_env={"LIVEWALL_KEEPALIVE": bad})
        started = s.start()
        try:
            case(f"LIVEWALL_KEEPALIVE={bad!r}: server still boots", started,
                 f"health on {s.base}")
        finally:
            err = s.stop()
        case(f"LIVEWALL_KEEPALIVE={bad!r}: fallback logged and keeps {expect_default}",
             started and "LIVEWALL_KEEPALIVE" in err and str(expect_default) in err,
             err[-300:])


def test_max_boards_ceiling() -> None:
    """`max_boards` is bounded, so a huge value cannot disable eviction.

    Pre-fix, `PUT /api/config {"max_boards": 2**63}` was accepted (a valid
    positive int), which made `len(BOARDS) > max_boards` never true — eviction
    was off and boards accumulated without bound. A no-auth write API must not
    expose that. The ceiling is env-tunable so an operator can raise it.
    """
    print("\n== 6. max_boards ceiling ==")

    # (a) a huge runtime value is refused and does not take effect
    tmp = tempfile.mkdtemp(prefix="lw-cfg-ceil-")
    s = Server(Path(tmp) / "config.json")
    try:
        case("server boots (ceiling)", s.start())
        st, body = call("PUT", s.base, "/api/config", {"max_boards": 2 ** 63})
        case("huge max_boards refused (400, ok:false, names the shape)",
             st == 400 and body.get("ok") is False
             and "positive integer" in str(body.get("error", "")),
             f"status {st} body {body}")
        st2, body2 = call("GET", s.base, "/api/config")
        case("huge max_boards did not take effect in memory",
             st2 == 200 and body2.get("max_boards") == 12, f"body {body2}")
        st3, body3 = call("PUT", s.base, "/api/config", {"max_boards": 1000})
        case("max_boards at the ceiling is accepted",
             st3 == 200 and body3.get("config", {}).get("max_boards") == 1000,
             f"status {st3} body {body3}")
    finally:
        s.stop()

    # (b) a huge value already in the config FILE is discarded on load
    tmp2 = tempfile.mkdtemp(prefix="lw-cfg-ceilfile-")
    cfg2 = Path(tmp2) / "config.json"
    cfg2.write_text(json.dumps({"max_boards": 2 ** 63}))
    s2 = Server(cfg2)
    started = s2.start()
    try:
        case("huge value in config file: server boots on defaults", started)
        if started:
            st, body = call("GET", s2.base, "/api/config")
            case("huge value in config file: default served",
                 st == 200 and body.get("max_boards") == 12, f"body {body}")
    finally:
        err = s2.stop()
    case("huge value in config file: the discard is LOGGED",
         "could not load config" in err and "max_boards" in err, err[-300:])

    # (c) an env seed above the ceiling is clamped, and the clamp is logged
    tmp3 = tempfile.mkdtemp(prefix="lw-cfg-ceilenv-")
    s3 = Server(Path(tmp3) / "config.json", extra_env={"LIVEWALL_MAX_BOARDS": "5000"})
    started = s3.start()
    try:
        case("env above ceiling: server boots", started)
        if started:
            st, body = call("GET", s3.base, "/api/config")
            case("env above ceiling: clamped to the ceiling",
                 st == 200 and body.get("max_boards") == 1000, f"body {body}")
    finally:
        err = s3.stop()
    case("env above ceiling: the clamp is LOGGED",
         "exceeds the ceiling" in err, err[-300:])

    # (d) raising the ceiling lets the higher value through
    tmp4 = tempfile.mkdtemp(prefix="lw-cfg-ceilraise-")
    s4 = Server(Path(tmp4) / "config.json",
                extra_env={"LIVEWALL_MAX_BOARDS_LIMIT": "5000"})
    started = s4.start()
    try:
        case("raised ceiling: server boots", started)
        if started:
            st, body = call("PUT", s4.base, "/api/config", {"max_boards": 5000})
            case("raised ceiling: higher value accepted",
                 st == 200 and body.get("config", {}).get("max_boards") == 5000,
                 f"status {st} body {body}")
    finally:
        s4.stop()


def test_ttl_magnitude() -> None:
    """A `ttl` (or `default_ttl`/`stale_ttl`) above 2**53 is refused, not a 500.

    JSON integers are arbitrary precision, but the summary computes `ttl - age`
    as a float: a huge value raises OverflowError (a 500, not the named refusal
    the API promises) and, once stored, poisons every later read. The bound is
    applied by `_config_value` on BOTH the write path and the startup file load,
    so the two cannot disagree.
    """
    print("\n== 7. ttl / config magnitude ==")
    HUGE = 10 ** 400

    # (a) a huge runtime config value is refused and does not take effect
    tmp = tempfile.mkdtemp(prefix="lw-cfg-ttl-")
    s = Server(Path(tmp) / "config.json")
    try:
        case("server boots (ttl magnitude)", s.start())
        st, body = call("PUT", s.base, "/api/config", {"default_ttl": HUGE})
        case("huge default_ttl refused (400, ok:false, names the shape)",
             st == 400 and body.get("ok") is False
             and "positive integer" in str(body.get("error", "")),
             f"status {st} body {body}")
        st2, body2 = call("GET", s.base, "/api/config")
        case("huge default_ttl did not take effect in memory",
             st2 == 200 and body2.get("default_ttl") == 1800, f"body {body2}")
    finally:
        s.stop()

    # (b) a huge value already in the config FILE is discarded on load
    tmp2 = tempfile.mkdtemp(prefix="lw-cfg-ttlfile-")
    cfg2 = Path(tmp2) / "config.json"
    cfg2.write_text(json.dumps({"default_ttl": HUGE}))
    s2 = Server(cfg2)
    started = s2.start()
    try:
        case("huge ttl in config file: server boots on defaults", started)
        if started:
            st, body = call("GET", s2.base, "/api/config")
            case("huge ttl in config file: default served",
                 st == 200 and body.get("default_ttl") == 1800, f"body {body}")
    finally:
        err = s2.stop()
    case("huge ttl in config file: the discard is LOGGED",
         "could not load config" in err and "default_ttl" in err, err[-300:])


def main() -> int:
    test_round_trip()
    test_missing_directory()
    _corrupt_case("not JSON", "{ this is not json")
    _corrupt_case("JSON but not an object", "[1, 2, 3]")
    _coerced_case("bool true", True)
    _coerced_case("fraction 7.9", 7.9)
    _coerced_case("zero 0", 0)
    _coerced_case("negative -5", -5)
    test_invalid_env_logged()
    test_max_boards_ceiling()
    test_ttl_magnitude()
    test_invalid_keepalive_env()
    test_ttl_env_seed_ceiling()

    gaps = [r for r in results if not r[1]]
    print(f"\n=== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ===")
    for n, _, d in gaps:
        print(f"  GAP: {n} — {d}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
