/**
 * Livewall's block renderer — the board components.
 *
 * The board spec IS the wire format: the server streams each board's `blocks`
 * verbatim and this module renders them. `renderBlock` is the dispatcher: it
 * normalises one block the way the old translation layer did (coercing numeric
 * strings, synthesising a single `stat` into a chip, applying the log render
 * cap, …) and hands typed props to the component. It is TOTAL: a malformed
 * block degrades to a visible value and never throws.
 *
 * Two things worth noting:
 *
 *  - The gauge, sparkline and line chart are SVG, not a canvas painter.
 *    Crisper at any scale, inspectable in devtools, and animatable.
 *  - Colours come from CSS custom properties, so light/dark needs no plumbing
 *    through props.
 *
 * Values: `stat`/`stats` values render through `lwStatValue` (a number exact, a
 * string verbatim), while `progress`/`gauge`/`bar` values are coerced to real
 * numbers (a ratio needs one).
 */

import { useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react';

import type { Block } from './types';
import { RENDER_LOG_LINES, lwFormat, lwStatValue, pctOf } from './format';
import { accentName } from './accent.ts';

/* --------------------------------------------------------------- coercion */
/* These mirror the retired emitter's helpers so a block renders the same
   whether it was just written or has sat in memory. Each is total. */

/** A field's text: null/undefined becomes empty, everything else String()s. */
const s = (v: unknown): string => (v == null ? '' : String(v));

/** Iterate a list field safely; a non-list yields [] instead of throwing. */
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/** Coerce a numeric field to a finite number, or `def`. Numeric strings count. */
const num = (v: unknown, def: number): number => {
  if (typeof v === 'boolean') return def;
  if (typeof v === 'number') return Number.isFinite(v) ? v : def;
  if (typeof v === 'string') {
    if (v.trim() === '') return def;
    const parsed = Number(v);
    return Number.isFinite(parsed) ? parsed : def;
  }
  return def;
};

/** A ratio denominator: a finite number > 0, else the default. */
const positive = (v: unknown, def = 100): number => {
  const parsed = num(v, def);
  return parsed <= 0 ? def : parsed;
};

/** A boolish field (the contract allows booleans or strings) as a boolean. */
const flag = (v: unknown): boolean =>
  typeof v === 'string' ? v.trim().toLowerCase() === 'true' : Boolean(v);

/** A component's numeric prop (already normalised, but defensive). */
const n = (v: unknown): number => num(v, 0);

/** The [min, max] of a list, by loop. `Math.min(...values)` spreads the list
    into call arguments, and V8 throws RangeError past its argument limit
    (~125k–200k) — the contract caps no list's length (only the 1 MiB request
    body), so a legal 150k-point chart must not be able to throw here. A loop
    is O(n) with no argument limit. An empty list yields [Infinity,
    -Infinity], exactly what the spread form returned. */
function extent(values: number[]): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return [lo, hi];
}

/* ------------------------------------------------------------------ Panel */

function Panel({
  children,
  tone,
  className,
}: {
  children: ReactNode;
  tone?: 'ok' | 'warn' | 'error' | 'info';
  className?: string;
}) {
  return (
    <div className={`lw-panel${tone ? ` lw-tone-${tone}` : ''}${className ? ` ${className}` : ''}`}>
      {children}
    </div>
  );
}

/* --------------------------------------------------------------- LwHeading */

export function LwHeading({ text, eyebrow }: { text?: string; eyebrow?: string }) {
  return (
    <div className="lw-heading">
      {eyebrow ? <div className="lw-eyebrow">{s(eyebrow).toUpperCase()}</div> : null}
      {/* A real heading, not a styled div: the board title is the h1 (App.tsx),
          so a block heading is the h2 beneath it. */}
      <h2 className="lw-heading-text">{s(text)}</h2>
    </div>
  );
}

/* ----------------------------------------------------------------- LwStats */

export interface StatsItem {
  label?: string;
  value?: string;
  unit?: string;
}

export function LwStats({ items }: { items: StatsItem[] }) {
  return (
    <Panel>
      <div className="lw-chips">
        {items.map((it, i) => (
          <div className="lw-chip" key={i}>
            <div className="lw-chip-label">{s(it.label).toUpperCase()}</div>
            <div className="lw-chip-value">
              {s(it.value)}
              {it.unit ? <span className="lw-chip-unit">{s(it.unit)}</span> : null}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------- LwProgress */

export function LwProgress({
  label,
  value,
  max,
  unit,
  fmt,
}: {
  label?: string;
  value: number;
  max: number;
  unit?: string;
  fmt?: string;
}) {
  const v = n(value);
  const m = n(max);
  const f = s(fmt) || undefined;
  const pct = pctOf(v, m);
  return (
    <Panel>
      <div className="lw-row-between">
        <div className="lw-label">{s(label)}</div>
        <div className="lw-muted lw-mono">
          {lwFormat(v, f)} / {lwFormat(m, f)}
          {unit ? ` ${s(unit)}` : ''}
        </div>
      </div>
      <div className="lw-track">
        <div className="lw-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="lw-pct lw-mono">{pct.toFixed(0)}%</div>
    </Panel>
  );
}

/* ----------------------------------------------------------------- LwGauge */

export function LwGauge({
  label,
  value,
  max,
  unit,
  color,
  fmt,
}: {
  label?: string;
  value: number;
  max: number;
  unit?: string;
  color?: string;
  fmt?: string;
}) {
  const v = n(value);
  const m = n(max);
  const f = s(fmt) || undefined;
  const pct = pctOf(v, m);
  const R = 36;
  const CIRC = 2 * Math.PI * R;
  return (
    <Panel>
      <div className="lw-gauge">
        <svg
          width="84"
          height="84"
          viewBox="0 0 84 84"
          role="img"
          aria-label={`${s(label) || 'gauge'}: ${pct.toFixed(0)}% (${lwFormat(v, f)} / ${lwFormat(m, f)}${unit ? ` ${s(unit)}` : ''})`}
        >
          <circle
            cx="42"
            cy="42"
            r={R}
            fill="none"
            stroke="var(--surface-soft)"
            strokeWidth="11"
          />
          <circle
            cx="42"
            cy="42"
            r={R}
            fill="none"
            stroke={`var(--accent-${accentName(color) || 'violet'})`}
            strokeWidth="11"
            strokeLinecap="round"
            strokeDasharray={`${(CIRC * pct) / 100} ${CIRC}`}
            transform="rotate(-90 42 42)"
          />
        </svg>
        <div className="lw-gauge-center lw-display">{pct.toFixed(0)}%</div>
        <div className="lw-gauge-legend">
          <div className="lw-label">{s(label)}</div>
          <div className="lw-muted lw-mono">
            {lwFormat(v, f)} / {lwFormat(m, f)}
            {unit ? ` ${s(unit)}` : ''}
          </div>
        </div>
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------ LwBars */

export interface BarDatum {
  label?: string;
  value?: number;
}

export function LwBars({
  label,
  color,
  data,
  fmt,
}: {
  label?: string;
  color?: string;
  data: BarDatum[];
  fmt?: string;
}) {
  const f = s(fmt) || undefined;
  const max = data.reduce((m, d) => Math.max(m, n(d.value)), 0) || 1;
  return (
    <Panel>
      {label ? <div className="lw-label lw-mb">{s(label)}</div> : null}
      {data.map((d, i) => {
        const v = n(d.value);
        return (
          <div className="lw-bar-row" key={i}>
            <div className="lw-bar-label lw-muted">{s(d.label)}</div>
            <div className="lw-track">
              <div
                className="lw-fill"
                style={{
                  width: `${pctOf(v, max)}%`,
                  background: `var(--accent-${accentName(color) || 'violet'})`,
                }}
              />
            </div>
            <div className="lw-bar-value lw-mono">{lwFormat(v, f)}</div>
          </div>
        );
      })}
    </Panel>
  );
}

/* ------------------------------------------------------------- LwSparkline */

export function LwSparkline({
  label,
  color,
  values,
}: {
  label?: string;
  color?: string;
  values: number[];
}) {
  const vals = values.map(n);
  const stroke = `var(--accent-${accentName(color) || 'violet'})`;
  /* Measured width, so the viewBox is 1:1: a stretched SVG (the old
     preserveAspectRatio="none") turned a hover dot into an ellipse and made the
     stroke thicker horizontally than vertically. */
  const [wrapRef, wrapW] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [focused, setFocused] = useState(false);
  const fmtV = (v: number) => s(Math.round(v * 1000) / 1000);
  /* The hovered index at component scope: the pointer handlers set it, and so
     do the keyboard handlers on the focusable svg (a11y) — the tooltip and the
     screen-reader readout are the SAME values a pointer gets. */
  const at = hover !== null && hover >= 0 && hover < vals.length ? hover : null;
  /* Announced only while the chart is focused, so a pointer scrub never spams
     the live region; the node itself stays mounted (dynamically inserted live
     regions are missed by screen readers). */
  const readout =
    focused && at !== null ? `point ${at + 1} of ${vals.length}: ${fmtV(vals[at])}` : '';
  let body: ReactNode;
  if (vals.length < 2) {
    body = <div className="lw-empty lw-muted">(no data)</div>;
  } else {
    const W = wrapW > 0 ? wrapW : 300;
    const H = 64;
    const [mn, mx] = extent(vals);
    const range = mx - mn === 0 ? 1 : mx - mn;
    const pts = vals.map((v, i) => {
      const x = (W / (vals.length - 1)) * i;
      const y = H - ((v - mn) / range) * (H - 4) - 2;
      return [x, y] as const;
    });
    const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const area = `${line} ${W},${H} 0,${H}`;
    const pick = (e: ReactPointerEvent<SVGSVGElement>) => {
      const r = e.currentTarget.getBoundingClientRect();
      if (!r.width) return;
      const i = Math.round(((e.clientX - r.left) / r.width) * (vals.length - 1));
      setHover(Math.max(0, Math.min(vals.length - 1, i)));
    };
    body = (
      <>
        <svg
          className="lw-spark"
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={`${s(label) || 'sparkline'}: ${vals.length} values, min ${fmtV(mn)}, max ${fmtV(mx)}, latest ${fmtV(vals[vals.length - 1])}`}
          tabIndex={0}
          onPointerMove={pick}
          onPointerDown={pick}
          onPointerLeave={(e) => {
            if (e.pointerType === 'mouse') setHover(null);
          }}
          onFocus={(e) => {
            /* Keyboard focus (focus-visible) arms the live readout; a pointer
               click that merely focuses the svg does not — scrubbing with a
               mouse must not spam announcements. */
            setFocused(e.currentTarget.matches(':focus-visible'));
            if (hover === null) setHover(vals.length - 1);
          }}
          onBlur={() => {
            setFocused(false);
            setHover(null);
          }}
          onKeyDown={(e) => {
            let next: number | null = null;
            if (e.key === 'ArrowRight') next = Math.min(vals.length - 1, (hover ?? vals.length - 1) + 1);
            else if (e.key === 'ArrowLeft') next = Math.max(0, (hover ?? 0) - 1);
            else if (e.key === 'Home') next = 0;
            else if (e.key === 'End') next = vals.length - 1;
            else return;
            e.preventDefault();
            setHover(next);
          }}
        >
          <polygon points={area} fill={stroke} opacity="0.12" />
          <polyline
            points={line}
            fill="none"
            stroke={stroke}
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {at !== null && (
            <g className="lw-spark-cursor">
              <line x1={pts[at][0]} x2={pts[at][0]} y1="0" y2={H}
                    stroke={stroke} strokeWidth="1" strokeDasharray="3 3" opacity="0.55" />
              <circle cx={pts[at][0]} cy={pts[at][1]} r="3"
                      fill={stroke} stroke="var(--surface)" strokeWidth="1.5" />
            </g>
          )}
        </svg>
        {at !== null && (
          <div
            className="lw-line-tip lw-spark-tip"
            style={{
              left: `${(pts[at][0] / W) * 100}%`,
              transform: pts[at][0] > W / 2 ? 'translateX(-100%)' : 'none',
            }}
          >
            {fmtV(vals[at])}
          </div>
        )}
      </>
    );
  }
  return (
    <Panel>
      {label ? <div className="lw-label lw-mb">{s(label)}</div> : null}
      <div className="lw-spark-wrap" ref={wrapRef}>
        {body}
        <span className="sr-only" role="status">{readout}</span>
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------ LwLine */

/* Measured pixel width of an element. A chart with axis TEXT cannot simply
   stretch: an SVG with a fixed viewBox letterboxes inside a wider box (that is
   what `preserveAspectRatio` defaults to), which left the line chart a narrow
   strip centred in a full-width card. Measuring the container and building the
   viewBox at that width keeps the text crisp AND fills the card. */
function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setW((prev) => {
        const next = Math.round(el.clientWidth);
        return next === prev ? prev : next;
      });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

export interface LwLineSeries {
  name?: string;
  color?: string;
  values: number[];
}

/**
 * A multi-series line chart with axes and an optional legend, hand-rolled SVG
 * (no chart library). Colours are accent token names resolved through
 * `var(--accent-<name>)`; an explicit `yMin`/`yMax` is honoured, else the y
 * range auto-scales to the data. Total: fewer than 2 drawable points (all
 * series) degrades to the visible `(no data)` state and never throws.
 */
export function LwLine({
  label,
  color,
  series,
  xLabels,
  yMin,
  yMax,
  legend,
}: {
  label?: string;
  color?: string;
  series: LwLineSeries[];
  xLabels?: string[];
  yMin?: number;
  yMax?: number;
  legend?: boolean;
}) {
  /* Defensive copy: a hostile field never throws — it just drops out. */
  const drawable = series.filter((s_) => Array.isArray(s_.values) && s_.values.length >= 2);
  const showLegend = legend === undefined ? drawable.length > 1 : legend;
  const [wrapRef, wrapW] = useElementWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [focused, setFocused] = useState(false);
  const strokeOf = (s_: LwLineSeries) =>
    `var(--accent-${accentName(s_.color) || accentName(color) || 'violet'})`;
  /* Point count at component scope so the keyboard handlers and the readout
     can share it with the svg body (the widest series' length, exactly what
     the x axis and the pointer pick already use). */
  const nPts = drawable.length ? Math.max(...drawable.map((s_) => s_.values.length)) : 0;
  const labels = xLabels && xLabels.length === nPts ? xLabels : null;
  const tickText = (v: number) => s(Math.round(v * 1000) / 1000);
  const at = hover !== null && hover >= 0 && hover < nPts ? hover : null;
  /* Announced only while focused (see LwSparkline): same values a pointer
     gets, without pointer scrubbing spamming the live region. */
  const readout =
    focused && at !== null
      ? `${labels ? s(labels[at]) : `#${at + 1}`} — ` +
        drawable
          .filter((s_) => at < s_.values.length)
          .map((s_, i) => `${s(s_.name) || `Series ${i + 1}`}: ${tickText(s_.values[at])}`)
          .join('; ')
      : '';

  let body: ReactNode;
  if (drawable.length === 0) {
    body = <div className="lw-empty lw-muted">(no data)</div>;
  } else {
    /* Width follows the card, so the chart fills it instead of letterboxing to
       a fixed 340px strip. Height stays fixed (the aspect ratio is the chart's
       to choose); 340 is only the pre-measurement fallback for one frame. */
    const W = wrapW > 0 ? wrapW : 340;
    const H = 170;
    const X0 = 44;
    const X1 = W - 6;
    const Y0 = 12;
    const Y1 = 136;
    const xLabY = 154;
    const n = Math.max(...drawable.map((s_) => s_.values.length));
    /* y range: explicit bounds win (the contract keeps them ordered); else the
       data's own range, unwedged when the data is perfectly flat. */
    const flat = drawable.flatMap((s_) => s_.values);
    const [dLo, dHi] = extent(flat);
    let lo = yMin ?? dLo;
    let hi = yMax ?? dHi;
    const auto = yMin === undefined || yMax === undefined;
    if (auto && hi - lo === 0) hi = lo + 1;
    /* An explicit bound on the far side of the data — a lone `yMin` above
       every point is contract-legal (the yMin<yMax rule fires only when BOTH
       are present) — resolves hi < lo, and a negative range mirrors the
       chart: value 3 rendered at the top, 50 at the bottom. Swap instead of
       rendering an inverted axis; well-ordered bounds are untouched. */
    if (hi < lo) [lo, hi] = [hi, lo];
    const range = hi - lo === 0 ? 1 : hi - lo;
    const x = (i: number) => X0 + ((X1 - X0) * i) / (n - 1);
    const y = (v: number) => Y1 - ((v - lo) / range) * (Y1 - Y0);
    /* Gridlines: 5 levels with a label per level (min/mid/max at minimum). */
    const ticks = Array.from({ length: 5 }, (_, i) => lo + (range * i) / 4);
    /* x labels: given `xLabels` when they fit, else the point indices; never
       crowd — at most 5 evenly spaced (first and last always included). */
    const xIdx: number[] =
      n <= 5
        ? Array.from({ length: n }, (_, i) => i)
        : Array.from(new Set(
            Array.from({ length: 5 }, (_, k) => Math.round((k * (n - 1)) / 4))
          ));
    /* Pointer → nearest point index. The viewBox width tracks the element
       width, so this is near 1:1; scale defensively in case the first frame
       rendered at the fallback width. */
    const pick = (e: ReactPointerEvent<SVGSVGElement>) => {
      const r = e.currentTarget.getBoundingClientRect();
      if (!r.width) return;
      const px = ((e.clientX - r.left) * W) / r.width;
      const i = Math.round((px - X0) / ((X1 - X0) / (n - 1)));
      setHover(Math.max(0, Math.min(n - 1, i)));
    };
    /* Where the hovered points sit vertically — used to flip the readout below
       the chart when it would otherwise cover the very point being read. */
    const hoverYs = at === null ? [] : drawable
      .filter((s_) => at < s_.values.length)
      .map((s_) => y(s_.values[at]));
    const tipBelow = hoverYs.length > 0 && Math.min(...hoverYs) < 58;
    body = (
      <>
      <svg
        className="lw-line"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={
          (s(label) || 'line chart') + ': ' +
          drawable.map((s_) => {
            const [mn, mx] = extent(s_.values);
            const last = s_.values[s_.values.length - 1];
            return `${s(s_.name) || 'series'} from ${tickText(mn)} to ${tickText(mx)}, latest ${tickText(last)}`;
          }).join('; ')
        }
        tabIndex={0}
        onPointerMove={pick}
        onPointerDown={pick}
        onPointerLeave={(e) => {
          if (e.pointerType === 'mouse') setHover(null);
        }}
        onFocus={(e) => {
          /* See LwSparkline: only keyboard focus arms the live readout. */
          setFocused(e.currentTarget.matches(':focus-visible'));
          if (hover === null) setHover(n - 1);
        }}
        onBlur={() => {
          setFocused(false);
          setHover(null);
        }}
        onKeyDown={(e) => {
          let next: number | null = null;
          if (e.key === 'ArrowRight') next = Math.min(n - 1, (hover ?? n - 1) + 1);
          else if (e.key === 'ArrowLeft') next = Math.max(0, (hover ?? 0) - 1);
          else if (e.key === 'Home') next = 0;
          else if (e.key === 'End') next = n - 1;
          else return;
          e.preventDefault();
          setHover(next);
        }}
      >
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={X0} x2={X1} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth="1" />
            <text x={X0 - 6} y={y(t) + 3.5} textAnchor="end" className="lw-line-tick">
              {tickText(t)}
            </text>
          </g>
        ))}
        {xIdx.map((i) => (
          <text
            key={i}
            x={x(i)}
            y={xLabY}
            textAnchor={i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}
            className="lw-line-tick"
          >
            {labels ? s(labels[i]) : String(i)}
          </text>
        ))}
        {drawable.map((s_, si) => (
          <polyline
            key={si}
            points={s_.values
              .map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
              .join(' ')}
            fill="none"
            stroke={strokeOf(s_)}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
        {at !== null && (
          <g className="lw-line-cursor">
            <line x1={x(at)} x2={x(at)} y1={Y0} y2={Y1}
                  stroke="var(--line)" strokeWidth="1" strokeDasharray="3 3" />
            {drawable.map((s_, si) =>
              at < s_.values.length ? (
                <circle key={si} cx={x(at)} cy={y(s_.values[at])} r="3.5"
                        fill={strokeOf(s_)} stroke="var(--surface)" strokeWidth="1.5" />
              ) : null,
            )}
          </g>
        )}
      </svg>
      {at !== null && (
        <div
          className="lw-line-tip"
          style={{
            left: `${(x(at) / W) * 100}%`,
            transform: x(at) > W / 2 ? 'translateX(-100%)' : 'none',
            ...(tipBelow ? { top: 'auto', bottom: 2 } : null),
          }}
        >
          <div className="lw-line-tip-x">{labels ? s(labels[at]) : `#${at}`}</div>
          {drawable.map((s_, i) => (
            <div className="lw-line-tip-row" key={i}>
              <span className="lw-line-swatch" style={{ background: strokeOf(s_) }} />
              <span className="lw-muted">{s(s_.name) || `Series ${i + 1}`}</span>
              <b>{at < s_.values.length ? tickText(s_.values[at]) : '—'}</b>
            </div>
          ))}
        </div>
      )}
      </>
    );
  }
  return (
    <Panel>
      {label ? <div className="lw-label lw-mb">{s(label)}</div> : null}
      <div className="lw-line-wrap" ref={wrapRef}>
        {body}
        <span className="sr-only" role="status">{readout}</span>
      </div>
      {drawable.length > 0 && showLegend && (
        <div className="lw-line-legend">
          {drawable.map((s_, i) => (
            <span className="lw-line-legend-item" key={i}>
              <span className="lw-line-swatch" style={{ background: strokeOf(s_) }} />
              <span className="lw-muted">{s(s_.name) || `Series ${i + 1}`}</span>
            </span>
          ))}
        </div>
      )}
    </Panel>
  );
}

/* ----------------------------------------------------------------- LwTable */

export function LwTable({
  columns,
  rows,
  tagColumn,
}: {
  columns: string[];
  rows: string[][];
  tagColumn: boolean;
}) {
  const cols = columns;
  const tag = tagColumn === true;
  return (
    <Panel>
      <div role="table">
        {cols.length > 0 && (
          <div className="lw-tr lw-thead" role="row">
            {cols.map((c, i) => (
              <div
                key={i}
                className="lw-th"
                role="columnheader"
                style={{ flex: i === 0 ? 3 : 2, textAlign: i === cols.length - 1 ? 'right' : 'left' }}
              >
                {s(c).toUpperCase()}
              </div>
            ))}
          </div>
        )}
        {rows.map((r, ri) => (
          <div className="lw-tr" role="row" key={ri}>
            {r.map((cell, ci) => {
              const last = ci === r.length - 1;
              const align = last ? 'right' : 'left';
              return (
                <div key={ci} className="lw-td" role="cell" style={{ flex: ci === 0 ? 3 : 2, textAlign: align }}>
                  {last && tag ? (
                    <span className={`lw-pill lw-pill-${pillKind(s(cell))}`}>{s(cell)}</span>
                  ) : (
                    <span className={last ? 'lw-mono' : ''}>{s(cell)}</span>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </Panel>
  );
}

function pillKind(v: string): string {
  const t = v.toLowerCase();
  if (['done', 'ok', 'success', 'complete', 'completed'].includes(t)) return 'ok';
  if (['running', 'active', 'in progress'].includes(t)) return 'run';
  if (['failed', 'error', 'err'].includes(t)) return 'error';
  if (['warn', 'warning', 'pending'].includes(t)) return 'warn';
  return 'muted';
}

/* ------------------------------------------------------------------- LwLog */

export function LwLog({ title, lines }: { title?: string; lines: string[] }) {
  return (
    <Panel>
      {title ? <div className="lw-label lw-mb">{s(title)}</div> : null}
      <div className="lw-log lw-mono">
        {lines.map((ln, i) => (
          <div key={i}>{s(ln)}</div>
        ))}
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------- LwCode */

/**
 * Copy `text` without the async clipboard API. It needs a secure context, and a
 * board served over plain HTTP on a LAN is not one, so the copy would silently
 * do nothing there. Returns whether the copy actually happened — a button that
 * reports success it did not get is worse than no button.
 */
function legacyCopy(text: string): boolean {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // Off-screen rather than display:none: a hidden element cannot be selected.
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * A `code` block: the log visual plus a copy button.
 *
 * A log tail is for watching; a code block is usually something you want to
 * take away (a command, a snippet, a config). The button is always visible
 * rather than hover-revealed — an affordance nobody discovers is not one — and
 * it is a real `<button>`, so it is tab-reachable and has a focus ring. The
 * result is announced through the same `sr-only` live region the charts use,
 * and on failure it says so instead of pretending.
 */
export function LwCode({ title, text }: { title?: string; text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A board can be replaced while the "Copied" label is still showing; clearing
  // the timer on unmount keeps a stale callback from setting state on a gone
  // component.
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const copy = async () => {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        ok = legacyCopy(text);
      }
    } catch {
      ok = legacyCopy(text);
    }
    setState(ok ? 'copied' : 'failed');
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState('idle'), 2000);
  };

  return (
    <Panel>
      <div className="lw-row-between lw-mb">
        {title ? <div className="lw-label">{s(title)}</div> : <span />}
        <button
          type="button"
          className="lw-copy"
          data-state={state}
          onClick={copy}
        >
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
        </button>
      </div>
      <div className="lw-log lw-mono">
        {text.split('\n').map((ln, i) => (
          <div key={i}>{s(ln)}</div>
        ))}
      </div>
      <span className="sr-only" role="status">
        {state === 'copied'
          ? 'Code copied to the clipboard'
          : state === 'failed'
            ? 'Could not copy the code'
            : ''}
      </span>
    </Panel>
  );
}

/* -------------------------------------------------------------------- LwKv */

export interface KvItem {
  k?: string;
  v?: string;
}

export function LwKv({ items }: { items: KvItem[] }) {
  return (
    <Panel>
      {items.map((it, i) => (
        <div className="lw-row-between lw-kv-row" key={i}>
          <div className="lw-muted">{s(it.k)}</div>
          <div className="lw-mono">{s(it.v)}</div>
        </div>
      ))}
    </Panel>
  );
}

/* ------------------------------------------------------------------ LwList */

export function LwList({ label, items }: { label?: string; items: string[] }) {
  return (
    <Panel>
      {label ? <div className="lw-label lw-mb">{s(label)}</div> : null}
      {items.map((it, i) => (
        <div className="lw-list-row" key={i}>
          <span className="lw-bullet" />
          <span>{s(it)}</span>
        </div>
      ))}
    </Panel>
  );
}

/* --------------------------------------------------------------- LwCallout */

export function LwCallout({ text, tone }: { text?: string; tone?: string }) {
  const raw = s(tone).toLowerCase();
  const resolved =
    raw === 'ok' || raw === 'success'
      ? 'ok'
      : raw === 'warn' || raw === 'warning'
        ? 'warn'
        : raw === 'error' || raw === 'err' || raw === 'fail'
          ? 'error'
          : 'info';
  return (
    <Panel tone={resolved} className="lw-callout">
      <span className="lw-callout-bar" />
      <span className="lw-callout-text">{s(text)}</span>
    </Panel>
  );
}

/* --------------------------------------------------------------- LwDivider */

export function LwDivider() {
  return <div className="lw-divider" />;
}

/* ------------------------------------------------------------------- text */

/** A plain text block: a card carrying one paragraph. */
export function LwText({ text }: { text?: string }) {
  return (
    <Panel>
      <div className="lw-body-text">{s(text)}</div>
    </Panel>
  );
}

/** The visible fallback for a block type the renderer does not know. */
export function LwUnknown({ type }: { type?: string }) {
  return (
    <Panel>
      <div className="lw-body-text">[unknown block: {s(type)}]</div>
    </Panel>
  );
}

/* --------------------------------------------------------------- dispatcher */

/**
 * Render one block. Total by construction: an unknown or malformed block
 * becomes a visible value rather than an exception, so one bad block cannot
 * blank the board.
 */
export function renderBlock(block: unknown): ReactNode {
  if (!isObj(block)) {
    return <LwUnknown type={s(block)} />;
  }
  const b = block as Block;
  // An absent `type` defaulted to "text" in the retired emitter; preserve it.
  const t = b.type !== undefined ? b.type : 'text';
  switch (t) {
    case 'heading':
      return <LwHeading text={s(b.text)} eyebrow={s(b.eyebrow)} />;
    case 'text':
      return <LwText text={s(b.text)} />;
    case 'stat':
      return (
        <LwStats items={[{ label: s(b.label), value: lwStatValue(b.value), unit: s(b.unit) }]} />
      );
    case 'stats':
      return (
        <LwStats
          items={list(b.items)
            .filter(isObj)
            .map((it) => ({
              label: s(it.label),
              value: lwStatValue(it.value),
              unit: s(it.unit),
            }))}
        />
      );
    case 'progress':
      return (
        <LwProgress
          label={s(b.label)}
          value={n(b.value)}
          max={positive(b.max)}
          unit={s(b.unit)}
          fmt={s(b.fmt)}
        />
      );
    case 'gauge':
      return (
        <LwGauge
          label={s(b.label)}
          value={n(b.value)}
          max={positive(b.max)}
          unit={s(b.unit)}
          color={s(b.color)}
          fmt={s(b.fmt)}
        />
      );
    case 'bar': {
      const data: BarDatum[] = [];
      for (const d of list(b.data)) {
        if (Array.isArray(d) && d.length === 2) {
          data.push({ label: s(d[0]), value: num(d[1], 0) });
        } else if (isObj(d)) {
          data.push({ label: s(d.label), value: num(d.value, 0) });
        }
      }
      return <LwBars label={s(b.label)} color={s(b.color)} data={data} fmt={s(b.fmt)} />;
    }
    case 'sparkline':
    case 'chart':
      return (
        <LwSparkline
          label={s(b.label)}
          color={s(b.color)}
          values={list(b.values).map((x) => num(x, 0))}
        />
      );
    case 'line': {
      const series: LwLineSeries[] = list(b.series)
        .filter(isObj)
        .map((s_) => ({
          name: s(s_.name),
          color: s(s_.color),
          values: list(s_.values).map((x) => num(x, 0)),
        }));
      if (series.length === 0) {
        // Single-unnamed-series sugar (`series` is absent, `values` present).
        const sugar = list(b.values).map((x) => num(x, 0));
        if (sugar.length > 0) series.push({ values: sugar });
      }
      return (
        <LwLine
          label={s(b.label)}
          color={s(b.color)}
          series={series}
          xLabels={list(b.xLabels).map((x) => s(x))}
          yMin={b.yMin === undefined ? undefined : num(b.yMin, 0)}
          yMax={b.yMax === undefined ? undefined : num(b.yMax, 0)}
          legend={b.legend === undefined ? undefined : flag(b.legend)}
        />
      );
    }
    case 'table':
      return (
        <LwTable
          columns={list(b.columns).map(s)}
          rows={list(b.rows).map((r) => list(r).map(s))}
          tagColumn={flag(b.tagColumn)}
        />
      );
    case 'log': {
      const lines = list(b.lines).slice(-RENDER_LOG_LINES).map(s);
      return <LwLog title={s(b.title)} lines={lines.length ? lines : ['(no output)']} />;
    }
    case 'kv': {
      const items: KvItem[] = [];
      for (const pair of list(b.items)) {
        let k: unknown;
        let v: unknown;
        if (Array.isArray(pair) && pair.length === 2) {
          [k, v] = pair;
        } else if (isObj(pair)) {
          k = pair.k;
          v = pair.v;
        } else {
          k = pair;
          v = '';
        }
        items.push({ k: s(k), v: s(v) });
      }
      return <LwKv items={items} />;
    }
    case 'list':
      return <LwList label={s(b.label)} items={list(b.items).map(s)} />;
    case 'callout':
      return <LwCallout text={s(b.text)} tone={s(b.tone || 'info')} />;
    case 'code':
      return <LwCode title={s(b.title)} text={s(b.text)} />;
    case 'divider':
      return <LwDivider />;
    default:
      return <LwUnknown type={s(t)} />;
  }
}
