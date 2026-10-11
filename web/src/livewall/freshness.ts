/**
 * Freshness — pure clock math for the Livewall shell.
 *
 * No React, no DOM. Given a board's server-computed age, the snapshot's server
 * clock, the client time at receipt, and the current client time, it decides
 * how old the board's data is and whether the wall can still be trusted to be
 * live. Every function is TOTAL: a missing or malformed field yields a sane
 * default rather than throwing, because a status wall must never blank out over
 * one bad frame.
 *
 * Semantics (durations in seconds; `clientNow`/`receivedAt` are epoch ms):
 *
 *  - board age = the board's server-computed `age` at snapshot time, PLUS the
 *    client's elapsed time since it received that snapshot. `age` is measured
 *    against the snapshot's own `now`, so adding only the client's elapsed time
 *    is skew-free: the client never subtracts a client wall clock from a server
 *    timestamp. The age therefore keeps ticking between snapshots. If `age` is
 *    absent, `now - updated` is used as the base (both are server clocks); with
 *    neither, the base is 0.
 *
 *  - data age = seconds since the client last received a snapshot. It is
 *    `null` before the first snapshot, so a just-opened page is not "stale".
 *
 *  - stale = the transport is not connected (its `status` is non-null), OR the
 *    data age exceeds STALE_AFTER_SECONDS.
 *
 * STALE_AFTER_SECONDS = 45. The backend keepalive (LIVEWALL_KEEPALIVE, default
 * 15s) is the longest a healthy stream stays silent, so 45s is three missed
 * keepalives — the same 3x margin the transport's stall watchdog already uses
 * (STALL_MS 45s against a 15s keepalive). The banner and the watchdog therefore
 * agree on when silence means dead.
 */

export const STALE_AFTER_SECONDS = 45;

/** The fields of a board the freshness math reads. */
export interface FreshnessBoard {
  age?: number;
  updated?: number;
}

export interface FreshnessInput {
  board?: FreshnessBoard | null;
  /** Server clock (epoch seconds) of the snapshot. */
  now?: number | null;
  /** Client clock (epoch ms) when the snapshot was received. */
  receivedAt?: number | null;
  /** Transport status; non-null means "not connected". */
  status?: string | null;
  /** Current client clock (epoch ms). */
  clientNow: number;
}

export interface Freshness {
  /** Current age of the board's data, in seconds (>= 0). */
  boardAge: number;
  /** Seconds since the last snapshot, or null before the first. */
  dataAge: number | null;
  /** True when the wall must not be trusted as live. */
  stale: boolean;
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** The board's age at snapshot time, from `age` (or `now - updated`). */
export function baseBoardAge(
  board: FreshnessBoard | null | undefined,
  now: number | null | undefined,
): number {
  if (board && finite(board.age)) return Math.max(0, board.age);
  if (board && finite(board.updated) && finite(now)) return Math.max(0, now - board.updated);
  return 0;
}

/**
 * Current board age: the base age plus the client's elapsed time since the
 * snapshot was received. This is the number that ticks without a new snapshot.
 */
export function boardAgeSeconds(
  board: FreshnessBoard | null | undefined,
  now: number | null | undefined,
  receivedAt: number | null | undefined,
  clientNow: number,
): number {
  const base = baseBoardAge(board, now);
  if (finite(receivedAt) && finite(clientNow)) {
    return Math.max(0, base + (clientNow - receivedAt) / 1000);
  }
  return base;
}

/** Seconds since the snapshot was received, or null before the first one. */
export function dataAgeSeconds(
  receivedAt: number | null | undefined,
  clientNow: number,
): number | null {
  if (!finite(receivedAt) || !finite(clientNow)) return null;
  return Math.max(0, (clientNow - receivedAt) / 1000);
}

/** True when the transport is not connected, or the data is older than the threshold. */
export function isStale(
  status: string | null | undefined,
  receivedAt: number | null | undefined,
  clientNow: number,
  threshold: number = STALE_AFTER_SECONDS,
): boolean {
  if (status != null) return true;
  const age = dataAgeSeconds(receivedAt, clientNow);
  return age != null && age > threshold;
}

/** Human formatter for an age in seconds: "just now", "12s ago", "4m ago", "1h ago". */
export function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 5) return 'just now';
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * Remaining TTL for a terminal board, ticking on the client clock.
 *
 * `expires_in` is the server's seconds-to-prune at snapshot time, so the same
 * skew-free pairing as `boardAgeSeconds` applies: subtract the client's elapsed
 * time since the snapshot was received. Returns `null` when the board is not
 * terminal (`expires_in` absent/null). Clamped at 0 for clock jitter.
 */
export function expiresInSeconds(
  expiresIn: number | null | undefined,
  receivedAt: number | null | undefined,
  clientNow: number,
): number | null {
  if (!finite(expiresIn)) return null;
  const base = Math.max(0, expiresIn);
  if (finite(receivedAt) && finite(clientNow)) {
    const elapsed = (clientNow - receivedAt) / 1000;
    // Clamp both ways: never below 0, and never above the snapshot's own
    // `expires_in` if the client clock jitters backwards past receipt.
    return Math.min(base, Math.max(0, base - elapsed));
  }
  return base;
}

/**
 * Human formatter for a remaining TTL, or '' when there is nothing to warn
 * about. Kept terse on purpose: this is a subtle warning, not a headline.
 */
export function formatExpiry(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  const s = Math.floor(seconds);
  if (s < 60) return `expires in ${s}s`;
  if (s < 3600) return `expires in ${Math.floor(s / 60)}m`;
  if (s < 86400) return `expires in ${Math.floor(s / 3600)}h`;
  return `expires in ${Math.floor(s / 86400)}d`;
}

/** One call: everything the shell needs to describe a board's freshness. */
export function freshness(input: FreshnessInput): Freshness {
  const receivedAt = input.receivedAt ?? null;
  return {
    boardAge: boardAgeSeconds(input.board, input.now ?? null, receivedAt, input.clientNow),
    dataAge: dataAgeSeconds(receivedAt, input.clientNow),
    stale: isStale(input.status ?? null, receivedAt, input.clientNow),
  };
}
