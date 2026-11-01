import { useState } from "react";
import { useCustomer, useStormToggle } from "../customer";
import { useAuth } from "../auth";
import { Link, useParams } from "react-router-dom";
import { num, useApi, type EventRow, type MetricPoint, type SiteDetail, type OverviewSite, type SteeringRow } from "../api";
import { ErrorNote, Eyebrow, LineChart, Stamp, StatusPill, ago, fmt, when, type Series } from "../components";
import { BoxManagedNote, ManagedFrom } from "../box";
import { BoxPorts } from "../boxports";
import { useCanManage } from "./org/common";
import { NodeState } from "./Overview";
import { canOperateConnect } from "../permissions";
import { NetworkSites } from "../NetworkSites";
import "../site-detail.css";

export function SiteList() {
  const { current } = useCustomer();
  const { user } = useAuth();
  const endpoint = user?.role === "admin"
    ? current ? `/overview?customer_id=${current.id}` : null
    : "/overview";
  const { data, error } = useApi<{ sites: OverviewSite[] }>(endpoint, 10_000);
  const sites = (data?.sites ?? []).filter((s) => s.kind === "site");
  const hubs = (data?.sites ?? []).filter((s) => s.kind === "pop");
  return (
    <>
      <div className="page-head">
        <Eyebrow>Business network</Eyebrow>
        <h1>Connected locations</h1>
        <p className="muted">
          Check the boxes connecting your offices and branches, then open a location to review its links and performance.
        </p>
        <ErrorNote error={error} />
      </div>
      {data && <NetworkSites sites={sites.map((site) => ({ ...site, paths: site.paths ?? [], steering: site.steering ?? [] }))} />}
      <section>
        {hubs.length > 0 && (
          <details style={{ marginTop: 20 }}>
            <summary>Network hub</summary>
            <p className="muted small">Your location links meet at this hub.</p>
            <ul>
              {hubs.map((s) => (
                <li key={s.id}><Link to={`/sites/${s.id}`}>{s.name}</Link>{" · "}<NodeState site={s} /></li>
              ))}
            </ul>
          </details>
        )}
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
  const { current } = useCustomer();
  const { user } = useAuth();
  const [minutes, setMinutes] = useState(15);
  const scope = user?.role === "admin" && current ? `customer_id=${current.id}` : "";
  const ready = user?.role !== "admin" || !!current;
  const site = useApi<SiteDetail>(ready ? `/sites/${id}${scope ? `?${scope}` : ""}` : null, 10_000);
  const canManage = useCanManage() && canOperateConnect(user);
  const metrics = useApi<{ points: MetricPoint[] }>(ready ? `/sites/${id}/metrics?minutes=${minutes}${scope ? `&${scope}` : ""}` : null, 10_000);
  const events = useApi<EventRow[]>(ready ? `/events?site_id=${id}&limit=20${scope ? `&${scope}` : ""}` : null, 10_000);
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
      points: (metrics.data?.points ?? [])
        .filter((m) => m.path === p.path)
        .map((m) => ({
          t: new Date(m.time).getTime(),
          v: num(m[field]),
          // Loss averages by probes sent, so a quiet minute doesn't count as much as a busy one.
          w: field === "loss_pct" ? (m.sent ?? null) : null,
        })),
    }));
  // A missed report or two is not an outage; break the line after a longer silence.
  const gapMs = minutes <= 60 ? 25_000 : 150_000;
  const slaFor = { latency: num(voice?.max_latency_ms), jitter: num(voice?.max_jitter_ms), loss: num(voice?.max_loss_pct) };

  return (
    <>
      <div className="page-head">
        <Eyebrow>{s.kind === "pop" ? "Network hub" : "Site"}</Eyebrow>
        <h1>{s.name}</h1>
        {s.location && <p className="muted">{s.location}</p>}
        <div className="site-status-line">
          {s.kind === "site" ? <NodeState site={s} /> : <StatusPill health={!s.node_id || !s.last_seen ? "warn" : s.online ? "ok" : "bad"}>{!s.node_id || !s.last_seen ? "Waiting for first report" : s.online ? "Online" : "Offline"}</StatusPill>}
          <span className="small muted">{s.last_seen ? `Last report ${ago(s.last_seen)}` : "No reports received yet"}</span>
        </div>
        {s.apply_error && <p className="pill bad">The box could not apply its latest configuration.</p>}
        <ErrorNote error={site.error} />
        {s.kind === "site" && <SiteStorm name={s.name} />}
      </div>
      <div className="grid">
        {s.kind === "site" && (
          <section className="card span-12" aria-labelledby="site-connections-h">
            <div className="card-head">
              <div>
                <Eyebrow>Performance</Eyebrow>
                <h2 id="site-connections-h">Connections</h2>
              </div>
              <div className="segmented" role="group" aria-label="Time window">
                {WINDOWS.map((w) => (
                  <button key={w.minutes} aria-pressed={minutes === w.minutes} onClick={() => setMinutes(w.minutes)}>
                    {w.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="table-wrap"><table className="paths">
              <thead>
                <tr>
                  <th scope="col">Connection</th>
                  <th scope="col">Carrier</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Latency</th>
                  <th scope="col" className="num">Jitter</th>
                  <th scope="col" className="num">Loss</th>
                  <th scope="col" className="num" title="Probes received of probes sent in the last 30 seconds">
                    Probes, 30 s
                  </th>
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
                    <td className="num mono">
                      <Level v={p.rtt_avg_ms} limit={slaFor.latency} unit=" ms" />
                    </td>
                    <td className="num mono">
                      <Level v={p.jitter_ms} limit={slaFor.jitter} unit=" ms" />
                    </td>
                    <td className="num mono">
                      <Level v={p.loss_pct} limit={slaFor.loss} unit="%" digits={2} />
                    </td>
                    <td className="num mono">
                      <Probes sent={p.sent} received={p.received} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            {s.paths.length === 0 && <p className="muted">Connection measurements have not arrived yet.</p>}
            <p className="muted small">
              Last 30 seconds, against the voice SLA, the strictest class. A diamond marks a figure above 80% of the SLA;
              a square marks a breach.
            </p>
            <ErrorNote error={metrics.error} />
            <div className="charts">
              <LineChart title="Latency (round trip)" unit=" ms" series={series("rtt_avg_ms")} from={from} to={to} sla={slaFor.latency} gapMs={gapMs} height={190} />
              <LineChart title="Jitter" unit=" ms" series={series("jitter_ms")} from={from} to={to} sla={slaFor.jitter} gapMs={gapMs} height={190} />
              <LineChart title="Loss" unit="%" series={series("loss_pct")} from={from} to={to} sla={slaFor.loss} gapMs={gapMs} height={190} band={false} />
            </div>
          </section>
        )}
        {s.kind === "site" && (
          <section className="card span-12" aria-labelledby="site-traffic-h">
            <div className="card-head">
              <div>
                <Eyebrow>Traffic priorities</Eyebrow>
                <h2 id="site-traffic-h">Current traffic</h2>
              </div>
              <Link to={`/decisions?site_id=${s.id}`}>Routing history at this site</Link>
            </div>
            <div className="table-wrap"><table className="paths">
              <thead>
                <tr>
                  <th scope="col">Traffic class</th>
                  <th scope="col">Chosen connection</th>
                  <th scope="col">Box reports</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {s.steering.map((c) => (
                  <tr key={c.class_name}>
                    <td className="mono">{c.class_name}</td>
                    <td>
                      {c.intended_label ?? <span className="muted">Default</span>}
                      {c.since && <span className="muted small"> since {when(c.since)}</span>}
                    </td>
                    <td>{actualCell(c)}</td>
                    <td className="small">{c.last_reason ?? <span className="muted">No moves yet</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            {s.steering.length === 0 && <p className="muted">No traffic class reports yet.</p>}
          </section>
        )}
        <section className="card span-12" aria-labelledby="site-events-h">
          <Eyebrow>Activity</Eyebrow>
          <h2 id="site-events-h">Recent activity</h2>
          <ErrorNote error={events.error} />
          <ul className="events stamped">
            {(events.data ?? []).map((e, i) => (
              <li key={`${e.time}-${i}`}>
                <Stamp iso={e.time} seconds />
                <span>
                  <strong>{eventWord(e.kind)}</strong>
                  {e.detail && Object.keys(e.detail).length > 0 && (
                    <span className="muted"> {Object.entries(e.detail).map(([k, v]) => `${k} ${v}`).join(", ")}</span>
                  )}
                </span>
              </li>
            ))}
            {events.data?.length === 0 && <li className="muted">No events recorded for this site yet.</li>}
          </ul>
        </section>
        {s.box?.console && (
          <details className="card span-12 site-disclosure">
            <summary>Box settings<span className="small muted">Management access and network ports</span></summary>
            <div className="site-box-settings">
              <BoxManagedNote box={s.box} />
              <ManagedFrom siteId={s.id} box={s.box} managed={s.box_managed} canManage={canManage} onChange={site.reload} />
              <BoxPorts siteId={s.id} customerId={s.customer_id} box={s.box} managed={s.box_managed} portal={s.box_network} canManage={canManage} onChange={site.reload} />
            </div>
          </details>
        )}
        <details className="card span-12 site-disclosure">
          <summary>Technical details<span className="small muted">Network addresses, configuration and tunnel diagnostics</span></summary>
          <dl className="site-network-facts">
            <div><dt>Network number (ASN)</dt><dd className="mono">{s.asn}</dd></div>
            <div><dt>{s.kind === "pop" ? "Hub network" : "Office network"}</dt><dd className="mono">{s.lan_prefixes.join(", ") || "Not configured"}</dd></div>
            <div><dt>Box reference</dt><dd className="mono">{s.node_id ?? "Not installed"}</dd></div>
            <div><dt>Configuration version</dt><dd className="mono">{s.applied_version ?? "Not applied yet"}</dd></div>
          </dl>
          {s.apply_error && <p className="pill bad">Last configuration failed: {s.apply_error}</p>}
          <section aria-labelledby="site-tunnels-h">
            <h2 id="site-tunnels-h">Tunnel diagnostics</h2>
            {s.tunnels.length === 0 ? (
              <p className="muted">No tunnel reports yet.</p>
            ) : (
              <table className="paths">
                <thead><tr><th scope="col">Tunnel</th><th scope="col">Handshake</th><th scope="col">BFD</th></tr></thead>
                <tbody>
                  {s.tunnels.map((t) => (
                    <tr key={t.tunnel}>
                      <td className="mono">{t.tunnel}</td>
                      <td>{t.handshake_age_s < 0 ? <StatusPill health="bad">None</StatusPill> : <StatusPill health={t.handshake_age_s <= 180 ? "ok" : "bad"}>{t.handshake_age_s} s ago</StatusPill>}</td>
                      <td>{bfdPill(t.bfd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted small">WireGuard renews its handshake every two minutes while traffic flows. Tunnel state from {s.tunnels[0] ? ago(s.tunnels[0].updated_at) : "–"}.</p>
          </section>
        </details>
      </div>
    </>
  );
}

function actualCell(c: SteeringRow) {
  if (!c.reported_at) return <span className="muted">No report in the last minute</span>;
  if (c.paused) return <StatusPill health="bad">Paused, no allowed path</StatusPill>;
  if (!c.actual) return <span className="muted" title="Following BGP">Default network route</span>;
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
  cert_renewed: "Certificate renewed",
  cert_installed: "New certificate in use",
  cert_renew_failed: "Certificate renewal failed, will retry",
  local_change: "Changed on the box",
  box_managed: "Managed from changed in the portal",
  box_network: "Ports sent from the portal",
  box_portal_applied: "Box kept the ports from the portal",
  box_portal_undone: "Box put its earlier ports back",
  box_portal_refused: "Box did not use the ports from the portal",
  box_software: "Software update sent from the portal",
  box_update_restarting: "Box restarting into new software",
  box_update_failed: "Software update did not install",
  box_update_refused: "Box did not take the software update",
};

function eventWord(kind: string) {
  return EVENT_WORDS[kind] ?? kind.replace(/_/g, " ");
}

/** A path figure against the voice SLA: a diamond and "near SLA" above 80% of the
 *  limit, a square and "over SLA" above it. Never colour alone. */
function Level({ v, limit, unit, digits = 1 }: { v: unknown; limit: number | null; unit: string; digits?: number }) {
  const n = num(v);
  const text = fmt(v, unit, digits);
  if (n === null || limit == null || limit <= 0 || n <= 0.8 * limit) return <>{text}</>;
  const over = n > limit;
  return (
    <span className={`lvl ${over ? "bad" : "warn"}`} title={`${over ? "Over" : "Near"} the voice SLA of ${limit}${unit}`}>
      {text}
      <span className="sr-only">{over ? ", over the voice SLA" : ", near the voice SLA"}</span>
    </span>
  );
}

/** Probes received of sent in the last 30 seconds, with the number lost. */
function Probes({ sent, received }: { sent: number | null; received: number | null }) {
  if (!sent) return <span className="muted">–</span>;
  const lost = sent - (received ?? 0);
  return (
    <span title={`${received ?? 0} of ${sent} probes came back`}>
      {received ?? 0}/{sent}
      {lost > 0 && <span className="probe-lost"> ({lost} lost)</span>}
    </span>
  );
}

/** This site's Storm Mode state and switch. */
function SiteStorm({ name }: { name: string }) {
  const { current, busy, error, toggle } = useStormToggle();
  const { user } = useAuth();
  const canOperate = canOperateConnect(user);
  const site = current?.sites.find((x) => x.name === name);
  if (!site) return null;
  return (
    <div className="form-actions" style={{ alignItems: "center", marginTop: 8 }}>
      {site.storm_mode ? (
        <span className="pill storm">Storm Mode on since {new Date(site.storm_since ?? "").toLocaleString()}</span>
      ) : (
        <span className="muted small">Storm Mode off at this site.</span>
      )}
      {canOperate && <button
        className={site.storm_mode ? "button secondary small" : "button small storm-on"}
        disabled={busy}
        onClick={() => toggle(site, !site.storm_mode)}
      >
        {site.storm_mode ? "Switch Storm Mode off" : "Switch Storm Mode on"}
      </button>}
      {error && <span className="small" style={{ color: "var(--danger)" }}>{error}</span>}
    </div>
  );
}
