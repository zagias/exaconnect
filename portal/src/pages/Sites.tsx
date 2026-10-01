import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { num, useApi, type EventRow, type MetricPoint, type SiteDetail, type SiteSummary, type SteeringRow } from "../api";
import { ErrorNote, Eyebrow, LineChart, StatusPill, ago, clock, fmt, type Series } from "../components";
import { NodeState } from "./Overview";

export function SiteList() {
  const { data, error } = useApi<{ sites: SiteSummary[] }>("/overview", 10_000);
  return (
    <>
      <div className="page-head">
        <Eyebrow>Sites</Eyebrow>
        <h1>Sites and PoPs</h1>
        <ErrorNote error={error} />
      </div>
      <section className="card">
        <table className="paths">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Location</th>
              <th scope="col">Agent</th>
              <th scope="col" className="num">Config version</th>
            </tr>
          </thead>
          <tbody>
            {(data?.sites ?? []).map((s) => (
              <tr key={s.id}>
                <td>
                  <Link to={`/sites/${s.id}`}>{s.name}</Link>
                  {s.kind === "pop" && <span className="muted"> (PoP)</span>}
                </td>
                <td>{s.location}</td>
                <td>
                  <NodeState site={s} />
                </td>
                <td className="num mono">
                  {s.node_id ? `${s.applied_version ?? 0} of ${s.desired_version ?? "–"}` : "–"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

const WINDOWS = [
  { minutes: 15, label: "15 min" },
  { minutes: 60, label: "1 hour" },
  { minutes: 360, label: "6 hours" },
];

export function SitePage() {
  const { id } = useParams();
  const [minutes, setMinutes] = useState(15);
  const site = useApi<SiteDetail>(`/sites/${id}`, 10_000);
  const metrics = useApi<{ points: MetricPoint[] }>(`/sites/${id}/metrics?minutes=${minutes}`, 10_000);
  const events = useApi<EventRow[]>(`/events?site_id=${id}&limit=20`, 10_000);
  const s = site.data;
  if (!s) {
    return (
      <div className="page-head">
        <Eyebrow>Site</Eyebrow>
        <h1>{site.error ? "We couldn't load this site" : "Loading"}</h1>
        <ErrorNote error={site.error} />
      </div>
    );
  }

  const voice = s.slas.find((c) => c.class_name === "voice");
  const to = Date.now();
  const from = to - minutes * 60_000;
  const series = (field: keyof MetricPoint): Series[] =>
    s.paths.map((p) => ({
      key: p.path,
      label: p.label,
      points: withGaps(
        (metrics.data?.points ?? [])
          .filter((m) => m.path === p.path)
          .map((m) => ({ t: new Date(m.time).getTime(), v: num(m[field]) })),
        minutes <= 60 ? 25_000 : 150_000,
      ),
    }));

  return (
    <>
      <div className="page-head">
        <Eyebrow>{s.kind === "pop" ? "PoP" : "Site"}</Eyebrow>
        <h1>{s.name}</h1>
        <p className="muted">
          {s.location} · AS {s.asn} · LAN {s.lan_prefixes.join(", ")} ·{" "}
          {s.node_id ? `agent seen ${ago(s.last_seen)}, config v${s.applied_version ?? 0}` : "agent not enrolled"}
        </p>
        {s.apply_error && <p className="pill bad">Last config failed: {s.apply_error}</p>}
      </div>
      <div className="grid">
        {s.kind === "site" && (
          <section className="card span-12">
            <div className="card-head">
              <div>
                <Eyebrow>Path health</Eyebrow>
                <h2>Every path to the PoP</h2>
              </div>
              <div className="segmented" role="group" aria-label="Time window">
                {WINDOWS.map((w) => (
                  <button key={w.minutes} aria-pressed={minutes === w.minutes} onClick={() => setMinutes(w.minutes)}>
                    {w.label}
                  </button>
                ))}
              </div>
            </div>
            <table className="paths">
              <thead>
                <tr>
                  <th scope="col">Path</th>
                  <th scope="col">Carrier</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Latency</th>
                  <th scope="col" className="num">Jitter</th>
                  <th scope="col" className="num">Loss</th>
                </tr>
              </thead>
              <tbody>
                {s.paths.map((p) => (
                  <tr key={p.path}>
                    <td>{p.label}</td>
                    <td>
                      {p.carrier} <span className="muted">({p.underlay_type})</span>
                    </td>
                    <td>{p.sent ? <StatusPill health={p.health} /> : <span className="muted">No recent probes</span>}</td>
                    <td className="num mono">{fmt(p.rtt_avg_ms, " ms")}</td>
                    <td className="num mono">{fmt(p.jitter_ms, " ms")}</td>
                    <td className="num mono">{fmt(p.loss_pct, "%", 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">Last 30 seconds, against the voice SLA, the strictest class.</p>
            <ErrorNote error={metrics.error} />
            <div className="charts">
              <LineChart title="Latency (round trip)" unit=" ms" series={series("rtt_avg_ms")} from={from} to={to} sla={num(voice?.max_latency_ms)} />
              <LineChart title="Jitter" unit=" ms" series={series("jitter_ms")} from={from} to={to} sla={num(voice?.max_jitter_ms)} />
              <LineChart title="Loss" unit="%" series={series("loss_pct")} from={from} to={to} sla={num(voice?.max_loss_pct)} />
            </div>
          </section>
        )}
        {s.kind === "site" && (
          <section className="card span-12">
            <div className="card-head">
              <div>
                <Eyebrow>Steering</Eyebrow>
                <h2>Where each class runs</h2>
              </div>
              <Link to="/decisions">All decisions</Link>
            </div>
            <table className="paths">
              <thead>
                <tr>
                  <th scope="col">Class</th>
                  <th scope="col">Controller wants</th>
                  <th scope="col">Agent reports</th>
                  <th scope="col">Last reason</th>
                </tr>
              </thead>
              <tbody>
                {s.steering.map((c) => (
                  <tr key={c.class_name}>
                    <td className="mono">{c.class_name}</td>
                    <td>
                      {c.intended_label ?? <span className="muted">Default</span>}
                      {c.since && <span className="muted small"> since {clock(c.since)}</span>}
                    </td>
                    <td>{actualCell(c)}</td>
                    <td className="small">{c.last_reason ?? <span className="muted">No moves yet</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
        <section className="card span-6">
          <Eyebrow>Tunnels</Eyebrow>
          <h2>WireGuard and BFD</h2>
          {s.tunnels.length === 0 ? (
            <p className="muted">No tunnel reports yet.</p>
          ) : (
            <table className="paths">
              <thead>
                <tr>
                  <th scope="col">Tunnel</th>
                  <th scope="col">Handshake</th>
                  <th scope="col">BFD</th>
                </tr>
              </thead>
              <tbody>
                {s.tunnels.map((t) => (
                  <tr key={t.tunnel}>
                    <td className="mono">{t.tunnel}</td>
                    <td>
                      {t.handshake_age_s < 0 ? (
                        <StatusPill health="bad">None</StatusPill>
                      ) : (
                        <StatusPill health={t.handshake_age_s <= 180 ? "ok" : "bad"}>{t.handshake_age_s} s ago</StatusPill>
                      )}
                    </td>
                    <td>{bfdPill(t.bfd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted small">
            WireGuard renews its handshake every two minutes while traffic flows. Tunnel state from{" "}
            {s.tunnels[0] ? ago(s.tunnels[0].updated_at) : "–"}.
          </p>
        </section>
        <section className="card span-6">
          <Eyebrow>Events</Eyebrow>
          <h2>What happened</h2>
          <ErrorNote error={events.error} />
          <ul className="events">
            {(events.data ?? []).map((e, i) => (
              <li key={`${e.time}-${i}`}>
                <span className="mono muted">{clock(e.time)}</span> <strong>{eventWord(e.kind)}</strong>
                {e.detail && Object.keys(e.detail).length > 0 && (
                  <span className="muted"> {Object.entries(e.detail).map(([k, v]) => `${k} ${v}`).join(", ")}</span>
                )}
              </li>
            ))}
            {events.data?.length === 0 && <li className="muted">Nothing yet.</li>}
          </ul>
        </section>
      </div>
    </>
  );
}

function actualCell(c: SteeringRow) {
  if (!c.reported_at) return <span className="muted">Not reported yet</span>;
  if (c.paused) return <StatusPill health="bad">Paused, no allowed path</StatusPill>;
  if (!c.actual) return <span className="muted">Following BGP</span>;
  return (
    <>
      {c.actual_label}
      {c.failover && <span className="pill warn"> Local failover</span>}
    </>
  );
}

function bfdPill(state: string | null) {
  if (!state) return <span className="muted">–</span>;
  return <StatusPill health={state === "up" ? "ok" : "bad"}>{state === "up" ? "Up" : state}</StatusPill>;
}

const EVENT_WORDS: Record<string, string> = {
  enrolled: "Enrolled",
  config_applied: "Config applied",
  config_failed: "Config failed",
  config_rolled_back: "Config rolled back",
  bfd_up: "BFD up",
  bfd_down: "BFD down",
  controller_silent: "Controller silent, holding last config",
  controller_back: "Controller back",
  class_moved: "Class moved",
  steering_failed: "Steering failed",
  steering_rejected: "Steering map rejected",
};

function eventWord(kind: string) {
  return EVENT_WORDS[kind] ?? kind.replace(/_/g, " ");
}

/** Inserts a null between points further apart than `gap` so the chart breaks the line. */
function withGaps(points: { t: number; v: number | null }[], gap: number) {
  const out: { t: number; v: number | null }[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (prev && p.t - prev.t > gap) out.push({ t: prev.t + 1, v: null });
    out.push(p);
  }
  return out;
}
