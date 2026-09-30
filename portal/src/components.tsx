import type { ReactNode } from "react";

export type Health = "ok" | "warn" | "bad";

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
