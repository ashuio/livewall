/**
 * The number rule, client-side — the single source now that the translation
 * layer is gone.
 *
 * A `stat`/`stats` value renders a NUMBER exact (6836, never '6.8K') and a
 * STRING verbatim (the agent opts into compaction by pre-formatting it).
 * Ratios go through `lwFormat`, compact only when `fmt` asks.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lwFormat, lwStatValue } from '../src/livewall/format.ts';

test('a numeric stat value renders exact, never compacted', () => {
  assert.equal(lwStatValue(6836), '6836');
  assert.equal(lwStatValue(1537899), '1537899');
  assert.equal(lwStatValue(12.0), '12');
  assert.equal(lwStatValue(12.25), '12.25');
});

test('a string stat value passes through verbatim', () => {
  assert.equal(lwStatValue('1.5M'), '1.5M');
  assert.equal(lwStatValue('6836'), '6836');
});

test('a non-value stat degrades to empty, never throws', () => {
  assert.equal(lwStatValue(null), '');
  assert.equal(lwStatValue(undefined), '');
  assert.equal(lwStatValue(true), '');
  assert.equal(lwStatValue({ a: 1 }), '');
  assert.equal(lwStatValue([1, 2]), '');
  assert.equal(lwStatValue(NaN), '');
  assert.equal(lwStatValue(Infinity), '');
});

test('lwFormat is exact unless compact is asked', () => {
  assert.equal(lwFormat(6836), '6836');
  assert.equal(lwFormat(1521227), '1521227');
  assert.equal(lwFormat(1521227, 'compact'), '1.5M');
  assert.equal(lwFormat(9410, 'compact'), '9.4K');
  assert.equal(lwFormat(999, 'compact'), '999');
});
