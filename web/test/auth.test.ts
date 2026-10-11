/**
 * Token resolution tests.
 *
 * `readToken` is the pure core of the client's auth: it decides the token from
 * a URL search string and a store, and returns the search string with the
 * `token` parameter stripped so it does not linger in the address bar. Tested
 * here without a DOM.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readToken, TOKEN_KEY } from '../src/livewall/auth.ts';

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

test('a ?token= is used, persisted, and stripped from the search', () => {
  const store = fakeStore();
  const { token, cleanSearch } = readToken('?token=abc123', store);
  assert.equal(token, 'abc123');
  assert.equal(cleanSearch, '');
  assert.equal(store._data[TOKEN_KEY], 'abc123');
});

test('other query params survive the strip', () => {
  const store = fakeStore();
  const { token, cleanSearch } = readToken('?token=abc&theme=dark', store);
  assert.equal(token, 'abc');
  assert.equal(cleanSearch, '?theme=dark');
});

test('with no ?token=, the stored value is used', () => {
  const store = fakeStore({ [TOKEN_KEY]: 'stored' });
  const { token, cleanSearch } = readToken('?theme=dark', store);
  assert.equal(token, 'stored');
  assert.equal(cleanSearch, '?theme=dark');
});

test('no token anywhere yields an empty string', () => {
  const { token, cleanSearch } = readToken('', fakeStore());
  assert.equal(token, '');
  assert.equal(cleanSearch, '');
});

test('an empty ?token= does not clobber the stored value', () => {
  const store = fakeStore({ [TOKEN_KEY]: 'stored' });
  const { token } = readToken('?token=', store);
  assert.equal(token, 'stored');
});

test('a throwing store does not crash resolution', () => {
  const hostile = {
    getItem: () => {
      throw new Error('blocked');
    },
    setItem: () => {
      throw new Error('blocked');
    },
  };
  const { token } = readToken('?token=abc', hostile);
  assert.equal(token, 'abc'); // the URL value still wins
  const stored = readToken('', hostile);
  assert.equal(stored.token, ''); // and a read failure is just empty
});
