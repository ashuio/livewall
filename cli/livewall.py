#!/usr/bin/env python3
"""livewall — command-line client for a Livewall backend.

Drives the same REST API the MCP server wraps, so a board can be programmed
from a shell, a cron job, or a CI step without an MCP host. Stdlib only
(urllib), so `pipx install ./cli` needs no dependency resolution.

Configuration (flags override the environment):

  LIVEWALL_URL     base URL of the backend   (default http://127.0.0.1:8470)
  LIVEWALL_TOKEN   bearer token, if the backend sets one

Examples:

  livewall ls
  livewall set build --title "Build" --status running
  livewall set build --file board.json --replace
  livewall update build --block p --set '{"value": 40}' --append "step 4"
  livewall watch build
  livewall demo
  livewall rm build
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

DEFAULT_URL = "http://127.0.0.1:8470"

# The config keys the backend actually reads (PUT /api/config ignores any
# other key and still returns 200), so the CLI must refuse a typo rather than
# report success for a write that changed nothing.
CONFIG_KEYS = ("default_ttl", "stale_ttl", "max_boards")

# Set in main() from the resolved flags/env.
BASE = DEFAULT_URL
TOKEN = ""


class CliError(Exception):
    """A user-facing failure (bad usage, refused write, unreachable backend)."""


def _board_path(bid: str) -> str:
    """The /api/boards/{id} path, with the id percent-encoded so a URL-hostile
    id (a space, a slash) is refused by the backend with a clear message rather
    than raising InvalidURL in urllib. A canonical id is unchanged."""
    return f"/api/boards/{quote(bid, safe='')}"


def _validate_base(url: str) -> str:
    """Refuse a base URL urllib cannot turn into a request, with a message the
    user can act on — urllib's own failure is a bare ValueError traceback."""
    try:
        p = urlparse(url)
    except ValueError as e:
        # urlparse itself raises for a malformed IPv6 literal (`http://[::1`),
        # so the scheme/netloc check below is never reached.
        raise CliError(
            f"invalid backend URL {url!r}: {e} "
            "(set LIVEWALL_URL or pass --url)") from e
    if p.scheme not in ("http", "https") or not p.netloc:
        raise CliError(
            f"invalid backend URL {url!r}: expected http://host[:port] "
            "(set LIVEWALL_URL or pass --url)")
    return url


def _validate_token(token: str) -> str:
    """Refuse a token carrying a control character: an HTTP header value cannot
    contain one, and urllib's failure is a bare ValueError traceback."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in token):
        raise CliError(
            "invalid token: it contains a control character "
            "(set LIVEWALL_TOKEN or pass --token)")
    return token


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _request(method: str, path: str, body=None):
    """Call the backend. Returns (status, parsed-json). Raises CliError on
    transport failure; returns the status for HTTP errors so callers decide."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            try:
                text = raw.decode()
                body = json.loads(text) if text else None
            except ValueError as e:
                raise CliError(
                    f"backend at {BASE} returned a non-JSON response "
                    f"(HTTP {r.status}); is LIVEWALL_URL pointing at Livewall?") from e
            return r.status, body
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except (ValueError, OSError, http.client.HTTPException):
            # A non-JSON or truncated error body must not crash the caller: the
            # status is preserved and `_ok` reports it without detail. Narrowed
            # from a bare `except Exception` (the standing law); the tuple covers
            # every realistic failure of e.read().decode() + json.loads.
            return e.code, None
    except urllib.error.URLError as e:
        raise CliError(
            f"cannot reach Livewall at {BASE}: {e.reason}. "
            "Is the backend running, and is LIVEWALL_URL correct?") from e
    except ValueError as e:
        # urllib raises ValueError for a header value it cannot encode (e.g. a
        # non-latin-1 token). A control character is caught earlier; this is the
        # backstop for anything else it refuses.
        raise CliError(f"invalid request to {BASE}: {e}") from e


def _ok(status: int, body) -> None:
    """Raise CliError if the backend refused the call."""
    if status < 400:
        return
    if status == 401:
        raise CliError(
            "unauthorized (401): the backend requires a token. "
            "Set LIVEWALL_TOKEN (or pass --token).")
    detail = ""
    if isinstance(body, dict):
        detail = body.get("error") or body.get("detail") or ""
    raise CliError(f"backend returned HTTP {status}: {detail or 'no detail'}")


def _require_dict(body, what: str) -> dict:
    """A JSON object from the backend, or a CliError naming what came back.

    A reachable non-Livewall server can answer 200 with valid JSON of the wrong
    shape (an array, a string), which the non-JSON guard does not catch because
    the body *is* JSON. The caller then indexes it as a mapping and tracebacks."""
    if not isinstance(body, dict):
        raise CliError(
            f"backend at {BASE} returned {what} that is not a JSON object")
    return body


# ------------------------------------------------------------------ rendering

def _fmt(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _as_list(v) -> list:
    """A list if the block field is one, else empty — the renderer stays total
    on a block with a missing or wrongly-typed field."""
    return v if isinstance(v, list) else []


def _pair(row, key_a: str, key_b: str):
    """Normalise a bar/kv row to (a, b).

    The contract accepts both the pair form (`["label", 3]`) and the object form
    (`{"label": "x", "value": 3}`), so the renderer must accept both; anything
    else yields None and is skipped.
    """
    if isinstance(row, dict):
        return row.get(key_a, ""), row.get(key_b, "")
    if isinstance(row, (list, tuple)) and len(row) == 2:
        return row[0], row[1]
    return None


def render_board(board: dict, color: bool = True) -> str:
    """A plain-text view of a board's blocks, for `get` and `watch`."""
    if not isinstance(board, dict):
        return "[board is not an object]\n"
    dim = "\033[2m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""
    out: list[str] = []
    title = board.get("title", "")
    status = board.get("status", "")
    sub = board.get("subtitle", "")
    head = f"{bold}{title}{reset}"
    if status:
        head += f"  [{status}]"
    out.append(head)
    if sub:
        out.append(f"{dim}{sub}{reset}")
    out.append("")

    def bar(frac: float, width: int = 24) -> str:
        frac = max(0.0, min(1.0, frac))
        filled = round(frac * width)
        return "█" * filled + "░" * (width - filled)

    for b in _as_list(board.get("blocks")):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "heading":
            eyebrow = b.get("eyebrow")
            out.append(f"{bold}{b.get('text', '')}{reset}"
                       + (f"  {dim}({eyebrow}){reset}" if eyebrow else ""))
        elif t == "text":
            out.append(str(b.get("text", "")))
        elif t in ("stat", "stats"):
            items = _as_list(b.get("items")) if t == "stats" else [b]
            for it in items:
                if not isinstance(it, dict):
                    continue
                unit = f" {it.get('unit')}" if it.get("unit") else ""
                out.append(f"  {it.get('label', '')}: {_fmt(it.get('value'))}{unit}")
        elif t in ("progress", "gauge"):
            val = b.get("value", 0)
            mx = b.get("max", 100) or 100
            try:
                frac = float(val) / float(mx)
            except (TypeError, ValueError, ZeroDivisionError):
                frac = 0.0
            unit = f" {b.get('unit')}" if b.get("unit") else ""
            out.append(f"  {b.get('label', '')}  [{bar(frac)}] {_fmt(val)}/{_fmt(mx)}{unit}")
        elif t == "bar":
            out.append(f"  {b.get('label', '')}")
            for row in _as_list(b.get("data")):
                pair = _pair(row, "label", "value")
                if pair is None:
                    continue
                out.append(f"    {pair[0]}: {_fmt(pair[1])}")
        elif t in ("sparkline", "chart"):
            vals = _as_list(b.get("values"))
            out.append(f"  {b.get('label', '')}: " + " ".join(_fmt(v) for v in vals))
        elif t == "line":
            if b.get("label"):
                out.append(f"  {b['label']}")
            series = [s for s in _as_list(b.get("series")) if isinstance(s, dict)]
            if not series:
                series = [{"values": _as_list(b.get("values"))}]
            for s in series:
                vals = _as_list(s.get("values"))
                prefix = f"{s.get('name')}: " if s.get("name") else ""
                out.append(f"    {prefix}" + " ".join(_fmt(v) for v in vals))
        elif t == "table":
            cols = _as_list(b.get("columns"))
            out.append("  " + " | ".join(str(c) for c in cols))
            out.append("  " + "-+-".join("-" * len(str(c)) for c in cols))
            for r in _as_list(b.get("rows")):
                out.append("  " + " | ".join(str(c) for c in _as_list(r)))
        elif t in ("log", "code"):
            if b.get("title"):
                out.append(f"  {bold}{b['title']}{reset}")
            for line in _as_list(b.get("lines")) or (str(b.get("text", "")).splitlines()):
                out.append(f"    {line}")
        elif t == "kv":
            for pair in _as_list(b.get("items")):
                norm = _pair(pair, "k", "v")
                if norm is None:
                    continue
                out.append(f"  {norm[0]}: {_fmt(norm[1])}")
        elif t == "list":
            if b.get("label"):
                out.append(f"  {b['label']}")
            for item in _as_list(b.get("items")):
                out.append(f"    • {item}")
        elif t == "callout":
            out.append(f"  [{b.get('tone', 'info')}] {b.get('text', '')}")
        elif t == "divider":
            out.append("  " + "─" * 40)
        else:
            out.append(f"  {dim}[unknown block: {t}]{reset}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ commands

def cmd_health(args) -> int:
    st, body = _request("GET", "/health")
    _ok(st, body)
    print(json.dumps(body, indent=2))
    return 0


def cmd_ls(args) -> int:
    st, body = _request("GET", "/api/boards")
    _ok(st, body)
    if body is None:
        body = {}
    body = _require_dict(body, "a board list")
    boards = body.get("boards", [])
    if not isinstance(boards, list):
        raise CliError(
            f"backend at {BASE} returned a `boards` value that is not a list")
    if not boards:
        print("(no boards)")
        return 0
    for b in boards:
        if not isinstance(b, dict):
            continue
        prog = b.get("progress")
        ptxt = f"  {round(prog * 100)}%" if isinstance(prog, (int, float)) else ""
        print(f"{b.get('id', '?'):24} {b.get('status', ''):8} {b.get('title', '')}{ptxt}")
    return 0


def cmd_get(args) -> int:
    st, body = _request("GET", _board_path(args.id))
    _ok(st, body)
    if body is None:
        body = {}
    body = _require_dict(body, "a board")
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        print(render_board(body, color=sys.stdout.isatty()))
    return 0


def _read_json_file(path: str, what: str):
    """Load JSON from `path`, turning an unreadable file or malformed JSON into
    a user-facing CliError instead of a traceback."""
    try:
        with open(path) as f:
            return json.load(f)
    except OSError as e:
        raise CliError(f"cannot read {what} {path!r}: {e.strerror or e}") from e
    except ValueError as e:
        raise CliError(f"{what} {path!r} is not valid JSON: {e}") from e


def _load_board(args) -> dict:
    if args.file:
        body = _read_json_file(args.file, "--file")
        if not isinstance(body, dict):
            raise CliError(f"{args.file}: expected a JSON object")
        return body
    body: dict = {}
    if args.title is not None:
        body["title"] = args.title
    if args.subtitle is not None:
        body["subtitle"] = args.subtitle
    if args.status is not None:
        body["status"] = args.status
    if args.ttl is not None:
        body["ttl"] = args.ttl
    if args.blocks_file:
        body["blocks"] = _read_json_file(args.blocks_file, "--blocks-file")
    if args.tab_file:
        body["tab"] = _read_json_file(args.tab_file, "--tab-file")
    if not body:
        raise CliError("nothing to set: pass --file, or --title/--blocks-file/etc.")
    return body


def cmd_set(args) -> int:
    body = _load_board(args)
    path = _board_path(args.id) + ("?replace=true" if args.replace else "")
    st, resp = _request("POST", path, body)
    if st == 409:
        raise CliError(
            f"board {args.id!r} already exists; pass --replace to overwrite it "
            "(or choose a different id).")
    _ok(st, resp)
    print(f"ok: {args.id}")
    return 0


def cmd_update(args) -> int:
    if args.set is None and args.append is None:
        raise CliError("nothing to update: pass --set and/or --append")
    payload: dict = {"id": args.block}
    if args.set is not None:
        try:
            payload["set"] = json.loads(args.set)
        except ValueError as e:
            raise CliError(f"--set is not valid JSON: {e}") from e
    if args.append is not None:
        payload["append"] = args.append
    st, resp = _request("POST", f"{_board_path(args.id)}/update", payload)
    _ok(st, resp)
    print(f"ok: {args.id}/{args.block}")
    return 0


def cmd_reset(args) -> int:
    st, resp = _request("POST", f"{_board_path(args.id)}/reset", {})
    _ok(st, resp)
    print(f"ok: reset {args.id}")
    return 0


def cmd_rm(args) -> int:
    st, resp = _request("DELETE", _board_path(args.id))
    _ok(st, resp)
    print(f"ok: removed {args.id}")
    return 0


def cmd_config(args) -> int:
    if not args.set:
        st, body = _request("GET", "/api/config")
        _ok(st, body)
        print(json.dumps(body, indent=2))
        return 0
    payload: dict = {}
    for pair in args.set:
        if "=" not in pair:
            raise CliError(f"--set expects key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        if k not in CONFIG_KEYS:
            raise CliError(
                f"unknown config key {k!r}; valid keys: {', '.join(CONFIG_KEYS)}")
        try:
            payload[k] = int(v)
        except ValueError:
            raise CliError(f"{k}={v}: config values are integers") from None
    st, body = _request("PUT", "/api/config", payload)
    _ok(st, body)
    print(json.dumps(body, indent=2))
    return 0


def cmd_watch(args) -> int:
    """Poll one board and redraw on change. Exits cleanly if the board is gone."""
    color = sys.stdout.isatty()
    last: str | None = None
    while True:
        st, body = _request("GET", _board_path(args.id))
        if st == 404:
            print(f"board {args.id!r} is gone (expired or deleted)")
            return 0
        _ok(st, body)
        if body is None:
            body = {}
        body = _require_dict(body, "a board")
        view = render_board(body, color=color)
        if view != last:
            if color and last is not None:
                sys.stdout.write("\033[2J\033[H")  # clear + home
            sys.stdout.write(view)
            sys.stdout.flush()
            last = view
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_demo(args) -> int:
    """Program a sample board so a newcomer sees it working in one command."""
    bid = args.id
    st, resp = _request("POST", f"{_board_path(bid)}?replace=true", {
        "title": "Livewall demo",
        "subtitle": "programmed by the CLI",
        "status": "running",
        "tab": {"label": "Demo", "accent": "violet",
                "indicator": {"kind": "bar", "value": 3, "max": 5}},
        "blocks": [
            {"id": "h", "type": "heading", "text": "Livewall", "eyebrow": "demo"},
            {"id": "p", "type": "progress", "label": "Steps", "value": 3, "max": 5},
            {"id": "s", "type": "stats", "items": [
                {"label": "boards", "value": 1},
                {"label": "blocks", "value": 4},
            ]},
            {"id": "l", "type": "log", "title": "activity", "lines": [
                "board created by the CLI",
                "open the web client to watch it live",
            ]},
        ],
    })
    _ok(st, resp)
    print(f"ok: demo board {bid!r} created — open {BASE}/ and look for the tab")
    return 0


# ------------------------------------------------------------------ argparse

def _positive_seconds(s: str) -> float:
    """argparse type for --interval: a positive, finite number of seconds.
    `float()` alone accepts -1 (sleep raises), nan and inf (sleep raises), so
    those crashed the watcher with a traceback."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive, finite number of seconds")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="livewall",
        description="Command-line client for a Livewall backend.")
    p.add_argument("--url", default=None,
                   help=f"backend base URL (env LIVEWALL_URL; default {DEFAULT_URL})")
    p.add_argument("--token", default=None,
                   help="bearer token (env LIVEWALL_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="GET /health").set_defaults(func=cmd_health)
    sub.add_parser("ls", help="list boards").set_defaults(func=cmd_ls)

    g = sub.add_parser("get", help="show one board")
    g.add_argument("id")
    g.add_argument("--json", action="store_true", help="raw JSON, not a text view")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("set", help="create (or --replace) a board")
    s.add_argument("id")
    s.add_argument("--file", help="a complete board JSON body")
    s.add_argument("--title")
    s.add_argument("--subtitle")
    s.add_argument("--status", choices=["idle", "running", "done", "error"])
    s.add_argument("--ttl", type=int)
    s.add_argument("--blocks-file", help="a JSON array of blocks")
    s.add_argument("--tab-file", help="a JSON tab object")
    s.add_argument("--replace", action="store_true",
                   help="overwrite an existing board id (else 409)")
    s.set_defaults(func=cmd_set)

    u = sub.add_parser("update", help="mutate one block")
    u.add_argument("id")
    u.add_argument("--block", required=True, help="the block id to change")
    u.add_argument("--set", help="JSON object of fields to set")
    u.add_argument("--append", help="append a line (log blocks only)")
    u.set_defaults(func=cmd_update)

    r = sub.add_parser("reset", help="clear a board's blocks")
    r.add_argument("id")
    r.set_defaults(func=cmd_reset)

    d = sub.add_parser("rm", help="delete a board")
    d.add_argument("id")
    d.set_defaults(func=cmd_rm)

    c = sub.add_parser("config", help="show or set global config")
    c.add_argument("--set", action="append", metavar="K=V",
                   help="set a config key (repeatable)")
    c.set_defaults(func=cmd_config)

    w = sub.add_parser("watch", help="follow one board until it disappears")
    w.add_argument("id")
    w.add_argument("--interval", type=_positive_seconds, default=2.0, help="poll seconds")
    w.add_argument("--once", action="store_true", help="print once and exit")
    w.set_defaults(func=cmd_watch)

    dm = sub.add_parser("demo", help="create a sample board")
    dm.add_argument("id", nargs="?", default="livewall-demo")
    dm.set_defaults(func=cmd_demo)

    return p


def main(argv=None) -> int:
    global BASE, TOKEN
    args = build_parser().parse_args(argv)
    try:
        BASE = _validate_base(
            (args.url or os.environ.get("LIVEWALL_URL") or DEFAULT_URL).rstrip("/"))
        TOKEN = _validate_token(args.token or os.environ.get("LIVEWALL_TOKEN") or "")
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
