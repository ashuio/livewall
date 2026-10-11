/**
 * Freshness module tests — pure clock math, no DOM, no React.
 *
 * These pin the semantics the shell depends on: board age advances with the
 * client clock between snapshots, the stale predicate fires on both the
 * transport-status branch and the data-age branch, the formatter's boundaries
 * are exact, and every function is total on missing/odd input.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  STALE_AFTER_SECONDS,
  baseBoardAge,
  boardAgeSeconds,
  dataAgeSeconds,
  expiresInSeconds,
  formatAge,
  formatExpiry,
  freshness,
  isStale,
} from '../src/livewall/freshness.ts';

/* -------------------------------------------------------------- board age */

test('board age advances with the client clock without a new snapshot', () => {
  const board = { age: 5 };
  const now = 1_759_938_000;
  const receivedAt = 1_000_000; // client ms
  assert.equal(boardAgeSeconds(board, now, receivedAt, receivedAt), 5, 'at receipt');
  assert.equal(boardAgeSeconds(board, now, receivedAt, receivedAt + 2_000), 7, '+2s');
  assert.equal(boardAgeSeconds(board, now, receivedAt, receivedAt + 12_500), 17.5, '+12.5s');
});

test('base age falls back to now - updated when age is absent', () => {
  assert.equal(baseBoardAge({ updated: 1_000 }, 1_012), 12);
  assert.equal(baseBoardAge({ age: 3, updated: 1_000 }, 1_012), 3, 'age wins');
  assert.equal(baseBoardAge({ updated: 1_000 }, null), 0, 'no now -> no fallback');
  assert.equal(baseBoardAge(null, 1_000), 0);
});

/* --------------------------------------------------------------- data age */

test('data age is null before the first snapshot and grows after it', () => {
  assert.equal(dataAgeSeconds(null, 5_000), null);
  assert.equal(dataAgeSeconds(1_000, 1_000), 0);
  assert.equal(dataAgeSeconds(1_000, 3_500), 2.5);
});

/* ------------------------------------------------------------- stale both ways */

test('stale is true whenever the transport reports a problem', () => {
  assert.equal(isStale('cannot reach backend', 1_000, 1_100), true);
  assert.equal(isStale('', 1_000, 1_100), true, 'even an empty problem string is non-null');
});

test('stale is false while connected and within the threshold', () => {
  const receivedAt = 1_000_000;
  assert.equal(isStale(null, receivedAt, receivedAt + 44_000), false, '44s < 45s');
  assert.equal(isStale(null, receivedAt, receivedAt), false);
  assert.equal(isStale(null, null, 5_000), false, 'no snapshot yet is not stale');
});

test('stale is true once the data age exceeds the threshold', () => {
  const receivedAt = 1_000_000;
  assert.equal(isStale(null, receivedAt, receivedAt + 45_000), false, 'exactly at the threshold is not stale');
  assert.equal(isStale(null, receivedAt, receivedAt + 45_001), true, 'past it is stale');
  assert.equal(STALE_AFTER_SECONDS, 45);
});

/* -------------------------------------------------------------- formatter */

test('formatAge boundaries are exact', () => {
  assert.equal(formatAge(0), 'just now');
  assert.equal(formatAge(4.999), 'just now');
  assert.equal(formatAge(5), '5s ago');
  assert.equal(formatAge(59.9), '59s ago');
  assert.equal(formatAge(60), '1m ago');
  assert.equal(formatAge(3_599), '59m ago');
  assert.equal(formatAge(3_600), '1h ago');
  assert.equal(formatAge(86_399), '23h ago');
  assert.equal(formatAge(86_400), '1d ago');
});

/* -------------------------------------------------------------- ttl countdown */

test('expiresInSeconds is null for a non-terminal board', () => {
  assert.equal(expiresInSeconds(null, 1_000, 1_500), null);
  assert.equal(expiresInSeconds(undefined, 1_000, 1_500), null);
  assert.equal(expiresInSeconds(Number.NaN, 1_000, 1_500), null);
});

test('expiresInSeconds ticks down with the client clock, skew-free', () => {
  const receivedAt = 1_000_000;
  assert.equal(expiresInSeconds(1800, receivedAt, receivedAt), 1800);
  assert.equal(expiresInSeconds(1800, receivedAt, receivedAt + 60_000), 1740);
  assert.equal(expiresInSeconds(1800, receivedAt, receivedAt + 12_500), 1787.5);
});

test('expiresInSeconds clamps to [0, base] on clock jitter', () => {
  const receivedAt = 1_000_000;
  assert.equal(expiresInSeconds(5, receivedAt, receivedAt + 30_000), 0, 'past expiry -> 0');
  assert.equal(expiresInSeconds(1800, receivedAt, receivedAt - 60_000), 1800,
    'backwards jitter never exceeds the snapshot value');
});

test('expiresInSeconds without a receipt time returns the base', () => {
  assert.equal(expiresInSeconds(90, null, 5_000), 90);
  assert.equal(expiresInSeconds(90, Number.NaN, 5_000), 90);
});

test('formatExpiry boundaries are exact and empty at/below zero', () => {
  assert.equal(formatExpiry(0), '');
  assert.equal(formatExpiry(-5), '');
  assert.equal(formatExpiry(Number.NaN), '');
  assert.equal(formatExpiry(59), 'expires in 59s');
  assert.equal(formatExpiry(60), 'expires in 1m');
  assert.equal(formatExpiry(3_599), 'expires in 59m');
  assert.equal(formatExpiry(3_600), 'expires in 1h');
  assert.equal(formatExpiry(86_400), 'expires in 1d');
});



test('every function is total on missing or odd input', () => {
  assert.equal(formatAge(Number.NaN), 'just now');
  assert.equal(formatAge(Number.POSITIVE_INFINITY), 'just now');
  assert.equal(formatAge(-5), 'just now');
  assert.equal(boardAgeSeconds(null, null, null, Number.NaN), 0);
  assert.equal(boardAgeSeconds({}, Number.NaN, Number.NaN, Number.NaN), 0);
  assert.equal(dataAgeSeconds(Number.NaN, 1_000), null);
  assert.equal(isStale(undefined, undefined, Number.NaN), false);
  const f = freshness({ clientNow: Number.NaN });
  assert.deepEqual(f, { boardAge: 0, dataAge: null, stale: false });
});

test('freshness combines board age, data age and the stale verdict', () => {
  const f = freshness({
    board: { age: 5 },
    now: 1_759_938_000,
    receivedAt: 1_000_000,
    status: null,
    clientNow: 1_002_500,
  });
  assert.deepEqual(f, { boardAge: 7.5, dataAge: 2.5, stale: false });

  const down = freshness({
    board: { age: 5 },
    now: 1_759_938_000,
    receivedAt: 1_000_000,
    status: 'stream ended — reconnecting',
    clientNow: 1_002_500,
  });
  assert.equal(down.stale, true);
});
