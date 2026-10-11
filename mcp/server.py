#!/usr/bin/env python3
"""livewall-mcp — MCP server exposing the Livewall API as MCP tools.

Livewall is an agent-programmable live status surface: an agent POSTs a JSON
"board spec" and a web client renders it, live. Many agents/sessions each own a
NAMED board (shown as a tab). This server wraps the Livewall REST API so any
MCP client can drive boards.

Contract hardening (the API's silent failure modes become server-side refusals):

- **Block vocabulary is validated.** An unknown `type` renders as
  "[unknown block]" in the client — a silent, ugly failure. This server refuses
  unknown types and names the valid set.
- **Tab spec is validated.** An unrecognised `accent` silently falls back and a
  bad `indicator.kind` silently degrades to a derived bar; both are refused here.
- **Every block needs a unique string `id`** for `update_block` to address it.
  Missing/duplicate ids are refused up front rather than discovered later.
- **Writes are read back** and report the resulting version + summary, so the
  caller never has to guess whether the board actually changed.
- **`update_block` on a missing id lists the ids that DO exist**, instead of a
  bare 404.

Env vars:
  LIVEWALL_URL   base URL of the Livewall backend (default http://127.0.0.1:8470)
  LIVEWALL_TOKEN bearer token, when the backend gates /api/* (see backend/auth.py)

A `.env` at the repo root is read if present (override with LIVEWALL_MCP_ENV_FILE).

Run: stdio (python server.py, or fastmcp run server.py:mcp).
"""

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

import httpx
from fastmcp import FastMCP
from pydantic import Field

# The board contract (vocabulary, limits, validators) lives in the backend and
# is shared, so this MCP guard and the HTTP API can never disagree about what is
# legal. This server is an EARLIER, friendlier guard; the API enforces the same
# rules for callers that skip MCP.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import contract  # noqa: E402

# Optional env file. Defaults to the repo-root .env so a clone is configured in
# one place; override with LIVEWALL_MCP_ENV_FILE.
_ENV_FILE = os.environ.get("LIVEWALL_MCP_ENV_FILE") or str(
    Path(__file__).resolve().parent.parent / ".env"
)
if os.path.exists(_ENV_FILE):
    for _line in open(_ENV_FILE):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k, _v.strip('"').strip("'"))

BASE_URL = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:8470").rstrip("/")
TIMEOUT = float(os.environ.get("LIVEWALL_TIMEOUT", "15"))

mcp = FastMCP("livewall")

# --------------------------------------------------------------------------- #
# vocabulary — the shared contract (backend/contract.py) is the source of truth
# --------------------------------------------------------------------------- #

# Human-readable field docs for describe_blocks. The *set* of valid types is
# contract.BLOCK_TYPES; both are derived from the same vocabulary.
BLOCK_FIELDS: dict[str, str] = {
    "heading": "text, eyebrow?",
    "text": "text",
    "stat": "label, value, unit?",
    "stats": "items:[{label,value,unit?}]",
    "progress": "label, value, max, unit?, fmt?",
    "gauge": "label, value, max, unit?, color? (accent name), fmt?",
    "bar": "label?, color? (accent name), data:[[label,value],...] or {label,value} objects, fmt?",
    "sparkline": "label?, color? (accent name), values:[num,...]",
    "chart": "alias of sparkline",
    "line": "label?, color? (accent name), series:[{name?,values:[num,...],color?}], values? (sugar), xLabels?, yMin?, yMax?, legend?",
    "table": "columns:[...], rows:[[...],...], tagColumn?",
    "log": "title?, lines:[...]",
    "kv": "items:[[key,value],...] or {k,v} objects",
    "list": "label?, items:[...]",
    "callout": "text, tone? (ok|warn|error|info)",
    "code": "text, title?",
    "divider": "(no fields)",
}

BLOCK_TYPES = contract.BLOCK_TYPES
STATUSES = contract.STATUSES
ACCENTS = contract.ACCENTS
INDICATORS = contract.INDICATORS
MAX_BLOCKS = contract.MAX_BLOCKS
MAX_LOG_LINES = contract.MAX_LOG_LINES


class LivewallError(Exception):
    """A caller-facing refusal (bad input or upstream rejection)."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _client() -> httpx.Client:
    headers = {}
    token = os.environ.get("LIVEWALL_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, headers=headers)


def _request(method: str, path: str, json_body: Optional[dict] = None) -> Any:
    try:
        with _client() as c:
            r = c.request(method, path, json=json_body)
    except httpx.HTTPError as e:
        raise LivewallError(
            f"cannot reach Livewall at {BASE_URL}: {e}. "
            "Is the backend running? Start it with `docker compose up` or "
            "`python backend/app.py`, and check LIVEWALL_URL."
        ) from e
    if r.status_code >= 400:
        try:
            detail = r.json()
        except ValueError:
            # The response is already in hand; only its body failed to parse as
            # JSON. Fall back to the raw text — this cannot mask a transport
            # failure (those were caught as httpx.HTTPError above), and the
            # caller still gets a LivewallError naming the status.
            detail = r.text[:300]
        raise LivewallError(f"Livewall returned HTTP {r.status_code}: {detail}")
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def _validate_blocks(blocks: Any) -> list[dict]:
    """Validate a block list against the vocabulary. Raises LivewallError."""
    try:
        return contract.validate_blocks(blocks)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


def _validate_tab(tab: Any) -> Optional[dict]:
    try:
        return contract.validate_tab(tab)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


def _validate_ttl(ttl: Any) -> Optional[int]:
    try:
        return contract.validate_ttl(ttl)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


def _validate_status(status: Any) -> Optional[str]:
    try:
        return contract.validate_status(status)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


def _validate_board_id(bid: Any) -> str:
    try:
        return contract.validate_board_id(bid)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


def _validate_text(value: Any, field: str, max_len: int) -> Optional[str]:
    try:
        return contract.validate_text(value, field, max_len)
    except contract.ContractError as e:
        raise LivewallError(str(e)) from e


# --------------------------------------------------------------------------- #
# tools — discovery
# --------------------------------------------------------------------------- #

@mcp.tool
def livewall_ping() -> dict:
    """Check the Livewall backend is reachable; returns health + current config."""
    return _request("GET", "/health")


@mcp.tool
def describe_blocks() -> dict:
    """Return the block vocabulary, tab spec, and statuses Livewall understands.

    Call this FIRST when programming a board — it is the authoritative list of
    block `type` values and their fields.
    """
    return {
        "block_types": BLOCK_FIELDS,
        "block_shape": "each block is {\"type\": <type>, \"id\"?: <str>, ...fields}",
        "board_fields": {
            "id": "path segment; lowercase slug (letters, digits, - and _). Choose a specific, unique id (task/run) — set_board on an existing id is refused with 409 unless replace=True.",
            "title": "string — board heading",
            "subtitle": "string — secondary line",
            "status": f"one of {list(STATUSES)}",
            "blocks": "list of block objects",
            "tab": "tab descriptor (see tab_spec)",
            "ttl": "seconds a FINISHED board survives after its last update",
        },
        "tab_spec": {
            "label": "tab text (defaults to board title)",
            "accent": f"dot colour: {list(ACCENTS)} (defaults from status)",
            "indicator": {
                "kind": f"one of {list(INDICATORS)}",
                "bar": "{kind:'bar', value:num, max:num} — mini bar + percent",
                "pct": "{kind:'pct', value:num} — just a percent",
                "text": "{kind:'text', text:'3/64'} — a badge",
                "none": "no indicator",
            },
        },
        "notes": [
            "A block with a progress/gauge and no explicit tab.indicator gets a derived bar.",
            "Block `id`s must be unique; update_block addresses blocks by id.",
            "Finished boards (done/error) are pruned after their ttl (global default if unset).",
            "NUMBER FORMATTING — you decide, per value (see number_format).",
        ],
        "number_format": {
            "rule": (
                "Numbers are EXACT by default. YOU choose when to compact, per value, "
                "based on what the number means."
            ),
            "how": [
                "Pass a NUMBER -> rendered exactly (6836 -> '6836'). Default; use for counts.",
                "Pass a STRING -> rendered VERBATIM, so you can pre-format it ('1.5M', "
                "'6,836', 'n/a'). Use for stat/stats values only.",
                "Pass \"fmt\": \"compact\" on a progress/gauge/bar block -> its numeric "
                "label renders shortened (1521227 -> '1.5M') while the bar still uses the "
                "true value for the ratio. Use for tokens, bytes, link totals.",
            ],
            "guidance": [
                "EXACT for anything a human would want to read off or diff: memory/"
                "document/node counts, rows processed, items done, error counts.",
                "COMPACT for quantities that are only meaningful at scale: token counts, "
                "byte sizes, total links.",
                "'6836' not '6.8K' for a count; '1.5M' is fine for tokens.",
            ],
            "examples": [
                '{"type":"stats","items":[{"label":"Memories","value":5371},'
                '{"label":"Tokens","value":"1.5M"}]}  <- number exact, string verbatim',
                '{"type":"gauge","label":"Tokens","value":1521227,"max":2000000,'
                '"fmt":"compact"}  <- label "1.5M", arc uses the real number',
                '{"type":"progress","label":"Shards","value":3,"max":64}  <- exact "3 / 64"',
            ],
        },
    }


# --------------------------------------------------------------------------- #
# tools — read
# --------------------------------------------------------------------------- #

@mcp.tool
def list_boards() -> dict:
    """List every board with its status, ttl, age, progress and resolved tab."""
    data = _request("GET", "/api/boards")
    boards = data.get("boards", [])
    return {"count": len(boards), "boards": boards}


@mcp.tool
def get_board(board_id: str) -> dict:
    """Get one board's full spec (title, status, blocks, tab, ttl)."""
    return _request("GET", f"/api/boards/{_validate_board_id(board_id)}")


@mcp.tool
def get_config() -> dict:
    """Get Livewall's global defaults: default_ttl, stale_ttl, max_boards."""
    return _request("GET", "/api/config")


# --------------------------------------------------------------------------- #
# tools — write
# --------------------------------------------------------------------------- #

@mcp.tool
def set_board(
    board_id: str,
    title: str,
    blocks: list[dict],
    subtitle: str = "",
    status: str = "running",
    tab: Optional[dict] = None,
    ttl: Optional[int] = None,
    replace: bool = False,
) -> dict:
    """Create a board. This is the main tool for programming a board.

    A board id is a shared, unowned namespace. Calling this on an id that is
    already live is refused with HTTP 409 — it will NOT silently overwrite
    another session's board. To replace a live board on purpose, pass
    replace=True. To update a board you already know, prefer update_board /
    update_block / reset_board (they never need the flag).

    Choose a SPECIFIC, unique id that includes the task or run (e.g.
    "migrate-users-2026-10-08"), not a bare generic word like "progress" —
    generic ids are exactly what collides across sessions.

    Args:
        board_id: board slug — lowercase letters, digits, '-' and '_' only.
            Required and specific; there is no default id.
        title: board heading.
        blocks: list of block objects — see describe_blocks for types/fields.
        subtitle: secondary line under the title.
        status: idle | running | done | error.
        tab: optional tab descriptor {label, accent, indicator}.
        ttl: seconds a finished board survives after its last update (default from config).
        replace: overwrite a board that already has this id (default False).
            Only set it when you intend to replace a live board.

    Numbers are EXACT by default; YOU decide per value whether to compact.
    Pass a number for exact ('6836'), a string to render it verbatim ('1.5M'),
    or set "fmt": "compact" on a progress/gauge/bar to shorten its label.
    Counts exact, tokens compact — see describe_blocks -> number_format.
    """
    board_id = _validate_board_id(board_id)
    payload = {
        "title": _validate_text(title, "title", contract.MAX_TITLE),
        "subtitle": _validate_text(subtitle, "subtitle", contract.MAX_SUBTITLE) or "",
        "status": _validate_status(status) or "running",
        "blocks": _validate_blocks(blocks),
    }
    if tab is not None:
        payload["tab"] = _validate_tab(tab)
    if ttl is not None:
        payload["ttl"] = _validate_ttl(ttl)
    path = f"/api/boards/{board_id}" + ("?replace=true" if replace else "")
    res = _request("POST", path, payload)
    # read back so the caller sees the authoritative state, not just an ack
    return {"ok": True, "id": res.get("id"), "version": res.get("version"),
            "summary": res.get("summary")}


@mcp.tool
def update_board(
    board_id: str,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    status: Optional[str] = None,
    tab: Optional[dict] = None,
    ttl: Optional[int] = None,
) -> dict:
    """Update board-level fields WITHOUT touching blocks (title/subtitle/status/tab/ttl).

    Use this to flip a board to `done`, retune its ttl, or restyle its tab.
    """
    board_id = _validate_board_id(board_id)
    payload: dict = {}
    if title is not None:
        payload["title"] = _validate_text(title, "title", contract.MAX_TITLE)
    if subtitle is not None:
        payload["subtitle"] = _validate_text(subtitle, "subtitle", contract.MAX_SUBTITLE)
    if status is not None:
        payload["status"] = _validate_status(status)
    if tab is not None:
        payload["tab"] = _validate_tab(tab)
    if ttl is not None:
        payload["ttl"] = _validate_ttl(ttl)
    if not payload:
        raise LivewallError("nothing to update — pass at least one field.")
    res = _request("PATCH", f"/api/boards/{board_id}", payload)
    return {"ok": True, "id": res.get("id"), "version": res.get("version"),
            "summary": res.get("summary")}


@mcp.tool
def update_block(
    board_id: str,
    block_id: str,
    set_fields: Optional[dict] = None,
    append: Optional[str] = None,
) -> dict:
    """Mutate ONE block of an existing board (cheap — no full-board resend).

    Args:
        board_id: the board to edit.
        block_id: the target block's `id`.
        set_fields: fields to overwrite on the block, e.g. {"value": 9410}.
        append: for `log` blocks, a line to append.

    Numbers are EXACT by default. To compact a progress/gauge/bar label, set
    {"fmt": "compact"} alongside the value. For a stat/stats chip, pass a
    pre-formatted string to render it verbatim. See describe_blocks ->
    number_format.
    """
    board_id = _validate_board_id(board_id)
    if not set_fields and append is None:
        raise LivewallError("pass `set_fields` and/or `append`.")
    payload: dict = {"id": block_id}
    if set_fields:
        payload["set"] = set_fields
    if append is not None:
        payload["append"] = append
    try:
        res = _request("POST", f"/api/boards/{board_id}/update", payload)
    except LivewallError as err:
        msg = str(err)
        if "no block id" not in msg:
            raise
        # Enrich a missing-block error with the ids that DO exist. The lookup is
        # BEST-EFFORT: if it fails we still raise the original refusal below
        # (with ids "unknown"), so swallowing the enrichment failure can never
        # turn a refusal into a silent no-op — it only costs the id list.
        # NOTE: do NOT wrap this raise in another try/except that re-raises `err`
        # — that sets err.__context__ to the new error while the new error's
        # __cause__ points back at err, forming a traceback cycle that Rich
        # renders forever (the MCP call hangs).
        ids: Optional[list] = None
        try:
            b = _request("GET", f"/api/boards/{board_id}")
            ids = [x.get("id") for x in (b.get("blocks") or []) if x.get("id")]
        except LivewallError:
            pass
        raise LivewallError(
            f"{msg} Existing block ids on {board_id!r}: "
            f"{ids if ids is not None else 'unknown'} "
            "(blocks without an id cannot be addressed by update_block)."
        ) from err
    return {"ok": True, "id": res.get("id"), "version": res.get("version"),
            "block": res.get("block")}


@mcp.tool
def reset_board(board_id: str) -> dict:
    """Clear a board's blocks and set it back to idle (keeps the board)."""
    return _request("POST", f"/api/boards/{_validate_board_id(board_id)}/reset")


@mcp.tool
def delete_board(board_id: str) -> dict:
    """Remove a board entirely (its tab disappears)."""
    return _request("DELETE", f"/api/boards/{_validate_board_id(board_id)}")


@mcp.tool
def set_config(
    default_ttl: Annotated[Optional[int], Field(strict=True)] = None,
    stale_ttl: Annotated[Optional[int], Field(strict=True)] = None,
    max_boards: Annotated[Optional[int], Field(strict=True)] = None,
) -> dict:
    """Set Livewall's global defaults.

    Args:
        default_ttl: terminal-board TTL when a board sets none (seconds).
        stale_ttl: how long a `running` board survives with no update (seconds).
        max_boards: hard cap on live boards; oldest-updated are evicted past it.

    Each value must be a positive integer; a bool or non-positive value is
    refused rather than silently coerced. The schema is strict, so a JSON bool
    (`int(True)==1`) or a numeric string is refused before it can be coerced.
    """
    payload: dict = {}
    for key, val in (("default_ttl", default_ttl), ("stale_ttl", stale_ttl),
                     ("max_boards", max_boards)):
        if val is not None:
            # A bool is an int subclass: `int(True)` is 1, so without this a
            # caller passing `true` would silently set the value to 1 second.
            if isinstance(val, bool):
                raise LivewallError(f"{key} must be an integer.")
            try:
                n = int(val)
            except (TypeError, ValueError):
                raise LivewallError(f"{key} must be an integer.")
            if n <= 0:
                raise LivewallError(f"{key} must be positive.")
            payload[key] = n
    if not payload:
        raise LivewallError("nothing to set — pass at least one field.")
    return _request("PUT", "/api/config", payload)


if __name__ == "__main__":
    mcp.run()
