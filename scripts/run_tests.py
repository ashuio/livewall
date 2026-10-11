#!/usr/bin/env python3
"""Livewall test runner — one command, a real exit code.

Boots a throwaway backend (free port, temp config file, web/dist served) and
runs the suites against it, then tears it down. The suites are integration
scripts (each drives the live backend over HTTP), so this orchestrates
subprocesses rather than using pytest directly.

ONE interpreter runs the backend and every suite: the `.venv/` that
`make install` builds. Resolution order (see `resolve_test_python`):

    LIVEWALL_TEST_PYTHON  ->  ./.venv/bin/python  ->  the interpreter running
                          this script

The per-suite overrides `LIVEWALL_MCP_PYTHON` / `LIVEWALL_PW_PYTHON` still win
for their own suite when set, so a split-interpreter setup keeps working.

Usage:
    python3 scripts/run_tests.py                 # build + every suite
    python3 scripts/run_tests.py backend cli     # a subset
    python3 scripts/run_tests.py --no-build web  # skip npm run build

Exit code is 0 only if every selected step passed; a dependency missing from the
selected suites exits 2 and names the fix (`make install`).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
BACKEND = REPO / "backend"
ALL = ("backend", "cli", "mcp", "web")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def resolve_test_python() -> str:
    """The interpreter that runs the backend and (by default) every suite.

    `make install` builds it at `.venv/`. An explicit `LIVEWALL_TEST_PYTHON`
    wins; then `.venv/`; then the interpreter running this script.
    """
    override = os.environ.get("LIVEWALL_TEST_PYTHON")
    if override:
        return override
    venv = REPO / ".venv/bin/python"
    if venv.exists():
        return str(venv)
    return sys.executable


TEST_PY = resolve_test_python()
MCP_PY = os.environ.get("LIVEWALL_MCP_PYTHON") or TEST_PY
PW_PY = os.environ.get("LIVEWALL_PW_PYTHON") or TEST_PY


def missing_imports(py: str, mods: list[str]) -> list[str]:
    """Which of `mods` the interpreter `py` cannot import (empty = all present)."""
    out: list[str] = []
    for m in mods:
        if subprocess.run([py, "-c", f"import {m}"], capture_output=True).returncode != 0:
            out.append(m)
    return out


def run_step(label: str, cmd: list[str], env: dict, cwd: Path) -> bool:
    print("\n" + "=" * 72)
    print(f"== {label}")
    print("==" + " " + " ".join(cmd))
    print("=" * 72, flush=True)
    proc = subprocess.run(cmd, env=env, cwd=str(cwd))
    print(f"-- {label}: {'PASS' if proc.returncode == 0 else f'FAIL (exit {proc.returncode})'}",
          flush=True)
    return proc.returncode == 0


def wait_health(base: str, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def main(argv: list[str]) -> int:
    build = True
    selected: list[str] = []
    for a in argv:
        if a == "--no-build":
            build = False
        elif a in ALL:
            selected.append(a)
        elif a == "all":
            selected = list(ALL)
        else:
            print(f"unknown argument: {a!r} (want one of {', '.join(ALL)})")
            return 2
    selected = selected or list(ALL)

    # Fail fast and LOUD when the interpreter for a selected suite cannot import
    # what it needs. The old runner silently fell back to the running
    # interpreter, so a clean checkout with no .venv died with a raw
    # ModuleNotFoundError from deep inside a suite — or, worse, ran the wrong
    # python and "passed" against a stale server.
    needed: list[tuple[str, str, list[str]]] = [("backend", TEST_PY, ["fastapi", "uvicorn"])]
    if "mcp" in selected:
        needed.append(("mcp", MCP_PY, ["fastmcp", "httpx"]))
    if "web" in selected:
        needed.append(("web", PW_PY, ["playwright"]))
    # `cli` needs only the standard library, and its suite drives a backend the
    # runner already boots — nothing to preflight.
    problems = [f"{label}: {py} cannot import `{m}`"
                for label, py, mods in needed for m in missing_imports(py, mods)]
    if problems:
        print("RESULT: FAIL (missing dependencies)")
        for p in problems:
            print(f"  {p}")
        print("  run `make install` to build .venv with every dependency,")
        print("  or set LIVEWALL_TEST_PYTHON to an interpreter that has them.")
        return 2

    if build and "web" in selected:
        if not run_step("build web client", ["npm", "run", "build"], dict(os.environ), WEB):
            print("\nRESULT: FAIL (web build)")
            return 1

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="livewall-tests-")
    env = dict(os.environ)
    env.update({
        "LIVEWALL_HOST": "127.0.0.1",
        "LIVEWALL_PORT": str(port),
        "LIVEWALL_CONFIG_FILE": os.path.join(tmp, "config.json"),
        "LIVEWALL_WEB_DIR": str(WEB / "dist"),
        "LIVEWALL_URL": base,
    })

    print(f"\nstarting backend on {base} (config={env['LIVEWALL_CONFIG_FILE']})", flush=True)
    server = subprocess.Popen([TEST_PY, str(BACKEND / "app.py")], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    results: dict[str, bool] = {}
    try:
        if not wait_health(base):
            print("RESULT: FAIL (backend did not become healthy)")
            return 1

        # Static cross-surface vocabulary parity (contract vs MCP vs client vs
        # CLI vs docs), derived from source. Needs no live backend; it runs on
        # EVERY selection because the vocabulary spans every surface.
        results["vocab"] = run_step(
            "vocabulary parity",
            [TEST_PY, str(BACKEND / "tests/vocab_parity_test.py")], env, REPO)

        if "backend" in selected:
            ok_edge = run_step(
                "backend edge_test", [TEST_PY, str(BACKEND / "tests/edge_test.py")], env, REPO)
            ok_stream = run_step(
                "backend stream_test", [TEST_PY, str(BACKEND / "tests/stream_test.py")], env, REPO)
            ok_storecc = run_step(
                "backend store_concurrency_test",
                [TEST_PY, str(BACKEND / "tests/store_concurrency_test.py")], env, REPO)
            ok_persist = run_step(
                "backend config_persist_test",
                [TEST_PY, str(BACKEND / "tests/config_persist_test.py")], env, REPO)
            ok_auth = run_step(
                "backend auth_test", [TEST_PY, str(BACKEND / "tests/auth_test.py")], env, REPO)
            # The pusher's value-resolution path is pure (no backend needed);
            # it pins the shipped examples/pusher.py the way vocab pins the
            # vocabulary.
            ok_pusher = run_step(
                "backend pusher_test", [TEST_PY, str(BACKEND / "tests/pusher_test.py")],
                env, REPO)
            results["backend"] = (
                ok_edge and ok_stream and ok_storecc and ok_persist and ok_auth
                and ok_pusher)
        if "cli" in selected:
            results["cli"] = run_step(
                "cli test_cli", [TEST_PY, str(REPO / "cli/test_cli.py")], env, REPO)
        if "mcp" in selected:
            results["mcp"] = run_step(
                "mcp test_live", [MCP_PY, str(REPO / "mcp/test_live.py")], env, REPO)
        if "web" in selected:
            ok_unit = run_step(
                "web unit tests", ["npm", "test"], env, WEB)
            ok_render = run_step(
                "web verify_render", [PW_PY, str(WEB / "scripts/verify_render.py")], env, WEB)
            ok_transport = run_step(
                "web verify_transport", [PW_PY, str(WEB / "scripts/verify_transport.py")], env, WEB)
            ok_a11y = run_step(
                "web verify_a11y", [PW_PY, str(WEB / "scripts/verify_a11y.py")], env, WEB)
            results["web"] = ok_unit and ok_render and ok_transport and ok_a11y
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    failed = [k for k in list(selected) + ["vocab"] if not results.get(k)]
    print("\n" + "=" * 72)
    for k in list(selected) + ["vocab"]:
        print(f"  {k:8} {'PASS' if results.get(k) else 'FAIL'}")
    print("RESULT:", "PASS" if not failed else f"FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
