"""Verify the React Livewall client renders the real board.

Because React renders to the DOM (unlike a canvas renderer), this can ASSERT on
text rather than eyeball a screenshot. Both are done: assertions are the gate,
the screenshot is the visual confirmation.

The harness is SELF-SEEDING: it POSTs a fixture board covering every custom
component at the start and DELETEs it at the end, so it passes on a clean
checkout with only the backend running (it used to assert on a board nothing in
the repo created).

Run against a served client (dev server, or the backend serving web/dist):

    LIVEWALL_URL=http://127.0.0.1:8470 python scripts/verify_render.py

Env:
    LIVEWALL_URL      base URL of a running Livewall (default is a dead port,
                      http://127.0.0.1:1, so a forgotten value fails loudly)
    LIVEWALL_CHROME   path to a chrome binary; omit to use Playwright's bundled one
    LIVEWALL_SHOTS    directory for screenshots (default ./shots)

Requires Playwright: pip install playwright && playwright install chromium
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# Deliberately DEAD default: a bare run must not seed/delete boards on a real
# backend (the product port 8470). Point LIVEWALL_URL at a running Livewall.
BASE = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:1").rstrip("/")
URL = BASE + "/"
CHROME = os.environ.get("LIVEWALL_CHROME") or None
OUT = Path(os.environ.get("LIVEWALL_SHOTS", "shots"))
OUT.mkdir(parents=True, exist_ok=True)

try:
    urllib.request.urlopen(BASE + "/health", timeout=4)
except Exception as e:
    print(f"no Livewall backend at {BASE} (/health: {e}).")
    print("  set LIVEWALL_URL to a running backend, e.g.")
    print(f"  LIVEWALL_URL=http://127.0.0.1:8470 python {sys.argv[0]}")
    sys.exit(2)

FIXTURE_ID = "render-check"

# One board exercising every custom component, with the exact numbers the
# assertions below look for. The negative check (`6.8K`, `5.4K`) only bites
# because 6836 / 5371 are present and must render UNcompacted.
FIXTURE = {
    "title": "Render check",
    "subtitle": "verify_render fixture",
    "status": "running",
    "ttl": 600,
    "blocks": [
        {"type": "heading", "text": "Rollout", "eyebrow": "deploy"},
        {"type": "callout", "text": "All green", "tone": "ok"},
        {"type": "progress", "label": "Rows copied", "value": 6836, "max": 6950, "unit": "rows"},
        {"type": "progress", "label": "Fraction", "value": 12.25, "max": 50},
        {"type": "gauge", "label": "Nodes updated", "value": 11, "max": 64},
        # Every ratio block (progress/gauge/bar) must format through
        # the one shared lwFormat/lwNum path. A fractional value in each, plus a
        # fmt:compact ratio, proves it at the DOM: exact where exact is due,
        # compact only when asked, and the ratio still computed either way.
        {"type": "gauge", "label": "Fractional gauge", "value": 7.5, "max": 10},
        {"type": "progress", "label": "Compact ratio", "value": 2500000, "max": 5000000, "fmt": "compact"},
        {"type": "stats", "items": [
            {"label": "Memories", "value": 5371},
            {"label": "Tokens", "value": "1.5M"},
            {"label": "Documents", "value": 1537899},
        ]},
        {"type": "stat", "label": "Cores", "value": 42, "unit": "cpu"},
        {"type": "bar", "label": "New memories (7d)", "data": [["Mon", 2333]]},
        {"type": "bar", "label": "Fractional bar", "data": [["x", 9.25]]},
        {"type": "sparkline", "label": "Observations per day", "values": [1, 3, 2, 5, 4, 6]},
        # Coverage: text, code and chart render. `code`
        # renders through LwLog and `chart` through LwSparkline, so their
        # CONTENT is asserted (a class count cannot tell them from a log or a
        # sparkline). The second `code` block proves hostile strings survive
        # React's text rendering byte-for-byte.
        {"type": "text", "text": "Plain text block body"},
        {"type": "code", "title": "build.sh", "text": "npm run build\nnpm test"},
        {"type": "code", "title": "hostile strings",
         "text": 'quote " and backslash \\ here\n</script><b>not html</b>'},
        {"type": "chart", "label": "Chart alias", "values": [2, 4, 3, 6, 5, 7]},
        # Multi-series line: y ticks on the 0..100 grid, both legend names and
        # both x-axis end labels asserted separately (a class-count cannot fake
        # a polyline or a legend item).
        {"type": "line", "label": "Tokens over time", "color": "violet", "id": "tokline", "series": [
            {"name": "p50", "values": [2, 14, 40, 77, 8], "color": "mint"},
            {"name": "p95", "values": [10, 30, 50, 95, 22]}],
         "xLabels": ["00:00", "06:00", "12:00", "18:00", "24:00"],
         "yMin": 0, "yMax": 100, "legend": True},
        # Sugar form: one unnamed series from `values` (auto y range).
        {"type": "line", "label": "Sugared line", "values": [3, 6, 4, 9, 7]},
        # Degradation: fewer than 2 points is accepted by the contract and must
        # render the visible (no data) state.
        {"type": "line", "label": "Line no data", "values": [7]},
        {"type": "table", "columns": ["Node", "unit", "Status"],
         "rows": [["alpha", "unit", "done"]], "tagColumn": True},
        # The contract accepts `tagColumn` as boolish — the STRING "false" is
        # legal and must coerce to FALSE (no pills), exactly like `legend`.
        {"type": "table", "columns": ["Task", "State"],
         "rows": [["sync", "pending"]], "tagColumn": "false"},
        {"type": "kv", "items": [["Source", "12 MB/s"]]},
        {"type": "list", "label": "Checks", "items": ["schema validated"]},
        {"type": "log", "lines": ["batch_retain completed"]},
        # Cap: a log renders only the newest RENDER_LOG_LINES (80) of its lines.
        # `cap line 021` is the first of the last 80; `cap line 020` must be gone.
        {"type": "log", "title": "capped log",
         "lines": [f"cap line {i:03d}" for i in range(1, 101)]},
        {"type": "callout", "text": "backup-heavy failed", "tone": "error"},
        {"type": "divider"},
        # Degenerate-but-legal numbers: a negative ratio clamps to 0%, a huge
        # value renders exact (never compacted, never Infinity).
        {"type": "progress", "label": "Negative progress", "value": -5, "max": 10},
        {"type": "progress", "label": "Huge progress",
         "value": 1000000000000000, "max": 2000000000000000},
        # A contract-legal HUGE point count. The contract caps no list's length
        # (only the 1 MiB request body), so a ~150k-point values list is
        # accepted and streamed. The renderer must not throw: `Math.min(...vals)`
        # with a spread that large raises RangeError in V8 and — there being no
        # error boundary — blanked the ENTIRE app, not just this block.
        {"type": "sparkline", "label": "Huge sparkline", "values": [1] * 150000},
        # A lone `yMin` above every point is contract-legal (the yMin/yMax pair
        # is order-checked only when BOTH are present). It used to resolve
        # hi < lo and mirror the chart (value 3 on top, 50 at the bottom).
        {"type": "line", "label": "Y floor", "values": [1, 2, 3], "yMin": 50},
    ],
}

# A board with no title, no subtitle and no blocks: the client's empty state.
EMPTY_ID = "render-empty"
EMPTY_FIXTURE = {"title": "", "subtitle": "", "blocks": []}

FLAGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]

# Text that must appear if the custom components actually rendered.
EXPECT = [
    "Rollout",              # LwHeading
    "All green",            # LwCallout (ok)
    "6836",                 # LwProgress value — EXACT, not 6.8K
    "6950",                 # LwProgress max
    "12.25",                # LwProgress fractional value — EXACT, not 12.3
    "Nodes updated",        # LwGauge label
    "11",                   # gauge value
    "Fractional gauge",     # LwGauge label (fractional)
    "7.5",                  # gauge value — exact, not rounded
    "75%",                  # gauge ratio
    "Fractional bar",       # LwBars label (fractional)
    "9.25",                 # bar value — exact, not 9.3
    "Compact ratio",        # LwProgress label (explicit compaction)
    "2.5M",                 # compact label from lwFormat(fmt)
    "5.0M",                 # compact max from lwFormat(fmt)
    "50%",                  # ratio still computed from the real numbers
    "Memories",             # LwStats chip
    "5371",                 # stat exact
    "1.5M",                 # stat pre-formatted string, verbatim
    "1537899",              # stat exact (7 digits, uncompacted)
    "Cores",                # single LwStats chip from a `stat` block
    "42",                   # stat value exact
    "cpu",                  # stat unit
    "New memories (7d)",    # LwBars label
    "2333",                 # bar value exact
    "Observations per day", # LwSparkline label
    "Plain text block body",  # text block (no custom component)
    "build.sh",             # code block title (renders via LwCode)
    "npm run build",        # code block first line
    "npm test",             # code block second line
    "Copy",                 # the copy control a `code` block carries
    "Chart alias",          # chart block label (renders via LwSparkline)
    "Tokens over time",     # LwLine label
    "p50",                  # LwLine first legend/series name
    "p95",                  # LwLine second legend/series name
    "00:00",                # LwLine first x-axis label
    "12:00",                # LwLine middle x-axis label
    "24:00",                # LwLine last x-axis label
    "Sugared line",         # LwLine values-sugar label
    "Line no data",         # LwLine label (degenerate)
    "(no data)",            # LwLine total degradation for a 1-point series
    'quote " and backslash \\ here',   # hostile string, verbatim
    "</script><b>not html</b>",        # hostile string, not parsed as HTML
    "unit",                 # LwTable cell
    "done",                 # LwTable status pill
    "pending",              # LwTable cell in the tagColumn:"false" table (no pill)
    "Source",               # LwKv key
    "12 MB/s",              # LwKv value
    "Checks",               # LwList label
    "schema validated",     # LwList item
    "batch_retain completed",  # LwLog line
    "cap line 021",         # capped log: first of the newest 80 rendered
    "cap line 100",         # capped log: newest line
    "backup-heavy failed",  # second callout (error tone)
    "Negative progress",    # degenerate-but-legal negative ratio
    "-5",                   # negative value renders verbatim, clamped to 0%
    "Huge progress",        # degenerate-but-legal huge ratio
    "1000000000000000",     # huge value exact — String(), never compacted
    "Huge sparkline",       # a contract-legal 150k-point values list
    "Y floor",              # lone yMin ABOVE the data (contract-legal)
]

# One class per custom component the fixture renders (all 15, including the
# textless divider), plus the copy control a `code` block adds. A component
# that fails to render disappears from the DOM.
REQUIRED_COMPONENTS = [
    "lw-heading", "lw-callout", "lw-pct", "lw-gauge", "lw-chips",
    "lw-bar-row", "lw-spark", "lw-tr", "lw-kv-row", "lw-list-row", "lw-log",
    "lw-divider", "lw-line", "lw-line-tick", "lw-line-legend", "lw-copy",
]


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
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


def wait_for_text(pg, needles, timeout=15.0):
    """Poll until every needle is in the body, or timeout. Returns body text."""
    end = time.time() + timeout
    body = ""
    while time.time() < end:
        body = pg.inner_text("body")
        low = body.lower()
        if all(n.lower() in low for n in needles):
            return body
        pg.wait_for_timeout(400)
    return body


errors: list[str] = []
missing: list[str] = []
bad: list[str] = []
missing_components: list[str] = []
cap_bad: list[str] = []
yaxis_bad: list[str] = []
pill_bad: list[str] = []
interact_bad: list[str] = []
seeded = False
empty_seeded = False
empty_ok = False

try:
    st, body = api("POST", f"/api/boards/{FIXTURE_ID}", FIXTURE)
    if st not in (200, 201):
        print(f"FIXTURE SEED FAILED: HTTP {st} {body}")
        sys.exit(1)
    seeded = True

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": True, "args": FLAGS}
        if CHROME:
            launch_kwargs["executable_path"] = CHROME
        br = p.chromium.launch(**launch_kwargs)
        ctx = br.new_context(viewport={"width": 430, "height": 1000})
        ctx.add_init_script("try{localStorage.setItem('daylight-theme','light')}catch(e){}")
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on(
            "response",
            lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None,
        )

        pg.goto(URL, wait_until="domcontentloaded")
        body = wait_for_text(pg, EXPECT)

        lower = body.lower()
        print(f"[1] body text length: {len(body)} chars")

        # Chip labels are uppercased in markup, so compare case-insensitively.
        missing = [t for t in EXPECT if t.lower() not in lower]
        if missing:
            print(f"[2] MISSING TEXT ({len(missing)}):")
            for m in missing:
                print(f"      - {m!r}")
        else:
            print(f"[2] all {len(EXPECT)} expected strings present")

        # Prove exactness explicitly: compacted forms must NOT appear.
        bad = [t for t in ("6.8K", "5.4K", "1.5K") if t in lower]
        print(f"[3] compacted forms present (want none): {bad or 'none'}")

        # Log cap: the oldest stored lines must be absent (only the newest 80).
        cap_bad = [t for t in ("cap line 020",) if t in lower]
        print(f"[3b] capped-log oldest lines present (want none): {cap_bad or 'none'}")

        # Count rendered custom components by class. Every custom component the
        # fixture exercises must be present, including the textless divider.
        counts = pg.evaluate("""() => {
          const out = {};
          for (const el of document.querySelectorAll('[class*="lw-"]')) {
            for (const c of el.classList) {
              if (c.startsWith('lw-')) out[c] = (out[c] || 0) + 1;
            }
          }
          return out;
        }""")
        missing_components = [c for c in REQUIRED_COMPONENTS if not counts.get(c)]
        print(f"[4] component classes: {dict(sorted(counts.items()))}")
        if missing_components:
            print(f"    MISSING COMPONENTS: {missing_components}")

        # [4b] The "Y floor" line block carries a lone `yMin` ABOVE its data
        # (contract-legal: the yMin/yMax order rule fires only when both are
        # present). The resolved y range must not invert: read the y-axis tick
        # labels bottom-to-top (descending text `y` attribute) and require the
        # values to ascend. The inverted chart renders 50 at the bottom and 3
        # at the top — silently mirrored geometry.
        yaxis_ok = pg.evaluate("""() => {
          const svgs = [...document.querySelectorAll('svg.lw-line')];
          const last = svgs[svgs.length - 1];
          if (!last) return null;
          const ticks = [...last.querySelectorAll('text.lw-line-tick')]
            .filter(t => parseFloat(t.getAttribute('x')) < 44)
            .map(t => ({y: parseFloat(t.getAttribute('y')), v: parseFloat(t.textContent)}))
            .sort((a, b) => b.y - a.y)
            .map(t => t.v);
          return ticks.length === 5 && ticks.every((v, i) => i === 0 || v > ticks[i-1]);
        }""")
        print(f"[4b] 'Y floor' y ticks ascend bottom-to-top: {yaxis_ok}")
        if yaxis_ok is not True:
            yaxis_bad.append("'Y floor' chart y axis is inverted (or ticks missing)")

        # [4c] tagColumn boolish coercion. The table with tagColumn:True must
        # pill its last column ("done"), and the table with the contract-legal
        # STRING "false" must NOT pill its last column ("pending") — a
        # Boolean() coercion renders "false" as true and pills it.
        pill_texts = pg.evaluate(
            "() => [...document.querySelectorAll('.lw-pill')].map(e => e.textContent)")
        if "done" not in pill_texts:
            pill_bad.append("tagColumn:True table did not render a status pill")
        if "pending" in pill_texts:
            pill_bad.append(
                'tagColumn:"false" table rendered a status pill '
                '(the string "false" was coerced truthy)')
        print(f"[4c] tagColumn pills: {pill_bad or 'ok'}")

        pg.screenshot(path=f"{OUT}/react_light.png", full_page=True)

        # Dark mode
        ctx2 = br.new_context(viewport={"width": 430, "height": 1000})
        ctx2.add_init_script("try{localStorage.setItem('daylight-theme','dark')}catch(e){}")
        pg2 = ctx2.new_page()
        pg2.goto(URL, wait_until="domcontentloaded")
        wait_for_text(pg2, EXPECT)
        bg = pg2.evaluate("getComputedStyle(document.body).backgroundColor")
        print(f"[5] dark mode body bg: {bg}")
        pg2.screenshot(path=f"{OUT}/react_dark.png", full_page=True)

        # Empty state: a board with no title, subtitle or blocks.
        st_e, _ = api("POST", f"/api/boards/{EMPTY_ID}", EMPTY_FIXTURE)
        if st_e in (200, 201):
            empty_seeded = True
            pg3 = ctx.new_page()
            pg3.goto(f"{URL}?board={EMPTY_ID}", wait_until="domcontentloaded")
            etxt = wait_for_text(pg3, ["waiting for the agent"], timeout=10.0)
            empty_ok = "waiting for the agent to program the board" in etxt
        print(f"[7] empty-state text present: {empty_ok}")

        # ------------------------------------------------chart interaction
        # The charts are interactive (pointer scrubbing over a measured-width
        # SVG). These checks are the gate for that surface: index clamping at
        # both ends, a resize re-measure, a touch scrub (touch-action) and a
        # live board update landing while a hover index is held.
        # Pin the page to the fixture board first: the [7] empty-state seed
        # above is now the NEWEST board and an unpinned page follows the
        # newest, which would leave these lookups staring at an empty board.
        try:
            pg.goto(f"{URL}?board={FIXTURE_ID}", wait_until="domcontentloaded")
            wait_for_text(pg, ["Tokens over time"])

            def line_tip_text() -> str:
                """The line chart readout's text, '' when absent (no 30s stall)."""
                t = pg.locator(".lw-line-tip")
                return t.first.text_content(timeout=1500) if t.count() > 0 else ""

            # Mouse hover, both ends and the middle. The first svg.lw-line is
            # "Tokens over time" (5 points, xLabels given).
            line = pg.locator("svg.lw-line").first
            line.scroll_into_view_if_needed()
            box = line.bounding_box()
            if not box:
                interact_bad.append("line svg has no bounding box")
            else:
                # viewBox width == element width (measured), so 1:1. The chart
                # gutter is X0=44 on the left, X1=W-6 on the right; clamping is
                # probed INSIDE the svg (a pointer outside it fires nothing).
                W = box["width"]

                def hover_abs(x: float) -> None:
                    pg.mouse.move(x, box["y"] + 80)
                    pg.wait_for_timeout(150)

                hover_abs(box["x"] + 44 + 0.5 * (W - 50))
                txt = line_tip_text()
                if "12:00" not in txt:
                    interact_bad.append(f"mid hover: tip={txt!r}")
                hover_abs(box["x"] + 20)  # px < X0: must clamp to index 0
                txt = line_tip_text()
                if "00:00" not in txt:
                    interact_bad.append(f"left clamp: tip={txt!r}")
                hover_abs(box["x"] + W - 3)  # px > X1: must clamp to the last index
                txt = line_tip_text()
                if "24:00" not in txt:
                    interact_bad.append(f"right clamp: tip={txt!r}")
                # Leaving the chart clears the readout (mouse only); move
                # SIDEWAYS to the board padding so the leave is guaranteed to
                # fire on the svg (above it can be off-viewport / the appbar).
                pg.mouse.move(5, box["y"] + 80)
                pg.wait_for_timeout(150)
                if line_tip_text():
                    interact_bad.append("tip still visible after pointer left")

            # Sparkline hover: the first svg.lw-spark is "Observations per day".
            spark = pg.locator("svg.lw-spark").first
            spark.scroll_into_view_if_needed()
            sbox = spark.bounding_box()
            if not sbox:
                interact_bad.append("spark svg has no bounding box")
            else:
                pg.mouse.move(sbox["x"] + sbox["width"] * 0.5, sbox["y"] + 30)
                pg.wait_for_timeout(150)
                stips = pg.locator(".lw-spark-tip")
                if stips.count() == 0 or not stips.first.is_visible():
                    interact_bad.append("spark tip did not appear on hover")

            # Resize: the width hook must re-measure and keep the chart usable.
            old_w = pg.evaluate("document.querySelector('svg.lw-line').viewBox.baseVal.width")
            pg.set_viewport_size({"width": 900, "height": 1000})
            pg.wait_for_timeout(400)
            new_w = pg.evaluate("document.querySelector('svg.lw-line').viewBox.baseVal.width")
            if not (new_w > old_w + 50):
                interact_bad.append(f"viewBox did not follow resize ({old_w} -> {new_w})")
            line = pg.locator("svg.lw-line").first
            line.scroll_into_view_if_needed()
            box = line.bounding_box()
            if box:
                pg.mouse.move(box["x"] + 44 + 0.5 * (box["width"] - 50), box["y"] + 80)
                pg.wait_for_timeout(150)
                txt = line_tip_text()
                if "12:00" not in txt:
                    interact_bad.append(f"hover broken after resize: {txt!r}")
            pg.set_viewport_size({"width": 430, "height": 1000})
            pg.wait_for_timeout(300)

            # Touch scrub: a finger-drag on the chart must TRACK the finger
            # (pointermove -> tooltip follows), not be claimed by the board
            # scroller as a pan gesture. Real swipes are never perfectly
            # horizontal, so the drag is horizontal-dominant with a vertical
            # component: pre-`touch-action` the browser claims ANY pan and
            # pointercancels after ~2 moves, freezing the readout at the
            # touch-down point. The scroller keeps vertical pans (touch-action:
            # pan-y), so scroll and pull-to-refresh are intact — asserted via
            # the vertical drag below.
            tctx = br.new_context(viewport={"width": 430, "height": 900}, has_touch=True)
            tctx.add_init_script(
                "try{localStorage.setItem('daylight-theme','light')}catch(e){}")
            tpg = tctx.new_page()
            tpg.on("pageerror", lambda e: interact_bad.append(f"touch pageerror: {e}"))
            tpg.on("console",
                   lambda m: interact_bad.append(f"touch console: {m.text}")
                   if m.type == "error" else None)
            tpg.goto(f"{URL}?board={FIXTURE_ID}", wait_until="domcontentloaded")
            wait_for_text(tpg, ["Tokens over time"])
            tline = tpg.locator("svg.lw-line").first
            tline.scroll_into_view_if_needed()
            tpg.wait_for_timeout(300)
            tbox = tline.bounding_box()
            if not tbox:
                interact_bad.append("touch: line svg has no bounding box")
            else:
                cdp = tctx.new_cdp_session(tpg)
                x0 = tbox["x"] + tbox["width"] * 0.3
                y0 = tbox["y"] + 80
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchStart",
                    "touchPoints": [{"x": x0, "y": y0}],
                })
                tpg.wait_for_timeout(120)
                # Track the readout's ANCHOR (inline style.left, a % of the
                # wrap), not its bounding-box edge: once the anchor crosses the
                # chart's midline the tip flips to translateX(-100%) and its
                # LEFT EDGE jumps back by the box width while the anchor
                # correctly moved on.
                tip_left = "parseFloat(document.querySelector('.lw-line-tip')?.style.left) || -1"
                start_pct = tpg.evaluate(tip_left)
                for step in range(1, 9):
                    cdp.send("Input.dispatchTouchEvent", {
                        "type": "touchMove",
                        "touchPoints": [{"x": x0 + step * 15, "y": y0 + step * 4}],
                    })
                    tpg.wait_for_timeout(40)
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchEnd", "touchPoints": []})
                tpg.wait_for_timeout(200)
                # The readout must have FOLLOWED the finger: its anchor must
                # advance by at least one chart index (xLabels are 5 across the
                # width, so one index is ~20% of the wrap). A pointercancelled
                # gesture freezes the anchor at the touch-down point; the
                # tooltip painted by onPointerDown alone does not move.
                end_pct = tpg.evaluate(tip_left)
                if end_pct - start_pct < 10:
                    interact_bad.append(
                        f"touch scrub did not track the drag "
                        f"(anchor {start_pct}% -> {end_pct}%, drag 120px)")
                # Blast radius of touch-action on the same surface: a
                # VERTICAL drag on the chart must still scroll the board.
                before = tpg.evaluate(
                    "document.querySelector('.board-scroll').scrollTop")
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchStart",
                    "touchPoints": [{"x": tbox["x"] + tbox["width"] / 2, "y": y0}],
                })
                for step in range(1, 9):
                    cdp.send("Input.dispatchTouchEvent", {
                        "type": "touchMove",
                        "touchPoints": [{
                            "x": tbox["x"] + tbox["width"] / 2,
                            "y": y0 - step * 25}],
                    })
                    tpg.wait_for_timeout(30)
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchEnd", "touchPoints": []})
                tpg.wait_for_timeout(300)
                after = tpg.evaluate(
                    "document.querySelector('.board-scroll').scrollTop")
                if abs(after - before) < 50:
                    interact_bad.append(
                        f"vertical drag on the chart did not scroll the board "
                        f"({before} -> {after})")
            tctx.close()

            # Live update mid-hover: hold a hover index while an SSE snapshot
            # replaces the series with a SHORTER one. The held index is state;
            # it must clamp away (or show new data) — never read past the end,
            # never throw.
            hov = pg.locator("svg.lw-line").first
            hov.scroll_into_view_if_needed()
            hbox = hov.bounding_box()
            if hbox:
                pg.mouse.move(hbox["x"] + 44 + 0.5 * (hbox["width"] - 50), hbox["y"] + 80)
                pg.wait_for_timeout(150)
                if not line_tip_text():
                    interact_bad.append("update test: tip not visible before update")
            # Shrink the series in place (the board write path agents use).
            st_u, body_u = api("POST", f"/api/boards/{FIXTURE_ID}/update", {
                "id": "tokline",
                "set": {"series": [
                    {"name": "p50", "values": [3, 9], "color": "mint"},
                    {"name": "p95", "values": [5, 11]},
                ], "xLabels": ["a", "b"]},
            })
            if st_u not in (200, 201):
                interact_bad.append(f"update POST failed: HTTP {st_u}")
            else:
                pg.wait_for_timeout(1500)
                # hover=2 held; the new series has 2 points, so index 2 is out
                # of range and the readout must be GONE (or honest new data) —
                # never a stale value past the end, never a throw.
                txt = line_tip_text()
                if "12:00" in txt or "18:00" in txt or "24:00" in txt:
                    interact_bad.append(f"stale tooltip after shrink: {txt!r}")
                # The chart still works on the new data.
                hbox = hov.bounding_box()
                if hbox:
                    pg.mouse.move(hbox["x"] + hbox["width"] - 20, hbox["y"] + 80)
                    pg.wait_for_timeout(150)
                    txt = line_tip_text()
                    # at clamps to the new last index (label "b"); the readout
                    # must show the NEW points (9 and 11), not stale ones.
                    if not ("9" in txt and "11" in txt):
                        interact_bad.append(f"hover broken after live update: {txt!r}")
            api("DELETE", f"/api/boards/{FIXTURE_ID}")  # restore the fixture
            api("POST", f"/api/boards/{FIXTURE_ID}", FIXTURE)
            pg.wait_for_timeout(800)
        except Exception as exc:  # pragma: no cover - harness guard
            interact_bad.append(f"interaction harness error: {exc}")
        print(f"[8] chart interaction: {interact_bad or 'ok'}")

        br.close()
finally:
    if seeded:
        api("DELETE", f"/api/boards/{FIXTURE_ID}")
    if empty_seeded:
        api("DELETE", f"/api/boards/{EMPTY_ID}")

# Console errors / failed HTTP responses are part of the gate, not a printout:
# a page that renders the text but 404s an asset or throws is not a pass.
real_errors = [e for e in errors if "WebGL" not in e]
print(f"[6] console/HTTP errors: {real_errors[:5] or 'none'}")

ok = (not missing and not bad and not cap_bad and not missing_components
      and not real_errors and empty_ok and not yaxis_bad and not interact_bad
      and not pill_bad)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
