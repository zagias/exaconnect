import { useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi, type Insight } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, ExampleTag, Eyebrow, StatusPill, ago } from "../components";
import { useCustomer, useStormToggle, who } from "../customer";

const KIND_LABEL: Record<Insight["kind"], string> = {
  storm_warning: "Hurricane watch",
  bill_shock: "Bill forecast",
  anomaly: "Carrier anomaly",
};

const SEVERITY: Record<Insight["severity"], { health: "ok" | "warn" | "bad"; word: string }> = {
  info: { health: "ok", word: "Note" },
  warning: { health: "warn", word: "Warning" },
  critical: { health: "bad", word: "Act now" },
};

/** A list of insights with actions: switch Storm Mode for the site, acknowledge. */
export function InsightList({ items, reload, readOnly = false }: { items: Insight[]; reload: () => void; readOnly?: boolean }) {
  const { current, busy, error, toggle } = useStormToggle();
  const [ackError, setAckError] = useState<string | null>(null);
  const ack = async (i: Insight) => {
    setAckError(null);
    try {
      await api(`/insights/${i.id}/acknowledge`, { method: "POST" });
      reload();
    } catch (e) {
      setAckError((e as Error).message);
    }
  };
  if (items.length === 0) return <p className="muted">Nothing to flag right now.</p>;
  return (
    <>
      <ErrorNote error={error ?? ackError} />
      <ul className="insights">
        {items.map((i) => {
          const site = current?.sites.find((s) => s.name === i.site);
          const suggest = i.kind === "storm_warning" && i.data.suggest_storm_mode === true && site && !site.storm_mode;
          const sev = SEVERITY[i.severity];
          return (
            <li key={i.id} className={`${i.kind} ${i.severity}`}>
              <div className="insight-head">
                <StatusPill health={sev.health}>{sev.word}</StatusPill>
                <span className="eyebrow" style={{ margin: 0 }}>
                  {KIND_LABEL[i.kind]}
                </span>
                {i.example && <ExampleTag />}
                <span className="muted small">
                  {i.resolved_at ? `cleared ${ago(i.resolved_at)}` : `since ${ago(i.first_seen)}, checked ${ago(i.last_seen)}`}
                </span>
              </div>
              <strong>{i.title.replace(/^Example data: /, "")}</strong>
              <p>{i.detail}</p>
              <div className="form-actions">
                {suggest && !readOnly && site && (
                  <button className="button small storm-on" disabled={busy} onClick={() => toggle(site, true)}>
                    Switch Storm Mode on for {site.name}
                  </button>
                )}
                {typeof i.data.advisory_url === "string" && i.data.advisory_url && (
                  <a className="small" href={i.data.advisory_url} target="_blank" rel="noreferrer">
                    NHC advisory
                  </a>
                )}
                {i.kind === "bill_shock" && (
                  <Link className="small" to="/metering">
                    Metering
                  </Link>
                )}
                {!readOnly && !i.resolved_at &&
                  (i.acknowledged_by ? (
                    <span className="muted small">Acknowledged by {who(i.acknowledged_by)}</span>
                  ) : (
                    <button className="button secondary small" onClick={() => ack(i)}>
                      Acknowledge
                    </button>
                  ))}
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

export default function Insights() {
  const { user } = useAuth();
  const { current } = useCustomer();
  const [history, setHistory] = useState(false);
  const q = new URLSearchParams();
  if (user?.role === "admin" && current) q.set("customer_id", current.id);
  if (history) q.set("include_resolved", "true");
  const list = useApi<Insight[]>(`/insights?${q}`, 30_000);
  const [busy, setBusy] = useState(false);
  const example = async (on: boolean) => {
    setBusy(true);
    try {
      await api(`/ai/storm-watch/example?on=${on}`, { method: "POST" });
      list.reload();
    } finally {
      setBusy(false);
    }
  };
  const items = list.data ?? [];
  const hasExample = items.some((i) => i.example && !i.resolved_at);
  return (
    <>
      <div className="page-head">
        <Eyebrow>Insights</Eyebrow>
        <h1>What the network is telling you</h1>
        <p className="muted">
          Hurricanes forecast near your sites (from the US National Hurricane Center, checked every 15 minutes), bills
          heading over commit, and carrier paths behaving worse than usual for the hour. Each one says how it was worked
          out.
        </p>
        <ErrorNote error={list.error} />
      </div>
      <section className="card">
        <div className="card-head">
          <h2>{history ? "All insights" : "Open insights"}</h2>
          <div className="form-actions">
            <label className="small">
              <input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} /> Include cleared
            </label>
            {user?.role === "admin" && (
              <button className="button secondary small" disabled={busy} onClick={() => example(!hasExample)}>
                {hasExample ? "Clear example hurricane" : "Show an example hurricane"}
              </button>
            )}
          </div>
        </div>
        <InsightList items={items} reload={list.reload} />
      </section>
    </>
  );
}

/** The most pressing open insights, for the overview. */
export function InsightSummary() {
  const { user } = useAuth();
  const { current } = useCustomer();
  const q = user?.role === "admin" && current ? `?customer_id=${current.id}` : "";
  const list = useApi<Insight[]>(`/insights${q}`, 30_000);
  const top = (list.data ?? []).filter((i) => i.severity !== "info").slice(0, 3);
  if (top.length === 0) return null;
  return (
    <section className="card span-12">
      <div className="card-head">
        <h2>Needs a look</h2>
        <Link to="/insights">All insights</Link>
      </div>
      <InsightList items={top} reload={list.reload} />
    </section>
  );
}
