/**
 * Transport lifecycle & payload-hostility tests.
 *
 * These exercise the parts of the client that exist to survive a hostile
 * network: the retry loop, the 45s stall watchdog, visibility-resume, and the
 * abort accounting that keeps a reconnect from leaking a server-side stream.
 *
 * They run on a fake clock and a fake byte stream (see ./fakes.ts) so every
 * timing path is deterministic — no sleeps, no races. A real timer never
 * decides whether these pass.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { LivewallTransport, tabView } from '../src/livewall/transport.ts';
import {
  FakeClock,
  FakeDocument,
  FakeNet,
  flush,
  sseFrame,
  sseJson,
  type FakeConnection,
} from './fakes.ts';

// Captured before any fake installs, so microtask flushing stays real.
const realSetTimeout = globalThis.setTimeout.bind(globalThis);

interface Harness {
  clock: FakeClock;
  doc: FakeDocument;
  net: FakeNet;
  boards: Array<{ boards: unknown[]; config: unknown; now: number | null; receivedAt: number }>;
  statuses: Array<string | null>;
  t: LivewallTransport;
}

function setup(): Harness {
  const clock = new FakeClock().install();
  const doc = new FakeDocument();
  const net = new FakeNet().install();
  (globalThis as unknown as { document: FakeDocument }).document = doc;

  const boards: Array<{ boards: unknown[]; config: unknown; now: number | null; receivedAt: number }> = [];
  const statuses: Array<string | null> = [];
  const t = new LivewallTransport('', {
    onBoards: (b, c, now, receivedAt) => boards.push({ boards: b, config: c, now, receivedAt }),
    onStatus: (s) => statuses.push(s),
  });
  return { clock, doc, net, boards, statuses, t };
}

function lastConnection(net: FakeNet): FakeConnection {
  return net.connections[net.connections.length - 1];
}

async function timeoutGuard(ms: number, label: string): Promise<never> {
  return new Promise((_, reject) =>
    realSetTimeout(() => reject(new Error(label)), ms),
  );
}

/* ------------------------------------------------------------- connect/drop */

test('start opens exactly one stream and reports connected', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1);
  assert.deepEqual(h.statuses, [null]);
  h.t.dispose();
});

test('a dropped stream retries exactly once after 2s (not sooner, not spinning)', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).close();
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'no immediate reconnect');

  h.clock.advance(1999);
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'no retry before 2s');

  h.clock.advance(1);
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2, 'retried at 2s');
  h.t.dispose();
});

test('a failing backend cannot spin the retry loop', async () => {
  const clock = new FakeClock().install();
  let calls = 0;
  (globalThis as unknown as { fetch: unknown }).fetch = () => {
    calls += 1;
    return Promise.reject(new Error('connection refused'));
  };
  const statuses: Array<string | null> = [];
  const t = new LivewallTransport('', {
    onBoards: () => {},
    onStatus: (s) => statuses.push(s),
  });
  t.start();
  await flush(realSetTimeout);
  clock.advance(10_000);
  await flush(realSetTimeout);
  assert.ok(calls <= 6, `expected <=6 attempts in 10s, saw ${calls}`);
  assert.ok(statuses.some((s) => s?.includes('cannot reach backend')));
  t.dispose();
});

/* ---------------------------------------------------------------- watchdog */

test('watchdog reconnects after 45s of silence and keeps the fresh stream', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  h.clock.advance(44_000);
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'must not fire before 45s');

  h.clock.advance(7_000); // crosses the next 10s watchdog tick at 50s
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2, 'watchdog reconnected');
  assert.ok(h.net.connections[0].aborted, 'stale stream aborted');
  assert.ok(!lastConnection(h.net).aborted, 'fresh stream must stay live');
  assert.ok(h.statuses.includes('stream stalled — reconnecting'));
  assert.ok(
    !h.statuses.some((s) => s?.includes('cannot reach backend')),
    'a self-initiated stall must not be reported as a backend failure',
  );
  h.t.dispose();
});

test('watchdog stays quiet while keepalives/data keep arriving', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  for (let i = 0; i < 6; i += 1) {
    h.clock.advance(10_000);
    lastConnection(h.net).push(': keepalive\n\n');
    await flush(realSetTimeout);
  }
  assert.equal(h.net.connections.length, 1, '60s of live data must not reconnect');
  h.t.dispose();
});

/* -------------------------------------------------------------- visibility */

test('a hidden tab does nothing; becoming visible resumes', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  h.doc.visibilityState = 'hidden';
  h.doc.fire('visibilitychange');
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'hidden must not reconnect');
  assert.equal(h.net.active().length, 1);

  h.doc.visibilityState = 'visible';
  h.doc.fire('visibilitychange');
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2, 'visible resumes');
  assert.equal(h.net.active().length, 1);
  h.t.dispose();
});

test('visibility resume opens one fresh stream and keeps it', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  h.doc.visibilityState = 'visible';
  h.doc.fire('visibilitychange');
  await flush(realSetTimeout);

  assert.equal(h.net.connections.length, 2);
  assert.ok(h.net.connections[0].aborted, 'old stream aborted');
  assert.ok(!lastConnection(h.net).aborted, 'new stream must stay live');
  assert.equal(h.net.active().length, 1, 'exactly one live connection');
  assert.deepEqual(h.statuses, [null, null], 'no spurious error status');
  h.t.dispose();
});

test('rapid hide/show x5 cannot stack connections', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  for (let i = 0; i < 5; i += 1) {
    h.doc.visibilityState = 'visible';
    h.doc.fire('visibilitychange');
    await flush(realSetTimeout);
  }
  assert.equal(h.net.connections.length, 6);
  assert.equal(h.net.active().length, 1, 'only the newest stream may be live');
  for (let i = 0; i < 5; i += 1) {
    assert.ok(h.net.connections[i].aborted, `connection ${i} must be aborted`);
  }
  assert.ok(!lastConnection(h.net).aborted);
  h.t.dispose();
});

/* -------------------------------------------------------------- unmounting */

test('dispose aborts the stream, drops the listener and clears timers', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);
  assert.equal(h.doc.listenerCount('visibilitychange'), 1);

  h.t.dispose();
  assert.ok(lastConnection(h.net).aborted, 'stream aborted on unmount');
  assert.equal(h.doc.listenerCount('visibilitychange'), 0, 'listener removed');
  assert.equal(h.clock.pending(), 0, 'no timers left armed');

  h.doc.fire('visibilitychange');
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'no reconnect after unmount');
});

test('dispose while a retry is pending cancels the retry', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).close();
  await flush(realSetTimeout);
  assert.ok(h.clock.pending() >= 1, 'a retry is armed');

  h.t.dispose();
  assert.equal(h.clock.pending(), 0);
  h.clock.advance(10_000);
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 1, 'retry must not fire after unmount');
});

test('a reconnect cancels a pending retry instead of leaking a second stream', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  // A drop arms a retry...
  lastConnection(h.net).close();
  await flush(realSetTimeout);
  assert.ok(h.clock.pending() >= 1, 'a retry is armed');

  // ...then a visibility resume / refresh reconnects before it fires.
  h.t.reconnect();
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2, 'reconnect opened one fresh stream');

  h.clock.advance(10_000);
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2, 'the stale retry must not open a third stream');
  assert.equal(h.net.active().length, 1, 'exactly one live connection');
  h.t.dispose();
});

/* ----------------------------------------------------------------- refresh */

test('refresh reconnects and resolves when the next snapshot lands', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  const p = h.t.refresh();
  await flush(realSetTimeout);
  assert.equal(h.net.connections.length, 2);
  assert.ok(!lastConnection(h.net).aborted, 'refresh must keep its fresh stream');

  lastConnection(h.net).push(sseJson({ boards: [], config: {} }));
  await Promise.race([p, timeoutGuard(500, 'refresh did not resolve')]);
  h.t.dispose();
});

test('refresh never hangs when the connection stalls', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  const p = h.t.refresh(100);
  h.clock.advance(100);
  await Promise.race([p, timeoutGuard(500, 'refresh timeout guard failed')]);
  h.t.dispose();
});

test('dispose resolves an in-flight refresh instead of leaving it hanging', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  const p = h.t.refresh(4000);
  await flush(realSetTimeout);
  h.t.dispose();
  await Promise.race([p, timeoutGuard(500, 'dispose left refresh hanging')]);
});

/* ------------------------------------------------------- payload hostility */

test('a malformed frame is surfaced, not silently dropped', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(sseFrame('{not json'));
  await flush(realSetTimeout);
  assert.ok(
    h.statuses.some((s) => s?.includes('bad payload')),
    'invalid JSON must surface a status',
  );

  lastConnection(h.net).push(sseJson({ nope: 1 }));
  await flush(realSetTimeout);
  assert.ok(
    h.statuses.some((s) => s?.includes('boards')),
    'a frame without a boards array must surface a status',
  );
  h.t.dispose();
});

test('a clean frame after a malformed one clears the frame error', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(sseFrame('{not json'));
  await flush(realSetTimeout);
  assert.ok(
    h.statuses[h.statuses.length - 1]?.includes('bad payload'),
    'the malformed frame must surface a status',
  );

  // The stream is still connected and the next frame parses cleanly: the
  // frame-level error is OVER, and the chip must stop claiming staleness
  // while good snapshots keep landing.
  lastConnection(h.net).push(sseJson({ boards: [{ id: 'a', title: 'A', blocks: [] }], config: {} }));
  await flush(realSetTimeout);
  assert.equal(
    h.statuses[h.statuses.length - 1],
    null,
    `a clean frame must clear the frame error; statuses=${JSON.stringify(h.statuses)}`,
  );
  assert.equal(h.boards.length, 1, 'the clean frame is still delivered');

  // And the pair can repeat: another bad frame re-flags, another clean one clears.
  lastConnection(h.net).push(sseFrame('[1,2]'));
  await flush(realSetTimeout);
  assert.ok(
    h.statuses[h.statuses.length - 1]?.includes('bad payload'),
    'a second malformed frame re-flags',
  );
  lastConnection(h.net).push(sseJson({ boards: [], config: {} }));
  await flush(realSetTimeout);
  assert.equal(h.statuses[h.statuses.length - 1], null, 'cleared again');
  h.t.dispose();
});

test('a frame whose every board failed keeps the error until a clean frame', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(sseJson({ boards: [null, null], config: {} }));
  await flush(realSetTimeout);
  assert.ok(
    h.statuses[h.statuses.length - 1]?.includes('bad board'),
    'an all-bad frame surfaces its error',
  );
  assert.equal(h.boards.length, 0, 'an all-bad frame is not dispatched');

  lastConnection(h.net).push(sseJson({ boards: [{ id: 'a', title: 'A', blocks: [] }], config: {} }));
  await flush(realSetTimeout);
  assert.equal(h.statuses[h.statuses.length - 1], null, 'cleared by the clean frame');
  h.t.dispose();
});

/* --------------------------------------------------- HTTP failure classes */

test('a 401 parks the transport: surfaced honestly, no retry loop', async () => {
  const clock = new FakeClock().install();
  const doc = new FakeDocument();
  (globalThis as unknown as { document: FakeDocument }).document = doc;
  const net = new FakeNet().install();
  net.failStatus = 401;
  const statuses: Array<string | null> = [];
  const t = new LivewallTransport('', {
    onBoards: () => {},
    onStatus: (s) => statuses.push(s),
  });
  t.start();
  await flush(realSetTimeout);
  assert.equal(net.connections.length, 1, 'one attempt');
  assert.ok(
    statuses.some((s) => s?.includes('backend returned HTTP 401')),
    `the 401 must be surfaced; statuses=${JSON.stringify(statuses)}`,
  );

  // Parked: no 2s retry loop, and the watchdog must not resurrect the loop.
  clock.advance(60_000);
  await flush(realSetTimeout);
  assert.equal(net.connections.length, 1, `a 401 must not retry; saw ${net.connections.length}`);

  // A deliberate reconnect (pull-to-refresh / visibility) may still try — and
  // re-parks on the same deterministic failure rather than resuming the loop.
  t.reconnect();
  await flush(realSetTimeout);
  assert.equal(net.connections.length, 2, 'an explicit reconnect still attempts');
  clock.advance(60_000);
  await flush(realSetTimeout);
  assert.equal(net.connections.length, 2, 'and re-parks instead of looping');
  t.dispose();
});

test('unpark only on success: a 401 followed by a 200 resumes the retry loop', async () => {
  const clock = new FakeClock().install();
  const doc = new FakeDocument();
  (globalThis as unknown as { document: FakeDocument }).document = doc;
  const net = new FakeNet().install();
  net.failStatus = 401;
  const statuses: Array<string | null> = [];
  const t = new LivewallTransport('', {
    onBoards: () => {},
    onStatus: (s) => statuses.push(s),
  });
  t.start();
  await flush(realSetTimeout);
  assert.ok(statuses.some((s) => s?.includes('HTTP 401')));

  // The backend's auth changes under us (server restart without the token):
  // the next connection succeeds and must UNPARK the transport.
  net.failStatus = null;
  t.reconnect();
  await flush(realSetTimeout);
  assert.deepEqual(statuses[statuses.length - 1], null, 'connected again');

  // The retry loop is alive again: a drop must re-arm the 2s retry.
  net.latest.close();
  await flush(realSetTimeout);
  clock.advance(2_000);
  await flush(realSetTimeout);
  assert.equal(net.connections.length, 3, 'retry loop resumed after success');
  t.dispose();
});

test('a 500 does NOT park: a server-side failure keeps the retry loop', async () => {
  const clock = new FakeClock().install();
  const doc = new FakeDocument();
  (globalThis as unknown as { document: FakeDocument }).document = doc;
  const net = new FakeNet().install();
  net.failStatus = 500;
  const statuses: Array<string | null> = [];
  const t = new LivewallTransport('', {
    onBoards: () => {},
    onStatus: (s) => statuses.push(s),
  });
  t.start();
  await flush(realSetTimeout);
  assert.ok(statuses.some((s) => s?.includes('HTTP 500')));
  clock.advance(2_000);
  await flush(realSetTimeout);
  assert.ok(net.connections.length >= 2, 'a 500 must still be retried');
  t.dispose();
});

test('a null board does not blank the good boards', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(
    sseJson({ boards: [null, { id: 'ok', title: 'OK', blocks: [] }], config: {} }),
  );
  await flush(realSetTimeout);

  const snap = h.boards[h.boards.length - 1];
  assert.ok(snap, 'good boards must still be announced');
  assert.deepEqual(
    (snap.boards as Array<{ id: string }>).map((b) => b.id),
    ['ok'],
  );
  assert.ok(h.statuses.some((s) => s?.includes('bad board')));
  h.t.dispose();
});

test('missing board fields fall back to safe defaults', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(sseJson({ boards: [{ id: 'a', title: 'A' }] }));
  await flush(realSetTimeout);

  const snap = h.boards[h.boards.length - 1];
  const board = (snap.boards as Array<{ blocks: unknown[]; tab: unknown }>)[0];
  assert.deepEqual(board.blocks, []);
  assert.deepEqual(board.tab, {});
  assert.deepEqual(snap.config, {});
  h.t.dispose();
});

test('a snapshot exposes its server clock and the client receipt time', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  h.clock.advance(1_234);
  lastConnection(h.net).push(sseJson({ boards: [], config: {}, now: 1_759_938_000.5 }));
  await flush(realSetTimeout);

  const snap = h.boards[h.boards.length - 1];
  assert.equal(snap.now, 1_759_938_000.5);
  assert.equal(snap.receivedAt, 1_234, 'receipt time is the client clock');
  h.t.dispose();
});

test('a frame without a clock reports now=null and still stamps receipt', async () => {
  const h = setup();
  h.t.start();
  await flush(realSetTimeout);

  lastConnection(h.net).push(sseJson({ boards: [], config: {} }));
  await flush(realSetTimeout);

  const snap = h.boards[h.boards.length - 1];
  assert.equal(snap.now, null);
  assert.equal(typeof snap.receivedAt, 'number');
  h.t.dispose();
});

test('tabView derives bar/pct/text/none indicators', () => {
  const base = { id: 'a', title: 'A', subtitle: '', status: 'running', updated: 0, progress: null, blocks: [] };
  assert.deepEqual(
    tabView({ ...base, tab: { indicator: { kind: 'bar', value: 3, max: 4 } } } as never),
    { label: 'A', accent: 'muted', kind: 'bar', pct: 75, text: '' },
  );
  assert.deepEqual(
    tabView({ ...base, tab: { label: 'L', accent: 'pink', indicator: { kind: 'pct', value: 42 } } } as never),
    { label: 'L', accent: 'pink', kind: 'pct', pct: 42, text: '' },
  );
  assert.deepEqual(
    tabView({ ...base, tab: { indicator: { kind: 'text', text: '2/5' } } } as never),
    { label: 'A', accent: 'muted', kind: 'text', pct: null, text: '2/5' },
  );
  assert.deepEqual(
    tabView({ ...base, tab: {} } as never),
    { label: 'A', accent: 'muted', kind: 'none', pct: null, text: '' },
  );
});
