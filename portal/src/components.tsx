import type { ReactNode } from "react";
import { num, type Health } from "./api";

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

// ---- Line chart ----

export interface Series {
  key: string; // path name: carrier-a, carrier-b, sat
  label: string;
  points: { t: number; v: number | null }[];
}

const W = 360;
const H = 190;
const PAD = { l: 50, r: 8, t: 16, b: 22 };

/** A small SVG line chart. One line per path; an optional dashed SLA line.
 *  Gaps in the data (no reading) break the line rather than joining across. */
export function LineChart({
  series,
  from,
  to,
  unit,
  sla,
  title,
}: {
  series: Series[];
  from: number;
  to: number;
  unit: string;
  sla?: number | null;
  title: string;
}) {
  const values = series.flatMap((s) => s.points.map((p) => p.v)).filter((v): v is number => v !== null);
  const top = niceMax(Math.max(sla ?? 0, ...values, 0) * 1.1 || 1);
  const x = (t: number) => PAD.l + ((t - from) / Math.max(1, to - from)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - v / top) * (H - PAD.t - PAD.b);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  const times = [from, (from + to) / 2, to];

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, ${series.map((s) => s.label).join(", ")}`}>
        {ticks.map((v) => (
          <g key={v}>
            <line className="grid-line" x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} />
            <text className="axis" x={PAD.l - 6} y={y(v) + 4} textAnchor="end">
              {formatTick(v)}
              {unit}
            </text>
          </g>
        ))}
        {times.map((t, i) => (
          <text key={t} className="axis" x={x(t)} y={H - 6} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>
            {new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
          </text>
        ))}
        {sla != null && (
          <g>
            <line className="sla-line" x1={PAD.l} x2={W - PAD.r} y1={y(sla)} y2={y(sla)} />
            <text className="sla-label" x={W - PAD.r} y={y(sla) - 4} textAnchor="end">
              SLA {formatTick(sla)}
              {unit}
            </text>
          </g>
        )}
        {series.map((s) => (
          <path key={s.key} className={`series series-${s.key}`} d={pathD(s.points, x, y)} />
        ))}
      </svg>
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

function pathD(points: Series["points"], x: (t: number) => number, y: (v: number) => number): string {
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

function niceMax(v: number): number {
  const mag = 10 ** Math.floor(Math.log10(v));
  // Quarters of these stay round numbers, so the four gridlines read cleanly.
  for (const m of [1, 2, 4, 6, 8, 10]) if (m * mag >= v) return m * mag;
  return 10 * mag;
}

function formatTick(v: number): string {
  return v >= 10 || v === 0 ? v.toFixed(0) : v.toFixed(v >= 1 ? 1 : 2);
}
