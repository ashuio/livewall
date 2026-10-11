/** Shapes of the Livewall backend's SSE snapshot. */

export interface TabIndicator {
  kind?: 'bar' | 'pct' | 'text' | 'none';
  value?: number;
  max?: number;
  text?: string;
}

export interface TabSpec {
  label?: string;
  accent?: string;
  indicator?: TabIndicator;
}

/** One agent-authored block. Fields vary by `type`; the renderer is total. */
export interface Block {
  type?: string;
  [field: string]: unknown;
}

/** One board, as summarised by the server for the tab bar + board body. */
export interface BoardSummary {
  id: string;
  title: string;
  subtitle: string;
  status: 'idle' | 'running' | 'done' | 'error';
  updated: number;
  /** Seconds since `updated` at snapshot time (server-computed). */
  age: number;
  /** Seconds until prune for a done/error board; `null` otherwise. */
  expires_in: number | null;
  progress: number | null;
  tab: TabSpec;
  /** The board's blocks, exactly as stored. The client renders them directly. */
  blocks: Block[];
}

/** A tab's worth of derived display data. */
export interface TabView {
  label: string;
  accent: string;
  kind: 'bar' | 'pct' | 'text' | 'none';
  pct: number | null;
  text: string;
}

export interface StreamConfig {
  default_ttl?: number;
  stale_ttl?: number;
  max_boards?: number;
}

export interface Snapshot {
  config: StreamConfig;
  /** Server clock (epoch seconds) at the instant this snapshot was taken. */
  now: number;
  boards: BoardSummary[];
}
