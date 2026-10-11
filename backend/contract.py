"""The Livewall board contract — vocabulary, limits and validators.

This is the single source of truth shared by the HTTP API (backend/app.py) and
the MCP server (mcp/server.py). The API is the network-exposed surface; the MCP
is an earlier guard in front of it. Both run the validators below, so the two
can never disagree about what is legal.

Every refusal names the offending field and the valid set, so a caller can fix
the request instead of guessing.
"""
from __future__ import annotations

import math
import re
from typing import NoReturn

# A board id is the URL path segment and the map key. It must be canonical so
# two distinct client-supplied ids can never slug to one board (one agent
# overwriting another's). Lowercase letters, digits, '-' and '_' only.
_BOARD_ID_RE = re.compile(r"[a-z0-9_-]+")

# Per-block-type writable fields (beyond the universal id/type). The keys are
# the block vocabulary the client renders.
BLOCK_FIELDS: dict[str, set] = {
    "heading": {"text", "eyebrow"},
    "text": {"text"},
    "stat": {"label", "value", "unit"},
    "stats": {"items"},
    "progress": {"label", "value", "max", "unit", "fmt"},
    "gauge": {"label", "value", "max", "unit", "color", "fmt"},
    "bar": {"label", "color", "data", "fmt"},
    "sparkline": {"label", "color", "values"},
    "chart": {"label", "color", "values"},
    "line": {"label", "color", "series", "values", "xLabels", "yMin", "yMax", "legend"},
    "table": {"columns", "rows", "tagColumn"},
    "log": {"title", "lines"},
    "kv": {"items"},
    "list": {"label", "items"},
    "callout": {"text", "tone"},
    "code": {"text", "title"},
    "divider": set(),
}

BLOCK_TYPES = frozenset(BLOCK_FIELDS)

# The keys each structured list-item kind checks inside one entry, in the order
# _check_kind checks them. Public on purpose: describe_blocks (mcp/server.py)
# and the vocabulary parity test (backend/tests/vocab_parity_test.py) derive
# the advertised item shape from this, so the validator, the agent-facing
# vocabulary and the renderers cannot drift apart. The pair forms ([a, b]) are
# positional and keyless; these keys are the DICT form each kind also accepts.
ITEM_KIND_KEYS: dict[str, tuple[str, ...]] = {
    "stats_items": ("label", "value", "unit"),
    "bar_data": ("label", "value"),
    "line_series": ("name", "values", "color"),
    "kv_items": ("k", "v"),
}

# The *value* kind of each writable field, per block type. Structure (a list of
# typed objects) is validated; these kinds close the value gap: a
# dict where the emitter iterates (stats.items) used to raise inside the emitter
# and 500 the write, and a dict where the client expects a number silently
# dropped the whole board's update. The kinds mirror the client's zod schemas:
# `scalar` is string|number (what the client's `str`/`num` unions accept),
# `numeric` additionally requires a finite number or a numeric string.
_FIELD_KINDS: dict[str, dict[str, str]] = {
    "heading": {"text": "scalar", "eyebrow": "scalar"},
    "text": {"text": "scalar"},
    "stat": {"label": "scalar", "value": "scalar", "unit": "scalar"},
    "stats": {"items": "stats_items"},
    "progress": {"label": "scalar", "value": "numeric", "max": "positive",
                 "unit": "scalar", "fmt": "scalar"},
    "gauge": {"label": "scalar", "value": "numeric", "max": "positive",
              "unit": "scalar", "color": "accent", "fmt": "scalar"},
    "bar": {"label": "scalar", "color": "accent", "data": "bar_data", "fmt": "scalar"},
    "sparkline": {"label": "scalar", "color": "accent", "values": "num_list"},
    "chart": {"label": "scalar", "color": "accent", "values": "num_list"},
    "line": {"label": "scalar", "color": "accent", "series": "line_series",
             "values": "num_list", "xLabels": "scalar_list", "yMin": "numeric",
             "yMax": "numeric", "legend": "boolish"},
    "table": {"columns": "scalar_list", "rows": "rows", "tagColumn": "boolish"},
    "log": {"title": "scalar", "lines": "scalar_list"},
    "kv": {"items": "kv_items"},
    "list": {"label": "scalar", "items": "scalar_list"},
    "callout": {"text": "scalar", "tone": "scalar"},
    "code": {"text": "scalar", "title": "scalar"},
    "divider": {},
}


def _is_number(v) -> bool:
    # bool is an int subclass in Python; a bool is NOT a number here.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_finite(v) -> bool:
    """True iff a number is representable as a finite double.

    JSON integers are arbitrary precision, so `10**400` is a legal JSON number
    but `float(10**400)` raises OverflowError. Such a value is not renderable:
    it used to escape the validator as a 500 (and, for a scalar field, store a
    board whose every later snapshot then raised). An int is bounded to
    MAX_SAFE_INT — the largest integer a JS number represents exactly — so a
    stored value also survives the round trip to the client unchanged.
    """
    if not _is_number(v):
        return False
    if isinstance(v, int):
        return -MAX_SAFE_INT <= v <= MAX_SAFE_INT
    return math.isfinite(v)


def _is_scalar(v) -> bool:
    return isinstance(v, str) or _is_finite(v)


def _is_numeric(v) -> bool:
    if _is_finite(v):
        return True
    if isinstance(v, str):
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _refuse(where, t, field, want, value) -> NoReturn:
    raise ContractError(
        f"block {where} ({t}): `{field}` must be {want}, got "
        f"{type(value).__name__}."
    )


def _reject_long(field: str, value) -> None:
    """Refuse a string value longer than MAX_TEXT_LEN (`field` is a display path)."""
    if isinstance(value, str) and len(value) > MAX_TEXT_LEN:
        raise ContractError(
            f"{field} is too long ({len(value)} chars); the limit is {MAX_TEXT_LEN}."
        )


def _check_kind(kind, value, where, t, field):
    """Refuse a field value whose type the client cannot render."""
    def _len(v, f=None):
        _reject_long(f"block {where} ({t}): `{f or field}`", v)

    if kind == "scalar":
        if not _is_scalar(value):
            _refuse(where, t, field, "a string or a finite number", value)
        _len(value)
    elif kind == "numeric":
        if not _is_numeric(value):
            _refuse(where, t, field, "a finite number (or numeric string)", value)
        _len(value)
    elif kind == "positive":
        if not _is_numeric(value) or float(value) <= 0:
            _refuse(where, t, field, "a positive number", value)
        _len(value)
    elif kind == "scalar_list":
        if not isinstance(value, list):
            _refuse(where, t, field, "a list", value)
        for j, x in enumerate(value):
            if not _is_scalar(x):
                _refuse(where, t, f"{field}[{j}]", "a string or a finite number", x)
            _len(x, f"{field}[{j}]")
    elif kind == "num_list":
        if not isinstance(value, list):
            _refuse(where, t, field, "a list of numbers", value)
        for j, x in enumerate(value):
            if not _is_numeric(x):
                _refuse(where, t, f"{field}[{j}]", "a number", x)
            _len(x, f"{field}[{j}]")
    elif kind == "stats_items":
        if not isinstance(value, list):
            _refuse(where, t, "items", "a list of {label, value, unit?} objects", value)
        for j, it in enumerate(value):
            if not isinstance(it, dict):
                _refuse(where, t, f"items[{j}]", "an object", it)
            for k in ITEM_KIND_KEYS["stats_items"]:
                if k in it:
                    if not _is_scalar(it[k]):
                        _refuse(where, t, f"items[{j}].{k}", "a string or a finite number", it[k])
                    _len(it[k], f"items[{j}].{k}")
    elif kind == "bar_data":
        if not isinstance(value, list):
            _refuse(where, t, "data", "a list of [label, value] pairs", value)
        bk, vk = ITEM_KIND_KEYS["bar_data"]
        for j, d in enumerate(value):
            if isinstance(d, (list, tuple)):
                if len(d) != 2:
                    _refuse(where, t, f"data[{j}]", "a [label, value] pair", d)
                label, val = d
            elif isinstance(d, dict):
                label, val = d.get(bk, ""), d.get(vk, 0)
            else:
                _refuse(where, t, f"data[{j}]", "a [label, value] pair or object", d)
            if not _is_scalar(label):
                _refuse(where, t, f"data[{j}].{bk}", "a string or a finite number", label)
            _len(label, f"data[{j}].{bk}")
            if not _is_numeric(val):
                _refuse(where, t, f"data[{j}].{vk}", "a number", val)
            _len(val, f"data[{j}].{vk}")
    elif kind == "line_series":
        # A multi-series line chart: one entry per series, every entry an
        # object with a `values` point list. All series must share one length
        # (they are drawn against the same x axis), and every point must be a
        # finite renderable number.
        if not isinstance(value, list):
            _refuse(where, t, field, "a list of {name?, values, color?} series", value)
        if len(value) < 1:
            raise ContractError(
                f"block {where} ({t}): `{field}` must contain at least one series."
            )
        lengths: set[int] = set()
        for j, s_ in enumerate(value):
            if not isinstance(s_, dict):
                _refuse(where, t, f"series[{j}]", "an object", s_)
            for k in ITEM_KIND_KEYS["line_series"]:
                if k == "values":
                    continue  # the required point list is checked below
                if k in s_:
                    if not _is_scalar(s_[k]):
                        _refuse(where, t, f"series[{j}].{k}",
                                "a string or a finite number", s_[k])
                    _len(s_[k], f"series[{j}].{k}")
            vals = s_.get("values")
            if not isinstance(vals, list):
                _refuse(where, t, f"series[{j}].values", "a list of numbers", vals)
            if len(vals) < 2:
                raise ContractError(
                    f"block {where} ({t}): `series[{j}].values` must have at least "
                    f"2 points, got {len(vals)}."
                )
            for pi, x in enumerate(vals):
                if not _is_numeric(x):
                    _refuse(where, t, f"series[{j}].values[{pi}]", "a number", x)
                _len(x, f"series[{j}].values[{pi}]")
            lengths.add(len(vals))
        if len(lengths) > 1:
            raise ContractError(
                f"block {where} ({t}): `series` lengths differ; every series must "
                f"have the same number of points "
                f"({', '.join(str(len(s_.get('values') or [])) for s_ in value)})."
            )
    elif kind == "kv_items":
        if not isinstance(value, list):
            _refuse(where, t, "items", "a list of [key, value] pairs", value)
        kk, vk = ITEM_KIND_KEYS["kv_items"]
        for j, pair in enumerate(value):
            if isinstance(pair, (list, tuple)):
                if len(pair) != 2:
                    _refuse(where, t, f"items[{j}]", "a [key, value] pair", pair)
                k, v = pair
            elif isinstance(pair, dict):
                k, v = pair.get(kk, ""), pair.get(vk, "")
            else:
                _refuse(where, t, f"items[{j}]", "a [key, value] pair or object", pair)
            if not _is_scalar(k):
                _refuse(where, t, f"items[{j}].{kk}", "a string or a finite number", k)
            _len(k, f"items[{j}].{kk}")
            if not _is_scalar(v):
                _refuse(where, t, f"items[{j}].{vk}", "a string or a finite number", v)
            _len(v, f"items[{j}].{vk}")
    elif kind == "rows":
        if not isinstance(value, list):
            _refuse(where, t, "rows", "a list of rows", value)
        for j, r in enumerate(value):
            if not isinstance(r, list):
                _refuse(where, t, f"rows[{j}]", "a list of cells", r)
            for ci, cell in enumerate(r):
                if not _is_scalar(cell):
                    _refuse(where, t, f"rows[{j}][{ci}]", "a string or a finite number", cell)
                _len(cell, f"rows[{j}][{ci}]")
    elif kind == "accent":
        # A colour is interpolated by the client into a CSS
        # `var(--accent-<name>)` token, so only a name in ACCENTS may pass: a
        # scalar-only check let `mint) url(//evil` append a url() to the
        # declaration — valid CSS (`background: <color> <image>`) — and every
        # viewer's browser fetched the attacker URL. Case-insensitive (like
        # `tab.accent`), but stored as given; the client normalises.
        if not isinstance(value, str) or value.lower() not in ACCENTS:
            _refuse(where, t, field,
                    f"an accent colour name ({', '.join(ACCENTS)})", value)
    elif kind == "boolish":
        if not isinstance(value, (bool, str)):
            _refuse(where, t, field, "a boolean", value)
        _len(value)


def validate_block_values(blk: dict, where) -> None:
    """Validate the value type of every present field of one block."""
    t = blk.get("type")
    for field, kind in _FIELD_KINDS.get(t, {}).items():
        if field in blk:
            _check_kind(kind, blk[field], where, t, field)
    if t == "line":
        _check_line_cross(blk, where)


def _check_line_cross(blk: dict, where) -> None:
    """Cross-field rules of a `line` block.

    The per-field grain of `_check_kind` sees one field at a time, so sibling
    consistency (series vs values vs xLabels vs the y bounds) is checked here,
    on the whole block. The update path validates the MERGED block, so these
    rules hold after an update_block too.
    """
    t = "line"
    src = None
    if "series" in blk and "values" in blk:
        raise ContractError(
            f"block {where} (line): provide `series` or `values`, not both."
        )
    if "series" in blk:
        src = blk["series"]
    elif "values" in blk:
        src = blk["values"]
    else:
        raise ContractError(
            f"block {where} (line): provide `series` or `values`."
        )
    # The chart's x extent is the per-series point count (one label per point),
    # not the number of series. A `series` list with different lengths has
    # already been refused by the `line_series` kind, so series[0].values is a
    # valid length anchor here.
    n_points = len(src) if "values" in blk else (
        len(src[0].get("values") or []) if src and isinstance(src[0], dict) else 0
    )
    x_labels = blk.get("xLabels")
    if x_labels is not None and len(x_labels) != n_points:
        raise ContractError(
            f"block {where} (line): `xLabels` has {len(x_labels)} entr"
            f"{'y' if len(x_labels) == 1 else 'ies'} but the chart has "
            f"{n_points} points; they must match."
        )
    if "yMin" in blk and "yMax" in blk:
        lo, hi = float(blk["yMin"]), float(blk["yMax"])
        if not hi > lo:
            raise ContractError(
                f"block {where} (line): `yMax` ({blk['yMax']}) must be greater "
                f"than `yMin` ({blk['yMin']})."
            )


def validate_set(btype: str, fields: dict, where) -> None:
    """Validate the `set` payload of update_block against the block's type."""
    if "id" in fields and not isinstance(fields["id"], str):
        raise ContractError(f"block {where} ({btype}): `id` must be a string.")
    if "id" in fields and len(fields["id"]) > MAX_BLOCK_ID_LEN:
        raise ContractError(
            f"block {where} ({btype}): `id` is too long ({len(fields['id'])} chars); "
            f"the limit is {MAX_BLOCK_ID_LEN}.")
    if "type" in fields and fields["type"] not in BLOCK_TYPES:
        raise ContractError(f"block {where} ({btype}): unknown type {fields['type']!r}.")
    kinds = _FIELD_KINDS.get(btype, {})
    for field, value in fields.items():
        if field in ("id", "type"):
            continue
        kind = kinds.get(field)
        if kind is not None:
            _check_kind(kind, value, where, btype, field)

STATUSES = ("idle", "running", "done", "error")
ACCENTS = ("violet", "pink", "lime", "mint", "ok", "error", "warn", "muted")
INDICATORS = ("bar", "pct", "text", "none")

# Only `log` blocks accept `append`: the client renders `log` from `lines`, but
# renders `code` from its static `text`, so appending to a `code` block would
# silently no-op.
APPENDABLE = frozenset({"log"})

MAX_BLOCKS = 60

# The client renders only RENDER_LOG_LINES of a log block. The server stores at
# most MAX_LOG_LINES so a single board cannot bloat memory and every SSE
# broadcast with log history no viewer can ever see. MAX_LOG_LINES is the
# storage/acceptance cap; RENDER_LOG_LINES is the display window inside it, so
# the window is always full during rapid appends.
MAX_LOG_LINES = 400
RENDER_LOG_LINES = 80

MAX_TITLE = 200
MAX_SUBTITLE = 500

# The maximum length of a single stored string value. Every text a remote client
# can store — block text fields, list/table/kv elements, stat labels and
# pre-formatted values, log lines, tab label/indicator text — is capped here, so
# one huge field cannot be stored and re-shipped in every SSE frame (each frame
# is a full snapshot of all boards). A board is a dashboard, not a document
# store: 8 KiB is far more than any dashboard field needs. List/row/value
# *counts* have no explicit cap; they are bounded transitively by
# MAX_BODY_BYTES (app.py), since a board write is a single request.
MAX_TEXT_LEN = 8192

# The largest integer a JS number represents exactly (2**53). JSON integers are
# arbitrary precision, so without this bound `10**400` is accepted and then
# `float()`d by the client renderer, the summary and the config arithmetic —
# each of
# which raises OverflowError (a 500, not the named refusal the contract
# promises). Above this a value also silently changes on the way to the client,
# which the contract never does. Every numeric value the contract accepts is
# therefore required to be a finite double within this range.
MAX_SAFE_INT = 2 ** 53

# Board ids are path segments and map keys, and block ids are addressed on every
# update. Both are echoed in every snapshot, so cap their length tightly.
MAX_BOARD_ID_LEN = 64
MAX_BLOCK_ID_LEN = 64


class ContractError(ValueError):
    """A caller-facing refusal naming the offending field and the valid set."""


def validate_blocks(blocks) -> list:
    """Validate a block list; returns it (with over-long logs trimmed)."""
    if blocks is None:
        return []
    if not isinstance(blocks, list):
        raise ContractError(
            f"`blocks` must be a list of block objects, got {type(blocks).__name__}."
        )
    if len(blocks) > MAX_BLOCKS:
        raise ContractError(
            f"too many blocks ({len(blocks)}); the limit is {MAX_BLOCKS}. "
            "Split into more boards or trim."
        )
    seen: set[str] = set()
    out: list = []
    for i, blk in enumerate(blocks):
        if not isinstance(blk, dict):
            raise ContractError(
                f"block {i} must be an object, got {type(blk).__name__}."
            )
        t = blk.get("type")
        if not t:
            raise ContractError(
                f"block {i} has no `type`. Valid types: "
                f"{', '.join(sorted(BLOCK_TYPES))}."
            )
        if t not in BLOCK_TYPES:
            raise ContractError(
                f"block {i}: unknown type {t!r}. Valid types: "
                f"{', '.join(sorted(BLOCK_TYPES))}."
            )
        bid = blk.get("id")
        if bid is not None:
            # `update_block` addresses blocks by id, and the MCP declares `id` as
            # a string. A non-string id used to be str()'d for the duplicate
            # check but stored raw, so `update_block(block_id="123")` could never
            # address a block stored with `id: 123`.
            if not isinstance(bid, str):
                raise ContractError(
                    f"block {i}: `id` must be a string, got {type(bid).__name__}."
                )
            if len(bid) > MAX_BLOCK_ID_LEN:
                raise ContractError(
                    f"block {i}: `id` is too long ({len(bid)} chars); "
                    f"the limit is {MAX_BLOCK_ID_LEN}."
                )
            if bid in seen:
                raise ContractError(
                    f"block {i}: duplicate id {bid!r}. Block ids must be unique "
                    "(update_block addresses blocks by id)."
                )
            seen.add(bid)
        # The writable-field vocabulary is per type (BLOCK_FIELDS); a field the
        # client does not read must be refused here exactly as update_block
        # refuses an unknown `set` key, or an agent bypasses the stricter gate by
        # resending the whole board with the field attached.
        allowed = BLOCK_FIELDS.get(t, set()) | {"id", "type"}
        unknown = [k for k in blk if k not in allowed]
        if unknown:
            raise ContractError(
                f"block {i} ({t}): unknown field(s) {sorted(unknown)}. "
                f"Valid fields: {sorted(allowed)}."
            )
        if t == "log":
            lines = blk.get("lines") or []
            if not isinstance(lines, list):
                raise ContractError(
                    f"block {i} (log): `lines` must be a list of strings."
                )
            if len(lines) > MAX_LOG_LINES:
                blk = {**blk, "lines": lines[-MAX_LOG_LINES:]}
        # Value types, not just structure: a dict where the client's renderer
        # iterates (or a number where it does not) must be refused with the
        # field named.
        validate_block_values(blk, i)
        out.append(blk)
    return out


def validate_tab(tab):
    if tab is None:
        return None
    if not isinstance(tab, dict):
        raise ContractError("`tab` must be an object or null.")
    accent = tab.get("accent")
    if accent is not None and str(accent).lower() not in ACCENTS:
        raise ContractError(
            f"tab.accent {accent!r} is not recognised. Valid: {', '.join(ACCENTS)}."
        )
    label = tab.get("label")
    if label is not None:
        if not _is_scalar(label):
            raise ContractError("tab.label must be a string or a finite number.")
        _reject_long("`tab.label`", label)
    ind = tab.get("indicator")
    if ind is not None:
        if not isinstance(ind, dict):
            raise ContractError("tab.indicator must be an object.")
        kind = ind.get("kind")
        if kind is not None and str(kind).lower() not in INDICATORS:
            raise ContractError(
                f"tab.indicator.kind {kind!r} is not recognised. "
                f"Valid: {', '.join(INDICATORS)}."
            )
        k = str(kind).lower()
        if k in ("bar", "pct"):
            if ind.get("value") is None:
                raise ContractError(
                    f"tab.indicator.kind={kind!r} requires a numeric `value`"
                    + (" and `max`." if k == "bar" else ".")
                )
            if not _is_numeric(ind.get("value")):
                raise ContractError(
                    "tab.indicator.value must be a number, got "
                    f"{type(ind.get('value')).__name__}."
                )
            _reject_long("`tab.indicator.value`", ind.get("value"))
        if k == "bar" and "max" in ind:
            if not _is_numeric(ind.get("max")):
                raise ContractError("tab.indicator.max must be a number.")
            _reject_long("`tab.indicator.max`", ind.get("max"))
        if k == "text":
            text = ind.get("text")
            if not text:
                raise ContractError("tab.indicator.kind='text' requires a `text` string.")
            if not isinstance(text, str):
                # The truthiness check above passes any truthy value (a dict,
                # a list, a number) — but the tab is a LABEL the client renders
                # as text, so anything but a string is refused with the type
                # named, not silently accepted to render nonsense.
                raise ContractError(
                    "tab.indicator.text must be a string, got "
                    f"{type(text).__name__}.")
            _reject_long("`tab.indicator.text`", text)
    return tab


def validate_status(status, allow_none: bool = True):
    if status is None:
        if allow_none:
            return None
        raise ContractError(f"`status` must be one of {', '.join(STATUSES)}.")
    if str(status) not in STATUSES:
        raise ContractError(
            f"status {status!r} is not recognised. Valid: {', '.join(STATUSES)}."
        )
    return str(status)


def validate_ttl(ttl):
    if ttl is None:
        return None
    # A ttl is stored and echoed as an integer number of seconds. `int(ttl)`
    # used to truncate a fractional float (2.9 stored as 2) and coerce a bool
    # (int(True) is 1) — both reported ok:true for a DIFFERENT number than the
    # caller sent, the same silent coercion _config_int refuses for the config
    # keys. A ttl must either take the caller's value or be refused: an int,
    # an integer-valued float (2.0 == 2 exactly), or a numeric string that is
    # an integer ("300"); a bool, a fraction, or a non-integer string is
    # refused. (The MCP layer declares ttl as a pydantic int, so this only
    # ever tightens the HTTP surface it shares the module with.)
    if isinstance(ttl, bool):
        raise ContractError("`ttl` must be an integer number of seconds, got a boolean.")
    if isinstance(ttl, float) and not ttl.is_integer():
        raise ContractError(
            f"`ttl` must be an integer number of seconds, got {ttl!r}.")
    try:
        n = int(ttl)
    except (TypeError, ValueError):
        raise ContractError(
            f"`ttl` must be an integer number of seconds, got {ttl!r}."
        )
    if n <= 0:
        raise ContractError("`ttl` must be a positive number of seconds.")
    if n > MAX_SAFE_INT:
        # A ttl is used in float arithmetic by the summary (`ttl - age`); an int
        # above 2**53 is not exactly a double, and far above it `float()` raises.
        raise ContractError(
            f"`ttl` is too large; the limit is {MAX_SAFE_INT} seconds."
        )
    return n


def validate_board_id(bid):
    """A board id must already be a canonical slug (no silent transformation).

    `_slug` used to rewrite "My Board" -> "my-board", so two distinct client
    ids could land on one board and one agent could overwrite another's. We
    refuse anything that is not already canonical instead.
    """
    if not isinstance(bid, str):
        raise ContractError(
            "invalid board id: use lowercase letters, digits, '-' or '_' "
            f"(max {MAX_BOARD_ID_LEN} chars, e.g. 'my-board')."
        )
    if len(bid) > MAX_BOARD_ID_LEN:
        raise ContractError(
            f"invalid board id {bid[:MAX_BOARD_ID_LEN]!r}…: too long "
            f"({len(bid)} chars); the limit is {MAX_BOARD_ID_LEN}."
        )
    if not _BOARD_ID_RE.fullmatch(bid):
        raise ContractError(
            f"invalid board id {bid!r}: use lowercase letters, digits, '-' or '_' "
            "(e.g. 'my-board')."
        )
    return bid


def validate_text(value, field: str, max_len: int, allow_none: bool = True):
    if value is None:
        if allow_none:
            return None
        raise ContractError(f"`{field}` must be a string, got NoneType.")
    if not isinstance(value, str):
        raise ContractError(
            f"`{field}` must be a string, got {type(value).__name__}."
        )
    if len(value) > max_len:
        raise ContractError(
            f"`{field}` is too long ({len(value)} chars); the limit is {max_len}."
        )
    return value
