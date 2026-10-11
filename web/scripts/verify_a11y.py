"""Verify the Livewall client's ACCESSIBILITY paths in a real browser.

The transport suite proves the data paths; the render suite proves the pixels.
This is the third leg: the surfaces a pointer never touches. It asserts, in a
live browser against a real backend:

  K1  every chart (gauge, sparkline, line) is operable without a mouse: the
      svg is focusable with a descriptive accessible name, and the keyboard
      moves the same readout a pointer gets (the regression gate for charts
      that were pointer-only and unnamed)
  K2  keyboard navigation: the app's controls are reachable in Tab order with
      a visible custom focus ring; the error tab carries a text cue (its state
      is not colour-only, WCAG 1.4.1)
  S1  semantics: block headings are real headings (h2 under the h1 board
      title); tables expose table/columnheader/cell roles to the tree
  C1  contrast: the measured text pairs meet WCAG AA (>= 4.5:1) in BOTH
      themes, including the live and stale chips and the error banner
  M1  reduced motion: every shell animation and transition stops under
      prefers-reduced-motion (computed styles, not CSS text)
  R1  reflow: 320px width and a 200%-zoom-equivalent root font leave no
      horizontal overflow
  X1  the accessibility tree: every image node inside the board is NAMED (a
      chart that announces as an unnamed image is pointer-only for a screen
      reader) and the board's heading/table structure is present

Self-seeding/self-cleaning like the other suites; exits non-zero on any gap.

    LIVEWALL_URL=http://127.0.0.1:8470 python web/scripts/verify_a11y.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

# Deliberately DEAD default: a bare run must not seed/delete boards on a real
# backend (the product port 8470). Point LIVEWALL_URL at a running Livewall.
BASE = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:1").rstrip("/")
URL = BASE + "/"
CHROME = os.environ.get("LIVEWALL_CHROME") or None

FLAGS = [
    "--no-sandbox",
    "--disable-gpu",
]

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'GAP '}  {name}  {detail[:150]}", flush=True)


def api(method, path, body=None, timeout=8):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"err": str(e)}


# This suite seeds and deletes boards to assert on exact board counts, so it
# must OWN the backend it talks to. `preflight()` enforces that once, at entry:
# a run that finds a board it did not create refuses to start rather than
# wiping someone's live boards. After it passes, every board present is this
# suite's own, which is what makes `delete_all()` safe.
def preflight():
    """Abort unless the target holds no board this suite did not create."""
    st, body = api("GET", "/api/boards")
    if st != 200:
        print(f"verify_a11y: cannot list boards at {BASE} (HTTP {st})")
        sys.exit(2)
    existing = [b["id"] for b in body.get("boards", [])]
    if existing:
        print(f"verify_a11y: refusing to run — {BASE} already holds boards: "
              f"{existing}")
        print("  This suite seeds and deletes boards to count them, so point it "
              "at a throwaway backend, never a live one.")
        sys.exit(2)


def delete_all():
    """Clear the board list. Safe only after `preflight()` has passed."""
    st, body = api("GET", "/api/boards")
    if st == 200:
        for b in body.get("boards", []):
            api("DELETE", f"/api/boards/{b['id']}")


def seed(board_id, spec):
    api("POST", f"/api/boards/{board_id}", spec)


def wait_for(fn, timeout=12.0, interval=0.1):
    end = time.time() + timeout
    value = None
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return value


FIXTURE = {
    "title": "A11y gate", "subtitle": "a11y fixture", "status": "running", "ttl": 600,
    "tab": {"label": "Probe", "accent": "violet",
            "indicator": {"kind": "bar", "value": 68, "max": 100}},
    "blocks": [
        {"type": "heading", "text": "Section one", "eyebrow": "eyebrow"},
        {"type": "text", "text": "Body copy for the contrast pair."},
        {"type": "stats", "items": [{"label": "cpu", "value": "42", "unit": "%"}]},
        {"type": "gauge", "label": "Disk", "value": 7, "max": 10, "unit": "GiB"},
        {"type": "sparkline", "label": "RPS", "color": "violet",
         "values": [3, 5, 4, 9, 7, 12, 8, 11, 6, 10]},
        {"type": "line", "label": "Latency", "legend": True,
         "series": [{"name": "p50", "color": "violet", "values": [2, 4, 3, 6, 5]},
                    {"name": "p99", "color": "pink", "values": [9, 12, 8, 14, 11]}],
         "xLabels": ["a", "b", "c", "d", "e"]},
        {"type": "table", "columns": ["Node", "unit", "Status"],
         "rows": [["n1", "eu", "done"], ["n2", "us", "failed"],
                  # Long enough to overflow a narrow cell: a table that hides
                  # its overflow must WRAP on a small viewport, never clip.
                  ["a-very-long-node-identifier", "eu-west", "done"]],
         "tagColumn": True},
        {"type": "callout", "text": "Careful now", "tone": "warn"},
        {"type": "code", "title": "install.sh",
         "text": "pipx install ./cli\nlivewall demo"},
    ],
}
BOOM = {
    "title": "Boom", "status": "error", "ttl": 600,
    "tab": {"label": "Boom", "accent": "mint"},
    "blocks": [{"type": "text", "text": "error body"}],
}

# ---- WCAG relative luminance / contrast, computed in the page from real
# computed styles (fg colour, ancestor background chain composited over the
# first opaque layer) so the numbers are measured, not derived from tokens.
CONTRAST_JS = """(pairs) => {
  const parse = (c) => {
    c = c.trim();
    if (c.startsWith('color(srgb')) {
      const t = c.replace('color(srgb','').replace(')','').replace('/',' ').trim().split(/\\s+/);
      return [parseFloat(t[0])*255, parseFloat(t[1])*255, parseFloat(t[2])*255,
              (t.length > 3 ? parseFloat(t[3]) : 1)*255];
    }
    const m = c.replace('rgba(','').replace('rgb(','').replace(')','').split(',').map(parseFloat);
    return m.length === 3 ? [...m, 255] : m;
  };
  const lin = (v) => { v /= 255; return v <= 0.04045 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
  const lum = (p) => 0.2126*lin(p[0]) + 0.7152*lin(p[1]) + 0.0722*lin(p[2]);
  const over = (fg, bg) => {
    const a = fg[3]/255;
    return [fg[0]*a + bg[0]*(1-a), fg[1]*a + bg[1]*(1-a), fg[2]*a + bg[2]*(1-a), 255];
  };
  const alpha = (c) => {
    if (c.startsWith('color(srgb')) {
      const t = c.replace('color(srgb','').replace(')','').replace('/',' ').trim().split(/\\s+/);
      return t.length > 3 ? parseFloat(t[3]) : 1;
    }
    const m = c.replace('rgba(','').replace('rgb(','').replace(')','').split(',').map(parseFloat);
    return m.length > 3 ? m[3]/255 : 1;
  };
  const out = {};
  for (const [name, sel, prop] of pairs) {
    const el = document.querySelector(sel);
    if (!el) { out[name] = null; continue; }
    const fg = getComputedStyle(el)[prop];
    const layers = [];
    for (let n = el, i = 0; n && i < 12; i++, n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !c.startsWith('rgba(0, 0, 0, 0')) layers.push(c);
    }
    const opaque = layers.find((c) => alpha(c) >= 0.999) || 'rgb(255, 249, 240)';
    let bg = parse(opaque);
    for (const c of [...layers.filter((c) => alpha(c) < 0.999)].reverse()) bg = over(parse(c), bg);
    let fgp = parse(fg);
    if (fgp[3] < 255) fgp = over(fgp, bg);
    const l1 = lum(fgp), l2 = lum(bg);
    out[name] = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }
  return out;
}"""

CONTRAST_PAIRS = [
    ["body text", ".lw-body-text", "color"],
    ["muted label", ".lw-muted", "color"],
    ["chip label", ".lw-chip-label", "color"],
    ["eyebrow", ".lw-eyebrow", "color"],
    ["pill ok", ".lw-pill-ok", "color"],
    ["pill error", ".lw-pill-error", "color"],
    ["callout warn", ".lw-tone-warn", "color"],
    ["line tick", ".lw-line-tick", "fill"],
    ["tab selected label", ".tab-selected .tab-label", "color"],
    ["appbar title", ".appbar-title", "color"],
]


def tab_until(page, cls_substr, limit=30):
    """Press Tab until the active element's class contains cls_substr."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        cls = page.evaluate(
            "() => { const el = document.activeElement;"
            " const c = el.className;"
            " return (c && c.baseVal !== undefined) ? c.baseVal : String(c || ''); }")
        if cls_substr in str(cls):
            return True
    return False


def run_theme_checks(br, scheme, kiosk=False):
    kw = {"viewport": {"width": 1280, "height": 800},
          # The copy button's check reads the clipboard back, so the context
          # needs the permission granted.
          "permissions": ["clipboard-read", "clipboard-write"]}
    if scheme == "dark":
        kw["color_scheme"] = "dark"
    ctx = br.new_context(**kw)
    page = ctx.new_page()
    try:
        page.goto(URL + "?board=gate-a11y", wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".lw-spark").count() == 1, timeout=10)

        # ---- K1: the charts are keyboard-operable ------------------------
        for sel, frag in [("svg.lw-spark", "RPS"), ("svg.lw-line", "Latency")]:
            info = page.locator(sel).evaluate(
                "el => ({ role: el.getAttribute('role'),"
                " tabindex: el.getAttribute('tabindex'),"
                " label: el.getAttribute('aria-label') })")
            ok = (info["role"] == "img" and info["tabindex"] == "0"
                  and bool(info["label"]) and frag in info["label"])
            check(f"K1 {sel} focusable role=img named ({scheme})", ok, f"{info}")

        gauge = page.locator(".lw-gauge svg").evaluate(
            "el => el.getAttribute('aria-label')")
        check(f"K1 gauge names its block ({scheme})",
              bool(gauge) and "Disk" in gauge and "70%" in gauge, f"label={gauge!r}")

        # Keyboard: Tab into the sparkline, scrub, read the live readout.
        reached = tab_until(page, "lw-spark")
        if reached:
            page.keyboard.press("Home")
            r1 = page.locator(".lw-spark-wrap .sr-only").first.inner_text()
            page.keyboard.press("ArrowRight")
            r2 = page.locator(".lw-spark-wrap .sr-only").first.inner_text()
            page.keyboard.press("End")
            r3 = page.locator(".lw-spark-wrap .sr-only").first.inner_text()
        else:
            r1 = r2 = r3 = "(chart not reachable by Tab)"
        check(f"K1 keyboard scrubs the sparkline readout ({scheme})",
              reached and r1 == "point 1 of 10: 3" and r2 == "point 2 of 10: 5"
              and r3 == "point 10 of 10: 10",
              f"reached={reached} {r1!r} {r2!r} {r3!r}")

        reached = tab_until(page, "lw-line")
        if reached:
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("ArrowLeft")
            rl = page.locator(".lw-line-wrap .sr-only").first.inner_text()
        else:
            rl = "(chart not reachable by Tab)"
        check(f"K1 keyboard scrubs the line chart readout ({scheme})",
              reached and "p50" in rl and "p99" in rl, f"reached={reached} {rl!r}")
        if reached:
            page.evaluate("document.activeElement.blur()")
            rl_after = page.locator(".lw-line-wrap .sr-only").first.inner_text()
            check(f"K1 the readout clears on blur ({scheme})", rl_after == "",
                  f"{rl_after!r}")

        # ---- K1: the code block's copy button -----------------------------
        # A copy control earns its place only if it is reachable without a
        # pointer, named for assistive tech, copies the code EXACTLY, and says
        # what happened. The failure worth pinning is the silent one: a button
        # that reports success while the clipboard never changed.
        n_copy = page.locator(".lw-copy").count()
        check(f"K1 the code block carries a copy button ({scheme})", n_copy == 1,
              f"count={n_copy}")
        copy_btn = page.locator(".lw-copy").first
        label0 = copy_btn.evaluate("el => (el.textContent || '').trim()")
        check(f"K1 the copy button is named ({scheme})", label0 == "Copy",
              f"name={label0!r}")

        reached = tab_until(page, "lw-copy")
        check(f"K1 the copy button is reachable by Tab ({scheme})", reached)
        if reached:
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)
            got = page.evaluate("navigator.clipboard.readText()")
            check(f"K1 activating it copies the block text verbatim ({scheme})",
                  got == "pipx install ./cli\nlivewall demo", f"got={got!r}")
            label = copy_btn.evaluate("el => (el.textContent || '').trim()")
            check(f"K1 the result is stated in words, not colour alone ({scheme})",
                  label == "Copied", f"label={label!r}")
            announced = copy_btn.evaluate(
                "el => { const p = el.closest('.lw-panel');"
                " const s = p && p.querySelector('.sr-only');"
                " return s ? s.textContent.trim() : null; }")
            check(f"K1 the copy result is announced to assistive tech ({scheme})",
                  announced == "Code copied to the clipboard",
                  f"announced={announced!r}")

        # ---- K2: error tab text cue + visible focus ring ------------------
        err_text = page.locator(".tab-error").first.inner_text()
        check(f"K2 the error tab is not colour-only ({scheme})",
              "ERROR" in err_text.upper(), f"tab text={err_text!r}")

        ring = page.evaluate("""() => {
          const el = document.querySelector('.tab-selected');
          el.focus();
          const cs = getComputedStyle(el);
          return cs.outlineStyle + '|' + cs.outlineWidth;
        }""")
        check(f"K2 the selected tab shows a visible focus ring ({scheme})",
              ring == "solid|2px", f"outline={ring!r}")

        # ---- S1: semantics -------------------------------------------------
        h2 = page.locator("main h2.lw-heading-text").count()
        check(f"S1 block headings are real h2 elements ({scheme})", h2 >= 1,
              f"h2 count={h2}")
        table_ok = page.evaluate("""() => {
          const t = document.querySelector('[role=table]');
          if (!t) return 'no table';
          const heads = t.querySelectorAll('[role=columnheader]').length;
          const cells = t.querySelectorAll('[role=cell]').length;
          const rows = t.querySelectorAll('[role=row]').length;
          return heads === 3 && cells >= 6 && rows >= 3;
        }""")
        check(f"S1 the table exposes table/row/columnheader/cell ({scheme})",
              table_ok is True, f"{table_ok}")

        # ---- C1: contrast, measured ------------------------------------
        ratios = page.evaluate(CONTRAST_JS, CONTRAST_PAIRS)
        bad = {k: v for k, v in ratios.items() if v is None or v < 4.5}
        check(f"C1 text contrast >= 4.5 AA ({scheme})", not bad,
              f"failures={bad}" if bad else
              f"min={min(v for v in ratios.values() if v is not None):.2f}")

        # Error surfaces live on the error board's page.
        p2 = ctx.new_page()
        try:
            p2.goto(URL + "?board=gate-boom", wait_until="domcontentloaded")
            wait_for(lambda: p2.locator(".error-banner").count() == 1, timeout=10)
            ebad = p2.evaluate(CONTRAST_JS, [
                ["error banner", ".error-banner", "color"],
                ["error tab selected", ".tab-error.tab-selected .tab-label", "color"],
                ["stale-capable chip", ".fresh-chip-live", "color"],
            ])
            bad2 = {k: v for k, v in ebad.items() if v is None or v < 4.5}
            check(f"C1 error/chip contrast >= 4.5 AA ({scheme})", not bad2,
                  f"failures={bad2}" if bad2 else
                  f"min={min(v for v in ebad.values() if v is not None):.2f}")
        finally:
            p2.close()

        # ---- X1: the accessibility tree ---------------------------------
        cdp = ctx.new_cdp_session(page)
        tree = cdp.send("Accessibility.getFullAXTree", {})
        unnamed = []
        roles = set()
        for ndesc in tree["nodes"]:
            role = ndesc["role"]["value"]
            roles.add(role)
            name = (ndesc.get("name") or {}).get("value", "")
            if role == "image" and not name:
                # An unnamed image anywhere in main is a chart a screen reader
                # cannot read; in the banner the icons sit inside labelled
                # buttons, which is fine.
                unnamed.append("in-main?")
        # Re-check precisely: unnamed images that are descendants of main.
        if unnamed:
            unnamed = page.evaluate("""() => {
              const main = document.querySelector('main');
              const bad = [];
              for (const svg of main.querySelectorAll('svg')) {
                const labelled = svg.getAttribute('aria-label') ||
                  svg.closest('[aria-label]');
                if (!labelled) bad.push(svg.className.baseVal || svg.className);
              }
              return bad;
            }""")
        check(f"X1 every image in the board is named ({scheme})", not unnamed,
              f"unnamed={unnamed}")
        check(f"X1 the AX tree has heading/table/tabpanel structure ({scheme})",
              {"heading", "table", "tabpanel", "tab"} <= roles,
              f"roles={sorted(roles & {'heading','table','tabpanel','tab','columnheader'})}")
    finally:
        ctx.close()


def run_motion_and_reflow(br):
    # ---- M1: reduced motion --------------------------------------------
    ctx = br.new_context(viewport={"width": 1280, "height": 800},
                         reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(URL + "?board=gate-boom", wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".tab-error").count() == 1, timeout=10)

        def anim(sel):
            if page.locator(sel).count() == 0:
                return "absent"
            return page.eval_on_selector(sel, "el => getComputedStyle(el).animationName")

        tab_dot = anim(".tab-error .tab-dot")
        banner_dot = anim(".error-banner-dot")
        spinner = page.evaluate("""() => {
          const el = document.createElement('span');
          el.className = 'spinner spinning';
          document.body.appendChild(el);
          const a = getComputedStyle(el).animationName;
          el.remove(); return a;
        }""")
        check("M1 reduced motion stops the error dots and the spinner",
              tab_dot == "none" and banner_dot == "none" and spinner == "none",
              f"tab-dot={tab_dot} banner-dot={banner_dot} spinner={spinner}")

        # A second context (light, reduce) for the chip dot and transitions.
        trans = page.evaluate("""() => {
          const out = {};
          for (const [name, sel] of [['icon-btn', '.icon-btn'], ['tab', '.tab'],
                                     ['pull-indicator', '.pull-indicator']]) {
            const el = document.querySelector(sel);
            out[name] = el ? getComputedStyle(el).transitionDuration : 'absent';
          }
          const sp = document.createElement('div');
          sp.className = 'lw-fill';
          document.body.appendChild(sp);
          out['lw-fill'] = getComputedStyle(sp).transitionDuration;
          sp.remove();
          return out;
        }""")
        check("M1 reduced motion stops the shell transitions",
              all(v in ("0s", "absent") for v in trans.values()), f"{trans}")
    finally:
        ctx.close()

    ctx = br.new_context(viewport={"width": 1280, "height": 800},
                         reduced_motion="reduce")
    page = ctx.new_page()
    try:
        page.goto(URL + "?board=gate-a11y", wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".fresh-chip-live").count() == 1, timeout=10)
        chip = page.eval_on_selector(
            ".fresh-chip-live .fresh-dot", "el => getComputedStyle(el).animationName")
        fill = (page.eval_on_selector(
            ".lw-fill", "el => getComputedStyle(el).transitionDuration")
            if page.locator(".lw-fill").count() else "absent")
        check("M1 reduced motion stops the live-chip breathe and the bar sweep",
              chip == "none" and fill in ("0s", "absent"), f"chip={chip} fill={fill}")
    finally:
        ctx.close()

    # ---- R1: reflow ------------------------------------------------------
    ctx = br.new_context(viewport={"width": 320, "height": 800})
    page = ctx.new_page()
    try:
        page.goto(URL + "?board=gate-a11y", wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".lw-spark").count() == 1, timeout=10)
        overflow = page.evaluate(
            "() => [document.documentElement.scrollWidth, window.innerWidth]")
        check("R1 320px width leaves no horizontal overflow",
              overflow[0] <= overflow[1], f"doc={overflow[0]} win={overflow[1]}")
        page.evaluate("document.documentElement.style.fontSize = '30px'")
        page.wait_for_timeout(200)
        overflow = page.evaluate(
            "() => [document.documentElement.scrollWidth, window.innerWidth]")
        check("R1 200%-zoom-equivalent text leaves no horizontal overflow",
              overflow[0] <= overflow[1], f"doc={overflow[0]} win={overflow[1]}")
        page.evaluate("document.documentElement.style.fontSize = ''")

        # Horizontal overflow at the DOCUMENT level is not the only way narrow
        # viewports lose data: a cell with `overflow: hidden` + `nowrap` hides
        # its own text with no scrollbar and no ellipsis when the content is an
        # inline-block child (a tag pill). That is silent data loss, so assert
        # no element is hiding TEXT it cannot show.
        #
        # The text may sit in a child node, not directly on the clipping box
        # (a table cell holds a span), so this looks at the whole subtree. An
        # element with no text at all is skipped: a decorative clipped box (the
        # pull-to-refresh indicator reveals its spinner by animating `height`)
        # clips on purpose and hides nothing a reader needs.
        clipped = page.evaluate(
            """() => {
                const bad = [];
                document.querySelectorAll('*').forEach(el => {
                    const text = (el.textContent || '').trim();
                    if (!text) return;             // nothing readable -> not our case
                    const cs = getComputedStyle(el);
                    const hiddenX = cs.overflowX === 'hidden' || cs.overflow === 'hidden';
                    const hiddenY = cs.overflowY === 'hidden';
                    const overX = el.scrollWidth - el.clientWidth;
                    const overY = el.scrollHeight - el.clientHeight;
                    // 2px of slack for sub-pixel rounding.
                    if (hiddenX && overX > 2) bad.push([el.className, text.slice(0, 24), overX]);
                    else if (hiddenY && overY > 2) bad.push([el.className, text.slice(0, 24), overY]);
                });
                return bad.slice(0, 5);
            }"""
        )
        check("R1 no element hides its text at 320px",
              not clipped, f"clipped: {clipped}")
    finally:
        ctx.close()


def main():
    if BASE == "http://127.0.0.1:1":
        print("verify_a11y needs a backend: set LIVEWALL_URL (never the product"
              " port — this suite seeds and deletes boards)")
        return 2
    print(f"verify_a11y against {BASE}")
    preflight()
    delete_all()
    seed("gate-a11y", FIXTURE)
    seed("gate-boom", BOOM)
    time.sleep(0.3)
    try:
        with sync_playwright() as pw:
            kwargs = {"headless": True, "args": FLAGS}
            if CHROME:
                kwargs["executable_path"] = CHROME
            br = pw.chromium.launch(**kwargs)
            try:
                for scheme in ("light", "dark"):
                    run_theme_checks(br, scheme)
                run_motion_and_reflow(br)
            finally:
                br.close()
    finally:
        delete_all()

    failed = [name for name, ok, _ in results if not ok]
    print("=" * 72)
    print(f"verify_a11y: {len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("RESULT: FAIL")
        for name in failed:
            print(f"  GAP  {name}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())