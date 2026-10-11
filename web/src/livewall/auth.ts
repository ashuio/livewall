/**
 * API token handling for the client.
 *
 * The backend gates `/api/*` when it is run with `LIVEWALL_TOKEN` set. The
 * client is served as a static bundle (never gated), so it needs to learn the
 * token another way: once, from a `?token=` query parameter, which it persists
 * to localStorage and then strips from the URL. Every subsequent fetch sends it
 * as an `Authorization: Bearer` header.
 *
 * The pure part (`readToken`) is separated from the DOM so it is unit-testable
 * without a browser.
 */

export const TOKEN_KEY = 'livewall-token';

export interface TokenStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/**
 * Pure: derive the token from a URL search string and a store.
 *
 * A `token` query parameter wins (and is written to the store, and returned as
 * `cleanSearch` without it); otherwise the stored value is used. Never throws.
 */
export function readToken(
  search: string,
  store: TokenStore,
): { token: string; cleanSearch: string } {
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(search);
  } catch {
    return { token: safeGet(store), cleanSearch: search };
  }
  const fromUrl = params.get('token');
  if (fromUrl) {
    try {
      store.setItem(TOKEN_KEY, fromUrl);
    } catch {
      /* storage may be unavailable (private mode); the token still works */
    }
    params.delete('token');
    const rest = params.toString();
    return { token: fromUrl, cleanSearch: rest ? `?${rest}` : '' };
  }
  return { token: safeGet(store), cleanSearch: search };
}

function safeGet(store: TokenStore): string {
  try {
    return store.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

/**
 * Resolve the token from the live page and persist a `?token=` to the store.
 *
 * It deliberately does NOT rewrite the address bar. The shell owns the URL (see
 * `viewstate.ts`'s `replaceSearch`), which strips `token` and preserves the
 * `board`/`kiosk` params in ONE write. Rewriting here as well would be a second
 * writer racing the shell's, and could clobber a deep-link.
 */
export function resolveToken(): string {
  if (typeof window === 'undefined') return '';
  return readToken(window.location.search, window.localStorage).token;
}
