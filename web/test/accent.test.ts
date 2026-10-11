/**
 * The accent whitelist, client-side — the defence-in-depth half of the
 * CSS-injection fix.
 *
 * A block's `color` is interpolated into a CSS `var(--accent-<name>)` token.
 * `accentName` must pass ONLY the accent vocabulary (lowercased): anything
 * else — in particular a payload like `mint) url(//evil`, which assembles the
 * valid declaration `background: var(--accent-mint) url(//evil)` and makes
 * every viewer's browser fetch the attacker URL — degrades to `''`, and the
 * callers fall back to violet. The backend contract refuses such values too;
 * this whitelist also neutralises boards stored before that fix.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ACCENTS, accentName } from '../src/livewall/accent.ts';

test('every contract accent passes, lowercased', () => {
  for (const a of ACCENTS) assert.equal(accentName(a), a);
  assert.equal(accentName('MINT'), 'mint');
  assert.equal(accentName('Violet'), 'violet');
});

test('a CSS url() payload degrades to empty, never interpolates', () => {
  assert.equal(accentName('mint) url(http://evil.example/beacon'), '');
  assert.equal(accentName('violet); background:url(//evil'), '');
  assert.equal(accentName('ok) url(//evil'), '');
});

test('unknown names, non-strings and empties degrade to empty', () => {
  assert.equal(accentName('not-an-accent'), '');
  assert.equal(accentName('#ff0000'), '');
  assert.equal(accentName(''), '');
  assert.equal(accentName(null), '');
  assert.equal(accentName(undefined), '');
  assert.equal(accentName(42), '');
});
