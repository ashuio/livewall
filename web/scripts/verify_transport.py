"""Verify the Livewall client's transport, lifecycle and multi-board paths.

The unit suite (web/test/*.test.ts) proves the transport state machine against a
fake clock and a controllable byte stream. This script is the other half: the
paths that only exist in a real browser against the real backend —

  S5  connection accounting: repeated reconnects leave the server's subscriber
      count at baseline (read from /health, the client-side counterpart to the
      backend stream suite's leak test)
  S6  the multi-tab path: the tab bar (only rendered with >1 board), the three
      indicator kinds, selection, and "follow the newest unless pinned"
  S7  pull-to-refresh: the touch gesture reconnects, including with no board
  S8  theme: system/light/dark, OS following, corrupt-value degradation, and
      agreement between the pre-paint script and the theme module
  S1  end-to-end: an unknown block degrades to a visible fallback and does not
      blank the board's other blocks (the renderer is total)
  U1  freshness: the app-bar chip renders and its age advances without a new
      snapshot, a real update pulses the board (never on first render), and
      killing the stream turns the chip stale (clearing on reconnect) without
      drawing anything over the board

It is self-seeding/self-cleaning and exits non-zero if any check fails.

    LIVEWALL_URL=http://127.0.0.1:8470 python web/scripts/verify_transport.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

# Deliberately DEAD default: a bare run must not seed/delete boards on a real
# backend (the product port 8470). Point LIVEWALL_URL at a running Livewall.
BASE = os.environ.get("LIVEWALL_URL", "http://127.0.0.1:1").rstrip("/")
URL = BASE + "/"
CHROME = os.environ.get("LIVEWALL_CHROME") or None

FLAGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


def launch(pw):
    """Launch Chromium, honouring LIVEWALL_CHROME like verify_render does.

    Without this the override was read but ignored, so on a host where the
    Playwright bundled browser is unavailable the transport suite failed while
    the render suite worked.
    """
    kwargs = {"headless": True, "args": FLAGS}
    if CHROME:
        kwargs["executable_path"] = CHROME
    return pw.chromium.launch(**kwargs)

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


def subscribers():
    st, body = api("GET", "/health")
    return body.get("subscribers") if st == 200 else None


def wait_for(fn, timeout=12.0, interval=0.1):
    end = time.time() + timeout
    value = None
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return value


def wait_subs(pred, timeout=12.0):
    end = time.time() + timeout
    seen = None
    while time.time() < end:
        seen = subscribers()
        if seen is not None and pred(seen):
            return seen
        time.sleep(0.1)
    return seen


# This suite seeds and deletes boards to assert on exact board counts, so it
# must OWN the backend it talks to. `preflight()` enforces that once, at entry:
# a run that finds a board it did not create refuses to start rather than
# wiping someone's live boards. After it passes, every board present is this
# suite's own, which is what makes `delete_all()` safe.
def preflight():
    """Abort unless the target holds no board this suite did not create."""
    st, body = api("GET", "/api/boards")
    if st != 200:
        print(f"verify_transport: cannot list boards at {BASE} (HTTP {st})")
        sys.exit(2)
    existing = [b["id"] for b in body.get("boards", [])]
    if existing:
        print(f"verify_transport: refusing to run — {BASE} already holds "
              f"boards: {existing}")
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
    st, body = api("POST", f"/api/boards/{board_id}", spec)
    return st, body


def body_text(page):
    return page.inner_text("body")


# --------------------------------------------------------------- S7 refresh
def check_pull_to_refresh_empty(pw):
    """The gesture must reconnect even when there is no board to scroll."""
    delete_all()
    ctx = launch(pw).new_context(
        viewport={"width": 430, "height": 900}, has_touch=True)
    page = ctx.new_page()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "/api/stream" in r.url else None)
    try:
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: "waiting for an agent" in body_text(page))
        initial = wait_for(lambda: len(requests) >= 1) and len(requests)
        if not initial:
            check("S7 pull-to-refresh with no board", False, "no initial stream request")
            return
        try:
            pull(page, ctx)
        except Exception as exc:  # noqa: BLE001 - surface the stage that failed
            check("S7 pull-to-refresh with no board reconnects", False,
                  f"gesture failed: {exc}")
            return
        got = wait_for(lambda: len(requests) > initial, timeout=6)
        check("S7 pull-to-refresh with no board reconnects", bool(got),
              f"stream requests {initial} -> {len(requests)}; {touch_diag(page)}")
    finally:
        ctx.close()


def pull(page, ctx):
    """Dispatch a real touch pull, waiting on delivery/commit (no fixed sleeps).

    CDP touch events are trusted, but Chromium delivers them asynchronously and
    React commits state after the handler returns, so each stage is gated on an
    observable instead of a guessed delay: the event actually arriving, the
    damped offset committing, and a paint before touchend so onTouchEnd reads
    the current `distance` (it closes over state, not a ref).
    """
    box = page.locator(".board-scroll").bounding_box()
    if not box:
        return
    cx = box["x"] + box["width"] / 2
    y0 = box["y"] + 30
    page.evaluate("""() => {
      window.__lwTouch = [];
      for (const t of ['touchstart', 'touchmove', 'touchend', 'touchcancel']) {
        document.addEventListener(t, () => window.__lwTouch.push(t));
      }
    }""")
    cdp = ctx.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
                                          "touchPoints": [{"x": cx, "y": y0}]})
    page.wait_for_function("() => window.__lwTouch.includes('touchstart')", timeout=3000)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                                          "touchPoints": [{"x": cx, "y": y0 + 220}]})
    page.wait_for_function("() => window.__lwTouch.includes('touchmove')", timeout=3000)
    page.wait_for_function(
        "() => { const el = document.querySelector('.pull-indicator');"
        " return !!el && el.style.height !== '' && el.style.height !== '0px'; }",
        timeout=3000)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_function(
        "() => window.__lwTouch.includes('touchend') || window.__lwTouch.includes('touchcancel')",
        timeout=3000)


def touch_diag(page):
    """Best-effort state read for a failing pull (never raises)."""
    try:
        return (page.evaluate("window.__lwTouch"),
                page.locator(".pull-indicator").evaluate("el => el.style.height"),
                page.locator(".spinner").evaluate("el => el.className"))
    except Exception as exc:  # noqa: BLE001
        return f"diag unavailable: {exc}"


# --------------------------------------------------------------- S6 multi-tab
def check_multi_tab(pw):
    delete_all()
    # Seeded oldest-first, so the LAST one is the newest and is auto-selected.
    seed("mt-alpha", {
        "title": "Alpha", "status": "running",
        "tab": {"label": "Alpha", "accent": "violet",
                "indicator": {"kind": "bar", "value": 3, "max": 4}},
        "blocks": [{"type": "callout", "text": "Alpha body", "tone": "info"}],
    })
    seed("mt-beta", {
        "title": "Beta", "status": "running",
        "tab": {"label": "Beta", "accent": "pink",
                "indicator": {"kind": "pct", "value": 42}},
        "blocks": [{"type": "heading", "text": "Beta body"}],
    })
    seed("mt-gamma", {
        "title": "Gamma", "status": "running",
        "tab": {"label": "Gamma", "accent": "lime",
                "indicator": {"kind": "text", "text": "2/5"}},
        "blocks": [{"type": "heading", "text": "Gamma body"}],
    })

    br = launch(pw)
    ctx = br.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".tabs").count() == 1
                 and page.locator(".tab").count() == 3)
        count = page.locator(".tab").count()
        check("S6 tab bar renders with 3 boards", count == 3, f"tabs={count}")

        alpha = page.locator(".tab", has_text="Alpha")
        beta = page.locator(".tab", has_text="Beta")
        gamma = page.locator(".tab", has_text="Gamma")

        bar_ok = (alpha.locator(".tab-track").count() == 1
                  and alpha.locator(".tab-ind").inner_text().strip() == "75%")
        check("S6 bar indicator renders track + 75%", bar_ok,
              f"track={alpha.locator('.tab-track').count()} "
              f"ind={alpha.locator('.tab-ind').inner_text().strip()!r}")

        pct_ok = (beta.locator(".tab-track").count() == 0
                  and beta.locator(".tab-ind").inner_text().strip() == "42%")
        check("S6 pct indicator renders 42%", pct_ok,
              f"ind={beta.locator('.tab-ind').inner_text().strip()!r}")

        text_ok = gamma.locator(".tab-badge").inner_text().strip() == "2/5"
        check("S6 text indicator renders badge 2/5", text_ok,
              f"badge={gamma.locator('.tab-badge').inner_text().strip()!r}")

        selected = page.locator(".tab-selected").inner_text()
        newest_ok = "Gamma" in selected and "Gamma body" in body_text(page)
        check("S6 newest board auto-selected", newest_ok, f"selected={selected!r}")

        alpha.click()
        page.wait_for_timeout(150)
        switched = ("Alpha" in page.locator(".tab-selected").inner_text()
                    and "Alpha body" in body_text(page))
        check("S6 clicking a tab selects it", switched,
              f"selected={page.locator('.tab-selected').inner_text()!r}")

        # Pinned: an update that makes Beta newest must not steal selection.
        api("POST", "/api/boards/mt-beta/update", {"id": "h1", "set": {"text": "Beta body"}})
        page.wait_for_timeout(600)
        still = "Alpha" in page.locator(".tab-selected").inner_text()
        check("S6 pinned selection follows neither the update nor the newest", still,
              f"selected={page.locator('.tab-selected').inner_text()!r}")

        beta.click()
        page.wait_for_timeout(150)
        beta_ok = "Beta body" in body_text(page)
        gamma.click()
        page.wait_for_timeout(150)
        gamma_ok = "Gamma body" in body_text(page)
        check("S6 switching between boards swaps block sets", beta_ok and gamma_ok,
              f"beta={beta_ok} gamma={gamma_ok}")
    finally:
        ctx.close()
        br.close()
        delete_all()


# --------------------------------------------------------- U2 deep-link
def query(page):
    return {k: v[0] for k, v in parse_qs(urlparse(page.url).query).items()}


def check_deeplink(pw):
    """?board=<id> selects/pins a board; tab clicks write the URL back."""
    delete_all()
    for bid, title, body in [
        ("dl-alpha", "Alpha", "Alpha body"),
        ("dl-beta", "Beta", "Beta body"),
        ("dl-gamma", "Gamma", "Gamma body"),
    ]:
        seed(bid, {
            "title": title, "status": "running",
            "tab": {"label": title, "accent": "violet"},
            "blocks": [{"type": "heading", "text": body}],
        })
    time.sleep(0.3)

    br = launch(pw)
    try:
        # 1. Deep-link opens on the named board and pins it.
        ctx = br.new_context(viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(URL + "?board=dl-alpha", wait_until="domcontentloaded")
            ok = wait_for(
                lambda: "Alpha body" in body_text(page)
                and page.locator(".tab-selected").count() == 1
                and "Alpha" in page.locator(".tab-selected").inner_text(),
                timeout=10)
            check("U2 ?board= deep-links and pins the named board", bool(ok),
                  f"url={page.url} selected="
                  f"{page.locator('.tab-selected').inner_text()!r}")
            check("U2 deep-link leaves board= in the address bar",
                  query(page).get("board") == "dl-alpha", f"url={page.url}")

            # 2. A tab click reflects the selection in the URL.
            page.locator(".tab", has_text="Beta").click()
            page.wait_for_timeout(150)
            clicked = ("Beta body" in body_text(page)
                       and query(page).get("board") == "dl-beta")
            check("U2 tab click selects the board and updates ?board=", clicked,
                  f"url={page.url}")
        finally:
            ctx.close()

        # 3. Unknown id falls back to the newest, does not crash, is noticed.
        ctx = br.new_context(viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(URL + "?board=does-not-exist", wait_until="domcontentloaded")
            fellback = wait_for(lambda: "Gamma body" in body_text(page), timeout=10)
            check("U2 unknown ?board= falls back to the newest (no crash)",
                  bool(fellback), f"body={body_text(page)[:80]!r}")
            notice = wait_for(lambda: page.locator(".board-notice").count() == 1,
                              timeout=6)
            check("U2 unknown ?board= shows a quiet notice", bool(notice),
                  f"notice={page.locator('.board-notice').inner_text() if notice else ''!r}")
            check("U2 unknown ?board= is dropped from the address bar",
                  query(page).get("board") is None, f"url={page.url}")
        finally:
            ctx.close()

        # 4. token + board compose: token is stripped, board survives.
        ctx = br.new_context(viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(URL + "?token=shh&board=dl-beta", wait_until="domcontentloaded")
            composed = wait_for(lambda: "Beta body" in body_text(page), timeout=10)
            q = query(page)
            check("U2 ?token= + ?board= compose (token stripped, board kept)",
                  bool(composed) and "token" not in q and q.get("board") == "dl-beta",
                  f"url={page.url}")
        finally:
            ctx.close()
    finally:
        br.close()
        delete_all()


# --------------------------------------------------------- U2 error alarm
def check_error_alarm(pw):
    """An error board is unmissable from the tab bar and the board view."""
    delete_all()
    seed("er-old", {
        "title": "Old", "status": "running",
        "tab": {"label": "Old", "accent": "violet"},
        "blocks": [{"type": "heading", "text": "Old body"}],
    })
    # The error board is NEWEST and deliberately tinted a non-error accent: the
    # alarm must not depend on the agent having chosen the red accent.
    seed("er-boom", {
        "title": "Boom", "status": "error",
        "tab": {"label": "Boom", "accent": "mint",
                "indicator": {"kind": "bar", "value": 1, "max": 4}},
        "blocks": [{"type": "heading", "text": "Boom body"}],
    })
    time.sleep(0.3)

    br = launch(pw)
    ctx = br.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".tab").count() == 2)
        sel_ok = wait_for(
            lambda: page.locator(".tab-selected").count() == 1
            and "Boom" in page.locator(".tab-selected").inner_text(), timeout=10)
        check("U2 error board stays selected when it is the newest", bool(sel_ok),
              f"selected="
              f"{page.locator('.tab-selected').inner_text() if page.locator('.tab-selected').count() else ''!r}")

        check("U2 error tab carries the alarm class (accent-independent)",
              page.locator(".tab-error").count() == 1,
              f"tab-error={page.locator('.tab-error').count()}")

        boom = page.locator(".tab-error")
        ind = boom.locator(".tab-ind")
        ind_text = ind.inner_text().strip() if ind.count() else ""
        ind_ok = boom.locator(".tab-track").count() == 1 and ind_text == "25%"
        check("U2 error board keeps its tab indicator", ind_ok,
              f"track={boom.locator('.tab-track').count()} ind={ind_text!r}")

        banner = wait_for(lambda: page.locator(".error-banner").count() == 1, timeout=6)
        check("U2 error board shows a prominent banner", bool(banner),
              f"banner={page.locator('.error-banner').inner_text() if banner else ''!r}")
    finally:
        ctx.close()
        br.close()
        delete_all()


# ------------------------------------------------------------- U2 kiosk
def check_kiosk(pw):
    """?kiosk=1: one board, no chrome, freshness still shown, an exit."""
    delete_all()
    seed("kk-alpha", {
        "title": "Alpha", "status": "running",
        "tab": {"label": "Alpha", "accent": "violet"},
        "blocks": [{"type": "heading", "text": "Alpha body"}],
    })
    seed("kk-beta", {
        "title": "Beta", "status": "running",
        "tab": {"label": "Beta", "accent": "pink"},
        "blocks": [{"type": "heading", "text": "Beta body"}],
    })
    time.sleep(0.3)

    br = launch(pw)
    try:
        # kiosk with no board: the followed board, chrome hidden, chip shown.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1", wait_until="domcontentloaded")
            board = wait_for(lambda: "Beta body" in body_text(page), timeout=10)
            chrome = (page.locator(".tabs").count() == 0
                      and page.locator(".appbar-actions").count() == 0)
            chip = page.locator(".fresh-chip").count() == 1
            check("U2 ?kiosk=1 hides the tab bar and app-bar actions", bool(chrome),
                  f"tabs={page.locator('.tabs').count()} "
                  f"actions={page.locator('.appbar-actions').count()}")
            check("U2 ?kiosk=1 shows the followed board", bool(board),
                  f"body={body_text(page)[:60]!r}")
            check("U2 kiosk keeps the freshness chip", chip,
                  f"chip={page.locator('.fresh-chip').inner_text() if chip else ''!r}")
            zoom = (page.locator(".board-kiosk").evaluate("el => getComputedStyle(el).zoom")
                    if page.locator(".board-kiosk").count() else "")
            check("U2 kiosk board is enlarged", zoom not in ("", "1", "normal"),
                  f"zoom={zoom!r}")
        finally:
            ctx.close()

        # kiosk + board: that board fullscreen; Escape restores the chrome.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1&board=kk-alpha", wait_until="domcontentloaded")
            named = wait_for(lambda: "Alpha body" in body_text(page), timeout=10)
            check("U2 ?kiosk=1&board= shows that board fullscreen", bool(named),
                  f"body={body_text(page)[:60]!r}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            exited = (page.locator(".appbar-actions").count() == 1
                      and page.locator(".tabs").count() == 1
                      and query(page).get("kiosk") is None)
            check("U2 Escape exits kiosk and restores the chrome", bool(exited),
                  f"url={page.url} actions={page.locator('.appbar-actions').count()} "
                  f"tabs={page.locator('.tabs').count()}")
        finally:
            ctx.close()

        # ?kiosk=0 (and absent) is normal mode.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=0", wait_until="domcontentloaded")
            # Wait for the tab bar too, not just the app bar: with two seeded
            # boards the tab strip only renders once the first snapshot lands,
            # so asserting on it before then was a race (observed flaky: the
            # pre-snapshot read was 0 while the detail re-read 1).
            wait_for(lambda: page.locator(".appbar").count() == 1
                     and page.locator(".tabs").count() == 1)
            normal = (page.locator(".tabs").count() == 1
                      and page.locator(".appbar-actions").count() == 1)
            check("U2 ?kiosk=0 is normal mode", bool(normal),
                  f"tabs={page.locator('.tabs').count()} "
                  f"actions={page.locator('.appbar-actions').count()}")
        finally:
            ctx.close()

        # Kiosk must still warn when the data goes stale.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1", wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".fresh-chip").count() == 1, timeout=10)
            page.route("**/api/stream", lambda route: route.abort())
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            # Kiosk hides the app-bar ACTIONS, not the status chip — so the chip
            # is still the staleness indicator there.
            went_stale = wait_for(
                lambda: page.locator('.fresh-chip[data-fresh="stale"]').count() == 1,
                timeout=8)
            check("U2 kiosk still shows staleness in the freshness chip",
                  bool(went_stale),
                  f"chip={page.inner_text('.fresh-chip') if went_stale else ''!r}")
        finally:
            ctx.close()
    finally:
        br.close()
        delete_all()


# --------------------------------------------------------- S5 accounting
def check_connection_accounting(pw):
    delete_all()
    base = wait_subs(lambda n: n == 0)
    br = launch(pw)
    ctx = br.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    peak = 0
    try:
        page.goto(URL, wait_until="domcontentloaded")
        live = wait_subs(lambda n: n >= 1, timeout=8)
        peak = max(peak, live or 0)
        for _ in range(5):
            page.evaluate(
                "() => document.dispatchEvent(new Event('visibilitychange'))")
            time.sleep(0.15)
            s = subscribers()
            if s is not None:
                peak = max(peak, s)
        after = wait_subs(lambda n: n == 1)
        check("S5 N reconnects leave exactly one subscriber", after == 1,
              f"before={base}, live={live}, peak={peak}, after={after}")
    finally:
        ctx.close()
        br.close()
    back = wait_subs(lambda n: n == base, timeout=12)
    check("S5 closing the client returns subscribers to baseline", back == base,
          f"baseline={base}, after close={back}")


# ---------------------------------------------------------------- S8 theme
def check_theme(pw):
    br = launch(pw)

    def open_ctx(color_scheme, storage=None):
        ctx = br.new_context(viewport={"width": 430, "height": 900},
                             color_scheme=color_scheme)
        if storage is not None:
            ctx.add_init_script(
                "try{localStorage.setItem('daylight-theme',"
                + json.dumps(storage) + ")}catch(e){}")
        return ctx

    try:
        # system follows the OS (dark OS -> dark)
        ctx = open_ctx("dark")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".appbar").count() == 1)
        theme = page.evaluate("document.documentElement.dataset.theme")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        meta = page.locator("meta[name=theme-color]").get_attribute("content")
        check("S8 system follows a dark OS", theme == "dark", f"data-theme={theme!r}")
        check("S8 pre-paint script + tokens agree (dark)", bg == "rgb(23, 18, 37)"
              and meta == "#171225", f"bg={bg} meta={meta}")
        # Follow the OS live while on system.
        page.emulate_media(color_scheme="light")
        page.wait_for_timeout(200)
        followed = page.evaluate("document.documentElement.dataset.theme")
        check("S8 system follows a live OS change", followed == "light",
              f"data-theme={followed!r}")
        ctx.close()

        # explicit light beats a dark OS
        ctx = open_ctx("dark", storage="light")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".appbar").count() == 1)
        theme = page.evaluate("document.documentElement.dataset.theme")
        check("S8 stored 'light' beats a dark OS", theme == "light", f"data-theme={theme!r}")
        ctx.close()

        # corrupt value degrades to system
        ctx = open_ctx("dark", storage="purple")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".appbar").count() == 1)
        theme = page.evaluate("document.documentElement.dataset.theme")
        stored = page.evaluate("localStorage.getItem('daylight-theme')")
        check("S8 corrupt value degrades to system", theme == "dark",
              f"data-theme={theme!r} stored={stored!r}")
        ctx.close()

        # cycling system -> light -> dark -> system
        ctx = open_ctx("light")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        wait_for(lambda: page.locator(".appbar").count() == 1)
        btn = page.locator("button[aria-label^='Theme:']")
        seen = []
        for _ in range(3):
            btn.click()
            page.wait_for_timeout(120)
            seen.append((
                btn.get_attribute("aria-label"),
                page.evaluate("document.documentElement.dataset.theme"),
                page.evaluate("localStorage.getItem('daylight-theme')"),
            ))
        expected = [
            ("Theme: Light", "light", "light"),
            ("Theme: Dark", "dark", "dark"),
            ("Theme: System", "light", "system"),
        ]
        check("S8 cycling applies + persists all three values", seen == expected,
              f"seen={seen}")
        ctx.close()
    finally:
        br.close()


# ---------------------------------------------------- S1 hostile block
def check_hostile_block_degrades(pw):
    """A block the renderer does not know must degrade to a visible value and
    must not blank the board's other blocks — the renderer is total."""
    frame = {"boards": [{"id": "e2e-bad", "title": "E2E", "subtitle": "",
                         "status": "running", "updated": 0, "progress": None,
                         "tab": {}, "blocks": [
                             {"type": "no-such-widget", "text": "x"},
                             {"type": "heading", "text": "Survives Board"},
                         ]}], "config": {}}
    body = f"data: {json.dumps(frame)}\n\n"

    br = launch(pw)
    ctx = br.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.route("**/api/stream", lambda route: route.fulfill(
        status=200, headers={"content-type": "text/event-stream",
                             "cache-control": "no-cache"}, body=body))
    try:
        page.goto(URL, wait_until="domcontentloaded")
        degraded = wait_for(
            lambda: "[unknown block: no-such-widget]" in body_text(page), timeout=8)
        survived = wait_for(lambda: "Survives Board" in body_text(page), timeout=8)
        check("S1 unknown block degrades to a visible fallback", bool(degraded),
              f"body={body_text(page)[:120]!r}")
        check("S1 one bad block does not blank the board", bool(survived),
              f"survived={bool(survived)}")
    finally:
        ctx.close()
        br.close()


# --------------------------------------------------- U1 auth park (401)
def _health_ok(base):
    try:
        urllib.request.urlopen(base + "/health", timeout=3)
        return True
    except Exception:
        return False


def check_auth_park(pw):
    """A deterministic credential failure must STOP, not retry forever.

    Boots its OWN token-gated backend on a free port (the shared one is
    unauthenticated). With a wrong token the client must surface HTTP 401 and
    then make no further stream attempts (pre-fix it retried every 2s). A
    correct token must still stream normally, proving the park is about the
    credential, not the server.
    """
    import socket
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    # Same env var the backend honours, so a standalone run can point the
    # check at a scratch build instead of the (production-served) web/dist.
    dist = Path(os.environ.get("LIVEWALL_WEB_DIR") or (repo / "web" / "dist"))
    if not dist.is_dir():
        check("U1 auth park: skipped, web/dist missing", False,
              "run the build first")
        return
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ)
    env["LIVEWALL_PORT"] = str(port)
    env["LIVEWALL_WEB_DIR"] = str(dist)
    TOKEN = "gatetoken"
    env["LIVEWALL_TOKEN"] = TOKEN
    auth_base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(repo / "backend" / "app.py")], cwd=str(repo), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = wait_for(lambda: _health_ok(auth_base), timeout=10)
        check("U1 auth backend boots", bool(up), auth_base)
        if not up:
            return
        br = launch(pw)
        ctx = br.new_context(viewport={"width": 430, "height": 900})
        page = ctx.new_page()
        page.add_init_script(
            "window.__calls=[];const o=window.fetch;"
            "window.fetch=function(...a){const u=String(a[0] instanceof Request?a[0].url:a[0]);"
            "if(u.includes('/api/stream'))window.__calls.push(Date.now());"
            "return o.apply(this,a);};")
        try:
            # WRONG token: HTTP 401 surfaces, and the retry loop must NOT run.
            page.goto(auth_base + "/?token=WRONG", wait_until="domcontentloaded")
            said = wait_for(
                lambda: "401" in (page.locator(".icon-status").get_attribute("title") or ""),
                timeout=10)
            check("U1 wrong token: status honestly says HTTP 401", bool(said),
                  (page.locator(".icon-status").get_attribute("title") or ""))
            t0 = page.evaluate("Date.now()")
            time.sleep(8)
            t1 = page.evaluate("Date.now()")
            n = len([t for t in page.evaluate("window.__calls") if t0 <= t <= t1])
            check("U1 wrong token: the transport parks (<=1 extra attempt in 8s, pre-fix ~4)",
                  n <= 1, f"{n} stream attempts in the 8s after the first 401")

            # The failure must be VISIBLE body text, not only the icon's
            # hover-only title: pre-fix the board area read the normal empty
            # state ("waiting for an agent to program a board…") while the
            # real state was an authorization failure — a stranger had no way
            # to learn they needed ?token=.
            body = page.inner_text("body")
            check("U1 wrong token: the failure is visible in the page body",
                  "401" in body and "waiting for an agent" not in body,
                  body[:200])

            # CORRECT token: the auth path works and the wall streams live —
            # the park above is about the credential, not the server.
            req = urllib.request.Request(
                auth_base + "/api/boards/authok", method="POST",
                data=json.dumps({"title": "Auth OK", "status": "running",
                                 "blocks": [{"type": "heading", "text": "auth"}]}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                check("U1 auth backend seeds with the correct token", r.status == 200)
            page2 = ctx.new_page()
            page2.add_init_script(
                "window.__calls=[];const o=window.fetch;"
                "window.fetch=function(...a){const u=String(a[0] instanceof Request?a[0].url:a[0]);"
                "if(u.includes('/api/stream'))window.__calls.push(Date.now());"
                "return o.apply(this,a);};")
            page2.goto(auth_base + f"/?token={TOKEN}", wait_until="domcontentloaded")
            live = wait_for(
                lambda: page2.locator('.fresh-chip[data-fresh="live"]').count() == 1,
                timeout=12)
            check("U1 correct token: the chip renders live (auth path unbroken)",
                  bool(live))
        finally:
            ctx.close()
            br.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=6)


# ------------------------------------------------------------- U1 freshness
def check_freshness(pw):
    """The wall must say at a glance whether it is live, and how old it is."""
    delete_all()
    board_id = "fr-fresh"
    seed(board_id, {
        "title": "Freshness", "status": "running",
        "blocks": [{"type": "heading", "text": "Freshness body"}],
        "tab": {"label": "Freshness", "accent": "violet"},
    })
    # Let the board age past the formatter's "just now" window so the chip text
    # is observably advancing (otherwise "just now" -> "just now").
    time.sleep(6)

    br = launch(pw)
    ctx = br.new_context(viewport={"width": 430, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded")
        chip = page.locator(".fresh-chip")

        def chip_text():
            try:
                return chip.inner_text()
            except Exception:  # noqa: BLE001 - absent on a client without the chip
                return ""

        def chip_age():
            try:
                return float(chip.get_attribute("data-age-seconds"))
            except Exception:  # noqa: BLE001
                return -1.0

        rendered = wait_for(lambda: chip.count() == 1, timeout=10)
        first, first_age = chip_text(), chip_age()
        check("U1 freshness chip renders 'live · updated …'",
              bool(rendered) and "live" in first and "updated" in first, f"chip={first!r}")

        # The pulse must NOT fire on the very first render.
        page.wait_for_timeout(300)
        check("U1 no update-pulse on first render",
              page.locator(".board-pulse").count() == 0,
              f"board-pulse={page.locator('.board-pulse').count()}")

        # Age advances on the client clock, with no new snapshot.
        page.wait_for_timeout(2600)
        second, second_age = chip_text(), chip_age()
        check("U1 freshness chip advances without a new snapshot",
              second_age >= first_age + 2.0 and second != first,
              f"{first!r}({first_age}) -> {second!r}({second_age})")

        # A real change between snapshots pulses the board, then clears.
        api("PATCH", f"/api/boards/{board_id}", {"subtitle": "tick"})
        pulsed = wait_for(lambda: page.locator(".board-pulse").count() == 1, timeout=4)
        check("U1 update-pulse fires on a real change", bool(pulsed),
              f"data-pulse={page.get_attribute('.board', 'data-pulse')}")
        cleared = wait_for(lambda: page.locator(".board-pulse").count() == 0, timeout=4)
        check("U1 update-pulse clears", bool(cleared))

        # prefers-reduced-motion: the pulse must be suppressed entirely.
        rctx = br.new_context(viewport={"width": 430, "height": 900}, reduced_motion="reduce")
        rpage = rctx.new_page()
        try:
            rpage.goto(URL, wait_until="domcontentloaded")
            wait_for(lambda: rpage.locator(".fresh-chip").count() == 1, timeout=10)
            api("PATCH", f"/api/boards/{board_id}", {"subtitle": "tick-reduced"})
            rpage.wait_for_timeout(600)
            check("U1 update-pulse suppressed under prefers-reduced-motion",
                  rpage.locator(".board-pulse").count() == 0,
                  f"board-pulse={rpage.locator('.board-pulse').count()}")
        finally:
            rctx.close()

        # Kill the stream: abort every NEW /api/stream request, then force a
        # reconnect (the visibility-resume path) so the fresh request is killed.
        # The already-open stream is untouched by the route, so the reconnect is
        # what makes the abort land.
        page.route("**/api/stream", lambda route: route.abort())
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        # Staleness is surfaced in the freshness CHIP, not an overlay: a board
        # that is already a solid colour must not be tinted or covered.
        went_stale = wait_for(
            lambda: page.locator('.fresh-chip[data-fresh="stale"]').count() == 1,
            timeout=8)
        check("U1 the freshness chip goes stale when the stream is killed",
              bool(went_stale),
              f"chip={page.inner_text('.fresh-chip') if went_stale else ''!r}")
        # And nothing is drawn OVER the board.
        check("U1 no overlay covers the board when stale",
              page.locator(".stale-overlay").count() == 0,
              f"overlays={page.locator('.stale-overlay').count()}")

        # Reconnect: stop aborting; the transport's own retry re-establishes the
        # stream and the chip returns to live.
        page.unroute("**/api/stream")
        live_again = wait_for(lambda: chip_text().startswith("live"), timeout=12)
        check("U1 the freshness chip returns to live on reconnect", bool(live_again),
              f"chip={chip_text()!r}")
    finally:
        ctx.close()
        br.close()
        delete_all()


# ---------------------------------------------------- U3 responsive layout
def viewport_measure(page):
    """One round-trip: overflow, chrome visibility, tab/button sizes, offenders."""
    return page.evaluate("""() => {
      const de = document.documentElement;
      const rect = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) };
      };
      const heights = (sel) => [...document.querySelectorAll(sel)]
        .map(e => Math.round(e.getBoundingClientRect().height));
      const tabs = heights('.tab');
      const btns = heights('.appbar .icon-btn');
      const offenders = [...document.querySelectorAll('*')]
        .filter(e => e.getBoundingClientRect().right > de.clientWidth + 0.5)
        .map(e => e.getAttribute('class') || e.tagName)
        .slice(0, 8);
      return {
        scrollWidth: de.scrollWidth,
        clientWidth: de.clientWidth,
        chip: rect('.fresh-chip'),
        alarm: rect('.error-banner'),
        tabStrip: document.querySelectorAll('.tabs').length,
        tabCount: tabs.length,
        minTabH: tabs.length ? Math.min(...tabs) : null,
        minBtnH: btns.length ? Math.min(...btns) : null,
        offenders,
      };
    }""")


# The three viewports the pass is required to work at. Widths define the bands:
# phone <= 600, tablet 601-1024, desktop >= 1025 (documented in App.css/README).
RESPONSIVE_VIEWPORTS = [
    ("phone", 390, 844),
    ("tablet", 820, 1180),
    ("desktop", 1280, 800),
]


def check_responsive(pw):
    """U3: no horizontal overflow, usable chrome and tappable targets by width."""
    delete_all()
    for i in range(3):
        seed(f"rs-{i}", {
            "title": f"Board {i}", "status": "running",
            "tab": {"label": f"Board {i}", "accent": "violet",
                    "indicator": {"kind": "pct", "value": 10 * (i + 1)}},
            "blocks": [{"type": "heading", "text": f"Board {i} body"}],
        })
    seed("rs-boom", {
        "title": "Boom", "status": "error",
        "tab": {"label": "Boom", "accent": "mint"},
        "blocks": [{"type": "heading", "text": "Boom body"}],
    })
    time.sleep(0.3)

    br = launch(pw)
    try:
        for label, w, h in RESPONSIVE_VIEWPORTS:
            ctx = br.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            try:
                page.goto(URL, wait_until="domcontentloaded")
                # 3 normal + 1 error = 4 tabs, and the error board is newest.
                wait_for(lambda: page.locator(".tab").count() == 4, timeout=10)
                m = viewport_measure(page)
                check(f"U3 {label} {w}x{h} no horizontal overflow",
                      m["scrollWidth"] <= m["clientWidth"],
                      f"scrollWidth={m['scrollWidth']} clientWidth={m['clientWidth']} "
                      f"offenders={m['offenders']}")
                check(f"U3 {label} freshness chip visible",
                      bool(m["chip"]) and m["chip"]["w"] > 0,
                      f"chip={m['chip']}")
                check(f"U3 {label} error alarm visible",
                      bool(m["alarm"]) and m["alarm"]["w"] > 0,
                      f"alarm={m['alarm']}")
                check(f"U3 {label} tab bar usable",
                      m["tabStrip"] == 1 and m["tabCount"] == 4
                      and (m["minTabH"] or 0) >= 30,
                      f"strips={m['tabStrip']} tabs={m['tabCount']} minTabH={m['minTabH']}")
                if label == "phone":
                    # Touch targets: >=40px practical minimum (WCAG AA floor is
                    # 24, AAA is 44; the baseline chrome was 34). Rationale is the
                    # requirement, not this implementation's exact size.
                    check("U3 phone touch targets are tappable (>=40px)",
                          (m["minBtnH"] or 0) >= 40 and (m["minTabH"] or 0) >= 40,
                          f"minBtnH={m['minBtnH']} minTabH={m['minTabH']}")
            finally:
                ctx.close()

        # Kiosk is the most likely wall display: it must not break at any width.
        for label, w, h in RESPONSIVE_VIEWPORTS:
            ctx = br.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            try:
                page.goto(URL + "?kiosk=1", wait_until="domcontentloaded")
                wait_for(lambda: page.locator(".fresh-chip").count() == 1, timeout=10)
                m = viewport_measure(page)
                check(f"U3 kiosk {label} {w}x{h} no overflow, chip kept",
                      m["scrollWidth"] <= m["clientWidth"] and bool(m["chip"]),
                      f"scrollWidth={m['scrollWidth']} clientWidth={m['clientWidth']} "
                      f"chip={m['chip']} offenders={m['offenders']}")
            finally:
                ctx.close()
    finally:
        br.close()
        delete_all()


# ------------------------------------------------------ U3 board overview
def check_overview(pw):
    """U3: an all-boards overview finds and selects a board among many."""
    delete_all()
    for i in range(3):
        seed(f"ov-{i}", {
            "title": f"Board {i}", "status": "running",
            "tab": {"label": f"Board {i}", "accent": "violet",
                    "indicator": {"kind": "bar", "value": i + 1, "max": 4}},
            "blocks": [{"type": "heading", "text": f"Board {i} body"}],
        })
    seed("ov-boom", {
        "title": "Boom", "status": "error",
        "tab": {"label": "Boom", "accent": "mint"},
        "blocks": [{"type": "heading", "text": "Boom body"}],
    })
    time.sleep(0.3)

    br = launch(pw)
    try:
        for label, w, h in [("phone", 390, 844), ("desktop", 1280, 800)]:
            ctx = br.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            try:
                page.goto(URL, wait_until="domcontentloaded")
                wait_for(lambda: page.locator(".tab").count() == 4, timeout=10)
                btn = page.locator(".overview-btn")
                reachable = wait_for(lambda: btn.count() == 1, timeout=6)
                check(f"U3 {label} overview reachable", bool(reachable),
                      f"buttons={btn.count()}")
                if not reachable:
                    continue
                btn.click()
                listed = wait_for(
                    lambda: page.locator(".board-overview .overview-card").count() == 4,
                    timeout=6)
                check(f"U3 {label} overview lists every board", bool(listed),
                      f"cards={page.locator('.overview-card').count()}")
                err_cards = page.locator(".overview-card[data-error='true']").count()
                check(f"U3 {label} overview flags the error board",
                      err_cards == 1, f"error-cards={err_cards}")
                check(f"U3 {label} overview shows per-board age",
                      page.locator(".overview-card .overview-age").count() == 4,
                      f"ages={page.locator('.overview-card .overview-age').count()}")
                m = viewport_measure(page)
                check(f"U3 {label} overview open has no horizontal overflow",
                      m["scrollWidth"] <= m["clientWidth"],
                      f"scrollWidth={m['scrollWidth']} clientWidth={m['clientWidth']} "
                      f"offenders={m['offenders']}")
                page.locator(".overview-card", has_text="Board 0").click()
                page.wait_for_timeout(200)
                selected = ("Board 0 body" in body_text(page)
                            and query(page).get("board") == "ov-0"
                            and page.locator(".board-overview").count() == 0)
                check(f"U3 {label} overview selects + updates ?board= + closes",
                      selected, f"url={page.url}")
            finally:
                ctx.close()

        # Keyboard route (bonus) and the age ticking while the overlay is open.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".tab").count() == 4, timeout=10)
            page.keyboard.press("g")
            opened = wait_for(lambda: page.locator(".board-overview").count() == 1,
                              timeout=6)
            check("U3 keyboard 'g' opens the overview", bool(opened),
                  f"overview={page.locator('.board-overview').count()}")
            age_ok, age_detail = False, "overview did not open"
            if opened:
                card = page.locator(".overview-card").first
                age1 = float(card.get_attribute("data-age-seconds"))
                page.wait_for_timeout(2600)
                age2 = float(card.get_attribute("data-age-seconds"))
                age_ok, age_detail = age2 >= age1 + 2.0, f"{age1} -> {age2}"
            check("U3 overview age ticks without a new snapshot", age_ok, age_detail)
            page.keyboard.press("Escape")
            closed = wait_for(lambda: page.locator(".board-overview").count() == 0,
                              timeout=6)
            check("U3 Escape closes the overview", bool(opened and closed),
                  f"opened={bool(opened)} overview={page.locator('.board-overview').count()}")
        finally:
            ctx.close()

        # Kiosk is chrome-free by design: the overview must not be reachable.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1", wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".fresh-chip").count() == 1, timeout=10)
            page.keyboard.press("g")
            page.wait_for_timeout(300)
            check("U3 kiosk hides the overview (button absent, key is a no-op)",
                  page.locator(".overview-btn").count() == 0
                  and page.locator(".board-overview").count() == 0,
                  f"btn={page.locator('.overview-btn').count()} "
                  f"overview={page.locator('.board-overview').count()}")
        finally:
            ctx.close()

        # 12 boards: the overview is the way to find one.
        delete_all()
        for i in range(12):
            seed(f"many-{i}", {
                "title": f"Board {i:02d}", "status": "running",
                "tab": {"label": f"Board {i:02d}", "accent": "violet"},
                "blocks": [{"type": "heading", "text": f"Board {i:02d} body"}],
            })
        time.sleep(0.3)
        ctx = br.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".tab").count() == 12, timeout=10)
            btn = page.locator(".overview-btn")
            if wait_for(lambda: btn.count() == 1, timeout=6):
                btn.click()
                listed = wait_for(
                    lambda: page.locator(".overview-card").count() == 12, timeout=6)
                check("U3 12 boards: overview lists all 12", bool(listed),
                      f"cards={page.locator('.overview-card').count()}")
                if listed:
                    page.locator(".overview-card", has_text="Board 05").click()
                    page.wait_for_timeout(200)
                    found = ("Board 05 body" in body_text(page)
                             and query(page).get("board") == "many-5")
                    check("U3 12 boards: overview finds + selects Board 05", found,
                          f"url={page.url}")
                else:
                    check("U3 12 boards: overview finds + selects Board 05", False,
                          "overview did not list 12 boards")
            else:
                check("U3 12 boards: overview lists all 12", False,
                      "no overview button")
                check("U3 12 boards: overview finds + selects Board 05", False,
                      "no overview button")
        finally:
            ctx.close()
    finally:
        br.close()
        delete_all()


# ------------------------------------------------ U4 end-to-end composition
def check_composition(pw):
    """U4: the U1-U4 client states compose instead of fighting.

    Every check here drives the real client and asserts a cross-feature
    interaction: kiosk x error x overview, stale + error coexisting, reduced
    motion over every animated element, the keyboard selection round-trip, and
    the terminal TTL countdown.
    """
    delete_all()
    seed("cx-run", {
        "title": "Run", "status": "running", "ttl": 1800,
        "tab": {"label": "Run", "accent": "violet"},
        "blocks": [{"type": "heading", "text": "Run body"}],
    })
    seed("cx-boom", {
        "title": "Boom", "status": "error", "ttl": 1800,
        "tab": {"label": "Boom", "accent": "mint"},
        "blocks": [{"type": "heading", "text": "Boom body"}],
    })
    time.sleep(0.3)

    br = launch(pw)
    try:
        # 1. kiosk x error x overview: chrome-free, chip + error banner, no
        #    overview button and `g` is a no-op.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1&board=cx-boom", wait_until="domcontentloaded")
            wait_for(lambda: "Boom body" in body_text(page), timeout=10)
            chrome = (page.locator(".tabs").count() == 0
                      and page.locator(".appbar-actions").count() == 0)
            chip = page.locator(".fresh-chip").count() == 1
            banner = page.locator(".error-banner").count() == 1
            page.keyboard.press("g")
            page.wait_for_timeout(300)
            unreachable = (page.locator(".overview-btn").count() == 0
                           and page.locator(".board-overview").count() == 0)
            check("U4 kiosk+error compose: chrome-free, chip+banner, overview unreachable",
                  chrome and chip and banner and unreachable,
                  f"chrome={chrome} chip={chip} banner={banner} overview={unreachable}")
        finally:
            ctx.close()

        # 2. U1 x U2: a killed stream on an error board shows staleness in the
        #    chip while the error banner keeps the board — and crucially nothing
        #    is drawn OVER the board, which is what made the old overlay harsh on
        #    a board that is already a solid colour.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?kiosk=1&board=cx-boom", wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".error-banner").count() == 1, timeout=10)
            page.route("**/api/stream", lambda route: route.abort())
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            stale = wait_for(
                lambda: page.locator('.fresh-chip[data-fresh="stale"]').count() == 1,
                timeout=8)
            both = bool(stale) and page.locator(".error-banner").count() == 1
            check("U4 stale chip + error banner coexist (neither clobbers the other)",
                  both, f"stale={bool(stale)} error={page.locator('.error-banner').count()}")
            check("U4 an error board is not covered when stale",
                  page.locator(".stale-overlay").count() == 0,
                  f"overlays={page.locator('.stale-overlay').count()}")
        finally:
            ctx.close()

        # 3. U1 x U2 x U4: reduced motion stops the pulse, every blinking dot,
        #    and the spinner (asserted on computed animation-name, not CSS text).
        ctx = br.new_context(viewport={"width": 430, "height": 900}, reduced_motion="reduce")
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".tab-error").count() == 1, timeout=10)

            def anim(sel):
                if page.locator(sel).count() == 0:
                    return "absent"
                return page.eval_on_selector(sel, "el => getComputedStyle(el).animationName")

            tab_dot = anim(".tab-error .tab-dot")
            banner_dot = anim(".error-banner-dot")
            chip_dot = anim(".fresh-chip-live .fresh-dot")
            spinner = page.evaluate("""() => {
              const el = document.createElement('span');
              el.className = 'spinner spinning';
              document.body.appendChild(el);
              const a = getComputedStyle(el).animationName;
              el.remove();
              return a;
            }""")
            check("U4 reduced-motion stops the error dot, banner dot, chip and spinner",
                  tab_dot == "none" and banner_dot == "none"
                  and chip_dot == "none" and spinner == "none",
                  f"tab-dot={tab_dot} banner-dot={banner_dot} "
                  f"chip-dot={chip_dot} spinner={spinner}")

            # Pulse: update the SELECTED board (the newest, cx-boom) so the
            # suppression check is not vacuous.
            api("PATCH", "/api/boards/cx-boom", {"subtitle": "tick"})
            page.wait_for_timeout(600)
            check("U4 reduced-motion suppresses the update pulse",
                  page.locator(".board-pulse").count() == 0,
                  f"board-pulse={page.locator('.board-pulse').count()}")
        finally:
            ctx.close()

        # 4. U2 x U3 x U4: g -> arrow -> Enter selects, closes, writes ?board=,
        #    and returns focus to the grid trigger.
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?board=cx-boom", wait_until="domcontentloaded")
            wait_for(lambda: page.locator(".tab").count() == 2, timeout=10)
            page.keyboard.press("g")
            opened = wait_for(lambda: page.locator(".board-overview").count() == 1, timeout=6)
            page.wait_for_timeout(100)
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(50)
            page.keyboard.press("Enter")
            closed = wait_for(lambda: page.locator(".board-overview").count() == 0, timeout=6)
            check("U4 keyboard g -> arrow -> Enter selects and closes the overview",
                  bool(opened and closed),
                  f"opened={bool(opened)} closed={bool(closed)}")
            active = page.evaluate(
                "() => (document.activeElement && document.activeElement.className) || ''")
            check("U4 focus returns to the overview trigger after close",
                  "overview-btn" in active, f"active={active!r}")
            check("U4 keyboard selection writes ?board=", query(page).get("board") == "cx-run",
                  f"url={page.url}")
        finally:
            ctx.close()

        # 5. U2 x U4: a deep-linked terminal board shows a ticking countdown;
        #    a non-terminal board shows none.
        delete_all()
        seed("ttl-done", {
            "title": "Done", "status": "done", "ttl": 1800,
            "tab": {"label": "Done", "accent": "violet"},
            "blocks": [{"type": "heading", "text": "Done body"}],
        })
        seed("ttl-run", {
            "title": "Run", "status": "running", "ttl": 1800,
            "tab": {"label": "Run", "accent": "violet"},
            "blocks": [{"type": "heading", "text": "Run body"}],
        })
        time.sleep(0.3)
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?board=ttl-done", wait_until="domcontentloaded")
            shown = wait_for(lambda: page.locator(".ttl-countdown").count() == 1, timeout=10)
            # Read the element only when it exists: a missing countdown is a GAP,
            # not an exception that would abort the remaining checks.
            text = page.locator(".ttl-countdown").inner_text() if shown else ""
            a1 = (float(page.get_attribute(".ttl-countdown", "data-expires-in-seconds") or -1)
                  if shown else -1.0)
            page.wait_for_timeout(2600)
            a2 = (float(page.get_attribute(".ttl-countdown", "data-expires-in-seconds") or -1)
                  if shown else -1.0)
            check("U4 terminal board shows a TTL countdown",
                  bool(shown) and "expires in" in text, f"text={text!r}")
            check("U4 TTL countdown ticks down on the client clock", a2 <= a1 - 2.0,
                  f"{a1} -> {a2}")
        finally:
            ctx.close()

        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(URL + "?board=ttl-run", wait_until="domcontentloaded")
            wait_for(lambda: "Run body" in body_text(page), timeout=10)
            page.wait_for_timeout(300)
            check("U4 non-terminal board shows no TTL countdown",
                  page.locator(".ttl-countdown").count() == 0,
                  f"countdowns={page.locator('.ttl-countdown').count()}")
        finally:
            ctx.close()
    finally:
        br.close()
        delete_all()


def main():
    print(f"verify_transport against {BASE}")
    # Fail loudly on a dead/mistyped target before this suite deletes any board.
    try:
        urllib.request.urlopen(BASE + "/health", timeout=4)
    except Exception as e:
        print(f"no Livewall backend at {BASE} (/health: {e}).")
        print("  set LIVEWALL_URL to a running backend, e.g.")
        print(f"  LIVEWALL_URL=http://127.0.0.1:8470 python {sys.argv[0]}")
        print("  (the default is a dead port so a bare run cannot touch a real one)")
        return 2
    # Never delete a board this suite did not create: refuse to run against a
    # backend that holds someone else's.
    preflight()
    with sync_playwright() as pw:
        delete_all()
        check_pull_to_refresh_empty(pw)
        check_multi_tab(pw)
        check_deeplink(pw)
        check_error_alarm(pw)
        check_kiosk(pw)
        check_connection_accounting(pw)
        check_theme(pw)
        check_hostile_block_degrades(pw)
        check_freshness(pw)
        check_auth_park(pw)
        check_responsive(pw)
        check_overview(pw)
        check_composition(pw)
        delete_all()

    gaps = [r for r in results if not r[1]]
    print(f"\n=== {len(results) - len(gaps)} pass, {len(gaps)} gap(s) ===")
    for name, _, detail in gaps:
        print(f"  GAP: {name} — {detail}")
    print("RESULT:", "PASS" if not gaps else "FAIL")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
