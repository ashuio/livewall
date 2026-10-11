/**
 * View-state tests — pure URL parsing/writing, no DOM, no React.
 *
 * These pin the contract the shell depends on: `?board=` and `?kiosk=1` are
 * read independently of each other and of `?token=`; writing one preserves the
 * others; empty/odd values degrade instead of throwing. The composition cases
 * run the token handling (`readToken`) and the view params together, because
 * that is the pair that must not clobber each other.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readToken, TOKEN_KEY } from '../src/livewall/auth.ts';
import {
  readViewParams,
  replaceSearch,
  writeViewParams,
} from '../src/livewall/viewstate.ts';

function fakeStore(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
    _data: data,
  };
}

function parsed(search: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(search).entries());
}

/* -------------------------------------------------------------- read */

test('no params means no board and normal mode', () => {
  assert.deepEqual(readViewParams(''), { board: null, kiosk: false });
  assert.deepEqual(readViewParams('?'), { board: null, kiosk: false });
});

test('?board= names a board', () => {
  assert.equal(readViewParams('?board=build-42').board, 'build-42');
});

test('an empty ?board= is not a board id', () => {
  assert.equal(readViewParams('?board=').board, null);
  assert.equal(readViewParams('?board=&kiosk=1').board, null);
});

test('kiosk is true only for ?kiosk=1', () => {
  assert.equal(readViewParams('?kiosk=1').kiosk, true);
  assert.equal(readViewParams('?kiosk=0').kiosk, false);
  assert.equal(readViewParams('?kiosk=true').kiosk, false);
  assert.equal(readViewParams('?kiosk=').kiosk, false);
});

test('board and kiosk are read independently of order', () => {
  for (const search of [
    '?board=x&kiosk=1',
    '?kiosk=1&board=x',
    '?theme=dark&board=x&kiosk=1',
  ]) {
    assert.deepEqual(readViewParams(search), { board: 'x', kiosk: true }, search);
  }
});

/* ------------------------------------------------------------- write */

test('writeViewParams sets board and preserves every other param', () => {
  const out = writeViewParams('?token=T&kiosk=1', { board: 'x' });
  assert.deepEqual(parsed(out), { token: 'T', kiosk: '1', board: 'x' });
});

test('writeViewParams removes a board without touching the rest', () => {
  const out = writeViewParams('?token=T&board=x&kiosk=1', { board: null });
  assert.deepEqual(parsed(out), { token: 'T', kiosk: '1' });
});

test('writeViewParams sets and clears kiosk', () => {
  assert.deepEqual(parsed(writeViewParams('?board=x', { kiosk: true })),
    { board: 'x', kiosk: '1' });
  assert.deepEqual(parsed(writeViewParams('?board=x&kiosk=1', { kiosk: false })),
    { board: 'x' });
});

test('writeViewParams replaces an existing board in place', () => {
  const out = writeViewParams('?board=old&kiosk=1', { board: 'new' });
  assert.deepEqual(parsed(out), { board: 'new', kiosk: '1' });
});

test('writeViewParams on an empty search yields a clean leading-? string', () => {
  assert.equal(writeViewParams('', { board: 'x' }), '?board=x');
  assert.equal(writeViewParams('', { board: null, kiosk: false }), '');
});

/* -------------------------------------------- composition with token */

test('token strip preserves board and kiosk in any order', () => {
  const store = fakeStore();
  for (const search of [
    '?token=T&board=x&kiosk=1',
    '?board=x&kiosk=1&token=T',
    '?kiosk=1&token=T&board=x',
  ]) {
    const { token, cleanSearch } = readToken(search, store);
    assert.equal(token, 'T', search);
    assert.ok(!cleanSearch.includes('token='), `token still present: ${cleanSearch}`);
    assert.deepEqual(readViewParams(cleanSearch), { board: 'x', kiosk: true }, search);
  }
  assert.equal(store._data[TOKEN_KEY], 'T');
});

test('updating the view preserves a token that is still in the URL', () => {
  // If the shell writes board/kiosk before the token strip (or a caller keeps a
  // raw search), the token must survive: the two writers are composed, not
  // racing.
  const raw = '?token=T&board=x&kiosk=1';
  const next = writeViewParams(raw, { board: 'y' });
  assert.deepEqual(parsed(next), { token: 'T', board: 'y', kiosk: '1' });
  // And the composed read still resolves board/kiosk.
  assert.deepEqual(readViewParams(next), { board: 'y', kiosk: true });
});

test('an unknown board id is surfaced verbatim so the shell can fall back', () => {
  // readViewParams does not know the board list; it must not hide the id.
  assert.equal(readViewParams('?board=does-not-exist').board, 'does-not-exist');
});

/* --------------------------------------------------------- totality */

test('replaceSearch without a DOM returns the search unchanged', () => {
  assert.equal(replaceSearch('?board=x'), '?board=x');
});

test('readViewParams never throws on odd input', () => {
  assert.deepEqual(readViewParams('%%%not-a-query'), { board: null, kiosk: false });
  assert.deepEqual(readViewParams('board=x&kiosk=1'), { board: 'x', kiosk: true });
});
