/**
 * Livewall — the shell: app bar, tab bar, and the selected board's surface.
 *
 * Boards are one-off by design: an agent programs one for a running operation,
 * it lives on its TTL, and it goes away. There is no persistence to add here.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useLivewall } from './livewall/useLivewall';
import { renderBlock } from './livewall/components';
import { tabView } from './livewall/transport';
import { expiresInSeconds, formatAge, formatExpiry, freshness } from './livewall/freshness';
import { readToken } from './livewall/auth';
import {
  readViewParams,
  replaceSearch,
  writeViewParams,
} from './livewall/viewstate';
import type { BoardSummary } from './livewall/types';
import {
  applyTheme,
  nextThemePref,
  readThemePref,
  setThemePref,
  themePrefLabel,
  watchSystemTheme,
  type ThemePref,
} from './livewall/theme';
import { usePullToRefresh } from './livewall/usePullToRefresh';

/** Accent name -> CSS var, matching the backend's `tab.accent` vocabulary. */
function accentVar(accent: string): string {
  switch (accent) {
    case 'violet':
      return 'var(--violet)';
    case 'pink':
      return 'var(--pink)';
    case 'lime':
      return 'var(--lime)';
    case 'mint':
      return 'var(--mint)';
    case 'ok':
      return 'var(--success-ink)';
    case 'error':
      return 'var(--error-ink)';
    case 'warn':
      return 'var(--warning-ink)';
    default:
      return 'var(--ink-muted)';
  }
}

function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}
function AutoIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none" />
    </svg>
  );
}
function CloudIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17.5 19a4.5 4.5 0 0 0 .5-9 6 6 0 0 0-11.6 1.6A4 4 0 0 0 7 19z" />
      <path d="M9 15l2 2 4-4" />
    </svg>
  );
}
function CloudOffIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17.5 19a4.5 4.5 0 0 0 .5-9 6 6 0 0 0-9.5-4.6" />
      <path d="M7 19a4 4 0 0 1-.6-7.9" />
      <path d="M3 3l18 18" />
    </svg>
  );
}
function ExitIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </svg>
  );
}
function GridIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </svg>
  );
}
function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

/**
 * Board overview — every board at once, for when the tab strip is too cramped
 * to scan (12+ boards). It is a pure view over the summaries the shell already
 * holds: no fetch. Each card shows the board's tab label/accent/indicator, its
 * status, its U1 age (ticking on the client clock) and the U2 error alarm, so a
 * failed board is as unmissable here as in the tab bar. Selecting a card calls
 * `onSelect`, which the shell routes through the ONE URL writer (`selectBoard`).
 */
interface BoardOverviewProps {
  boards: BoardSummary[];
  selectedId: string | null;
  now: number | null;
  receivedAt: number | null;
  status: string | null;
  clientNow: number;
  onSelect: (id: string) => void;
  onClose: () => void;
}

function BoardOverview({
  boards,
  selectedId,
  now,
  receivedAt,
  status,
  clientNow,
  onSelect,
  onClose,
}: BoardOverviewProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const gridRef = useRef<HTMLDivElement | null>(null);

  // Initial focus: the selected card if there is one, else the first card,
  // else the close button. Landing on a card (not the close button) is what
  // lets the arrow keys move between boards immediately.
  useEffect(() => {
    const selectedCard = gridRef.current?.querySelector<HTMLElement>('.overview-card-selected');
    const firstCard = gridRef.current?.querySelector<HTMLElement>('.overview-card');
    (selectedCard ?? firstCard ?? closeRef.current)?.focus();
  }, []);

  // Focus trap + grid navigation. Tab wraps within the dialog so an overlay
  // never lets focus wander into the page behind it; arrows move between cards
  // and Enter/Space activate the focused card (they are buttons).
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Tab') {
      const root = dialogRef.current;
      if (!root) return;
      const focusables = Array.from(
        root.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !root.contains(active)) {
        e.preventDefault();
        first.focus();
      }
      return;
    }
    const grid = gridRef.current;
    if (!grid) return;
    const cards = Array.from(grid.querySelectorAll<HTMLButtonElement>('.overview-card'));
    if (cards.length === 0) return;
    const idx = cards.indexOf(document.activeElement as HTMLButtonElement);
    if (idx === -1) return;
    let next = idx;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = Math.min(cards.length - 1, idx + 1);
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = Math.max(0, idx - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = cards.length - 1;
    else return;
    e.preventDefault();
    cards[next].focus();
  };

  return (
    <div className="overview-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="board-overview"
        role="dialog"
        aria-modal="true"
        aria-label="All boards"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="overview-head">
          <span className="overview-title">All boards</span>
          <span className="overview-count">{boards.length}</span>
          <button
            ref={closeRef}
            className="icon-btn overview-close"
            aria-label="Close board overview"
            onClick={onClose}
          >
            <CloseIcon />
          </button>
        </div>
        <div className="overview-grid" ref={gridRef}>
          {boards.map((b) => {
            const tv = tabView(b);
            const isError = b.status === 'error';
            const isSel = b.id === selectedId;
            const fresh = freshness({ board: b, now, receivedAt, status, clientNow });
            return (
              <button
                key={b.id}
                className={`overview-card${isSel ? ' overview-card-selected' : ''}${isError ? ' overview-card-error' : ''}`}
                data-error={isError ? 'true' : undefined}
                data-board-id={b.id}
                data-age-seconds={fresh.boardAge.toFixed(1)}
                onClick={() => onSelect(b.id)}
              >
                <span className="overview-card-head">
                  <span
                    className="tab-dot"
                    style={{ background: isError ? 'var(--error-ink)' : accentVar(tv.accent) }}
                  />
                  <span className="overview-card-title">{tv.label}</span>
                  {isError ? <span className="overview-error-tag">ERROR</span> : null}
                </span>
                <span className="overview-card-meta">
                  <span className="overview-status">{b.status}</span>
                  <span className="overview-age">
                    {fresh.stale ? 'stale' : 'updated'} {formatAge(fresh.boardAge)}
                  </span>
                </span>
                {tv.kind === 'bar' && tv.pct != null ? (
                  <span className="overview-track">
                    <span className="overview-fill" style={{ width: `${tv.pct}%` }} />
                  </span>
                ) : null}
                {tv.kind === 'pct' && tv.pct != null ? (
                  <span className="overview-ind">{tv.pct.toFixed(0)}%</span>
                ) : null}
                {tv.kind === 'text' && tv.text ? (
                  <span className="overview-badge">{tv.text}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/**
 * The selected board's body: its title and subtitle, then each block in order.
 * A board with nothing programmed (no title, no subtitle, no blocks) shows the
 * waiting line instead of an empty panel. Rendering is delegated to
 * `renderBlock`, which is total, so one malformed block cannot blank the board.
 */
function BoardView({ board }: { board: BoardSummary }) {
  const blocks = board.blocks;
  if (!board.title && !board.subtitle && blocks.length === 0) {
    return <div className="empty">waiting for the agent to program the board…</div>;
  }
  return (
    <div className="board-body">
      {board.title ? <h1 className="lw-board-title">{board.title}</h1> : null}
      {board.subtitle ? <p className="lw-board-subtitle">{board.subtitle}</p> : null}
      {blocks.map((block, i) => (
        <div key={i}>{renderBlock(block)}</div>
      ))}
    </div>
  );
}

/** How long the update-pulse stays lit, in ms. */
const PULSE_MS = 900;

/** Re-render on a fixed cadence so clock-derived ages tick without new data. */
function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Honour the OS reduced-motion setting for JS-driven effects. Total: false if unavailable. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

export default function App() {
  const {
    boards,
    status,
    refresh,
    now: snapshotNow,
    receivedAt,
  } = useLivewall();
  const clientNow = useNow();

  const [pref, setPref] = useState<ThemePref>(() => readThemePref());
  // Deep-link seed: read `?board=` once, at mount. The board may not exist yet
  // (the first snapshot has not arrived), so a named board starts PINNED; the
  // selection effect below falls back to the newest if it never appears.
  const [initialView] = useState(() =>
    readViewParams(typeof window !== 'undefined' ? window.location.search : ''),
  );
  const [selectedId, setSelectedId] = useState<string | null>(initialView.board);
  const [pinned, setPinned] = useState(initialView.board != null);
  const [notFound, setNotFound] = useState<string | null>(null);
  const [kiosk, setKiosk] = useState(initialView.kiosk);
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [pulsing, setPulsing] = useState<Set<string>>(() => new Set());

  const pendingBoard = useRef<string | null>(initialView.board);

  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const overviewBtnRef = useRef<HTMLButtonElement | null>(null);
  const pull = usePullToRefresh(scrollerRef, refresh);

  // Theme: apply on change, and follow the OS while on 'system'.
  useEffect(() => {
    applyTheme(pref);
    if (pref !== 'system') return;
    return watchSystemTheme(() => applyTheme(pref));
  }, [pref]);

  const cycleTheme = useCallback(() => {
    setPref((p) => {
      const next = nextThemePref(p);
      setThemePref(next);
      return next;
    });
  }, []);

  // The ONE writer of the address bar: persist+strip `?token=` (auth) while
  // keeping `?board=`/`?kiosk=` in the same write. No other effect touches it,
  // so the params cannot clobber each other.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const { cleanSearch } = readToken(window.location.search, window.localStorage);
    replaceSearch(cleanSearch);
  }, []);

  // Selection: follow the newest board unless the user pinned one (a deep-link
  // starts pinned). While no boards exist yet, a pinned selection is kept
  // PENDING so the first snapshot can still satisfy the deep-link.
  useEffect(() => {
    if (boards.length === 0) {
      if (!pinned) setSelectedId(null);
      return;
    }
    const stillThere = selectedId != null && boards.some((b) => b.id === selectedId);
    if (!stillThere) {
      setSelectedId(boards[0].id);
      setPinned(false);
    } else if (!pinned) {
      setSelectedId(boards[0].id);
    }
  }, [boards, selectedId, pinned]);

  // Resolve a pending deep-link once the first boards arrive: if the named
  // board is present it stays selected; otherwise the effect above falls back
  // to the newest, we say so quietly, and the stale id leaves the address bar.
  useEffect(() => {
    if (boards.length === 0 || pendingBoard.current == null) return;
    const pending = pendingBoard.current;
    pendingBoard.current = null;
    if (!boards.some((b) => b.id === pending)) {
      setNotFound(pending);
      if (typeof window !== 'undefined') {
        replaceSearch(writeViewParams(window.location.search, { board: null }));
      }
    }
  }, [boards]);

  // A user's tab click: pin the board and reflect it in the address bar so the
  // URL is shareable.
  const selectBoard = useCallback((id: string) => {
    setSelectedId(id);
    setPinned(true);
    setNotFound(null);
    if (typeof window !== 'undefined') {
      replaceSearch(writeViewParams(window.location.search, { board: id }));
    }
  }, []);

  // Leave kiosk: drop the param (keeping board) and restore the chrome. The
  // Escape key is the documented keyboard way out for a passer-by.
  const exitKiosk = useCallback(() => {
    setKiosk(false);
    if (typeof window !== 'undefined') {
      replaceSearch(writeViewParams(window.location.search, { kiosk: false }));
    }
  }, []);

  useEffect(() => {
    if (!kiosk) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') exitKiosk();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [kiosk, exitKiosk]);

  // The overview is chrome for scale: it is closed the moment it cannot apply
  // (kiosk hides it, and one board has nothing to choose between).
  useEffect(() => {
    if (kiosk || boards.length <= 1) setOverviewOpen(false);
  }, [kiosk, boards.length]);

  // Keyboard route to the overview: `g` toggles it. Ignore keystrokes aimed at
  // a text field so it never steals a character from a board.
  useEffect(() => {
    if (kiosk) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {
        return;
      }
      if (e.key === 'g' || e.key === 'G') setOverviewOpen((open) => !open);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [kiosk]);

  // Escape closes the overview. It does not touch the kiosk Escape path above:
  // the overview is never open in kiosk.
  useEffect(() => {
    if (!overviewOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOverviewOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [overviewOpen]);

  // Restore focus to the trigger when the overview closes — whether via Escape,
  // a card selection, or the `g` toggle — so keyboard users are not dropped at
  // the top of the document.
  const overviewWasOpen = useRef(false);
  useEffect(() => {
    if (overviewWasOpen.current && !overviewOpen) overviewBtnRef.current?.focus();
    overviewWasOpen.current = overviewOpen;
  }, [overviewOpen]);

  // Arrow keys move between tabs (the tablist convention); Enter/Space select
  // natively because each tab is a button. Home/End jump to the ends. Roving
  // tabindex (below) keeps only the selected tab in the Tab order.
  const onTabsKeyDown = useCallback((e: React.KeyboardEvent<HTMLElement>) => {
    const tabs = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('.tab'));
    if (tabs.length === 0) return;
    const idx = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (idx === -1) return;
    let next = idx;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % tabs.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    else return;
    e.preventDefault();
    tabs[next].focus();
  }, []);

  const selected: BoardSummary | null = useMemo(
    () => boards.find((b) => b.id === selectedId) ?? null,
    [boards, selectedId],
  );

  const connected = status == null;

  // Freshness: board age ticks on the client clock; stale is a dead transport
  // or data older than the keepalive-derived threshold.
  const fresh = freshness({ board: selected, now: snapshotNow, receivedAt, status, clientNow });
  const stale = fresh.stale;

  // Terminal boards (done/error) carry `expires_in` (seconds to prune); surface
  // a subtle countdown that ticks on the client clock like the age. Null — and
  // therefore hidden — for non-terminal boards.
  const ttlRemaining = expiresInSeconds(selected?.expires_in, receivedAt, clientNow);
  const ttlLabel = ttlRemaining == null ? '' : formatExpiry(ttlRemaining);

  // Update-pulse: light the board briefly when its `updated` changes between
  // snapshots. Never on first render (the ref starts empty), and never under
  // reduced-motion.
  const prevUpdated = useRef<Map<string, number>>(new Map());
  useEffect(() => {
    const prev = prevUpdated.current;
    const changed = boards
      .filter((b) => prev.has(b.id) && prev.get(b.id) !== b.updated)
      .map((b) => b.id);
    prevUpdated.current = new Map(boards.map((b) => [b.id, b.updated]));
    if (changed.length === 0 || prefersReducedMotion()) return;
    setPulsing(new Set(changed));
    const timer = setTimeout(() => setPulsing(new Set()), PULSE_MS);
    return () => clearTimeout(timer);
  }, [boards]);

  const selectedPulses = selected != null && pulsing.has(selected.id);

  return (
    <div className="app">
      <header className="appbar">
        <div className="appbar-title">Livewall</div>
        <div className="appbar-right">
          {selected ? (
            <div className="appbar-status">
              <span
                className={`fresh-chip ${stale ? 'fresh-chip-stale' : 'fresh-chip-live'}`}
                data-fresh={stale ? 'stale' : 'live'}
                data-age-seconds={fresh.boardAge.toFixed(1)}
                title={stale ? 'Not receiving live updates' : 'Streaming live'}
              >
                <span className="fresh-dot" aria-hidden="true" />
                {stale ? 'stale' : 'live'} · updated {formatAge(fresh.boardAge)}
              </span>
              {ttlLabel ? (
                <span
                  className="ttl-countdown"
                  data-expires-in-seconds={ttlRemaining!.toFixed(1)}
                >
                  {ttlLabel}
                </span>
              ) : null}
            </div>
          ) : null}
          {!kiosk ? (
            <div className="appbar-actions">
              {boards.length > 1 ? (
                <button
                  className="icon-btn overview-btn"
                  title="All boards (g)"
                  aria-label="All boards"
                  aria-expanded={overviewOpen}
                  ref={overviewBtnRef}
                  onClick={() => setOverviewOpen(true)}
                >
                  <GridIcon />
                </button>
              ) : null}
              {boards.length > 1 ? <span className="appbar-count">{boards.length} boards</span> : null}
              <span
                className={`icon-btn icon-status${connected ? '' : ' icon-btn-error'}`}
                role="status"
                title={status ?? 'Connected'}
                aria-label={status ?? 'Connected'}
              >
                {connected ? <CloudIcon /> : <CloudOffIcon />}
              </span>
              <button
                className="icon-btn"
                title={`Theme: ${themePrefLabel(pref)}`}
                aria-label={`Theme: ${themePrefLabel(pref)}`}
                onClick={cycleTheme}
              >
                {pref === 'light' ? <SunIcon /> : pref === 'dark' ? <MoonIcon /> : <AutoIcon />}
              </button>
            </div>
          ) : (
            <button
              className="icon-btn kiosk-exit"
              title="Exit kiosk (Esc)"
              aria-label="Exit kiosk (Esc)"
              onClick={exitKiosk}
            >
              <ExitIcon />
            </button>
          )}
        </div>
      </header>

      {!kiosk && boards.length > 1 ? (
        <nav className="tabs" role="tablist" aria-label="Boards" onKeyDown={onTabsKeyDown}>
          {boards.map((b, i) => {
            const tv = tabView(b);
            const isSel = b.id === selectedId;
            const isError = b.status === 'error';
            return (
              <button
                key={b.id}
                id={`tab-${b.id}`}
                role="tab"
                aria-selected={isSel}
                aria-controls="board-panel"
                className={`tab${isSel ? ' tab-selected' : ''}${isError ? ' tab-error' : ''}`}
                data-error={isError ? 'true' : undefined}
                tabIndex={isSel || (selectedId == null && i === 0) ? 0 : -1}
                onClick={() => selectBoard(b.id)}
              >
                <span
                  className="tab-dot"
                  style={{
                    background: isError
                      ? isSel
                        ? 'var(--paper)'
                        : 'var(--error-ink)'
                      : accentVar(tv.accent),
                  }}
                />
                <span className="tab-label">{tv.label}</span>
                {/* Error must not be colour-only (WCAG 1.4.1): the red chrome
                    and blinking dot get a text cue, the same tag the overview
                    card shows. */}
                {isError ? <span className="tab-error-tag">ERROR</span> : null}
                {tv.kind === 'bar' && tv.pct != null ? (
                  <>
                    <span className="tab-track">
                      <span className="tab-fill" style={{ width: `${tv.pct}%` }} />
                    </span>
                    <span className="tab-ind">{tv.pct.toFixed(0)}%</span>
                  </>
                ) : null}
                {tv.kind === 'pct' && tv.pct != null ? (
                  <span className="tab-ind">{tv.pct.toFixed(0)}%</span>
                ) : null}
                {tv.kind === 'text' && tv.text ? <span className="tab-badge">{tv.text}</span> : null}
              </button>
            );
          })}
        </nav>
      ) : null}

      {notFound ? (
        <div className="board-notice" role="status">
          board &lsquo;{notFound}&rsquo; not found &mdash; following the newest
        </div>
      ) : null}

      <div className="board-area">
        {selected?.status === 'error' ? (
          <div className="error-banner" role="alert" data-error="true">
            <span className="error-banner-dot" aria-hidden="true" />
            ERROR — {selected.title || selected.id}
          </div>
        ) : null}
        <main className="board-scroll" ref={scrollerRef} {...pull.handlers}>
          <div
            className="pull-indicator"
            style={{ height: `${pull.distance}px`, opacity: pull.distance > 0 ? 1 : 0 }}
            aria-hidden="true"
          >
            <span className={`spinner${pull.refreshing ? ' spinning' : ''}`} />
          </div>

          <div
            id="board-panel"
            role="tabpanel"
            aria-labelledby={
              !kiosk && boards.length > 1 && selected ? `tab-${selected.id}` : undefined
            }
          >
            {selected == null ? (
              status ? (
                /* A transport problem with nothing to show must be VISIBLE
                   body text, not just the icon's hover-only title: pre-fix a
                   parked 401 rendered the normal empty state ("waiting for an
                   agent…") and a stranger had no way to learn they needed
                   ?token= — or that the backend was down at all. The status
                   strings already name the problem and the fix (e.g. "backend
                   returned HTTP 401 — not retrying; reload with a valid
                   ?token="). A healthy empty page (status null) keeps the
                   ordinary empty state. */
                <div className="board-notice" role="status">{status}</div>
              ) : (
                <div className="empty">waiting for an agent to program a board…</div>
              )
            ) : (
              <div
                className={`board${kiosk ? ' board-kiosk' : ''}${selectedPulses ? ' board-pulse' : ''}`}
                data-pulse={selectedPulses ? 'true' : undefined}
              >
                <BoardView board={selected} />
              </div>
            )}
          </div>
        </main>

        {/*
          Staleness lives in the top chip ONLY. It used to raise a full-area
          overlay with a banner, which tinted and covered the board — intrusive
          on a board that is already a solid colour (an error board, a status
          wall), where it competed with the content instead of annotating it.
          The chip is the single source of that state; this live region keeps it
          announced to screen readers without adding anything visual.
        */}
        <div className="sr-only" role="status" aria-live="polite">
          {stale ? 'Not receiving live updates' : ''}
        </div>
      </div>

      {!kiosk && overviewOpen && boards.length > 1 ? (
        <BoardOverview
          boards={boards}
          selectedId={selectedId}
          now={snapshotNow}
          receivedAt={receivedAt}
          status={status}
          clientNow={clientNow}
          onSelect={(id) => {
            selectBoard(id);
            setOverviewOpen(false);
          }}
          onClose={() => setOverviewOpen(false)}
        />
      ) : null}
    </div>
  );
}
