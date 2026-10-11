#!/usr/bin/env python3
"""
Livewall — an agent-programmable live status surface (multi-board).

Many agents / sessions each own a NAMED board. Each board is a spec
(title/subtitle/blocks); the client shows them as tabs and renders the selected
one's blocks directly.

Designed to be driven over MCP: every operation is a plain JSON endpoint.

Every write is validated at THIS layer against the shared contract
(backend/contract.py): unknown block types, bad statuses/tabs/ttls, malformed
bodies and non-canonical board ids are refused with a message naming the
offending field and the valid set. The MCP server runs the same contract as an
earlier guard, so the two cannot disagree.

Endpoints
  GET    /health
  GET    /api/boards                 -> board summaries (id, title, status, ttl, ...)
  GET    /api/boards/{id}            -> full board
  POST   /api/boards/{id}            -> create a board; 409 if the id is taken
                                        (?replace=true to overwrite)
  PATCH  /api/boards/{id}            -> partial board fields {title,subtitle,status,tab,ttl}
  POST   /api/boards/{id}/update     -> mutate one block {id,set:{...},append:"..."}
  POST   /api/boards/{id}/reset      -> clear one board's blocks
  DELETE /api/boards/{id}            -> remove a board
  GET    /api/config                 -> global defaults
  PUT    /api/config                 -> set defaults {default_ttl,stale_ttl,max_boards}
  GET    /api/stream                 -> SSE snapshot of all boards (client)
  GET    /                           -> the built web client (if present)

When `LIVEWALL_TOKEN` is set, every `/api/*` endpoint requires it (as
`Authorization: Bearer <token>` or `?token=<token>`); `/health` and the static
client stay open. Unset, the API is open — the quickstart default.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import auth
import contract

HERE = Path(__file__).resolve().parent

# Operator-facing diagnostics. Uvicorn configures its own loggers; this module
# logger propagates to the root handler (and Python's `lastResort` handler if
# none is installed), so a warning here reaches stderr in every deployment.
logger = logging.getLogger("livewall")

# Everything is environment-driven so a fresh clone runs on defaults and a deploy
# overrides without editing code. See .env.example at the repo root.
# Host defaults to loopback: exposing a dashboard to the network should be a
# deliberate act, not an accident.
HOST = os.environ.get("LIVEWALL_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVEWALL_PORT", "8470"))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    if n < 1:
        # Preserve the old clamp, but say so: silently turning -5 into 1 would
        # leave the operator believing their setting took effect.
        logger.warning("%s=%r is not positive; clamping to 1", name, raw)
        return 1
    return n


def _config_int(v) -> int:
    """Coerce a config value to a positive int, or raise ValueError.

    `int(True)` is 1 and `int(7.9)` is 7: both silently change what the caller
    asked for, and `max(1, ...)` used to turn `-5` into 1. A config write must
    either take the value the caller sent or be refused — never a different
    number reported as success. Accepts a real int, an integer-valued float, or
    a numeric string (the MCP/HTTP layers have always taken strings).
    """
    if isinstance(v, bool):
        raise ValueError("a boolean is not an integer")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("a fractional number is not an integer")
        n = int(v)
    elif isinstance(v, int):
        n = v
    elif isinstance(v, str):
        n = int(v)  # raises ValueError on "abc" / "7.5"
    else:
        raise ValueError(f"{type(v).__name__} is not an integer")
    if n <= 0:
        raise ValueError("must be positive")
    return n


# A hard ceiling on `max_boards`. The cap is operator-tunable, but an unbounded
# value disables eviction entirely — `PUT /api/config {"max_boards": 2**63}`, or
# a hand-edited config file, would let boards accumulate without limit. On a
# service that ships with no authentication by design, that is a
# resource-exhaustion path. The ceiling is itself env-tunable
# (LIVEWALL_MAX_BOARDS_LIMIT) so an operator who genuinely needs more raises it
# deliberately rather than by accident.
MAX_BOARDS_LIMIT = _env_int("LIVEWALL_MAX_BOARDS_LIMIT", 1000)

# The upper bound for each config key, where one exists. A key absent here has no
# ceiling (only `max_boards` does).
_CONFIG_CEILING = {"max_boards": MAX_BOARDS_LIMIT}

# A hard cap on the bytes read from any request body. `await req.json()` read the
# whole body with no limit, so a single huge POST could exhaust memory on a
# service that ships with no authentication by design. 1 MiB is far more than a
# board spec needs (the largest legal board is 60 blocks plus a 400-line log) and
# small enough that a flood of them cannot balloon the process. Env-tunable for
# an operator who genuinely needs more.
MAX_BODY_BYTES = _env_int("LIVEWALL_MAX_BODY_BYTES", 1_048_576)


def _config_value(key: str, v) -> int:
    """Coerce a config value to a positive int within the key's ceiling.

    The single validator for every config value, used by BOTH the write path
    (`PUT /api/config`) and the startup file load, so the two cannot disagree.
    """
    try:
        n = _config_int(v)
    except (TypeError, ValueError) as e:
        # Normalise the base refusals so every one of them names the shape it
        # wants; the callers prefix the key.
        raise ValueError(f"must be a positive integer ({e})") from None
    if n > contract.MAX_SAFE_INT:
        # `default_ttl`/`stale_ttl` are used in float arithmetic by the summary
        # (`ttl - age`); an int above 2**53 is not exactly a double, and far
        # above it `float()` raises. `max_boards` has its own tighter ceiling
        # below, but the bound applies to every key.
        raise ValueError(
            f"must be a positive integer no greater than {contract.MAX_SAFE_INT}")
    ceiling = _CONFIG_CEILING.get(key)
    if ceiling is not None and n > ceiling:
        raise ValueError(
            f"must be a positive integer no greater than {ceiling} "
            f"(raise LIVEWALL_MAX_BOARDS_LIMIT to allow more)")
    return n


def _seed_max_boards() -> int:
    """The env-seeded board cap, clamped to MAX_BOARDS_LIMIT with a warning."""
    n = _env_int("LIVEWALL_MAX_BOARDS", 12)
    if n > MAX_BOARDS_LIMIT:
        logger.warning(
            "LIVEWALL_MAX_BOARDS=%d exceeds the ceiling %d; clamping to it "
            "(raise LIVEWALL_MAX_BOARDS_LIMIT to allow more)", n, MAX_BOARDS_LIMIT)
        return MAX_BOARDS_LIMIT
    return n


def _seed_ttl(name: str, default: int) -> int:
    """The env-seeded ttl keys, clamped to MAX_SAFE_INT with a warning.

    The write path (`_config_value`) refuses a ttl above 2**53 because the
    summary uses it in float arithmetic; the env seed bypassed that bound, so
    `LIVEWALL_DEFAULT_TTL=10**400` was silently accepted and every terminal
    board's expiry was silently disabled. Same clamp-and-say-so as
    `_seed_max_boards`.
    """
    n = _env_int(name, default)
    if n > contract.MAX_SAFE_INT:
        logger.warning(
            "%s=%d exceeds the safe bound %d; clamping to it", name, n,
            contract.MAX_SAFE_INT)
        return contract.MAX_SAFE_INT
    return n


# Global, MCP-settable defaults. Seeded from the environment, then overridable at
# runtime via PUT /api/config.
CONFIG = {
    "default_ttl": _seed_ttl("LIVEWALL_DEFAULT_TTL", 1800),  # seconds a TERMINAL board survives
    "stale_ttl": _seed_ttl("LIVEWALL_STALE_TTL", 3600),      # seconds a RUNNING board survives idle
    "max_boards": _seed_max_boards(),                       # cap; oldest-updated evicted past this
}

# Config persists to disk so a restart does NOT silently revert it (the dict is
# process memory; without this, every restart reset it). Point this at a volume
# to keep it across container recreation.
CONFIG_FILE = Path(os.environ.get("LIVEWALL_CONFIG_FILE") or (HERE / "config.json"))
if CONFIG_FILE.exists():
    # An ABSENT file is normal (first run) and silent. A file that exists but
    # cannot be read, is not JSON, or is not a JSON object is CORRUPT: falling
    # back to the seeded defaults is still correct (a bad file must not stop
    # boot), but the operator must be told their saved config was discarded.
    try:
        _saved = json.loads(CONFIG_FILE.read_text())
        if not isinstance(_saved, dict):
            raise ValueError(f"expected a JSON object, got {type(_saved).__name__}")
        # Validate and build before committing so a half-corrupt file cannot
        # leave CONFIG partially overridden. The SAME rule as the write path
        # (`_config_int`): a bool, fraction, or non-positive value is refused
        # rather than silently coerced. `max(1, int(v))` used to boot a saved
        # `true` as `1`, `7.9` as `7`, and `-5` as `1` with no warning — the
        # exact coercion the write path was hardened against, left in the load
        # layer. A bad value now falls back to the defaults and is logged.
        _loaded = {}
        for k in CONFIG:
            if k in _saved:
                try:
                    _loaded[k] = _config_value(k, _saved[k])
                except (TypeError, ValueError) as e:
                    raise ValueError(f"{k}: {e}") from e
    except (OSError, ValueError, TypeError) as e:
        logger.warning("could not load config from %s (%s); using defaults",
                       CONFIG_FILE, e)
    else:
        CONFIG.update(_loaded)


def _save_config(cfg: dict) -> None:
    """Persist config to disk. Raises on failure.

    This deliberately does NOT swallow: `PUT /api/config` must not report
    success when the write did not happen. The caller persists BEFORE committing
    to memory, so a failed write leaves the running config and the file both
    unchanged (either both change or neither does).
    """
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")

# board_id -> {"title","subtitle","status","updated","blocks","version","ttl","tab"}
BOARDS: dict[str, dict] = {}
SUBS: set = set()

# SSE backpressure. Every broadcast is a COMPLETE snapshot of all boards, so a
# slow subscriber only ever needs the most recent one: the queue holds at most
# one pending snapshot and a newer broadcast replaces the unread older one
# (newest-wins coalescing). This bounds memory at one snapshot per subscriber
# and means a stalled client can never make the server grow without limit.
STREAM_QUEUE_MAX = 1

# A hard cap on concurrent SSE subscribers. Each subscriber holds an
# `asyncio.Queue`; a plain set with no cap let N concurrent `/api/stream`
# connections grow memory and file descriptors without bound. 100 is generous
# for a LAN/Tailnet tool and env-tunable (LIVEWALL_MAX_SUBSCRIBERS). A
# connection past the cap is refused with 503; the client's transport retries,
# so a capped-out client is not wedged.
MAX_SUBSCRIBERS = _env_int("LIVEWALL_MAX_SUBSCRIBERS", 100)

# Seconds of silence before the stream emits its keepalive snapshot. Env-tunable
# so the timing can be exercised without a 15s wait in tests. A bare
# float(os.environ.get(...)) killed the boot on an operator typo
# (LIVEWALL_KEEPALIVE=abc raised ValueError at import, before the port ever
# bound), and 0 or a negative value would make the keepalive wait_for time out
# instantly and emit a full snapshot in a hot loop — so an invalid or
# non-positive value falls back to the default and says so, like every other
# env knob here.
KEEPALIVE = 15.0
_raw_keepalive = os.environ.get("LIVEWALL_KEEPALIVE")
if _raw_keepalive is not None:
    try:
        _ka = float(_raw_keepalive)
    except (TypeError, ValueError):
        logger.warning("invalid LIVEWALL_KEEPALIVE=%r; using default %s",
                       _raw_keepalive, KEEPALIVE)
    else:
        if _ka <= 0:
            logger.warning("LIVEWALL_KEEPALIVE=%r is not positive; using default %s",
                           _raw_keepalive, KEEPALIVE)
        else:
            KEEPALIVE = _ka

# Bound on uvicorn's graceful shutdown, passed to uvicorn.run() by __main__.
# Without it uvicorn waits for in-flight connections to finish — and an SSE
# stream only finishes when the CLIENT disconnects, so a stop with any stream
# open hangs forever (observed live: SIGTERM with one stream still had not
# exited after 60s; systemd then sat in its default 90s stop timeout and
# SIGKILLed the process — the "State 'stop-sigterm' timed out. Killing."
# journal entries). With the bound, uvicorn cancels the stalled connection
# tasks instead; the cancelled stream generators are closed by
# _StreamResponse, so SUBS still drains before exit. Keep this comfortably
# below the unit's TimeoutStopSec (docs/deploying.md) so SIGKILL stays a
# last resort, and above the slowest legitimate in-flight request (all of
# the write endpoints are sub-second, so 3 is generous).
GRACEFUL_SHUTDOWN_TIMEOUT = _env_int("LIVEWALL_GRACEFUL_SHUTDOWN_TIMEOUT", 3)


def _resolve_id(bid: str):
    """Validate a client-supplied board id.

    Returns (canonical_id, None) or (None, JSONResponse). We REFUSE ids that are
    not already canonical instead of slugging them: slugging collapsed "My
    Board" and "my.board" onto "my-board", letting one agent overwrite another's
    board. Two distinct ids now can never collide.
    """
    try:
        return contract.validate_board_id(bid), None
    except contract.ContractError as e:
        return None, JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# The explicit opt-in for replacing a live board id. Only these literals are
# accepted (case-insensitive); anything else is refused rather than silently
# treated as false, so a typo cannot quietly disarm the collision guard.
_REPLACE_TRUE = frozenset({"true", "1", "yes", "on"})
_REPLACE_FALSE = frozenset({"false", "0", "no", "off", ""})


def _replace_flag(req: Request):
    """Parse the `replace` query flag for set_board.

    Returns (replace, None) or (None, error-response). Absent means False.
    """
    raw = req.query_params.get("replace")
    if raw is None:
        return False, None
    val = raw.strip().lower()
    if val in _REPLACE_TRUE:
        return True, None
    if val in _REPLACE_FALSE:
        return False, None
    return None, JSONResponse(
        {"ok": False,
         "error": f"replace={raw!r} is not a boolean; use replace=true to "
                  "overwrite an existing board, or omit it."},
        status_code=400)


def _too_large():
    return JSONResponse(
        {"ok": False,
         "error": f"request body is too large; the limit is {MAX_BODY_BYTES} bytes."},
        status_code=413)


async def _read_json(req: Request):
    """Read a JSON object body, capped at MAX_BODY_BYTES.

    Returns (body, None) or (None, error-response). `await req.json()` read the
    whole body with no size cap, so one huge POST could exhaust memory. The body
    is now read in chunks and refused with 413 the moment it exceeds the cap. An
    advertised `Content-Length` over the cap is refused up front as a fast path,
    but is never trusted on its own: a chunked body (no Content-Length) is still
    capped while streaming.
    """
    cl = req.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return None, _too_large()
        except ValueError:
            return None, JSONResponse(
                {"ok": False, "error": "invalid Content-Length header."},
                status_code=400)
    chunks: list[bytes] = []
    total = 0
    async for chunk in req.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None, _too_large()
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        # `json.loads` on the request bytes: a malformed body (bad JSON or
        # invalid UTF-8) is a CLIENT error and gets the named 400. Anything else
        # (e.g. a client disconnect while reading the body, raised by the
        # `async for` above) is not the client's JSON being wrong, so it is NOT
        # relabelled as one — it propagates rather than being misreported.
        # RecursionError is a RuntimeError, not a ValueError: legal-but-deeply-
        # nested JSON overflows the json C scanner's stack long before
        # MAX_BODY_BYTES (a ~120 kB body nests deep enough), and a client whose
        # body cannot be decoded gets the named 400, not a 500.
        return None, JSONResponse(
            {"ok": False, "error": "request body must be valid JSON."}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"ok": False, "error": "request body must be a JSON object."}, status_code=400)
    return body, None


def _blank(title: str = "Livewall") -> dict:
    return {
        "title": title,
        "subtitle": "idle — nothing programmed",
        "status": "idle",
        "updated": time.time(),
        "blocks": [],
        "version": 0,
        "ttl": None,
        "tab": None,
    }


def _ttl_of(b: dict) -> int:
    """Effective terminal-prune TTL for a board."""
    t = b.get("ttl")
    try:
        t = int(t)
        return t if t > 0 else CONFIG["default_ttl"]
    except (TypeError, ValueError):
        return CONFIG["default_ttl"]


def _prune() -> int:
    """Remove expired boards. Returns how many were removed."""
    now = time.time()
    removed = 0
    for bid in list(BOARDS):
        b = BOARDS[bid]
        age = now - b["updated"]
        if b["status"] in ("done", "error") and age > _ttl_of(b):
            del BOARDS[bid]
            removed += 1
        elif b["status"] == "running" and age > CONFIG["stale_ttl"]:
            del BOARDS[bid]
            removed += 1
    if len(BOARDS) > CONFIG["max_boards"]:
        order = sorted(BOARDS.items(), key=lambda kv: kv[1]["updated"])
        for bid, _ in order[: len(BOARDS) - CONFIG["max_boards"]]:
            del BOARDS[bid]
            removed += 1
    return removed


def _progress_of(b: dict) -> float | None:
    for blk in b.get("blocks", []):
        if blk.get("type") in ("progress", "gauge"):
            try:
                return max(0.0, min(1.0, float(blk.get("value", 0)) / float(blk.get("max", 100) or 100)))
            except (TypeError, ValueError, OverflowError):
                # OverflowError: an int too large for float (a board already in
                # memory, e.g. loaded before the contract bound existed). The
                # read path stays total rather than 500ing.
                return None
    return None


# Per-block-type writable fields (beyond the universal id/type) and the block
# vocabulary/limits live in contract.py — shared with the MCP guard so the two
# cannot disagree. `update_block` uses BLOCK_FIELDS to refuse unknown `set` keys
# and APPENDABLE to refuse `append` on blocks the client renderer would silently ignore.
FIELDS = contract.BLOCK_FIELDS
LOG_LIKE = contract.APPENDABLE


def _tab_of(bid: str, b: dict) -> dict:
    """Resolve the agent-authored tab descriptor, with sensible defaults.

    Board spec may carry:
        {"tab": {"label": "...", "accent": "pink",
                 "indicator": {"kind": "bar", "value": 9410, "max": 12800}}}
    Indicator kinds: bar | pct | text | none.
    """
    t = b.get("tab") if isinstance(b.get("tab"), dict) else {}
    label = str(t.get("label") or b.get("title") or bid)
    accent = str(t.get("accent") or "").lower()
    if accent not in contract.ACCENTS:
        accent = {"running": "violet", "done": "ok", "error": "error"}.get(b["status"], "muted")

    ind = t.get("indicator")
    if not isinstance(ind, dict):
        ind = {}
    kind = str(ind.get("kind") or "").lower()
    if kind not in contract.INDICATORS:
        first = next((x for x in b.get("blocks", []) if x.get("type") in ("progress", "gauge")), None)
        if first is not None:
            kind, ind = "bar", {"kind": "bar", "value": first.get("value", 0), "max": first.get("max", 100)}
        else:
            kind, ind = "none", {"kind": "none"}
    else:
        ind = {"kind": kind, **{k: v for k, v in ind.items() if k != "kind"}}
    return {"label": label, "accent": accent, "indicator": ind}


def _summary(bid: str, b: dict, now: float | None = None) -> dict:
    # A snapshot computes ONE `now` and threads it through every board, so all
    # ages in a frame are measured against the same instant (and the frame's
    # top-level `now`). Standalone callers (a single board read/write response)
    # pass nothing and get the current clock.
    now = time.time() if now is None else now
    ttl = _ttl_of(b)
    terminal = b["status"] in ("done", "error")
    # The subtraction converts `ttl` to float; an int above 2**53 is not exactly
    # a double and far above it raises. The contract bounds ttl, so this only
    # covers a board already in memory, but the read path must stay total.
    try:
        expires_in = round(max(0.0, ttl - (now - b["updated"])), 1) if terminal else None
    except (TypeError, ValueError, OverflowError):
        expires_in = None
    return {
        "id": bid,
        "title": b["title"],
        "subtitle": b["subtitle"],
        "status": b["status"],
        "updated": b["updated"],
        "age": round(now - b["updated"], 1),
        "version": b["version"],
        "ttl": b.get("ttl"),
        "effective_ttl": ttl,
        "expires_in": expires_in,
        "progress": _progress_of(b),
        "tab": _tab_of(bid, b),
    }


def _snapshot() -> dict:
    _prune()  # reads always reflect expiry, regardless of reaper timing
    # One clock read for the whole frame: every board's `age` (and `expires_in`)
    # is measured against this same `now`, which is also published so the client
    # can advance ages between snapshots without trusting its own wall clock.
    now = time.time()
    return {
        "config": CONFIG,
        "now": now,
        "boards": [
            {**_summary(bid, b, now), "blocks": b["blocks"]}
            for bid, b in sorted(BOARDS.items(), key=lambda kv: -kv[1]["updated"])
        ],
    }


def _enqueue(q: asyncio.Queue, msg: str) -> None:
    """Hand a snapshot to one subscriber, coalescing if it has not read yet.

    Bounded queue + newest-wins: when the queue is full the unread older
    snapshot is discarded so the client receives the newest state instead of
    falling behind. Everything here is synchronous, so on the event loop the
    full/drop/put sequence is atomic.
    """
    try:
        q.put_nowait(msg)
        return
    except asyncio.QueueFull:
        pass
    try:
        q.get_nowait()  # drop the stale snapshot
    except asyncio.QueueEmpty:
        pass
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:
        # Only possible if STREAM_QUEUE_MAX were 0; the newest snapshot is
        # already queued or being dropped, so there is nothing more to do.
        pass


def _broadcast() -> None:
    # Serialize once, then fan out. A serialization failure here is a real
    # server-side bug and propagates (it is not per-subscriber and must not be
    # swallowed); only the bounded put is handled, explicitly, in _enqueue.
    msg = json.dumps(_snapshot())
    for q in list(SUBS):
        _enqueue(q, msg)


def _bump(bid: str) -> None:
    BOARDS[bid]["version"] += 1
    BOARDS[bid]["updated"] = time.time()
    # _broadcast -> _snapshot() prunes, so a separate _prune() here was a second
    # full pass over BOARDS per write. The bump sets updated=now, so this board
    # cannot be the one pruned.
    _broadcast()


async def _reaper() -> None:
    """Prune expired boards even when nothing else is happening."""
    while True:
        await asyncio.sleep(5)
        if _prune():
            _broadcast()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        # Await the cancelled reaper instead of firing the cancel and walking
        # away: the fire-and-forget cancel relied on asyncio.run()'s teardown
        # to reap the task, and a reaper that had already died of a REAL
        # exception (a serialization bug in _broadcast, which deliberately
        # propagates) surfaced only as a GC-time "exception was never
        # retrieved" note long after the fact. gather() re-raises a real
        # exception here — visible in the shutdown log — while the expected
        # CancelledError is swallowed.
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(task)


app = FastAPI(title="Livewall", lifespan=lifespan)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Require the bearer token on the whole `/api/*` surface when configured.

    `/health` and the static client are deliberately NOT gated: a liveness
    probe cannot carry a credential, and the client bundle is public code (it
    ships on GitHub) — the data, not the app, is what the token protects. The
    client learns the token from `?token=` once and then sends it as a header
    on every fetch.
    """
    path = request.url.path
    if auth.enabled() and (path == "/api" or path.startswith("/api/")):
        if not auth.authorized(request):
            return JSONResponse(
                {"ok": False, "error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


@app.get("/health")
async def health(req: Request):
    # `subscribers` is the live SSE client count. It is exposed so a client
    # leak (a reconnect that abandons a connection without aborting it) is
    # observable from outside the process; the client-side accounting
    # check reads it.
    #
    # This is the one endpoint the auth gate deliberately leaves open (a
    # liveness probe cannot carry a credential). Board ids are the addressing
    # namespace of every agent session and `config` is global state, so an
    # UNAUTHENTICATED caller on a token-protected backend gets liveness only.
    # When auth is off, or the caller presents the token, the full payload is
    # returned as before.
    if auth.enabled() and not auth.authorized(req):
        return {"ok": True}
    return {"ok": True, "boards": len(BOARDS), "ids": list(BOARDS),
            "subscribers": len(SUBS), "config": CONFIG}


# ------------------------------------------------------------------ config
@app.get("/api/config")
async def get_config():
    return CONFIG


@app.put("/api/config")
async def put_config(req: Request):
    body, err = await _read_json(req)
    if err:
        return err
    # Validate all keys first: a later bad value must not leave an earlier one
    # already applied.
    changes: dict = {}
    for key in ("default_ttl", "stale_ttl", "max_boards"):
        if key in body:
            try:
                changes[key] = _config_value(key, body[key])
            except (TypeError, ValueError) as e:
                return JSONResponse(
                    {"ok": False, "error": f"{key} {e}"},
                    status_code=400)
    # Persist FIRST, then commit. A config that cannot be written to disk must
    # not be reported as saved (the pre-fix code swallowed the write failure and
    # returned ok:true), and must not take effect in memory either: either the
    # file and the running config both change, or neither does.
    new_config = {**CONFIG, **changes}
    try:
        _save_config(new_config)
    except (OSError, TypeError, ValueError) as e:
        logger.error("could not persist config to %s: %s", CONFIG_FILE, e)
        # The client gets a named failure but NOT the path or the raw exception:
        # an OSError's text embeds the filesystem path, which is internal detail
        # a remote caller should not receive. The log has both.
        return JSONResponse(
            {"ok": False,
             "error": "could not persist config; the server log has the reason."},
            status_code=500)
    CONFIG.clear()
    CONFIG.update(new_config)
    _prune()
    _broadcast()
    return {"ok": True, "config": CONFIG}


# ------------------------------------------------------------------ boards
@app.get("/api/boards")
async def list_boards():
    _prune()
    return {"boards": [_summary(bid, b) for bid, b in
                       sorted(BOARDS.items(), key=lambda kv: -kv[1]["updated"])]}


@app.get("/api/boards/{bid}")
async def get_board(bid: str):
    bid, err = _resolve_id(bid)
    if err:
        return err
    b = BOARDS.get(bid)
    # The ok flag: every other refusal on the API carries {"ok": false,
    # "error": ...}; this one omitted it, so a client keying on `ok` could not
    # tell this apart from a success-shaped body.
    return JSONResponse(b) if b else JSONResponse(
        {"ok": False, "error": f"no board {bid!r}"}, status_code=404)


@app.post("/api/boards/{bid}")
async def set_board(bid: str, req: Request):
    bid, err = _resolve_id(bid)
    if err:
        return err
    replace, err = _replace_flag(req)
    if err:
        return err
    body, err = await _read_json(req)
    if err:
        return err
    # Validate EVERYTHING before mutating: a partial write must not survive a
    # later field's refusal.
    try:
        status = contract.validate_status(body.get("status"))
        blocks = contract.validate_blocks(body.get("blocks", []))
        title = contract.validate_text(body.get("title"), "title", contract.MAX_TITLE)
        subtitle = contract.validate_text(body.get("subtitle"), "subtitle", contract.MAX_SUBTITLE)
        tab = contract.validate_tab(body["tab"]) if "tab" in body else None
        ttl = contract.validate_ttl(body["ttl"]) if "ttl" in body else None
    except contract.ContractError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    # The board id is a shared, unowned namespace. A full set_board on an id that
    # already exists is refused (409) unless the caller explicitly opted in, so
    # two sessions cannot silently clobber each other's tab. Callers that know
    # the id should use PATCH /update /reset instead; this guard is only about a
    # wholesale REPLACE of a live board.
    existing = BOARDS.get(bid)
    if existing is not None and not replace:
        return JSONResponse(
            {"ok": False,
             "error": f"board {bid!r} already exists; choose a different id, or "
                      "pass replace=true to overwrite it."},
            status_code=409)
    b = existing or _blank()
    if title is not None:
        b["title"] = title
    b["subtitle"] = subtitle if subtitle is not None else ""
    b["status"] = status or "running"
    b["blocks"] = blocks
    if "tab" in body:
        b["tab"] = tab
    if "ttl" in body:
        b["ttl"] = ttl
    BOARDS[bid] = b
    _bump(bid)
    return {"ok": True, "id": bid, "version": b["version"], "summary": _summary(bid, b)}


@app.patch("/api/boards/{bid}")
async def patch_board(bid: str, req: Request):
    """Partial update of board-level fields (no block changes)."""
    bid, err = _resolve_id(bid)
    if err:
        return err
    b = BOARDS.get(bid)
    if not b:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    body, err = await _read_json(req)
    if err:
        return err
    # Re-fetch after the await: the 5s reaper (or another request's _prune) can
    # delete the board while we waited for the body. The early check above is
    # only a fast 404; without this re-fetch `_bump` would KeyError -> 500.
    b = BOARDS.get(bid)
    if not b:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    # Validate all present fields into `changes` first, then apply — a later
    # refusal must not leave an earlier field already mutated.
    try:
        changes: dict = {}
        if "title" in body:
            changes["title"] = contract.validate_text(
                body["title"], "title", contract.MAX_TITLE, allow_none=False)
        if "subtitle" in body:
            changes["subtitle"] = contract.validate_text(
                body["subtitle"], "subtitle", contract.MAX_SUBTITLE, allow_none=False)
        if "status" in body:
            changes["status"] = contract.validate_status(body["status"], allow_none=False)
        if "tab" in body:
            changes["tab"] = contract.validate_tab(body["tab"])
        if "ttl" in body:
            changes["ttl"] = contract.validate_ttl(body["ttl"])
    except contract.ContractError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    b.update(changes)
    _bump(bid)
    return {"ok": True, "id": bid, "version": b["version"], "summary": _summary(bid, b)}


@app.post("/api/boards/{bid}/update")
async def update_block(bid: str, req: Request):
    bid, err = _resolve_id(bid)
    if err:
        return err
    b = BOARDS.get(bid)
    if not b:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    body, err = await _read_json(req)
    if err:
        return err
    # Re-fetch after the await (see patch_board): a prune during the body read
    # must not leave us mutating a detached dict and then KeyError in _bump.
    b = BOARDS.get(bid)
    if not b:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    target = body.get("id")
    # `id` is the address of the mutation. Absent (or null), `target` was None
    # and matched the FIRST anonymous (id-less) block — `blk.get("id") == None`
    # — so an update with no destination silently mutated whatever happened to
    # sit first; a non-string id fell through to a misleading "no block id 123"
    # 404. Both are refused with the field named. (An empty-string id is still
    # a legal stored address and still addresses itself here.)
    if not isinstance(target, str):
        return JSONResponse(
            {"ok": False,
             "error": "`id` must be a string naming the block to update "
                      f"(got {type(target).__name__ if target is not None else 'nothing'})."},
            status_code=400)
    set_fields = body.get("set")
    if set_fields is not None and not isinstance(set_fields, dict):
        return JSONResponse(
            {"ok": False, "error": "`set` must be an object of field: value pairs."},
            status_code=400)
    if "append" in body and not isinstance(body["append"], str):
        return JSONResponse(
            {"ok": False, "error": "`append` must be a string."}, status_code=400)
    if "append" in body:
        # An appended log line is stored and re-sent in every snapshot; cap it
        # like any other stored string (the contract's MAX_TEXT_LEN).
        try:
            contract.validate_text(body["append"], "append", contract.MAX_TEXT_LEN,
                                   allow_none=False)
        except contract.ContractError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    for blk in b["blocks"]:
        if blk.get("id") == target:
            btype = blk.get("type", "")
            allowed = FIELDS.get(btype, set()) | {"id", "type"}
            if "append" in body and btype not in LOG_LIKE:
                return JSONResponse(
                    {"ok": False, "error":
                     f"block {target!r} is a {btype!r} block — `append` only works on "
                     f"{' / '.join(sorted(LOG_LIKE))} blocks. Use `set` instead."},
                    status_code=400)
            bad = [k for k in (set_fields or {}) if k not in allowed]
            if bad:
                return JSONResponse(
                    {"ok": False, "error":
                     f"unknown field(s) {bad} for a {btype!r} block. "
                     f"Valid fields: {sorted(allowed)}."},
                    status_code=400)
            # Value types too: a hostile `set` used to reach the emitter (500)
            # or the client (silent whole-board drop). Same contract as set_board.
            try:
                contract.validate_set(btype, set_fields or {}, repr(target))
            except contract.ContractError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            # A `set` may rewrite `id`/`type`. `validate_set` checks values
            # against the block's OLD type, so a type change must re-validate the
            # MERGED block under the NEW type (a `stat` -> `progress` with
            # value "abc" passes the old scalar check but is not numeric for a
            # progress). An `id` rewrite must also preserve the board's unique-id
            # invariant that set_board enforces, or update_block can no longer
            # address the block it just renamed.
            new_id = set_fields.get("id") if set_fields else None
            if new_id is not None and any(
                    o is not blk and o.get("id") == new_id for o in b["blocks"]):
                return JSONResponse(
                    {"ok": False, "error":
                     f"block id {new_id!r} already exists on {bid!r}; block ids must be "
                     "unique (update_block addresses blocks by id)."},
                    status_code=400)
            merged = {**blk, **(set_fields or {})}
            try:
                contract.validate_block_values(merged, repr(target))
            except contract.ContractError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            if "append" in body:
                blk.setdefault("lines", []).append(body["append"])
                if len(blk["lines"]) > contract.MAX_LOG_LINES:
                    blk["lines"] = blk["lines"][-contract.MAX_LOG_LINES:]
            for k, v in (set_fields or {}).items():
                blk[k] = v
            _bump(bid)
            return {"ok": True, "id": bid, "version": b["version"], "block": blk}
    return JSONResponse({"ok": False, "error": f"no block id {target!r}"}, status_code=404)


@app.post("/api/boards/{bid}/reset")
async def reset_board(bid: str):
    bid, err = _resolve_id(bid)
    if err:
        return err
    b = BOARDS.get(bid)
    if not b:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    b["blocks"] = []
    b["status"] = "idle"
    b["subtitle"] = "idle — nothing programmed"
    _bump(bid)
    return {"ok": True, "id": bid, "version": b["version"]}


@app.delete("/api/boards/{bid}")
async def delete_board(bid: str):
    bid, err = _resolve_id(bid)
    if err:
        return err
    if bid not in BOARDS:
        return JSONResponse({"ok": False, "error": f"no board {bid!r}"}, status_code=404)
    del BOARDS[bid]
    _broadcast()
    return {"ok": True, "id": bid}


# ------------------------------------------------------------------ stream
class _StreamResponse(StreamingResponse):
    """A StreamingResponse that always closes its body iterator AND its subscriber.

    Why this exists: on a client disconnect, Starlette's ASGI-2.3 code path
    (uvicorn's h11 protocol advertises spec_version 2.3) cancels the
    response-sending task mid-`send()` and NEVER closes the body iterator.
    That is harmless for a finite response, but an SSE generator suspended
    at a bare `yield` — which is exactly where it sits whenever the client
    has stopped reading and uvicorn's flow control has parked the send — is
    then ABANDONED with its `finally` unexecuted: the subscriber queue stays
    in SUBS until the cyclic GC happens to collect the generator (on an
    idle server, that is never; observed live: 5 subscribers still counted
    10+ minutes after their clients disconnected). Closing the iterator
    here runs that `finally` on every exit path — normal end, client
    disconnect, and the shutdown cancel from GRACEFUL_SHUTDOWN_TIMEOUT.

    The queue is ALSO held by the response itself and discarded in this
    `finally`, because closing the iterator covers one case short of all of
    them: a disconnect that lands BEFORE the generator's first iteration.
    Starlette sends the response headers before the first `async for`
    (starlette/responses.py:251), so a client that vanishes in that window
    cancels the task with the generator UNSTARTED — and closing a never-
    started generator runs no `finally` at all. The discard here is the
    authority; the generator's own `finally` remains as an idempotent
    backstop (set.discard is a no-op twice).
    """

    def __init__(self, *args, queue: asyncio.Queue, **kwargs):
        super().__init__(*args, **kwargs)
        self._queue = queue

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # aclose() on an already-closed generator is a no-op, so the
            # normal paths (generator exhausted, or cancellation that
            # propagated INTO the generator and already ran its finally)
            # are unaffected.
            await self.body_iterator.aclose()
            SUBS.discard(self._queue)


async def _stream_gen(q: asyncio.Queue):
    """SSE generator: one initial snapshot, then coalesced broadcasts.

    The queue is bounded (STREAM_QUEUE_MAX) and _broadcast drops the unread
    older snapshot on overflow, so a client that stops reading cannot make
    the server accumulate frames. After KEEPALIVE seconds of silence a full
    snapshot frame is emitted; the TimeoutError is swallowed here so a quiet
    stream never terminates.

    The silence frame is a `data:` snapshot, not a `: keepalive` comment: the
    client measures data age by snapshots only (the transport skips comment
    lines), so a bare comment left a healthy, connected wall flip its chip to
    "stale" after 45s of no board changes and announce "Not receiving live
    updates" while connected. A snapshot frame is both the liveness signal and
    a data frame, so "no snapshot for 45s" now genuinely means the stream is
    dead — which is what the freshness heuristic (STALE_AFTER_SECONDS = 45 vs
    the 15s keepalive) documents as its intent.
    """
    try:
        yield f"data: {json.dumps(_snapshot())}\n\n"
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=KEEPALIVE)
                yield f"data: {msg}\n\n"
            except asyncio.TimeoutError:
                # _snapshot() also prunes, exactly as a write-driven broadcast
                # would; the reaper runs every 5s anyway, so this adds no new
                # pruning semantics — just a liveness pulse that carries data.
                yield f"data: {json.dumps(_snapshot())}\n\n"
    finally:
        SUBS.discard(q)


@app.get("/api/stream")
async def stream_all():
    # Cap concurrent subscribers: refuse with 503 BEFORE building the response,
    # so the client sees a clean refusal rather than a stream that dies on its
    # first read. There is no await between this check and the `SUBS.add` below,
    # so the two are atomic on the event loop.
    if len(SUBS) >= MAX_SUBSCRIBERS:
        return JSONResponse(
            {"ok": False,
             "error": f"too many stream subscribers; the limit is {MAX_SUBSCRIBERS}."},
            status_code=503)
    q: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
    SUBS.add(q)
    return _StreamResponse(
        _stream_gen(q),
        queue=q,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# An unknown /api path must answer in the refusal vocabulary, not FastAPI's
# default `{"detail": ...}`: docs/api.md promises every refusal carries
# `ok: false`, and a JSON client keying on `ok` saw a body that was neither
# success nor failure. A mistyped POST also used to get the static mount's
# 405 — which wrongly implies the endpoint exists with other methods.
# Registered AFTER every real route (and before the static mount), so only
# genuinely unknown endpoints land here; `/api/boards`, `/api/stream` etc.
# are matched first.
@app.api_route("/api/{rest:path}",
               methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def _api_no_route(rest: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": f"no such endpoint: /api/{rest}"},
        status_code=404,
    )


# The client is the React app (web/). It is same-origin: it fetches /api/*
# relative, so it works on the direct port AND behind a reverse proxy with no
# CORS or base-href concerns.
_WEB = Path(os.environ.get("LIVEWALL_WEB_DIR") or (HERE.parent / "web" / "dist"))
if _WEB.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
else:
    # Not fatal — the API is fully usable headless (that is the MCP path), and in
    # development the client is served by Vite. Explain that instead of a bare 404.
    @app.get("/")
    async def _no_client():
        return JSONResponse(
            {
                "ok": True,
                "detail": "Livewall API is running; no web client build found.",
                "build": "cd web && npm install && npm run build",
                "endpoints": ["/health", "/api/boards", "/api/stream"],
            }
        )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level=os.environ.get("LIVEWALL_LOG_LEVEL", "warning"),
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT,
    )
