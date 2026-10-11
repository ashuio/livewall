"""Cross-surface vocabulary parity — derived from source, not hard-coded.

The block vocabulary (17 types, their fields, their limits, their formatters)
is encoded in four places that share no generated source:

  1. backend/contract.py  — the authority (BLOCK_FIELDS / _FIELD_KINDS / limits)
  2. mcp/server.py        — a hand-written per-type description (describe_blocks)
  3. web/src/livewall/components.tsx — the client dispatcher renderBlock
  4. cli/livewall.py      — the CLI renderer's per-type branches

This test DERIVES each surface's vocabulary from its source and fails on any
disagreement, so a vocabulary drift — a type or field added to the contract but
not to a consumer — cannot recur silently.
It hard-codes no vocabulary of its own: every expected value is read out of
contract.py, and every actual value is parsed out of the other three sources.

Run: python3 backend/tests/vocab_parity_test.py  (no backend needed — static).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "backend"))
import contract  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(f"{name}: {detail}" if detail else name)
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"ok    {name}")


def read(rel: str) -> str:
    return (REPO / rel).read_text()


CONTRACT_SRC = read("backend/contract.py")
MCP_SRC = read("mcp/server.py")
TSX_SRC = read("web/src/livewall/components.tsx")
CLI_SRC = read("cli/livewall.py")
DOCS_SRC = read("docs/blocks.md")

TYPES = set(contract.BLOCK_FIELDS)

# ---------------------------------------------------------------- mcp/server.py
# The type set must be DERIVED from the contract (an assignment from
# contract.BLOCK_TYPES), not re-listed; the descriptions are hand-written and
# are checked token-by-token against the contract's fields.

mcp_tree = ast.parse(MCP_SRC)


def top_level_module_dict(tree: ast.Module, name: str) -> dict[str, str]:
    """The module-level `name: dict[str, str] = {...}` assignment, or {}."""
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = node.value
            if isinstance(value, ast.Dict) and all(
                isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                for k, v in zip(value.keys, value.values)
            ):
                return {k.value: v.value for k, v in zip(value.keys, value.values)}
    return {}


def derived_from_contract(tree: ast.Module, name: str) -> bool:
    """True iff module-level `name = contract.<name>` (derived, not re-listed)."""
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            v = node.value
            return isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) \
                and v.value.id == "contract" and v.attr == name
    return False


MCP_FIELDS = top_level_module_dict(mcp_tree, "BLOCK_FIELDS")


check("mcp: BLOCK_FIELDS describes exactly the contract's types",
      set(MCP_FIELDS) == TYPES,
      f"mcp-only={sorted(set(MCP_FIELDS) - TYPES)} contract-only={sorted(TYPES - set(MCP_FIELDS))}")

for name in ("BLOCK_TYPES", "STATUSES", "ACCENTS", "INDICATORS",
             "MAX_BLOCKS", "MAX_LOG_LINES"):
    check(f"mcp: {name} is derived from contract (not re-listed)",
          derived_from_contract(mcp_tree, name))

# Split a description on top-level commas (brackets/braces/quotes protect inner
# commas) and take each field's leading identifier: "label?, color?, ..." or
# "items:[{label,value,unit?}]".
_PROTECT = {"(": ")", "[": "]", "{": "}"}


def described_fields(desc: str) -> set[str]:
    out: list[str] = []
    depth = 0
    buf = ""
    for ch in desc:
        if ch in _PROTECT:
            depth += 1
        elif ch in _PROTECT.values():
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    fields = set()
    for tok in out:
        m = re.match(r"\s*\(no fields\)\s*$", tok)
        if m:
            continue
        m = re.match(r"\s*([a-zA-Z_]\w*)", tok)
        if m:
            fields.add(m.group(1))
    return fields


for t, desc in sorted(MCP_FIELDS.items()):
    if t not in TYPES:
        continue
    if "alias of" in desc:
        alias_target = re.search(r"alias of (\w+)", desc).group(1)
        check(f"mcp: {t} alias target exists and has identical fields",
              alias_target in TYPES and
              contract.BLOCK_FIELDS[t] == contract.BLOCK_FIELDS.get(alias_target),
              f"alias of {alias_target!r}")
        continue
    want = contract.BLOCK_FIELDS[t]
    got = described_fields(desc)
    check(f"mcp: {t} describes exactly the contract's fields",
          got == want,
          f"advertised-but-rejected={sorted(got - want)} accepted-but-undocumented={sorted(want - got)}")

# Structured item kinds: contract.ITEM_KIND_KEYS names the keys each kind
# checks; every key must be advertised by the owning type's description (an
# object form the contract accepts but agents are never told about is drift).
item_owners: dict[str, tuple[str, str]] = {}  # kind -> (type, field)
for t, kinds in contract._FIELD_KINDS.items():
    for f, kind in kinds.items():
        if kind in contract.ITEM_KIND_KEYS:
            item_owners[kind] = (t, f)

for kind, (t, f) in sorted(item_owners.items()):
    desc = MCP_FIELDS.get(t, "")
    missing = [k for k in contract.ITEM_KIND_KEYS[kind]
               if not re.search(rf"\b{re.escape(k)}\b", desc)]
    check(f"mcp: {t}.{f} ({kind}) advertises every contract item key",
          not missing,
          f"keys {missing} of {contract.ITEM_KIND_KEYS[kind]} absent from {desc!r}")

# ------------------------------------------------------------------- client tsx
# renderBlock's dispatcher is the client's vocabulary encoding (types.ts's
# Block is an open record). Parse the switch cases and each case's field reads.

sw = TSX_SRC.index("switch (t) {")
sw_end = TSX_SRC.index("default:", sw)
switch_src = TSX_SRC[sw:sw_end]

cases: list[tuple[str, str]] = []
matches = list(re.finditer(r"case '(\w+)':", switch_src))
for i, m in enumerate(matches):
    body = switch_src[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(switch_src)]
    cases.append((m.group(1), body))
# A fallthrough case (`case 'a': case 'b':`) has an empty body; it shares the
# next labelled body. Resolve before checking.
resolved: dict[str, str] = {}
for i, (name, body) in enumerate(cases):
    if not body.strip():
        nxt = next((b for n, b in cases[i:] if b.strip()), "")
        resolved[name] = nxt
    else:
        resolved[name] = body

check("client: renderBlock dispatches exactly the contract's types",
      set(resolved) == TYPES,
      f"client-only={sorted(set(resolved) - TYPES)} contract-only={sorted(TYPES - set(resolved))}")

for t in sorted(TYPES):
    body = resolved.get(t, "")
    got = set(re.findall(r"\bb\.(\w+)", body))
    want = contract.BLOCK_FIELDS[t]
    check(f"client: {t} reads exactly the contract's fields",
          got == want,
          f"rendered-but-not-validated={sorted(got - want)} validated-but-not-rendered={sorted(want - got)}")

# Inner item reads: the reader variable per type whose field holds structured
# items, compared against the contract's keys for that kind. `.length` is the
# Array.isArray pair check (structural, not a key read) and is excluded.
INNER_READER = {"stats": r"\bit\.(\w+)", "bar": r"\bd\.(\w+)",
                "line": r"\bs_\.(\w+)", "kv": r"\bpair\.(\w+)"}
for t, rx in INNER_READER.items():
    kind = next((k for f, k in contract._FIELD_KINDS.get(t, {}).items()
                 if k in contract.ITEM_KIND_KEYS), None)
    if kind is None:
        continue
    got = {name for name in re.findall(rx, resolved.get(t, "")) if name != "length"}
    want = set(contract.ITEM_KIND_KEYS[kind])
    check(f"client: {t} item objects read exactly {kind}'s keys",
          got == want, f"reads={sorted(got)} contract={sorted(want)}")

# boolish fields: the contract accepts booleans AND strings, so the client must
# coerce through flag() (which honours "false") — not Boolean(), which turns
# the legal string "false" into true.
boolish_fields = [(t, f) for t, kinds in contract._FIELD_KINDS.items()
                  for f, k in kinds.items() if k == "boolish"]
for t, f in boolish_fields:
    body = resolved.get(t, "")
    uses_flag = re.search(rf"\w*\(\s*b\.{f}\s*\)", body)
    check(f"client: {t}.{f} (boolish) coerces through flag(), not Boolean()",
          uses_flag is not None and uses_flag.group(0).startswith("flag("),
          f"coercion {uses_flag.group(0) if uses_flag else '(absent)'} for {t}.{f}")

# ------------------------------------------------------------------- cli
# render_board's branches are the CLI's vocabulary encoding.

start = CLI_SRC.index('t = b.get("type")')
end = CLI_SRC.index("[unknown block", start)
cli_src = CLI_SRC[start:end]

headers = list(re.finditer(r"(?:el)?if t (?:== \"(\w+)\"|in \(([^)]*)\)):", cli_src))
cli_branches: dict[str, str] = {}
branch_names: dict[str, list[str]] = {}
for i, m in enumerate(headers):
    body = cli_src[m.end(): headers[i + 1].start() if i + 1 < len(headers) else len(cli_src)]
    names = [m.group(1)] if m.group(1) else re.findall(r'"(\w+)"', m.group(2) or "")
    for n_ in names:
        cli_branches[n_] = body
        branch_names[body] = branch_names.get(body, []) + [n_]

check("cli: render_board renders exactly the contract's types",
      set(cli_branches) == TYPES,
      f"cli-only={sorted(set(cli_branches) - TYPES)} contract-only={sorted(TYPES - set(cli_branches))}")

for t in sorted(TYPES):
    body = cli_branches.get(t, "")
    got = set(re.findall(r"\bb\.get\(['\"](\w+)['\"]", body))
    # A branch shared by sibling types may read the sibling's fields (log/code
    # share one branch; contract guarantees the other field is absent), so the
    # assertion is subset against the branch's whole type union.
    allowed = set().union(*(contract.BLOCK_FIELDS[n_] for n_ in branch_names[body])) \
        | {"type", "id"}
    extra = got - allowed
    check(f"cli: {t} reads only contract fields (of its branch's types)",
          not extra, f"reads fields no contract type declares: {sorted(extra)}")

# Inner reads per structured kind.
for t, rx, kind in (("stats", r"\bit\.get\(['\"](\w+)['\"]", "stats_items"),
                    ("line", r"\bs\.get\(['\"](\w+)['\"]", "line_series")):
    got = set(re.findall(rx, cli_branches.get(t, "")))
    want = set(contract.ITEM_KIND_KEYS[kind])
    check(f"cli: {t} inner reads are a subset of {kind}'s keys",
          got <= want, f"reads={sorted(got)} contract={sorted(want)}")
for t, kind in (("bar", "bar_data"), ("kv", "kv_items")):
    body = cli_branches.get(t, "")
    pairs = set(re.findall(r"_pair\(\w+, ['\"](\w+)['\"], ['\"](\w+)['\"]", body))
    got = {k for p in pairs for k in p}
    want = set(contract.ITEM_KIND_KEYS[kind])
    check(f"cli: {t} pair keys are a subset of {kind}'s keys",
          got <= want, f"reads={sorted(got)} contract={sorted(want)}")

# ---------------------------------------------------------------- limits/sets
fmt_ts = read("web/src/livewall/format.ts")
m = re.search(r"RENDER_LOG_LINES\s*=\s*(\d+)", fmt_ts)
check("client: RENDER_LOG_LINES equals contract.RENDER_LOG_LINES",
      bool(m) and int(m.group(1)) == contract.RENDER_LOG_LINES,
      f"format.ts={m.group(1) if m else '?'} contract={contract.RENDER_LOG_LINES}")

css = read("web/src/livewall/components.css")
css_accents = set(re.findall(r"--accent-([a-z]+):", css))
check("client css: --accent-* tokens equal contract.ACCENTS",
      css_accents == set(contract.ACCENTS),
      f"css-only={sorted(css_accents - set(contract.ACCENTS))} "
      f"contract-only={sorted(set(contract.ACCENTS) - css_accents)}")

m = re.search(r"var\(--accent-\$\{accentName\([^)]*\)\s*\|\|\s*'(\w+)'\}\)", TSX_SRC)
check("client: colour fallback token is a contract ACCENT",
      bool(m) and m.group(1) in contract.ACCENTS, f"fallback={m.group(1) if m else '?'}")

# SECURITY: a `color` is interpolated into a CSS `var(--accent-<name>)` token.
# An unwhitelisted string there is an injection sink (`mint) url(//evil` makes
# the declaration `background: var(--accent-mint) url(//evil)` — valid CSS —
# and every viewer's browser fetches the attacker URL). Every interpolation in
# the client must therefore go through `accentName`, which passes only the
# contract's accent vocabulary, and the whitelist itself must equal
# contract.ACCENTS.
bypasses = re.findall(r"var\(--accent-\$\{(?!accentName\()", TSX_SRC)
check("client: no --accent- interpolation bypasses the accentName whitelist",
      not bypasses,
      f"{len(bypasses)} unguarded interpolation(s) in components.tsx")

accent_ts = read("web/src/livewall/accent.ts")
ts_accents = set(re.findall(r"^\s*'([a-z]+)',\s*$", accent_ts, re.M))
check("client: accent.ts whitelist equals contract.ACCENTS",
      ts_accents == set(contract.ACCENTS),
      f"ts-only={sorted(ts_accents - set(contract.ACCENTS))} "
      f"contract-only={sorted(set(contract.ACCENTS) - ts_accents)}")

m = re.search(r"choices=\[([^\]]*)\]", CLI_SRC)
cli_statuses = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()
check("cli: --status choices equal contract.STATUSES",
      cli_statuses == set(contract.STATUSES),
      f"cli={sorted(cli_statuses)} contract={sorted(contract.STATUSES)}")

types_ts = read("web/src/livewall/types.ts")
m = re.search(r"status: ((?:'\w+' \| )*'\w+')", types_ts)
ts_statuses = set(re.findall(r"'(\w+)'", m.group(1))) if m else set()
check("client: BoardSummary status union equals contract.STATUSES",
      ts_statuses == set(contract.STATUSES),
      f"ts={sorted(ts_statuses)} contract={sorted(contract.STATUSES)}")

m = re.search(r"kind\?: ((?:'\w+' \| )*'\w+')", types_ts)
ts_kinds = set(re.findall(r"'(\w+)'", m.group(1))) if m else set()
check("client: TabIndicator kind union equals contract.INDICATORS",
      ts_kinds == set(contract.INDICATORS),
      f"ts={sorted(ts_kinds)} contract={sorted(contract.INDICATORS)}")

# Defaults: a missing ratio denominator is 100 on every surface that assumes one.
m = re.search(r"const positive = \(v: unknown, def = (\d+)\)", TSX_SRC)
client_def = int(m.group(1)) if m else None
m = re.search(r'b\.get\("max", (\d+)\)', CLI_SRC)
cli_def = int(m.group(1)) if m else None
check("defaults: client positive() default equals cli max default (100)",
      client_def == cli_def == 100, f"client={client_def} cli={cli_def}")

# The backend's summary derives board progress from the ratio blocks; the type
# set it reads must equal the types the contract kinds define as ratio blocks
# (value: numeric + max: positive) — a fifth encoding, kept derived.
ratio_types = {t for t, kinds in contract._FIELD_KINDS.items()
               if kinds.get("value") == "numeric" and kinds.get("max") == "positive"}
app_src = read("backend/app.py")
found: set[str] = set()
for grp in re.findall(r'\.get\("type"\) in \(([^)]*)\)', app_src):
    found |= set(re.findall(r'"(\w+)"', grp))
check("backend: summary progress reads exactly the contract's ratio types",
      found == ratio_types,
      f"app.py={sorted(found)} contract-derived={sorted(ratio_types)}")

# ------------------------------------------------------------------- docs
# docs/blocks.md is a fifth copy of the vocabulary. Every long inline-backtick
# phrase (>= 4 words — a quoted sentence, e.g. a refusal message) must exist in
# one of the code surfaces (quotes normalised, since docs quote JSON-style and
# code backticks field names). Fenced code blocks are excluded: examples are
# prose, not vocabulary. So are fragments that START with '{': those are the
# docs' own shape notation for a field kind (e.g. "{name?, values: number list,
# color?}"), a definition, not a quote of code.
#
# Refusal messages are reassembled from contract.py's AST (the constant parts
# of every ContractError raise, concatenated) rather than matched against raw
# source: an f-string split across lines would otherwise never match, no
# matter how the docs phrase it.
def _joined_constant(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_joined_constant(v) for v in node.values
                       if isinstance(v, (ast.Constant, ast.JoinedStr)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined_constant(node.left) + _joined_constant(node.right)
    return ""


contract_msgs = [
    _joined_constant(arg)
    for node in ast.walk(ast.parse(CONTRACT_SRC))
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    for arg in node.exc.args
]
contract_msgs = [m for m in contract_msgs if m]

docs_inline = re.sub(r"```.*?```", "", DOCS_SRC, flags=re.S)
sources = [CONTRACT_SRC, MCP_SRC, TSX_SRC, CLI_SRC]
norm = lambda s_: re.sub(r"\s+", " ", s_.replace('"', "").replace("'", "").replace("`", ""))
sources_n = [norm(s_) for s_ in sources]
msgs_n = [norm(m) for m in contract_msgs]
bad_quotes = []
for frag in re.findall(r"`([^`\n]+)`", docs_inline):
    if frag.lstrip().startswith("{"):
        continue
    if len(frag.split()) < 4:
        continue
    if not (any(norm(frag) in s_ for s_ in sources_n)
            or any(norm(frag) in m_ for m_ in msgs_n)):
        bad_quotes.append(frag)
check("docs: every quoted >=4-word phrase exists in a code surface",
      not bad_quotes, f"quoted but found nowhere: {bad_quotes}")

# Every type the docs document must be a contract type (and vice versa).
docs_types = {a for pair in re.findall(r"^### `(\w+)`(?: / `(\w+)`)?", docs_inline, re.M)
              for a in pair if a}
check("docs: documents exactly the contract's types", docs_types == TYPES,
      f"docs-only={sorted(docs_types - TYPES)} contract-only={sorted(TYPES - docs_types)}")

# The contract's ITEM_KIND_KEYS must be load-bearing: the per-kind key tuples
# may not also exist as literals inside _check_kind (a second copy).
check_kind_src = CONTRACT_SRC[CONTRACT_SRC.index("def _check_kind"):
                              CONTRACT_SRC.index("def validate_block_values")]
for kind, keys in contract.ITEM_KIND_KEYS.items():
    lit = "(" + ", ".join(f'"{k}"' for k in keys) + ")"
    check(f"contract: {kind} keys come from ITEM_KIND_KEYS (no literal copy)",
          lit not in check_kind_src, f"literal {lit} still hard-coded in _check_kind")
for stale in ('get("k", "")', 'get("v", "")'):
    check(f"contract: {stale!r} not hard-coded in _check_kind",
          stale not in check_kind_src)

print()
if FAILURES:
    print(f"VOCAB PARITY: FAIL ({len(FAILURES)} of {CHECKS} checks)")
    for f_ in FAILURES:
        print(f"  - {f_}")
    sys.exit(1)
print(f"VOCAB PARITY: PASS ({CHECKS} checks)")