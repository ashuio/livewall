/**
 * View-state — the address bar as the client's view state.
 *
 * Two knobs live in the query string:
 *   - `?board=<id>`  deep-links a board (pins it; the tab bar then reflects it)
 *   - `?kiosk=1`     chrome-free single-board mode for a wall
 *
 * The pure parsing/writing is separated from the DOM (like `auth.ts` and
 * `freshness.ts`) so composition with the token handling can be unit-tested
 * without a browser.
 *
 * OWNERSHIP: `replaceSearch` is the ONE writer of `window.location.search`.
 * `auth.ts` deliberately no longer rewrites the URL; the shell reads the token
 * with `readToken` (which persists it and returns the search with `token`
 * stripped) and hands that result to `replaceSearch`. One write strips the
 * token and keeps `board`/`kiosk`; there is no second writer to race it.
 */

export const BOARD_PARAM = 'board';
export const KIOSK_PARAM = 'kiosk';

export interface ViewParams {
  /** Deep-linked board id, or null when none is named. */
  board: string | null;
  /** True only for `?kiosk=1`; `?kiosk=0` and absent are normal mode. */
  kiosk: boolean;
}

/**
 * Parse `board`/`kiosk` out of a URL search string. TOTAL: a malformed or empty
 * search yields the defaults rather than throwing.
 *
 * An empty `?board=` is treated as "no board" (not an id of `''`), so a stray
 * `?board=` cannot pin the shell to a board that can never exist.
 */
export function readViewParams(search: string): ViewParams {
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(search);
  } catch {
    return { board: null, kiosk: false };
  }
  const raw = params.get(BOARD_PARAM);
  return {
    board: raw != null && raw !== '' ? raw : null,
    kiosk: params.get(KIOSK_PARAM) === '1',
  };
}

/**
 * Return `search` with `board`/`kiosk` updated, preserving every other
 * parameter (e.g. `token`, `theme`) and the order it appeared in.
 *
 * `board: null` (or `''`) removes the parameter; `kiosk: false` removes it.
 * Always returns a leading-`?` string, or `''` when nothing is left.
 */
export function writeViewParams(
  search: string,
  patch: { board?: string | null; kiosk?: boolean },
): string {
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(search);
  } catch {
    params = new URLSearchParams();
  }
  if (patch.board !== undefined) {
    if (patch.board) params.set(BOARD_PARAM, patch.board);
    else params.delete(BOARD_PARAM);
  }
  if (patch.kiosk !== undefined) {
    if (patch.kiosk) params.set(KIOSK_PARAM, '1');
    else params.delete(KIOSK_PARAM);
  }
  const rest = params.toString();
  return rest ? `?${rest}` : '';
}

/**
 * Replace the current URL's search without adding a history entry, returning
 * the search that was applied. This is the ONLY place the client writes the
 * address bar. Non-fatal if `history` is unavailable (the URL is cosmetic).
 */
export function replaceSearch(search: string): string {
  if (typeof window === 'undefined') return search;
  if (search !== window.location.search) {
    try {
      window.history.replaceState(
        {},
        '',
        window.location.pathname + search + window.location.hash,
      );
    } catch {
      /* non-fatal: the view is already correct in memory */
    }
  }
  return search;
}
