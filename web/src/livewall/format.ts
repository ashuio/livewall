/**
 * Number formatting — exact by default, compact only when asked.
 *
 * The board shows exact values (6836, not '6.8K'): counts must be readable to
 * the unit. Compaction is an explicit per-value choice by the agent, and there
 * are three ways to express it:
 *
 *   a number           -> exact           (6836)
 *   a string           -> verbatim        ('1.5M', pre-formatted by the agent)
 *   fmt: 'compact'     -> label shortened (1521227 -> '1.5M')
 *
 * `fmt` exists because progress/gauge/bar need a REAL number to compute the
 * ratio — you cannot pass '1.5M' there. It shortens only the label.
 *
 * This is the single source of the number rule: `stat`/`stats` chips render
 * through `lwStatValue`, ratios through `lwFormat`.
 */

/**
 * The display window for a `log`/`code` block, in lines. The contract stores up
 * to `MAX_LOG_LINES` (400); the renderer shows the newest `RENDER_LOG_LINES`.
 * Kept in step with `RENDER_LOG_LINES` in backend/contract.py.
 */
export const RENDER_LOG_LINES = 80;

/**
 * Exact: 6836 -> "6836", 12.5 -> "12.5", 12.25 -> "12.25".
 *
 * `String(v)` is JS's shortest round-trip representation, which matches the
 * backend's `repr(n)` for non-integers. The old `toFixed(1)` rounded 12.25 to
 * "12.3" while the backend chip showed "12.25" — the same number rendered two
 * ways depending on the block.
 */
function lwNum(v: number): string {
  return String(v);
}

/** Opt-in compaction: 1521227 -> "1.5M", 9410 -> "9.4K", 999 -> "999". */
function lwCompact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}B`;
  if (a >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (a >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return lwNum(v);
}

/** Dispatch on the block's optional `fmt` field. Anything but 'compact' is exact. */
export function lwFormat(v: number, fmt?: string): string {
  return fmt === 'compact' ? lwCompact(v) : lwNum(v);
}

/**
 * A `stat`/`stats` chip value: a STRING passes through verbatim (the agent opts
 * into compaction by pre-formatting it), a NUMBER renders exact (6836, never
 * '6.8K'). Everything else degrades to "" so the renderer stays total.
 */
export function lwStatValue(v: unknown): string {
  if (typeof v === 'string') return v;
  if (typeof v === 'number') return Number.isFinite(v) ? lwNum(v) : '';
  return '';
}

/** Percent for a bar/gauge ratio, clamped to 0..100. */
export function pctOf(value: number, max: number): number {
  if (!max || max <= 0) return 0;
  return Math.max(0, Math.min(100, (value / max) * 100));
}
