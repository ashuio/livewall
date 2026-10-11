/**
 * Theme preference: system (default) | light | dark.
 *
 * Persisted in localStorage under a stable key ('daylight-theme') so the choice
 * survives reloads. The resolved theme is written to <html data-theme>, which
 * the CSS keys off; an inline script in index.html applies it before first
 * paint so there is no flash.
 */

export type ThemePref = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

const STORAGE_KEY = 'daylight-theme';
const CYCLE: ThemePref[] = ['system', 'light', 'dark'];

export function readThemePref(): ThemePref {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    // localStorage can throw (private mode, storage disabled). The preference
    // is cosmetic and the caller has a safe default, so falling back to
    // 'system' is correct — there is nothing for the operator to act on.
    return 'system';
  }
}

export function resolveTheme(pref: ThemePref, prefersDark: boolean): ResolvedTheme {
  if (pref === 'light' || pref === 'dark') return pref;
  return prefersDark ? 'dark' : 'light';
}

/** Write the resolved theme to <html data-theme> and return it. */
export function applyTheme(pref: ThemePref): ResolvedTheme {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const resolved = resolveTheme(pref, mq.matches);
  document.documentElement.dataset.theme = resolved;
  return resolved;
}

export function setThemePref(pref: ThemePref): ResolvedTheme {
  try {
    window.localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    /* storage unavailable — apply for this session only */
  }
  return applyTheme(pref);
}

/** system -> light -> dark -> system. */
export function nextThemePref(pref: ThemePref): ThemePref {
  const idx = CYCLE.indexOf(pref);
  return CYCLE[(idx + 1) % CYCLE.length] ?? 'system';
}

export function themePrefLabel(pref: ThemePref): string {
  return pref === 'system' ? 'System' : pref === 'light' ? 'Light' : 'Dark';
}

/** Follow the OS while the preference is 'system'. Returns an unsubscribe fn. */
export function watchSystemTheme(onChange: () => void): () => void {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  mq.addEventListener('change', onChange);
  return () => mq.removeEventListener('change', onChange);
}
