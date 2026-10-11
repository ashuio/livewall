/**
 * useLivewall — wires the SSE transport into the shell's board state.
 *
 * Each snapshot is a complete, self-contained set of boards (each carrying its
 * own `blocks`), so the hook simply holds the latest list keyed by id; there is
 * no client-side surface model to keep in sync.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { LivewallTransport } from './transport';
import type { BoardSummary, StreamConfig } from './types';

export interface LivewallState {
  boards: BoardSummary[];
  config: StreamConfig;
  status: string | null;
  /** Server clock (epoch seconds) of the latest snapshot, or null before one. */
  now: number | null;
  /** Client clock (epoch ms) when the latest snapshot was received, or null. */
  receivedAt: number | null;
  refresh: () => Promise<void>;
}

export function useLivewall(): LivewallState {
  const [boards, setBoards] = useState<BoardSummary[]>([]);
  const [config, setConfig] = useState<StreamConfig>({});
  const [status, setStatus] = useState<string | null>(null);
  const [clock, setClock] = useState<{ now: number | null; receivedAt: number } | null>(null);

  const transportRef = useRef<LivewallTransport | null>(null);

  useEffect(() => {
    const transport = new LivewallTransport('', {
      onBoards: (list, cfg, now, receivedAt) => {
        setBoards(list);
        setConfig(cfg);
        setClock({ now, receivedAt });
      },
      onStatus: setStatus,
    });
    transportRef.current = transport;

    transport.start();

    return () => {
      transport.dispose();
      transportRef.current = null;
    };
  }, []);

  const refresh = useCallback(
    () => transportRef.current?.refresh() ?? Promise.resolve(),
    [],
  );

  return {
    boards,
    config,
    status,
    now: clock?.now ?? null,
    receivedAt: clock?.receivedAt ?? null,
    refresh,
  };
}
