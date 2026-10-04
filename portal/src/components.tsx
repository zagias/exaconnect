import { useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { num, type Health } from "./api";
import "./charts.css";

export type { Health };

const healthWord: Record<Health, string> = { ok: "Within SLA", warn: "At risk", bad: "Down" };

/** Status with its word and mark (round, diamond, square), never colour alone. */
export function StatusPill({ health, children }: { health: Health; children?: ReactNode }) {
  return <span className={`pill ${health}`}>{children ?? healthWord[health]}</span>;
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <div className="eyebrow">{children}</div>;
}

export function ExampleTag() {
  return <span className="tag">Example data</span>;
}

export function StatTile({ label, figure, change, example }: { label: string; figure: string; change?: string; example?: boolean }) {
  return (
    <section className="card span-4">
      <div className="card-head" style={{ marginBottom: 0 }}>
        <Eyebrow>{label}</Eyebrow>
        {example && <ExampleTag />}
      </div>
      <div className="stat-figure">{figure}</div>
      {change && <div className="muted">{change}</div>}
    </section>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <p className="pill bad" role="alert">
      {error}
    </p>
  );
}

/** "12.3 ms", or a dash when there is no reading. */
export function fmt(v: unknown, unit: string, digits = 1): string {
  const n = num(v);
  return n === null ? "–" : `${n.toFixed(digits)}${unit}`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}


export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---- Dates and times ----

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

function localDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function hhmm(d: Date, seconds = false): string {
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) });
}

/** "4 Oct", with the year only when it is not this year. */
export function dayMonth(d: Date): string {
  const year = d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : "";
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${year}`;
}
// Spelled out here: some browsers write "Sept" for en-GB.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "14:41" today, "Yesterday 19:30", otherwise "2 Oct 14:00". */
export function when(iso: string | number, seconds = false): string {
  const d = new Date(iso);
  const days = Math.round((localDay(new Date()) - localDay(d)) / DAY);
  if (days === 0) return hhmm(d, seconds);
  if (days === 1) return `Yesterday ${hhmm(d, seconds)}`;
  return `${dayMonth(d)} ${hhmm(d, seconds)}`;
}

/** A timestamp written by `when`, with the full date and time on hover. */
export function Stamp({ iso, seconds = false }: { iso: string; seconds?: boolean }) {
  const d = new Date(iso);
  return (
    <time className="mono stamp" dateTime={d.toISOString()} title={d.toLocaleString("en-GB", { dateStyle: "full", timeStyle: "medium" })}>
      {when(iso, seconds)}
    </time>
  );
}

/** "1 Oct to now", "3 Oct 15:30 to 4 Oct 15:30". `to` within five minutes of now reads "now". */
export function rangeLabel(from: number, to: number): string {
  const f = (t: number) => {
    const d = new Date(t);
    return d.getHours() === 0 && d.getMinutes() === 0 ? dayMonth(d) : `${dayMonth(d)} ${hhmm(d)}`;
  };
  return `${f(from)} to ${Math.abs(Date.now() - to) < 5 * MIN ? "now" : f(to)}`;
}

// ---- Line chart ----

export interface Series {
  key: string; // path name (carrier-a, carrier-b, sat) or in / out
  label: string;
  /** `w` weights the point when several are averaged into one, such as probes sent
   *  for a loss percentage, so a bucket reads sum lost / sum sent. */
  points: { t: number; v: number | null; w?: number | null }[];
}

interface Pt {
  t: number;
  v: number | null;
  lo?: number;
  hi?: number;
}

interface Drawn {
  key: string;
  label: string;
  pts: Pt[];
  /** Milliseconds each drawn point stands for. */
  res: number;
  bucketed: boolean;
}

/** Bucket sizes for downsampling: round durations so a point means "a 5-minute average". */
const BUCKETS = [10e3, 15e3, 30e3, MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY];
/** Steps for the time axis, chosen by span. */
const TICK_STEPS = [MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY];
const PAD = { r: 10, t: 12, b: 24 };
const CHAR = 6.7; // IBM Plex Mono at 11 px
const PX_PER_BUCKET = 2.5;

/** A line chart drawn at its real pixel width, so text stays 11 px at any size.
 *  One line per series, an optional dashed SLA line and reference lines (95th
 *  percentile, commit). Long ranges are averaged into buckets sized to the width,
 *  with a faint min–max band, so spikes keep their true weight. Gaps in the data
 *  (a null point, or a jump longer than `gapMs`) break the line. Hover, touch or
 *  the arrow keys show a crosshair with every series' value at that time. */
export function LineChart({
  series,
  from,
  to,
  unit,
  sla,
  refs = [],
  title,
  height = 200,
  gapMs,
  band = true,
  note,
}: {
  series: Series[];
  from: number;
  to: number;
  unit: string;
  sla?: number | null;
  /** Extra horizontal reference lines, such as a 95th percentile or a commit. */
  refs?: { value: number; label: string; kind: "p95" | "commit" }[];
  title: string;
  height?: number;
  /** Break the line where readings are further apart than this. */
  gapMs?: number;
  /** Draw the min–max band behind averaged lines. */
  band?: boolean;
  /** A short line under the title, such as the period ("1 Oct to now"). */
  note?: ReactNode;
}) {
  const [box, measured] = useWidth();
  const [hoverT, setHoverT] = useState<number | null>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [tipWidth, setTipWidth] = useState(190);
  useLayoutEffect(() => {
    const w = tip.current?.offsetWidth;
    if (w && w !== tipWidth) setTipWidth(w);
  });
  const width = measured || 360;
  const span = Math.max(1, to - from);

  // Downsample first (against a rough plot width), then scale to what is drawn.
  const roughPlot = Math.max(120, width - 50 - PAD.r);
  const want = span / (roughPlot / PX_PER_BUCKET);
  const drawn: Drawn[] = series.map((s) => {
    const inRange = s.points.filter((p) => p.t >= from - want && p.t <= to + want);
    const spacing = medianSpacing(inRange);
    const bucket = BUCKETS.find((b) => b >= want) ?? BUCKETS[BUCKETS.length - 1];
    const use = spacing > 0 && bucket >= spacing * 1.5 ? bucket : 0;
    const gap = gapMs ? Math.max(gapMs, use * 2.5) : 0;
    return { key: s.key, label: s.label, pts: prepare(inRange, use, gap), res: use || spacing, bucketed: use > 0 };
  });

  // Scale to the lines and limits; a band that reaches higher is clipped at the top
  // rather than squashing the averages into the floor.
  const values: number[] = [];
  for (const d of drawn) for (const p of d.pts) if (p.v !== null) values.push(p.v);
  const top = niceMax(Math.max(sla ?? 0, ...refs.map((r) => r.value), ...values, 0) * 1.1 || 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  const tickDigits = [0, 1, 2, 3].find((n) => ticks.every((v) => Math.abs(Number(v.toFixed(n)) - v) < 1e-9)) ?? 3;
  const tickText = (v: number) => v.toFixed(tickDigits);
  const padL = Math.ceil(Math.max(...ticks.map((v) => tickText(v).length)) * CHAR) + 12;
  const plotW = Math.max(40, width - padL - PAD.r);
  const plotH = height - PAD.t - PAD.b;
  const x = (t: number) => padL + ((t - from) / span) * plotW;
  const y = (v: number) => PAD.t + (1 - Math.min(v, top) / top) * plotH;
  const xTicks = timeTicks(from, to, plotW);

  // Reference lines, labelled at the right edge; labels that would collide move
  // below their own line, then further down until they clear.
  const lines = [
    ...(sla != null ? [{ key: "sla", value: sla, text: `SLA ${limitText(sla)}${unit}`, line: "sla-line", label: "sla-label" }] : []),
    ...refs.map((r) => ({ key: r.kind, value: r.value, text: `${r.label} ${limitText(r.value)}${unit}`, line: `ref-line ref-${r.kind}`, label: `ref-label ref-${r.kind}` })),
  ]
    .map((l) => ({ ...l, ly: y(l.value), base: 0 }))
    .sort((a, b) => a.ly - b.ly);
  const taken: number[] = [];
  for (const l of lines) {
    const fits = (b: number) => b - 10 >= PAD.t - 8 && b <= PAD.t + plotH - 2 && taken.every((o) => Math.abs(o - b) >= 13);
    let base = [l.ly - 4, l.ly + 13].find(fits);
    if (base === undefined) {
      base = l.ly + 13;
      while (!fits(base) && base < height) base += 13;
    }
    taken.push(base);
    l.base = base;
  }

  // Hover: snap to the nearest drawn time across all series.
  const times = [...new Set(drawn.flatMap((d) => d.pts.filter((p) => p.v !== null).map((p) => p.t)))].sort((a, b) => a - b);
  const snapped = hoverT === null || times.length === 0 ? null : times[nearest(times, hoverT)];
  const readings =
    snapped === null
      ? []
      : drawn.map((d) => {
          const tol = Math.max(d.res, 1000) * 0.75;
          const vals = d.pts.filter((p) => p.v !== null);
          const p = vals.length ? vals[nearest(vals.map((q) => q.t), snapped)] : undefined;
          return { d, p: p && Math.abs(p.t - snapped) <= tol ? p : undefined };
        });
  const res = Math.max(0, ...drawn.map((d) => d.res));
  const bucketed = drawn.some((d) => d.bucketed);

  const tFromEvent = (e: PointerEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return from + ((e.clientX - r.left) / r.width) * span;
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (times.length === 0) return;
    const i = snapped === null ? times.length - 1 : times.indexOf(snapped);
    const step = e.shiftKey ? 10 : 1;
    const next: Record<string, number> = {
      ArrowLeft: Math.max(0, i - step),
      ArrowRight: Math.min(times.length - 1, i + step),
      Home: 0,
      End: times.length - 1,
    };
    if (e.key in next) {
      e.preventDefault();
      setHoverT(times[next[e.key]]);
    } else if (e.key === "Escape") setHoverT(null);
  };

  const cx = snapped === null ? 0 : x(snapped);
  // Tooltip beside the crosshair, flipped to the left near the right edge and kept inside the chart.
  const tipW = Math.min(tipWidth, width);
  const tipX = Math.max(0, Math.min(width - tipW, cx + 14 + tipW > width ? cx - 14 - tipW : cx + 14));

  return (
    <figure className="chart xchart">
      <figcaption>
        <span className="chart-title">
          {title} <span className="chart-unit">{unit.trim()}</span>
        </span>
        {(note || res > 0) && (
          <span className="chart-note">
            {note}
            {note && res > 0 && " · "}
            {res > 0 && (bucketed ? `${durationLabel(res)} averages${band ? ", shaded min–max" : ""}` : `every ${durationLabel(res)}`)}
          </span>
        )}
      </figcaption>
      <div
        ref={box}
        className="chart-box"
        tabIndex={0}
        onKeyDown={onKey}
        onFocus={() => times.length && hoverT === null && setHoverT(times[times.length - 1])}
        onBlur={() => setHoverT(null)}
      >
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}, ${series.map((s) => s.label).join(", ")}`}>
          {ticks.map((v) => (
            <g key={v}>
              <line className="grid-line" x1={padL} x2={padL + plotW} y1={y(v)} y2={y(v)} />
              <text className="axis" x={padL - 6} y={y(v) + 4} textAnchor="end">
                {tickText(v)}
              </text>
            </g>
          ))}
          {xTicks.map((k) => {
            const half = (k.label.length * CHAR) / 2;
            const px = x(k.t);
            const anchor = px - half < padL - 4 ? "start" : px + half > width - 2 ? "end" : "middle";
            return (
              <g key={k.t}>
                <line className="tick" x1={px} x2={px} y1={PAD.t + plotH} y2={PAD.t + plotH + 4} />
                <text className="axis" x={px} y={height - 6} textAnchor={anchor}>
                  {k.label}
                </text>
              </g>
            );
          })}
          {band &&
            drawn.map((d) =>
              d.bucketed ? <path key={`band-${d.key}`} className={`band series-${d.key}`} d={bandD(d.pts, x, y)} /> : null,
            )}
          {lines.map((l) => (
            <line key={l.key} className={l.line} x1={padL} x2={padL + plotW} y1={l.ly} y2={l.ly} />
          ))}
          {drawn.map((d) => (
            <path key={d.key} className={`series series-${d.key}`} d={pathD(d.pts, x, y)} />
          ))}
          {lines.map((l) => (
            <text key={l.key} className={`${l.label} halo`} x={padL + plotW} y={l.base} textAnchor="end">
              {l.text}
            </text>
          ))}
          {values.length === 0 && (
            <text className="axis empty" x={padL + plotW / 2} y={PAD.t + plotH / 2} textAnchor="middle">
              No readings in this window
            </text>
          )}
          {snapped !== null && (
            <g className="crosshair" aria-hidden="true">
              <line x1={cx} x2={cx} y1={PAD.t} y2={PAD.t + plotH} />
              {readings.map(({ d, p }) =>
                p && p.v !== null ? <circle key={d.key} className={`dot series-${d.key}`} cx={cx} cy={y(p.v)} r={3.5} /> : null,
              )}
            </g>
          )}
          <rect
            className="hit"
            x={padL}
            y={PAD.t}
            width={plotW}
            height={plotH}
            onPointerMove={(e) => setHoverT(tFromEvent(e))}
            onPointerDown={(e) => setHoverT(tFromEvent(e))}
            onPointerLeave={(e) => e.pointerType === "mouse" && setHoverT(null)}
          />
        </svg>
        {snapped !== null && (
          <div
            ref={tip}
            className="chart-tip"
            role="status"
            style={{ left: tipX, top: PAD.t }}
          >
            <div className="tip-time">{tipTime(snapped, span)}</div>
            {readings.map(({ d, p }) => (
              <div key={d.key} className="tip-row">
                <span className={`swatch series-${d.key}`} aria-hidden="true" />
                <span className="tip-label">{d.label}</span>
                <span className="tip-value">
                  {p && p.v !== null ? `${formatValue(p.v)}${unit}` : "–"}
                  {p && p.hi !== undefined && p.lo !== undefined && p.hi - p.lo > Math.max(1e-9, Math.abs(p.v ?? 0) * 0.05) && (
                    <span className="tip-range">
                      {" "}
                      ({formatValue(p.lo)}–{formatValue(p.hi)})
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      <ul className="legend">
        {series.map((s) => (
          <li key={s.key}>
            <span className={`swatch series-${s.key}`} aria-hidden="true" />
            {s.label}
          </li>
        ))}
      </ul>
    </figure>
  );
}

/** Width of an element in CSS pixels, kept current as it resizes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(Math.floor(el.clientWidth));
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const cw = Math.floor(entries[0].contentRect.width);
      setW((prev) => (prev === cw ? prev : cw));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function medianSpacing(points: Series["points"]): number {
  const ts = points.filter((p) => p.v !== null).map((p) => p.t);
  if (ts.length < 2) return 0;
  const d: number[] = [];
  for (let i = 1; i < ts.length; i++) d.push(ts[i] - ts[i - 1]);
  d.sort((a, b) => a - b);
  return d[d.length >> 1];
}

/** Raw points (bucket 0) or bucket averages weighted by `w`, with each bucket's
 *  min and max. A null point, or a jump longer than `gap`, becomes a null so the
 *  line breaks. */
function prepare(points: Series["points"], bucket: number, gap: number): Pt[] {
  const out: Pt[] = [];
  const brk = (t: number) => {
    if (out.length && out[out.length - 1].v !== null) out.push({ t, v: null });
  };
  const push = (p: Pt) => {
    const last = out[out.length - 1];
    if (gap && last && last.v !== null && p.t - last.t > gap) out.push({ t: last.t + 1, v: null });
    out.push(p);
  };
  if (!bucket) {
    for (const p of points) {
      if (p.v === null) brk(p.t);
      else push({ t: p.t, v: p.v });
    }
    return out;
  }
  let key = NaN;
  let n = 0, st = 0, sv = 0, sw = 0, lo = Infinity, hi = -Infinity;
  const flush = () => {
    if (sw > 0) push({ t: st / n, v: sv / sw, lo, hi });
    n = st = sv = sw = 0;
    lo = Infinity;
    hi = -Infinity;
  };
  for (const p of points) {
    if (p.v === null) {
      flush();
      key = NaN;
      brk(p.t);
      continue;
    }
    const b = Math.floor(p.t / bucket);
    if (b !== key) {
      flush();
      key = b;
    }
    const w = p.w == null ? 1 : p.w;
    if (!(w > 0)) continue;
    n++;
    st += p.t;
    sv += p.v * w;
    sw += w;
    lo = Math.min(lo, p.v);
    hi = Math.max(hi, p.v);
  }
  flush();
  return out;
}

/** Index of the value in a sorted array closest to `t`. */
function nearest(sorted: number[], t: number): number {
  let lo = 0;
  let hi = sorted.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 && Math.abs(sorted[lo - 1] - t) <= Math.abs(sorted[lo] - t) ? lo - 1 : lo;
}

/** Round clock times across the span: minutes for an hour, hours for a day or two,
 *  "4 Oct" for longer. Midnight inside a multi-hour span reads as the date. */
function timeTicks(from: number, to: number, plotW: number): { t: number; label: string }[] {
  const span = to - from;
  const target = Math.max(2, Math.floor(plotW / 72));
  const step = TICK_STEPS.find((s) => span / s <= target) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const out: { t: number; label: string }[] = [];
  const start = new Date(from);
  start.setHours(0, 0, 0, 0);
  if (step >= DAY) {
    const every = step / DAY;
    for (const d = new Date(start); d.getTime() <= to; d.setDate(d.getDate() + 1)) {
      if (d.getTime() < from) continue;
      if (every > 1 && (d.getDate() - 1) % every !== 0) continue;
      out.push({ t: d.getTime(), label: dayMonth(d) });
    }
    return out;
  }
  for (let t = start.getTime() + Math.ceil((from - start.getTime()) / step) * step; t <= to; t += step) {
    const d = new Date(t);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    out.push({ t, label: midnight && span > 6 * HOUR ? dayMonth(d) : hhmm(d) });
  }
  return out;
}

function tipTime(t: number, span: number): string {
  const d = new Date(t);
  return `${dayMonth(d)} ${hhmm(d, span <= 2 * HOUR)}`;
}

function durationLabel(ms: number): string {
  if (ms < MIN) return `${Math.round(ms / 1000)} s`;
  if (ms < HOUR) return `${Math.round(ms / MIN)} min`;
  if (ms < DAY) return `${Math.round(ms / HOUR)} h`;
  return `${Math.round(ms / DAY)} days`;
}

function pathD(points: Pt[], x: (t: number) => number, y: (v: number) => number): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p.v === null) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

/** The min–max band of bucketed points, one closed shape per unbroken run. */
function bandD(points: Pt[], x: (t: number) => number, y: (v: number) => number): string {
  let d = "";
  let run: Pt[] = [];
  const close = () => {
    if (run.length > 1) {
      d += run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.hi as number).toFixed(1)}`).join("");
      d += [...run].reverse().map((p) => `L${x(p.t).toFixed(1)},${y(p.lo as number).toFixed(1)}`).join("") + "Z";
    }
    run = [];
  };
  for (const p of points) {
    if (p.v === null || p.hi === undefined) close();
    else run.push(p);
  }
  close();
  return d;
}

function niceMax(v: number): number {
  const mag = 10 ** Math.floor(Math.log10(v));
  // Quarters of these stay round numbers, so the four gridlines read cleanly.
  for (const m of [1, 2, 4, 6, 8, 10]) if (m * mag >= v) return m * mag;
  return 10 * mag;
}

/** A reading: whole numbers from 100, one decimal from 10, two below. */
export function formatValue(v: number): string {
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
}

/** A limit as it was set: "30", "1", "89.1". */
function limitText(v: number): string {
  return Number.isInteger(v) ? String(v) : formatValue(v).replace(/\.?0+$/, "");
}
