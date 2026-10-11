/**
 * Livewall transport — streams the multi-board snapshot over SSE.
 *
 * Deliberately NOT `EventSource`:
 *  - EventSource's reconnect is opaque and it cannot detect a SILENTLY dead
 *    stream (a connection that stays open but stops delivering). Mobile
 *    browsers do exactly that when a tab is backgrounded.
 *  - It gives no hook for the "resumed from background, re-sync now" case.
 *
 * So this is a fetch-based SSE reader with three things EventSource cannot give
 * us: a retry loop, a stall watchdog, and a visibility-resume reconnect.
 * Getting these wrong is what left the old board permanently stale after a
 * phone sleep, so they are not optional.
 */

import type { Block, BoardSummary, Snapshot, TabView } from './types';
// The explicit `.ts` extension is required: the Node unit-test runner strips
// types and resolves value imports literally (no extensionless fallback), and
// `tsconfig` sets allowImportingTsExtensions so the build accepts it too.
import { resolveToken } from './auth.ts';

const RETRY_MS = 2000;
const WATCHDOG_MS = 10_000;
const STALL_MS = 45_000; // server keepalive is 15s; 45s of silence = dead

export type StatusListener = (problem: string | null) => void;
/**
 * Fired for every snapshot. `now` is the server clock (epoch seconds) the
 * snapshot was measured against, or `null` if the frame omitted it; `receivedAt`
 * is the client's own clock (epoch ms) at the moment the frame arrived. The two
 * are never compared to each other — the shell advances each board's age by
 * `board.age + (clientNow - receivedAt)`, which is skew-free.
 */
export type BoardsListener = (
  boards: BoardSummary[],
  config: Snapshot['config'],
  now: number | null,
  receivedAt: number,
) => void;

/** Normalise a raw server board object into a BoardSummary. */
function toSummary(raw: Record<string, unknown>): BoardSummary {
  const tab = (raw.tab as Record<string, unknown>) ?? {};
  return {
    id: String(raw.id ?? ''),
    title: String(raw.title ?? ''),
    subtitle: String(raw.subtitle ?? ''),
    status: (raw.status as BoardSummary['status']) ?? 'idle',
    updated: Number(raw.updated ?? 0),
    age: typeof raw.age === 'number' && Number.isFinite(raw.age) ? raw.age : 0,
    expires_in: typeof raw.expires_in === 'number' ? raw.expires_in : null,
    progress: typeof raw.progress === 'number' ? raw.progress : null,
    tab: tab as BoardSummary['tab'],
    blocks: Array.isArray(raw.blocks) ? (raw.blocks as Block[]) : [],
  };
}

/** Derive the tab-bar display data from a board's agent-authored tab spec. */
export function tabView(b: BoardSummary): TabView {
  const t = b.tab ?? {};
  const ind = t.indicator ?? {};
  const kind = (ind.kind ?? 'none') as 'bar' | 'pct' | 'text' | 'none';
  let pct: number | null = null;
  if (kind === 'pct') pct = Number(ind.value ?? 0);
  if (kind === 'bar') {
    const max = !ind.max || ind.max === 0 ? 100 : Number(ind.max);
    pct = (Number(ind.value ?? 0) / max) * 100;
  }
  return {
    label: String(t.label ?? b.title ?? b.id),
    accent: String(t.accent ?? 'muted'),
    kind,
    pct,
    text: String(ind.text ?? ''),
  };
}

export class LivewallTransport {
  private baseUrl: string;
  private abort: AbortController | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private lastLine = Date.now();
  private closed = false;
  private connecting = false;
  private onVisibility: (() => void) | null = null;

  /**
   * Bumped every time a connection is torn down. Each connect() captures the
   * epoch it started under; a callback whose epoch is stale belongs to a
   * superseded connection and must do nothing. Without this, aborting the old
   * fetch to reconnect lands in the old connect()'s catch, which mistakes the
   * self-inflicted AbortError for a backend failure, schedules a retry and
   * tears down the FRESH connection — a visibility resume then costs two extra
   * connections and leaves the surface briefly dead.
   */
  private epoch = 0;

  /** Set while a refresh() is waiting for the next snapshot. */
  private refreshWaiters: Array<() => void> = [];

  /** Bearer token, if the backend requires one. Resolved once at construction. */
  private token: string;

  /**
   * True while the CURRENT status describes a frame-level parse problem.
   * Frame problems are transient — the connection itself is fine — so the
   * next frame that parses cleanly must clear the status again. Without this
   * tracking, one malformed payload left `status` non-null forever: the chip
   * read "stale" while good snapshots kept arriving, and only a reconnect
   * (which re-reports connected) could clear it.
   */
  private frameError = false;

  /**
   * True while parked on a DETERMINISTIC HTTP failure (401/403). Retrying
   * cannot succeed until the credential changes, and the credential is
   * resolved once at construction — so the honest behaviour is to stop, keep
   * the problem in the status, and let an explicit reconnect (refresh,
   * visibility resume) try again. Without the park, a wrong token retried
   * every RETRY_MS forever: a per-client 0.5 QPS hammer on the auth path for
   * as long as the tab stayed open.
   */
  private parked = false;

  private handlers: {
    onBoards: BoardsListener;
    onStatus: StatusListener;
  };

  constructor(
    baseUrl: string,
    handlers: {
      onBoards: BoardsListener;
      onStatus: StatusListener;
    },
  ) {
    this.baseUrl = baseUrl;
    this.handlers = handlers;
    this.token = resolveToken();
  }

  private endpoint(path: string): string {
    // Empty baseUrl = same origin (works on the direct port AND behind Traefik).
    return this.baseUrl ? `${this.baseUrl}${path}` : path;
  }

  start(): void {
    if (this.closed) return;
    void this.connect();

    this.watchdog = setInterval(() => {
      if (this.closed || this.parked) return;
      if (Date.now() - this.lastLine > STALL_MS) {
        this.handlers.onStatus('stream stalled — reconnecting');
        this.reconnect();
      }
    }, WATCHDOG_MS);

    // A backgrounded mobile tab drops its stream; re-sync on return.
    this.onVisibility = () => {
      if (document.visibilityState === 'visible') this.reconnect();
    };
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  reconnect(): void {
    if (this.closed) return;
    this.teardown();
    void this.connect();
  }

  /**
   * Force a fresh connection and resolve when the next snapshot lands, so a
   * caller's spinner ends when data is actually in. Never hangs: the timeout
   * completes it anyway (the connection keeps retrying regardless).
   */
  refresh(timeoutMs = 4000): Promise<void> {
    if (this.closed) return Promise.resolve();
    return new Promise<void>((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this.refreshWaiters = this.refreshWaiters.filter((w) => w !== finish);
        resolve();
      };
      const timer = setTimeout(finish, timeoutMs);
      this.refreshWaiters.push(finish);
      this.reconnect();
    });
  }

  private async connect(): Promise<void> {
    if (this.closed || this.connecting) return;
    this.connecting = true;
    const epoch = this.epoch;
    const controller = new AbortController();
    this.abort = controller;
    this.lastLine = Date.now();

    try {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
        'Cache-Control': 'no-cache',
      };
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      const res = await fetch(this.endpoint('/api/stream'), {
        headers,
        signal: controller.signal,
      });
      // A reconnect (or dispose) while we were awaiting supersedes this
      // attempt: do not touch state, do not schedule a retry.
      if (this.closed || epoch !== this.epoch) return;
      if (!res.ok) {
        if (res.status === 401 || res.status === 403) {
          // Deterministic credential failure: retrying cannot succeed (the
          // token is resolved once, at construction). Park: surface the
          // status, schedule nothing. An explicit reconnect (refresh(),
          // visibility resume) may still attempt — and re-parks on the same
          // answer. The park lifts on the next successful connect.
          this.teardown();
          this.parked = true;
          this.handlers.onStatus(
            `backend returned HTTP ${res.status} — not retrying; reload with a valid ?token=`,
          );
          return;
        }
        this.handlers.onStatus(`backend returned HTTP ${res.status}`);
        this.scheduleRetry(epoch);
        return;
      }
      this.parked = false;
      this.connecting = false;
      this.handlers.onStatus(null); // connected

      const reader = res.body?.getReader();
      if (!reader) {
        this.handlers.onStatus('stream has no body');
        this.scheduleRetry(epoch);
        return;
      }
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { value, done } = await reader.read();
        if (this.closed || epoch !== this.epoch) return;
        if (done) break;
        this.lastLine = Date.now();
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line.
        let idx: number;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          this.handleFrame(frame);
        }
      }
      // Stream ended without an error — treat as a drop. Say so: a clean EOF
      // (a proxy closing an idle connection, a server restart) is still not a
      // connected stream, and the shell must be able to tell that from live.
      this.handlers.onStatus('stream ended — reconnecting');
      this.scheduleRetry(epoch);
    } catch (err) {
      // A stale epoch means this rejection came from our own teardown; it is
      // not a backend failure and must not abort the replacement connection.
      if (this.closed || epoch !== this.epoch) return;
      this.handlers.onStatus(`cannot reach backend: ${String(err)}`);
      this.scheduleRetry(epoch);
    }
  }

  private handleFrame(frame: string): void {
    // Errors seen while parsing THIS frame. They are reported through the
    // status channel, but they must not outlive the frame: the next frame
    // that parses cleanly clears the status (the stream is demonstrably
    // healthy again). A frame whose every board failed is not dispatched, so
    // its error legitimately persists until a good frame arrives.
    let frameFailed = false;
    const fail = (message: string): void => {
      frameFailed = true;
      this.frameError = true;
      this.handlers.onStatus(message);
    };
    for (const rawLine of frame.split('\n')) {
      const line = rawLine.trimEnd();
      if (!line.startsWith('data:')) continue; // ': keepalive' etc.
      const payload = line.slice(5).trim();
      if (!payload) continue;
      let decoded: Record<string, unknown>;
      try {
        decoded = JSON.parse(payload) as Record<string, unknown>;
      } catch (err) {
        // Surfaced via onStatus (the app bar shows it); the last good view is
        // kept rather than blanking the surface. Not a silent swallow.
        fail(`bad payload: ${String(err)}`);
        continue;
      }
      if (decoded === null || typeof decoded !== 'object' || Array.isArray(decoded)) {
        fail('bad payload: expected a JSON object');
        continue;
      }
      const rawBoards = decoded.boards;
      if (!Array.isArray(rawBoards)) {
        fail('bad payload: missing `boards` array');
        continue;
      }
      const list: BoardSummary[] = [];
      for (const raw of rawBoards) {
        try {
          list.push(toSummary(raw as Record<string, unknown>));
        } catch (err) {
          // Surfaced via onStatus; the good boards in this frame are kept
          // (the per-board catch exists so one bad board cannot drop them all).
          fail(`bad board in payload: ${String(err)}`);
        }
      }
      // Boards arrived but none parsed: the frame is malformed. Keep the last
      // good view instead of blanking the surface, and say so.
      if (rawBoards.length > 0 && list.length === 0) continue;
      const config =
        decoded.config !== null && typeof decoded.config === 'object'
          ? (decoded.config as Snapshot['config'])
          : {};
      const now =
        typeof decoded.now === 'number' && Number.isFinite(decoded.now)
          ? decoded.now
          : null;
      // This frame parsed cleanly: a frame-level error reported earlier on
      // this connection is over — say so, or the chip would keep claiming the
      // wall is stale while fresh snapshots keep landing.
      if (!frameFailed && this.frameError) {
        this.frameError = false;
        this.handlers.onStatus(null);
      }
      this.handlers.onBoards(list, config, now, Date.now());
      for (const w of [...this.refreshWaiters]) w();
    }
  }

  private scheduleRetry(epoch?: number): void {
    if (this.closed) return;
    // A retry requested by a superseded connection is ignored: the current
    // connection owns its own lifecycle.
    if (epoch !== undefined && epoch !== this.epoch) return;
    this.teardown();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, RETRY_MS);
  }

  private teardown(): void {
    this.epoch += 1; // invalidate any callbacks still in flight
    this.abort?.abort();
    this.abort = null;
    this.connecting = false;
    // Cancel any pending retry too: teardown is called by reconnect()/refresh()
    // as well as by scheduleRetry(), and a stale retry firing after a fresh
    // connection is up would open a second, untracked stream.
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  dispose(): void {
    this.closed = true;
    if (this.watchdog) {
      clearInterval(this.watchdog);
      this.watchdog = null;
    }
    if (this.onVisibility) document.removeEventListener('visibilitychange', this.onVisibility);
    this.teardown();
    // Release anyone awaiting refresh() rather than making them wait out their
    // timeout after the component is already gone.
    for (const w of [...this.refreshWaiters]) w();
    this.refreshWaiters = [];
  }
}
