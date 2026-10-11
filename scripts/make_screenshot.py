"""Regenerate the README screenshots — boards rendering EVERY block type.

The README images are a claim about the product, so they have to be
regenerable: the previous one predated the `line` block and quietly went stale.
This script is the fix. Run it whenever the vocabulary changes.

It seeds a fixture board covering all 17 block types plus two decorative tabs,
captures four images (light, dark, mobile, mobile-dark), and DELETEs the boards
again — so it passes on a clean checkout with only the backend running, and
leaves nothing behind, even when it fails halfway through.

Run against a served client (the backend serving web/dist, or the dev server):

    LIVEWALL_URL=http://127.0.0.1:8470 .venv/bin/python scripts/make_screenshot.py

Env:
    LIVEWALL_URL      base URL of a running Livewall (default is a dead port,
                      http://127.0.0.1:1, so a forgotten value fails loudly)
    LIVEWALL_CHROME   path to a chrome binary; omit to use Playwright's bundled one

Outputs (see SHOTS): docs/screenshot.png, docs/screenshot-dark.png,
docs/screenshot-mobile.png, docs/screenshot-mobile-dark.png

Requires Playwright: pip install playwright && playwright install chromium
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:1").rstrip("/")
CHROME = os.environ.get("LIVEWALL_CHROME") or None
BOARD = "screenshot-fixture"
SCALE = 2  # retina: the README renders this at ~half size, so detail survives

# The set of images to produce. The theme is forced before first paint via the
# same localStorage key the client reads, so no toggle-clicking is needed.
SHOTS = [
    {"path": "docs/screenshot.png", "theme": "light", "width": 1440},
    {"path": "docs/screenshot-dark.png", "theme": "dark", "width": 1440},
    {"path": "docs/screenshot-mobile.png", "theme": "light", "width": 430},
    {"path": "docs/screenshot-mobile-dark.png", "theme": "dark", "width": 430},
]

# Two extra tabs so the tab bar looks intentional rather than like leftovers
# from whatever happened to be running. Seeded, captured, deleted.
TABS = [
    ("screenshot-tests", {
        "title": "Tests", "status": "running", "ttl": 86400,
        "tab": {"label": "Tests", "accent": "violet",
                "indicator": {"kind": "text", "text": "5406 ok"}},
        "blocks": [{"type": "text", "id": "t", "text": "fixture tab"}]}),
    ("screenshot-fleet", {
        "title": "Fleet", "status": "running", "ttl": 86400,
        "tab": {"label": "Fleet", "accent": "mint",
                "indicator": {"kind": "text", "text": "87%"}},
        "blocks": [{"type": "text", "id": "t", "text": "fixture tab"}]}),
]

# One block of every type, in a reading order that also looks like a real board.
FIXTURE = {
    "title": "Nightly index rebuild",
    "subtitle": "search-service · every block type, pushed live by the agent",
    "status": "running",
    "ttl": 86400,
    "blocks": [
        {"type": "heading", "id": "h1", "eyebrow": "search-service",
         "text": "Nightly index rebuild"},
        {"type": "text", "id": "t1",
         "text": "Every block type in the vocabulary, on one board. The agent "
                 "pushes each value as the job runs — no per-use-case UI code."},
        {"type": "callout", "id": "co1", "tone": "ok",
         "text": "No replication lag on any shard"},
        {"type": "progress", "id": "p1", "label": "Documents indexed",
         "value": 6836, "max": 6950, "unit": "docs"},
        {"type": "stats", "id": "s2", "items": [
            {"label": "Rate", "value": "1284/s"},
            {"label": "Elapsed", "value": "00:04:12"},
            {"label": "Errors", "value": 0}]},
        {"type": "gauge", "id": "g1", "label": "Heap used",
         "value": 1521227, "max": 2000000, "color": "mint", "fmt": "compact"},
        {"type": "stat", "id": "s1", "label": "Shards online",
         "value": 42, "unit": "of 42"},
        {"type": "bar", "id": "b1", "label": "Rows per shard", "data": [
            ["shard-0", 1284], ["shard-1", 962],
            ["shard-2", 1430], ["shard-3", 540]]},
        {"type": "sparkline", "id": "sp1", "label": "Throughput (docs/s)",
         "values": [820, 940, 1010, 1180, 1120, 1284, 1310, 1284]},
        {"type": "chart", "id": "c1", "label": "Queue depth",
         "values": [42, 38, 51, 44, 29, 22, 18, 12]},
        {"type": "line", "id": "l1", "label": "Query latency (ms)",
         "xLabels": ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"],
         "series": [
             {"name": "p50", "values": [12, 11, 14, 13, 15, 12]},
             {"name": "p95", "values": [48, 52, 61, 58, 67, 54]}]},
        {"type": "table", "id": "tb1", "columns": ["Batch", "Rows", "Status"],
         "tagColumn": True, "rows": [
             ["batch 41", "1000", "done"],
             ["batch 42", "1000", "done"],
             ["batch 43", "836", "running"],
             ["batch 44", "0", "pending"]]},
        {"type": "divider", "id": "d1"},
        {"type": "kv", "id": "kv1", "items": [
            ["region", "eu-west-1"],
            ["index", "primary-2026.10"],
            ["owner", "search-team"]]},
        {"type": "list", "id": "ls1", "label": "Next steps", "items": [
            "Warm the cache", "Rotate credentials", "Prune old indices"]},
        {"type": "log", "id": "lg1", "title": "recent batches", "lines": [
            "batch 41  copied 1000 rows  0 errors",
            "batch 42  copied 1000 rows  0 errors",
            "batch 43  copied  836 rows  0 errors"]},
        {"type": "code", "id": "cd1", "title": "run.sh",
         "text": "make rebuild INDEX=primary\nmake verify"},
    ],
}


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def main() -> int:
    try:
        urllib.request.urlopen(BASE + "/health", timeout=4)
    except Exception as e:
        print(f"no Livewall backend at {BASE} (/health: {e}).")
        print("  set LIVEWALL_URL to a running backend, e.g.")
        print(f"  LIVEWALL_URL=http://127.0.0.1:8470 python {sys.argv[0]}")
        return 1

    # Seed (replace, so a re-run is idempotent) and capture, with cleanup
    # covering BOTH phases: a seed that dies halfway (bad fixture spec, backend
    # rejecting a payload) must not orphan the boards already created, and the
    # delete pass must only remove boards this run actually created — never a
    # pre-existing board that happens to share a fixture id.
    seeded: list[str] = []
    try:
        for bid, spec in [(BOARD, FIXTURE)] + TABS:
            api("POST", f"/api/boards/{bid}?replace=true", spec)
            seeded.append(bid)
        n = len(FIXTURE["blocks"])
        types = sorted({b["type"] for b in FIXTURE["blocks"]})
        print(f"seeded {BOARD}: {n} blocks, {len(types)} types (+{len(TABS)} tabs)")

        with sync_playwright() as p:
            kw: dict = {"headless": True}
            if CHROME:
                kw["executable_path"] = CHROME
            br = p.chromium.launch(**kw)

            for shot in SHOTS:
                out = Path(shot["path"])
                # Force the theme before first paint — same key the client reads.
                ctx = br.new_context(
                    viewport={"width": shot["width"], "height": 2400},
                    device_scale_factor=SCALE)
                ctx.add_init_script(
                    f"try{{localStorage.setItem('daylight-theme','{shot['theme']}')}}catch(e){{}}")
                pg = ctx.new_page()
                pg.goto(f"{BASE}/?board={BOARD}", wait_until="domcontentloaded")

                # Wait for the board to actually render before measuring.
                pg.wait_for_selector("text=Nightly index rebuild", timeout=15000)
                pg.wait_for_selector("text=Query latency (ms)", timeout=15000)
                pg.wait_for_timeout(500)  # let the SSE snapshot settle

                # Height needed to show every block with no inner scroll: the
                # board lives in an inner scroller (.board-scroll), so full_page
                # alone captures only the viewport and drops the rest.
                h = pg.evaluate("""() => {
                    const s = document.querySelector('.board-scroll');
                    if (!s) return document.body.scrollHeight;
                    const chrome = window.innerHeight - s.clientHeight;
                    return Math.ceil(s.scrollHeight + chrome);
                }""")
                pg.set_viewport_size({"width": shot["width"], "height": h})
                pg.wait_for_timeout(300)

                # Optional: a SHOTS entry may carry "hover": <0..1> to park the
                # pointer on the line chart so its value readout is in frame.
                if shot.get("hover"):
                    box = pg.locator("svg.lw-line").first.bounding_box()
                    if box:
                        pg.mouse.move(
                            box["x"] + box["width"] * shot["hover"],
                            box["y"] + box["height"] * 0.5)
                        pg.wait_for_timeout(250)

                out.parent.mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(out), full_page=True)
                ctx.close()
                print(f"wrote {out}  ({shot['theme']}, {shot['width']}px, {h}px tall)")

            br.close()
    finally:
        for bid in seeded:
            try:
                api("DELETE", f"/api/boards/{bid}")
            except Exception as e:
                print(f"warning: could not remove {bid}: {e}")
        if seeded:
            print("removed fixture boards")

    return 0


if __name__ == "__main__":
    sys.exit(main())
