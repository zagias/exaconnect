import { useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi, type Insight } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, ExampleTag, Eyebrow, StatusPill, ago } from "../components";
import { useCustomer, useStormToggle, who } from "../customer";

const KIND_LABEL: Record<Insight["kind"], string> = {
  storm_warning: "Hurricane watch",
  hazard: "Disaster watch",
  bill_shock: "Bill forecast",
  anomaly: "Carrier anomaly",
};

/** Sites an insight names (hurricane and disaster watches list every site in range). */
interface InsightSite {
  name: string;
  suggest_storm_mode?: boolean;
}

function sitesToSwitch(i: Insight): string[] {
  if (i.kind !== "storm_warning" && i.kind !== "hazard") return [];
  const listed = Array.isArray(i.data.sites)
    ? (i.data.sites as InsightSite[])
    : null;
  if (listed)
    return listed
      .filter((s) => s.suggest_storm_mode === true)
      .map((s) => s.name);
  return i.data.suggest_storm_mode === true && i.site ? [i.site] : [];
}

const SEVERITY: Record<
  Insight["severity"],
  { health: "ok" | "warn" | "bad"; word: string }
> = {
  info: { health: "ok", word: "Note" },
  warning: { health: "warn", word: "Warning" },
  critical: { health: "bad", word: "Act now" },
};

/** A list of insights with actions: switch Storm Mode for the site, acknowledge. */
export function InsightList({
  items,
  reload,
  readOnly = false,
}: {
  items: Insight[];
  reload: () => void;
  readOnly?: boolean;
}) {
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
  if (items.length === 0)
    return <p className="muted">Nothing to flag right now.</p>;
  return (
    <>
      <ErrorNote error={error ?? ackError} />
      <ul className="insights">
        {items.map((i) => {
          const toSwitch = (current?.sites ?? []).filter(
            (s) => sitesToSwitch(i).includes(s.name) && !s.storm_mode,
          );
          const links = Array.isArray(i.data.links)
            ? (i.data.links as { label: string; url: string }[])
            : [];
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
                  {i.resolved_at
                    ? `cleared ${ago(i.resolved_at)}`
                    : `since ${ago(i.first_seen)}, checked ${ago(i.last_seen)}`}
                </span>
              </div>
              <strong>{i.title.replace(/^Example data: /, "")}</strong>
              <p>{i.detail}</p>
              <div className="form-actions">
                {!readOnly &&
                  !i.resolved_at &&
                  toSwitch.map((site) => (
                    <button
                      key={site.id}
                      className="button small storm-on"
                      disabled={busy}
                      onClick={() => toggle(site, true)}
                    >
                      Switch Storm Mode on for {site.name}
                    </button>
                  ))}
                {typeof i.data.advisory_url === "string" &&
                  i.data.advisory_url && (
                    <a
                      className="small"
                      href={i.data.advisory_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      NHC advisory
                    </a>
                  )}
                {links.map((l) => (
                  <a
                    key={l.url + l.label}
                    className="small"
                    href={l.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {l.label}
                  </a>
                ))}
                {i.kind === "bill_shock" && (
                  <Link className="small" to="/metering">
                    Metering
                  </Link>
                )}
                {!readOnly &&
                  !i.resolved_at &&
                  (i.acknowledged_by ? (
                    <span className="muted small">
                      Acknowledged by {who(i.acknowledged_by)}
                    </span>
                  ) : (
                    <button
                      className="button secondary small"
                      onClick={() => ack(i)}
                    >
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
          Hurricanes forecast near your sites (from the US National Hurricane
          Center, checked every 15 minutes); earthquakes, tsunami messages,
          floods, volcanoes and wildfires near them (from USGS, GDACS and
          tsunami.gov, checked every 10 minutes, with one alert per event
          however many sources report it); bills heading over commit; and
          carrier paths behaving worse than usual for the hour. Each one says
          how it was worked out.
        </p>
        <ErrorNote error={list.error} />
      </div>
      <section className="card">
        <div className="card-head">
          <h2>{history ? "All insights" : "Open insights"}</h2>
          <div className="form-actions">
            <label className="small">
              <input
                type="checkbox"
                checked={history}
                onChange={(e) => setHistory(e.target.checked)}
              />{" "}
              Include cleared
            </label>
            {user?.role === "admin" && (
              <button
                className="button secondary small"
                disabled={busy}
                onClick={() => example(!hasExample)}
              >
                {hasExample ? "Clear example alerts" : "Show example alerts"}
              </button>
            )}
          </div>
        </div>
        <InsightList items={items} reload={list.reload} />
      </section>
    </>
  );
}

const SEVERITY_ORDER: Record<Insight["severity"], number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

/** Open insights that should count as "needs attention": warnings and worse, not yet acknowledged. */
export function pressing(items: Insight[]): Insight[] {
  return items
    .filter((i) => !i.resolved_at && i.severity !== "info")
    .sort(
      (a, b) =>
        SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] ||
        Number(!!a.acknowledged_by) - Number(!!b.acknowledged_by) ||
        b.first_seen.localeCompare(a.first_seen),
    );
}

/** The most pressing open insights for the overview: one compact row each, with the
 *  long text and source links behind "Details". Rows only; the overview owns the card. */
export function InsightSummary({
  items,
  reload,
  max = 3,
  siteIds = {},
}: {
  items: Insight[];
  reload: () => void;
  max?: number;
  /** Site name to id, so an anomaly row can link to its site. */
  siteIds?: Record<string, string>;
}) {
  const { current, busy, error, toggle } = useStormToggle();
  const [open, setOpen] = useState<number | null>(null);
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
  const top = pressing(items).slice(0, max);
  if (top.length === 0) return null;
  return (
    <>
      {(error ?? ackError) && (
        <li className="attn-row">
          <ErrorNote error={error ?? ackError} />
        </li>
      )}
      {top.map((i) => {
        const sev = SEVERITY[i.severity];
        const toSwitch = (current?.sites ?? []).filter(
          (s) => sitesToSwitch(i).includes(s.name) && !s.storm_mode,
        );
        const links = Array.isArray(i.data.links)
          ? (i.data.links as { label: string; url: string }[])
          : [];
        const expanded = open === i.id;
        const detailId = `insight-${i.id}-detail`;
        const siteId = i.site ? siteIds[i.site] : undefined;
        return (
          <li key={i.id} className={`attn-row ${i.severity} ${i.kind}`}>
            <div className="attn-main">
              <span className="attn-meta">
                <StatusPill health={sev.health}>{sev.word}</StatusPill>
                <span className="attn-kind">{KIND_LABEL[i.kind]}</span>
                {i.example && <ExampleTag />}
                <span className="attn-age">{ago(i.first_seen)}</span>
              </span>
              <span className="attn-title" title={i.title.replace(/^Example data: /, "")}>
                {i.title.replace(/^Example data: /, "")}
              </span>
            </div>
            <div className="attn-actions">
              {toSwitch.map((site) => (
                <button
                  key={site.id}
                  className="button small storm-on"
                  disabled={busy}
                  onClick={() => toggle(site, true)}
                >
                  Storm Mode on for {site.name}
                </button>
              ))}
              {i.kind === "bill_shock" && (
                <Link className="button small secondary" to="/metering">
                  Open metering
                </Link>
              )}
              {i.kind === "anomaly" && siteId && (
                <Link className="button small secondary" to={`/sites/${siteId}`}>
                  Open {i.site}
                </Link>
              )}
              <button
                className="button small secondary"
                aria-expanded={expanded}
                aria-controls={detailId}
                onClick={() => setOpen(expanded ? null : i.id)}
              >
                {expanded ? "Hide details" : "Details"}
              </button>
              {i.acknowledged_by ? (
                <span className="attn-ack">
                  Acknowledged by {who(i.acknowledged_by)}
                </span>
              ) : (
                <button className="button small secondary" onClick={() => ack(i)}>
                  Acknowledge
                </button>
              )}
            </div>
            {expanded && (
              <div className="attn-detail" id={detailId}>
                <p>{i.detail}</p>
                <p className="attn-links">
                  <span className="muted">
                    Since {ago(i.first_seen)}, checked {ago(i.last_seen)}.
                  </span>
                  {typeof i.data.advisory_url === "string" &&
                    i.data.advisory_url && (
                      <a href={i.data.advisory_url} target="_blank" rel="noreferrer">
                        NHC advisory
                      </a>
                    )}
                  {links.map((l) => (
                    <a key={l.url + l.label} href={l.url} target="_blank" rel="noreferrer">
                      {l.label}
                    </a>
                  ))}
                </p>
              </div>
            )}
          </li>
        );
      })}
    </>
  );
}
