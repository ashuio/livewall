#!/usr/bin/env python3
"""Dependency-pin guard — the reproducibility policy, enforced.

Livewall's install surface must be reproducible: a `make install` today has to
resolve to the same dependency set in six months. Every install source this
repo ships therefore has to pin exactly:

- `backend/requirements.txt` and `mcp/requirements.txt` — every requirement
  must be `name==version`. A range (`>=`, `~=`, bare name) floats and lets a
  future upstream release change what a stranger builds.
- The `Makefile` `pip install` line — every direct package must be `==`-pinned.
  This is where `fastmcp httpx playwright` historically floated unbounded.
- `cli/pyproject.toml` — the CLI is standard-library only; its dependency
  list must stay empty (or, if it ever grows, carry exact pins only).

Transitive dependencies are intentionally not pinned here: they are
constrained by the direct pins' own metadata, and duplicating the whole tree
would rot faster than it protects. The direct pins are the policy.

Exit 0 when every install source is pinned; exit 1 naming each violation.
`make test` runs this before the suites (see the Makefile), so an unpinned
dependency fails the build instead of shipping.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REQ_FILES = ("backend/requirements.txt", "mcp/requirements.txt")
PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9._+!]*$")
BARE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_requirements(rel: str, problems: list[str]) -> None:
    path = REPO / rel
    if not path.exists():
        problems.append(f"{rel}: missing — the file must exist and be pinned")
        return
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "-c")):
            continue
        if not PINNED.match(line):
            problems.append(
                f"{rel}:{lineno}: `{line}` is not an exact pin (want `name==version`)")


def check_makefile(problems: list[str]) -> None:
    for lineno, raw in enumerate((REPO / "Makefile").read_text().splitlines(), 1):
        if "pip install" not in raw:
            continue
        for token in raw.split():
            if token in ("pip", "install") or token.startswith("-"):
                continue
            if token.endswith(".txt") or "/" in token:
                continue
            if not PINNED.match(token):
                problems.append(
                    f"Makefile:{lineno}: pip installs `{token}` unpinned (want `name==version`)")


def check_cli_pyproject(problems: list[str]) -> None:
    text = (REPO / "cli/pyproject.toml").read_text()
    match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if match is None:
        problems.append("cli/pyproject.toml: no `dependencies` key found")
        return
    entries = [e.strip().strip("'\"") for e in match.group(1).split(",") if e.strip()]
    for entry in entries:
        if not PINNED.match(entry):
            problems.append(
                f"cli/pyproject.toml: dependency `{entry}` is not an exact pin")


def main() -> int:
    problems: list[str] = []
    for rel in REQ_FILES:
        check_requirements(rel, problems)
    check_makefile(problems)
    check_cli_pyproject(problems)
    if problems:
        print("RESULT: FAIL (unpinned dependencies — installs are not reproducible)")
        for p in problems:
            print(f"  {p}")
        return 1
    print("RESULT: PASS (every install source pins exactly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
