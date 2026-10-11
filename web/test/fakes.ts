/**
 * Deterministic test fakes for the Livewall transport.
 *
 * The transport's hard parts are timers and connection lifetime, so the tests
 * run it against a controllable clock and a controllable byte stream rather
 * than sleeping and hoping. Nothing here uses real time: `advance(ms)` runs
 * exactly the timers that are due, and `push()`/`close()` deliver exactly the
 * bytes/EOF we choose.
 *
 * The fake fetch mimics the one property that matters: aborting the signal
 * rejects the *pending reader.read()* with an AbortError, exactly as a real
 * fetch body does. That is the path the stale-abort bug lived on.
 */

type Timer = { fn: () => void; at: number; interval: number | null };

export class FakeClock {
  now = 0;
  private timers = new Map<number, Timer>();
  private nextId = 1;

  install(): this {
    const g = globalThis as unknown as {
      setTimeout: (fn: () => void, ms?: number) => number;
      setInterval: (fn: () => void, ms?: number) => number;
      clearTimeout: (id: number) => void;
      clearInterval: (id: number) => void;
      Date: { now: () => number };
    };
    g.setTimeout = (fn, ms) => this.add(fn, ms ?? 0, null);
    g.setInterval = (fn, ms) => this.add(fn, ms ?? 0, ms ?? 0);
    g.clearTimeout = (id) => void this.timers.delete(id);
    g.clearInterval = (id) => void this.timers.delete(id);
    g.Date.now = () => this.now;
    return this;
  }

  private add(fn: () => void, ms: number, interval: number | null): number {
    const id = this.nextId++;
    this.timers.set(id, { fn, at: this.now + ms, interval });
    return id;
  }

  /** Number of timers currently armed (leak check). */
  pending(): number {
    return this.timers.size;
  }

  /** Run every timer due at or before now+ms, in chronological order. */
  advance(ms: number): void {
    const target = this.now + ms;
    for (;;) {
      let next: { id: number; timer: Timer } | null = null;
      for (const [id, timer] of this.timers) {
        if (timer.at <= target && (!next || timer.at < next.timer.at)) next = { id, timer };
      }
      if (!next) break;
      this.now = next.timer.at;
      if (next.timer.interval != null) {
        this.timers.set(next.id, { ...next.timer, at: this.now + next.timer.interval });
      } else {
        this.timers.delete(next.id);
      }
      next.timer.fn();
    }
    this.now = target;
  }
}

export class FakeDocument {
  visibilityState: 'visible' | 'hidden' = 'visible';
  private listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, fn: () => void): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(fn);
  }

  removeEventListener(type: string, fn: () => void): void {
    this.listeners.get(type)?.delete(fn);
  }

  listenerCount(type: string): number {
    return this.listeners.get(type)?.size ?? 0;
  }

  /** Fire every listener for `type` (what the browser does on a real change). */
  fire(type: string): void {
    for (const fn of [...(this.listeners.get(type) ?? [])]) fn();
  }
}

export type Chunk = { value: Uint8Array; done: boolean };

export class FakeConnection {
  readonly url: string;
  aborted = false;
  closed = false;
  private queue: string[] = [];
  private pending: { resolve: (c: Chunk) => void; reject: (e: unknown) => void } | null = null;

  constructor(url: string) {
    this.url = url;
  }

  /** Deliver `data` to the reader, now or when it next asks. */
  push(data: string): void {
    if (this.pending) {
      const p = this.pending;
      this.pending = null;
      p.resolve({ value: ENC.encode(data), done: false });
    } else {
      this.queue.push(data);
    }
  }

  /** End the stream cleanly (server closed the connection). */
  close(): void {
    this.closed = true;
    if (this.pending) {
      const p = this.pending;
      this.pending = null;
      p.resolve({ value: new Uint8Array(), done: true });
    }
  }

  read(): Promise<Chunk> {
    if (this.aborted) return Promise.reject(abortError());
    const next = this.queue.shift();
    if (next !== undefined) return Promise.resolve({ value: ENC.encode(next), done: false });
    if (this.closed) return Promise.resolve({ value: new Uint8Array(), done: true });
    return new Promise((resolve, reject) => {
      this.pending = { resolve, reject };
    });
  }

  abort(): void {
    this.aborted = true;
    if (this.pending) {
      const p = this.pending;
      this.pending = null;
      p.reject(abortError());
    }
  }
}

export class FakeNet {
  connections: FakeConnection[] = [];
  /**
   * When set, every fetch resolves with `{ok: false, status}` instead of a
   * stream — the HTTP-failure paths (401/403/500/503 classification). The
   * default (null) keeps the always-OK streaming behaviour.
   */
  failStatus: number | null = null;

  get latest(): FakeConnection {
    return this.connections[this.connections.length - 1];
  }

  /** Connections that are neither aborted nor closed. */
  active(): FakeConnection[] {
    return this.connections.filter((c) => !c.aborted && !c.closed);
  }

  install(): this {
    (globalThis as unknown as Record<string, unknown>).fetch = (
      url: string,
      opts: { signal: AbortSignal },
    ) => {
      const conn = new FakeConnection(url);
      this.connections.push(conn);
      if (this.failStatus != null) {
        return new Promise((resolve, reject) => {
          const onAbort = () => reject(abortError());
          if (opts.signal.aborted) {
            onAbort();
            return;
          }
          opts.signal.addEventListener('abort', onAbort, { once: true });
          resolve({ ok: false, status: this.failStatus, body: null });
        });
      }
      return new Promise((resolve, reject) => {
        const onAbort = () => {
          conn.abort();
          reject(abortError());
        };
        if (opts.signal.aborted) {
          onAbort();
          return;
        }
        opts.signal.addEventListener('abort', onAbort, { once: true });
        resolve({
          ok: true,
          status: 200,
          body: { getReader: () => ({ read: () => conn.read() }) },
        });
      });
    };
    return this;
  }
}

const ENC = new TextEncoder();

export function abortError(): Error {
  const e = new Error('This operation was aborted');
  e.name = 'AbortError';
  return e;
}

/** An SSE `data:` frame carrying `payload` (already serialised). */
export function sseFrame(payload: string): string {
  return `data: ${payload}\n\n`;
}

/** An SSE `data:` frame carrying a JSON value. */
export function sseJson(value: unknown): string {
  return sseFrame(JSON.stringify(value));
}

/** Flush microtasks (and real macrotasks) so awaited transport work settles. */
export function flush(realSetTimeout: typeof setTimeout): Promise<void> {
  return new Promise((resolve) => realSetTimeout(resolve, 0));
}
