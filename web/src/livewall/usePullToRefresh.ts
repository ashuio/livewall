/**
 * Pull-to-refresh for a scroll container.
 *
 * Flutter ships RefreshIndicator; the DOM does not. This is the equivalent:
 * engage only at the very top of the scroller, follow the finger with a damped
 * offset, and fire past a threshold.
 *
 * Touch-only on purpose — a mouse drag on a desktop is text selection, not a
 * pull, and hijacking it would be worse than not having the gesture.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

const THRESHOLD = 70; // px of damped pull needed to trigger
const MAX_PULL = 110;
const DAMPING = 0.5;

export interface PullState {
  /** Current damped pull distance in px (0 when idle). */
  distance: number;
  /** True while the refresh callback is running. */
  refreshing: boolean;
  /** Attach to the scrollable element. */
  handlers: {
    onTouchStart: (e: React.TouchEvent) => void;
    onTouchMove: (e: React.TouchEvent) => void;
    onTouchEnd: () => void;
  };
}

export function usePullToRefresh(
  scroller: React.RefObject<HTMLElement | null>,
  onRefresh: () => Promise<void>,
): PullState {
  const [distance, setDistance] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const startY = useRef<number | null>(null);
  const pulling = useRef(false);

  const onTouchStart = useCallback((e: React.TouchEvent) => {
    const el = scroller.current;
    if (!el || el.scrollTop > 0) {
      startY.current = null;
      return;
    }
    startY.current = e.touches[0]?.clientY ?? null;
    pulling.current = false;
  }, [scroller]);

  const onTouchMove = useCallback(
    (e: React.TouchEvent) => {
      if (startY.current == null || refreshing) return;
      const el = scroller.current;
      if (!el || el.scrollTop > 0) return;

      const dy = (e.touches[0]?.clientY ?? 0) - startY.current;
      if (dy <= 0) {
        setDistance(0);
        pulling.current = false;
        return;
      }
      pulling.current = true;
      setDistance(Math.min(MAX_PULL, dy * DAMPING));
    },
    [scroller, refreshing],
  );

  const onTouchEnd = useCallback(() => {
    const wasPulling = pulling.current;
    const reached = distance >= THRESHOLD;
    startY.current = null;
    pulling.current = false;

    if (wasPulling && reached && !refreshing) {
      setRefreshing(true);
      setDistance(THRESHOLD); // hold the indicator visible while refreshing
      void onRefresh().finally(() => {
        setRefreshing(false);
        setDistance(0);
      });
    } else {
      setDistance(0);
    }
  }, [distance, refreshing, onRefresh]);

  // Safety: if the component unmounts mid-refresh, drop the held offset.
  useEffect(() => () => setDistance(0), []);

  return { distance, refreshing, handlers: { onTouchStart, onTouchMove, onTouchEnd } };
}
