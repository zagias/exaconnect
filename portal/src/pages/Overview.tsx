import { Link } from "react-router-dom";
import {
  num,
  useApi,
  useControllerStatus,
  type ClassSla24h,
  type Health,
  type Insight,
  type Overview as OverviewData,
  type OverviewSite,
  type RecentMove,
  type SiteSummary,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Eyebrow, StatusPill, ago, fmt } from "../components";
import { useCustomer } from "../customer";
import { NetworkSites, siteHealth } from "../NetworkSites";
import { canOperateConnect } from "../permissions";
import { InsightSummary, pressing } from "./Insights";
import "../overview.css";

/** How many alert rows show before "All alerts". */
const MAX_ROWS = 5;

const PATH_WORD: Record<Health, string> = { ok: "Within SLA", warn: "At risk", bad: "Down" };

/** A network problem the overview lists alongside insights. */
interface NetworkIssue {
  key: string;
  health: Health;
  word: string;
  kind: string;
  title: string;
  /** A few words for the summary line. */
  short: string;
  siteId: string;
}

function networkIssues(sites: OverviewSite[]): NetworkIssue[] {
  const out: NetworkIssue[] = [];
  for (const s of sites) {
    if (!s.node_id) continue;
    if (!s.online) {
      out.push({
        key: `${s.id}-offline`,
        health: "bad",
        word: "Offline",
        kind: s.kind === "pop" ? "Network hub" : "Site",
        title: `${s.name} has not reported since ${ago(s.last_seen)}`,
        short: `${s.name} offline`,
        siteId: s.id,
      });
      continue;
    }
    if (s.apply_ok === false)
      out.push({
        key: `${s.id}-config`,
        health: "warn",
        word: "Config failed",
        kind: "Box",
        title: `${s.name} could not apply its latest config and kept the last good one`,
        short: `config failed at ${s.name}`,
        siteId: s.id,
      });
    if (s.kind !== "site") continue; // the hub only reflects probes
    if (!s.paths.length) {
      out.push({
        key: `${s.id}-unmeasured`,
        health: "warn",
        word: "Waiting for measurements",
        kind: "Connection",
        title: `${s.name} is reporting, but its connections have no measurements yet`,
        short: `waiting for connection measurements at ${s.name}`,
        siteId: s.id,
      });
    }
    for (const p of s.paths) {
      if (p.sent && p.health === "ok") continue;
      out.push({
        key: `${s.id}-${p.path}`,
        health: p.sent ? p.health : "warn",
        word: p.sent ? PATH_WORD[p.health] : "No probes",
        kind: "Connection",
        title: !p.sent
          ? `${p.label} at ${s.name}: no probe results in the last 30 s`
          : !p.received
            ? `${p.label} at ${s.name}: no probes came back in the last 30 s (100% loss)`
            : `${p.label} at ${s.name}: ${fmt(p.rtt_avg_ms, " ms")}, jitter ${fmt(p.jitter_ms, " ms")}, loss ${fmt(p.loss_pct, "%", 2)}`,
        short: `${p.label} at ${s.name} ${(p.sent ? PATH_WORD[p.health] : "no probes").toLowerCase()}`,
        siteId: s.id,
      });
    }
  }
  return out;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** Before any box has checked in: what to do next, instead of a dashboard of zeros (ADR 0043). */
function FirstRun({ waiting }: { waiting: number }) {
  const { user } = useAuth();
  const manager = user?.role === "admin" || user?.org_role === "owner" || user?.org_role === "admin";
  return (
    <section className="card ov-first" aria-labelledby="ov-first-h">
      <h2 id="ov-first-h">{waiting > 0 ? `Waiting for ${plural(waiting, "box", "boxes")} to check in` : "Connect your first location"}</h2>
      <p className="muted">
        {waiting > 0
          ? "Your locations are set up in Connect. Once the XaKore box at each one is installed and checks in, its links, SLA and routing moves show here."
          : "Connect starts working once a location has its XaKore box. Add your locations, list each one's internet links, and install the box with the code you are given."}
      </p>
      {manager ? (
        <p className="form-actions">
          <Link className="button" to="/locations">
            {waiting > 0 ? "See locations and install codes" : "Add and connect locations"}
          </Link>
          <Link className="button secondary" to="/org/setup">
            Set-up checklist
          </Link>
        </p>
      ) : (
        <p className="small muted">Ask your organisation&apos;s owner or an admin to connect a location.</p>
      )}
    </section>
  );
}

export default function Overview() {
  const { user } = useAuth();
  const { current } = useCustomer();
  const controller = useControllerStatus();
  const q = user?.role === "admin" && current ? `?customer_id=${current.id}` : "";
  const raw = useApi<OverviewData>(user?.role === "admin" && !current ? null : `/overview${q}`, 10_000);
  const error = raw.error;
  // Fill fields an older controller doesn't send, so a portal deployed ahead
  // of its controller degrades instead of crashing.
  const data: OverviewData | null = raw.data
    ? {
        ...raw.data,
        sla_24h: raw.data.sla_24h ?? [],
        recent_decisions: raw.data.recent_decisions ?? [],
        moves_24h: raw.data.moves_24h ?? 0,
        sites: raw.data.sites.map((s) => ({ ...s, steering: s.steering ?? [] })),
      }
    : null;
  const networkReady = data !== null && !error;
  const insights = useApi<Insight[]>(user?.role === "admin" && !current ? null : `/insights${q}`, 30_000);

  const alertsReady = insights.data !== null && !insights.error;
  const sites = data?.sites ?? [];
  const edge = sites.filter((s) => s.kind === "site");
  const enrolled = edge.filter((s) => s.node_id);
  const online = enrolled.filter((s) => s.online);
  const paths = edge.filter((s) => s.node_id && s.online).flatMap((s) => s.paths);
  const healthy = paths.filter((p) => p.sent && p.health === "ok");
  const issues = networkIssues(sites);
  const alerts = pressing(insights.data ?? []);
  const unacked = alerts.filter((i) => !i.acknowledged_by);
  const needs = issues.length + unacked.length;
  const criticalOpen = alerts.some((i) => i.severity === "critical");
  const siteIds = Object.fromEntries(sites.map((s) => [s.name, s.id]));

  const headline = error
    ? data ? "Current network status unavailable" : "We couldn't load the network"
    : !data
    ? "Loading the network"
    : enrolled.length === 0
      ? "Your network isn't connected yet"
      : online.length === 0
        ? "Your sites are not reporting"
        : needs > 0
        ? `${plural(needs, "issue needs", "issues need")} attention`
        : paths.length === 0
          ? "Waiting for connection measurements"
          : paths.some((p) => !p.sent)
            ? "Some connections have no recent measurements"
        : !alertsReady
          ? insights.error ? "Network measured; alert status unavailable" : "Network measured; checking alerts"
          : criticalOpen
            ? "Within SLA, alerts being watched"
            : "All sites within SLA";

  const networkLine =
    !networkReady
      ? "Current connection health could not be checked."
      : issues.length === 0
      ? paths.length && paths.every((p) => p.sent) ? `Network: all ${plural(paths.length, "path")} within SLA.` : "Waiting for current connection measurements."
      : `Network: ${issues.slice(0, 3).map((i) => i.short).join(", ")}${issues.length > 3 ? ` and ${issues.length - 3} more` : ""}.`;
  const alertLine = !alertsReady
    ? insights.error ? "Risk and cost alerts could not be loaded." : "Checking risk and cost alerts."
    : alerts.length === 0
      ? "No open alerts."
      : `Alerts: ${alerts
          .slice(0, 3)
          .map((i) => `${i.example ? "example " : ""}${KIND_WORD[i.kind]}${i.site ? ` at ${i.site}` : ""}${i.acknowledged_by ? " (acknowledged)" : ""}`)
          .join(", ")}${alerts.length > 3 ? ` and ${alerts.length - 3} more` : ""}.`;

  const shown = Math.min(issues.length, MAX_ROWS) + Math.min(alerts.length, Math.max(0, MAX_ROWS - issues.length));
  const moreAlerts = alerts.length - Math.max(0, MAX_ROWS - issues.length);

  return (
    <div className="ov">
      <header className="ov-head">
        <Eyebrow>Overview</Eyebrow>
        <h1 className={needs > 0 ? "ov-needs" : undefined}>{headline}</h1>
        {data && enrolled.length > 0 && (
          <p className="ov-summary">
            {networkLine} {alertLine}
          </p>
        )}
        <p className="ov-controller">
          <span className={`ov-dot ${controller.ok ? "ok" : "bad"}`} aria-hidden="true" />
          {controller.ok ? "Connect is online" : "Monitoring service unavailable"}
          {" · refreshes every 10 s"}
        </p>
        <ErrorNote error={error ?? insights.error} />
        {error && data && <p className="small muted" role="status">Readings below are from the last successful load. They do not confirm current network health.</p>}
      </header>

      {data && enrolled.length === 0 && <><FirstRun waiting={edge.length} />{edge.length > 0 && <NetworkSites sites={edge} />}</>}
      {data && enrolled.length > 0 && (
        <>
          <KpiStrip
            data={data}
            online={online.length}
            enrolled={enrolled.length}
            healthy={healthy.length}
            paths={paths.length}
            stale={!networkReady}
          />

          <section className="card ov-attn" aria-labelledby="ov-attn-h">
            <div className="ov-card-head">
              <h2 id="ov-attn-h">Needs attention</h2>
              {needs > 0 ? (
                <span className="ov-count mono">{needs}</span>
              ) : (
                <StatusPill health={networkReady && alertsReady && paths.length > 0 && paths.every((path) => path.sent) ? "ok" : "warn"}>{!networkReady ? "Network status unavailable" : !alertsReady ? insights.error ? "Alerts unavailable" : "Checking alerts" : paths.length > 0 && paths.every((path) => path.sent) ? "Nothing open" : "Measurements pending"}</StatusPill>
              )}
              <Link className="ov-more" to="/insights">
                Risk and cost alerts{alerts.length > 0 ? ` (${alerts.length})` : ""}
              </Link>
            </div>
            {issues.length + alerts.length === 0 ? (
              <p className="muted small ov-empty">
                {!networkReady ? "Current connection health is unavailable. The readings below are the last known measurements." : !alertsReady ? "Risk and cost alert status is not confirmed. Check the site inventory for current connection health." : paths.length > 0 && paths.every((path) => path.sent) ? "All measured connections are within SLA. There are no open risk or cost alerts." : "Connection health is not confirmed yet. Waiting for measurements; there are no open risk or cost alerts."}
              </p>
            ) : (
              <ul className="attn-list">
                {issues.slice(0, MAX_ROWS).map((i) => (
                  <li key={i.key} className={`attn-row ${i.health === "bad" ? "critical" : "warning"}`}>
                    <div className="attn-main">
                      <span className="attn-meta">
                        <StatusPill health={i.health}>{i.word}</StatusPill>
                        <span className="attn-kind">{i.kind}</span>
                        <span className="attn-age">now</span>
                      </span>
                      <span className="attn-title">{i.title}</span>
                    </div>
                    <div className="attn-actions">
                      <Link className="button small secondary" to={`/sites/${i.siteId}`}>
                        Open site
                      </Link>
                    </div>
                  </li>
                ))}
                <InsightSummary
                  items={insights.data ?? []}
                  reload={insights.reload}
                  max={Math.max(0, MAX_ROWS - issues.length)}
                  siteIds={siteIds}
                  readOnly={!canOperateConnect(user)}
                />
              </ul>
            )}
            {moreAlerts > 0 && shown > 0 && (
              <p className="ov-foot small">
                <Link to="/insights">{plural(moreAlerts, "more alert")} on Insights</Link>
              </p>
            )}
            {issues.length > MAX_ROWS && <p className="ov-foot small"><a href="#network-sites">Review all sites ({issues.length} network issues)</a></p>}
          </section>

          <NetworkSites sites={edge} />

          <div className="ov-row">
            <SlaCard rows={data.sla_24h} target={data.sla_target_pct} />
            <RecentMoves moves={data.recent_decisions} />
          </div>
        </>
      )}
    </div>
  );
}

const KIND_WORD: Record<Insight["kind"], string> = {
  storm_warning: "hurricane watch",
  hazard: "disaster watch",
  bill_shock: "bill forecast",
  anomaly: "carrier anomaly",
};

// ---- KPI strip ----

function KpiStrip({
  data,
  online,
  enrolled,
  healthy,
  paths,
  stale,
}: {
  data: OverviewData;
  online: number;
  enrolled: number;
  healthy: number;
  paths: number;
  stale: boolean;
}) {
  const storm = data.sites.filter((s) => s.storm_mode);
  const sla = (name: string) => data.sla_24h.find((c) => c.class_name === name);
  const pathsHealth: Health = paths > 0 && healthy === paths ? "ok" : data.sites.some((s) => s.paths.some((p) => p.health === "bad")) ? "bad" : "warn";
  const last = data.recent_decisions[0];
  return (
    <section className="kpis" aria-label="Key figures">
      <div className="kpi">
        <div className="kpi-label">{stale ? "Sites online, last known" : "Sites online"}</div>
        <div className="kpi-figure mono">
          {online}
          <span className="kpi-of"> of {enrolled}</span>
        </div>
        {enrolled === 0 ? <span className="muted small">Waiting for a connection</span> : online === enrolled ? (
          <StatusPill health="ok">All reporting</StatusPill>
        ) : (
          <StatusPill health="bad">{enrolled - online} offline</StatusPill>
        )}
      </div>
      <div className="kpi">
        <div className="kpi-label">{stale ? "Paths within SLA, last known" : "Paths within SLA now"}</div>
        <div className="kpi-figure mono">
          {healthy}
          <span className="kpi-of"> of {paths}</span>
        </div>
        {paths === 0 ? <span className="muted small">Waiting for measurements</span> : <StatusPill health={pathsHealth}>
          {pathsHealth === "ok" ? "Within SLA" : `${paths - healthy} not within SLA`}
        </StatusPill>}
      </div>
      <div className="kpi kpi-sla">
        <div className="kpi-label">SLA met, last 24 h</div>
        {(["voice", "business"] as const).map((name) => {
          const c = sla(name);
          const pct = c?.pct ?? null;
          return (
            <div key={name} className="kpi-sla-row">
              <span className="kpi-class">{name}</span>
              <span className="kpi-pct mono">{pct === null ? "–" : `${pct.toFixed(2)}%`}</span>
              {pct === null ? (
                <span className="muted small">No data</span>
              ) : pct >= data.sla_target_pct ? (
                <StatusPill health="ok">Met</StatusPill>
              ) : (
                <StatusPill health="warn">Below {data.sla_target_pct}%</StatusPill>
              )}
            </div>
          );
        })}
      </div>
      <div className="kpi">
        <div className="kpi-label">Routing moves, 24 h</div>
        <div className="kpi-figure mono">{data.moves_24h}</div>
        <div className="kpi-sub">{last ? `Last ${ago(last.time)}` : data.moves_24h > 0 ? "Open Activity for details" : "None in 24 h"}</div>
      </div>
      <div className={`kpi${storm.length ? " kpi-storm-on" : ""}`}>
        <div className="kpi-label">Storm Mode</div>
        <div className="kpi-figure kpi-word">
          <span className={`storm-mark${storm.length ? " on" : ""}`} aria-hidden="true" />
          {storm.length ? "On" : "Off"}
        </div>
        <div className="kpi-sub">
          {storm.length ? `At ${plural(storm.length, "site")}` : "No sites on satellite standby"}
        </div>
      </div>
    </section>
  );
}

// ---- SLA over 24 h ----

function SlaCard({ rows, target }: { rows: ClassSla24h[]; target: number }) {
  return (
    <section className="card ov-sla" aria-labelledby="ov-sla-h">
      <div className="ov-card-head">
        <h2 id="ov-sla-h">SLA met, last 24 h</h2>
      </div>
      <table className="sla-table">
        <thead>
          <tr>
            <th scope="col">Class</th>
            <th scope="col" className="num">Met</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.class_name}>
              <th scope="row">
                <span className="class-chip">{c.class_name}</span>
                <div className="sla-limits">{limits(c)}</div>
              </th>
              <td className="num">
                <span className="mono sla-pct">{c.pct === null ? "–" : `${c.pct.toFixed(2)}%`}</span>
                {c.sites.length > 1 && (
                  <details className="sla-sites"><summary>By site ({c.sites.length})</summary>
                    {c.sites.map((s) => (
                      <span key={s.site_id}>
                        {s.site} <span className="mono">{s.pct === null ? "–" : `${s.pct.toFixed(1)}%`}</span>
                      </span>
                    ))}
                  </details>
                )}
              </td>
              <td>
                {c.pct === null ? (
                  <span className="muted small">No data</span>
                ) : c.best_effort ? (
                  <span className="muted small">Best effort</span>
                ) : c.pct >= target ? (
                  <StatusPill health="ok">Met</StatusPill>
                ) : (
                  <StatusPill health="warn">Below target</StatusPill>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small ov-note">
        Share of 10 s probe windows in which the path each class was on met that class's SLA. Target {target}%.
      </p>
    </section>
  );
}

function limits(c: ClassSla24h): string {
  const parts = [
    c.max_latency_ms != null && `≤ ${num(c.max_latency_ms)} ms`,
    c.max_jitter_ms != null && `jitter ≤ ${num(c.max_jitter_ms)} ms`,
    c.max_loss_pct != null && `loss ≤ ${num(c.max_loss_pct)}%`,
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : "No limits set";
}

// ---- Recent moves ----

const MOVE_WORD: Record<string, string> = {
  move: "Moved early",
  move_back: "Moved back",
  failover: "Failover",
  storm: "Storm Mode",
};

function RecentMoves({ moves }: { moves: RecentMove[] }) {
  return (
    <section className="card ov-moves" aria-labelledby="ov-moves-h">
      <div className="ov-card-head">
        <h2 id="ov-moves-h">Recent routing moves</h2>
        <Link className="ov-more" to="/decisions">
          All routing moves
        </Link>
      </div>
      {moves.length === 0 ? (
        <p className="muted small ov-empty">No routing moves yet. Each move appears here with its reason.</p>
      ) : (
        <ol className="moves">
          {moves.map((m) => (
            <li key={m.id} className={`move ${m.kind}`}>
              <div className="move-when">
                <span className="mono">{hhmm(m.time)}</span>
                <span className="muted small">{ago(m.time)}</span>
              </div>
              <div className="move-what">
                <span className="move-kind">{MOVE_WORD[m.kind] ?? m.kind}</span>
                <span className="class-chip">{m.class_name}</span>
                <span>
                  at <Link to={`/sites/${m.site_id}`}>{m.site}</Link>:
                </span>
                <span className="move-path">
                  {m.from_label ?? m.from_path ?? "–"} <span aria-label="to">→</span>{" "}
                  <strong className={m.to_path === "sat" ? "to-sat" : undefined}>{m.to_label ?? m.to_path}</strong>
                </span>
              </div>
              <p className="move-reason">{m.reason}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function NodeState({ site }: { site: SiteSummary }) {
  const state = siteHealth(site);
  return <StatusPill health={state.tone}>{state.label}</StatusPill>;
}
