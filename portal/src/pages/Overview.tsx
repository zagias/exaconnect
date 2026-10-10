import type { MouseEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  num,
  useApi,
  useControllerStatus,
  type ClassSla24h,
  type Health,
  type Insight,
  type Overview as OverviewData,
  type OverviewSite,
  type OverviewSteering,
  type PathRow,
  type RecentMove,
  type SiteSummary,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Eyebrow, StatusPill, ago, fmt } from "../components";
import { useCustomer, useStormToggle } from "../customer";
import { InsightSummary, pressing } from "./Insights";
import "../overview.css";

/** How many alert rows show before "All alerts". */
const MAX_ROWS = 3;

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
        kind: s.kind === "pop" ? "PoP" : "Site",
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
        kind: "Agent",
        title: `${s.name} could not apply its latest config and kept the last good one`,
        short: `config failed at ${s.name}`,
        siteId: s.id,
      });
    if (s.kind !== "site") continue; // the PoP only reflects probes
    for (const p of s.paths) {
      if (p.health === "ok") continue;
      out.push({
        key: `${s.id}-${p.path}`,
        health: p.health,
        word: p.sent ? PATH_WORD[p.health] : "No probes",
        kind: "Path",
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
          ? "Your locations are set up in Connect. Once the ExaCarib box at each one is installed and checks in, its links, SLA and routing moves show here."
          : "Connect starts working once a location has its ExaCarib box. Add your locations, list each one's internet links, and install the box with the code you are given."}
      </p>
      {manager ? (
        <p className="form-actions">
          <Link className="button" to="/org/locations">
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
  const raw = useApi<OverviewData>(`/overview${q}`, 10_000);
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
  const insights = useApi<Insight[]>(`/insights${q}`, 30_000);

  const sites = data?.sites ?? [];
  const edge = sites.filter((s) => s.kind === "site");
  const pop = sites.find((s) => s.kind === "pop") ?? null;
  const enrolled = sites.filter((s) => s.node_id);
  const online = enrolled.filter((s) => s.online);
  const paths = edge.filter((s) => s.node_id && s.online).flatMap((s) => s.paths);
  const healthy = paths.filter((p) => p.sent && p.health === "ok");
  const issues = networkIssues(sites);
  const alerts = pressing(insights.data ?? []);
  const unacked = alerts.filter((i) => !i.acknowledged_by);
  const needs = issues.length + unacked.length;
  const criticalOpen = alerts.some((i) => i.severity === "critical");
  const siteIds = Object.fromEntries(sites.map((s) => [s.name, s.id]));

  const headline = !data
    ? "Loading the network"
    : enrolled.length === 0
      ? "Your network isn't connected yet"
      : needs > 0
        ? `${plural(needs, "thing needs", "things need")} attention`
        : criticalOpen
          ? "Within SLA, alerts being watched"
          : "All sites within SLA";

  const networkLine =
    issues.length === 0
      ? `Network: all ${plural(paths.length, "path")} within SLA.`
      : `Network: ${issues.map((i) => i.short).join(", ")}.`;
  const alertLine =
    alerts.length === 0
      ? "No open alerts."
      : `Alerts: ${alerts
          .slice(0, 3)
          .map((i) => `${i.example ? "example " : ""}${KIND_WORD[i.kind]}${i.site ? ` at ${i.site}` : ""}${i.acknowledged_by ? " (acknowledged)" : ""}`)
          .join(", ")}${alerts.length > 3 ? ` and ${alerts.length - 3} more` : ""}.`;

  const shown = issues.length + Math.min(alerts.length, Math.max(0, MAX_ROWS - issues.length));
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
          {controller.ok ? `Controller online, version ${controller.version}` : "We can't reach the controller"}
          {" · refreshes every 10 s"}
        </p>
        <ErrorNote error={error} />
      </header>

      {data && enrolled.length === 0 && <FirstRun waiting={edge.length} />}
      {data && enrolled.length > 0 && (
        <>
          <KpiStrip
            data={data}
            online={online.length}
            enrolled={enrolled.length}
            healthy={healthy.length}
            paths={paths.length}
          />

          <section className="card ov-attn" aria-labelledby="ov-attn-h">
            <div className="ov-card-head">
              <h2 id="ov-attn-h">Needs attention</h2>
              {needs > 0 ? (
                <span className="ov-count mono">{needs}</span>
              ) : (
                <StatusPill health="ok">Nothing open</StatusPill>
              )}
              <Link className="ov-more" to="/insights">
                All alerts{alerts.length > 0 ? ` (${alerts.length})` : ""}
              </Link>
            </div>
            {issues.length + alerts.length === 0 ? (
              <p className="muted small ov-empty">
                Every path is within SLA and there are no hurricane, disaster, bill or carrier alerts.
              </p>
            ) : (
              <ul className="attn-list">
                {issues.map((i) => (
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
                />
              </ul>
            )}
            {moreAlerts > 0 && shown > 0 && (
              <p className="ov-foot small">
                <Link to="/insights">{plural(moreAlerts, "more alert")} on Insights</Link>
              </p>
            )}
          </section>

          <section className="card ov-topo" aria-labelledby="ov-topo-h">
            <div className="ov-card-head">
              <h2 id="ov-topo-h">Network map</h2>
              <span className="muted small">Live, from the last 30 s of probes. Chips show the classes on each path.</span>
            </div>
            <Topology sites={edge} pop={pop} />
          </section>

          <div className="ov-sites">
            {edge.map((s) => (
              <SiteCard key={s.id} site={s} />
            ))}
          </div>

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
}: {
  data: OverviewData;
  online: number;
  enrolled: number;
  healthy: number;
  paths: number;
}) {
  const storm = data.sites.filter((s) => s.storm_mode);
  const sla = (name: string) => data.sla_24h.find((c) => c.class_name === name);
  const pathsHealth: Health = healthy === paths ? "ok" : data.sites.some((s) => s.paths.some((p) => p.health === "bad")) ? "bad" : "warn";
  const last = data.recent_decisions[0];
  return (
    <section className="kpis" aria-label="Key figures">
      <div className="kpi">
        <div className="kpi-label">Sites online</div>
        <div className="kpi-figure mono">
          {online}
          <span className="kpi-of"> of {enrolled}</span>
        </div>
        {online === enrolled ? (
          <StatusPill health="ok">All reporting</StatusPill>
        ) : (
          <StatusPill health="bad">{enrolled - online} offline</StatusPill>
        )}
      </div>
      <div className="kpi">
        <div className="kpi-label">Paths within SLA now</div>
        <div className="kpi-figure mono">
          {healthy}
          <span className="kpi-of"> of {paths}</span>
        </div>
        <StatusPill health={pathsHealth}>
          {pathsHealth === "ok" ? "Within SLA" : `${paths - healthy} not within SLA`}
        </StatusPill>
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
        <div className="kpi-sub">{last ? `Last ${ago(last.time)}` : "None in 24 h"}</div>
      </div>
      <div className={`kpi${storm.length ? " kpi-storm-on" : ""}`}>
        <div className="kpi-label">Storm Mode</div>
        <div className="kpi-figure kpi-word">
          <span className={`storm-mark${storm.length ? " on" : ""}`} aria-hidden="true" />
          {storm.length ? "On" : "Off"}
        </div>
        <div className="kpi-sub">
          {storm.length ? `At ${storm.map((s) => s.name).join(", ")}` : "No sites on satellite standby"}
        </div>
      </div>
    </section>
  );
}

// ---- Topology ----

/** Where traffic for a class really goes: the agent's report when it differs. */
function effectivePath(c: OverviewSteering): string | null {
  return c.actual ?? c.intended;
}

function classesOn(site: OverviewSite, path: string): OverviewSteering[] {
  return site.steering.filter((c) => !c.paused && effectivePath(c) === path);
}

function lineState(p: PathRow): Health | "none" {
  return p.sent ? p.health : "none";
}

function lineWord(p: PathRow): string {
  return p.sent ? PATH_WORD[p.health] : "No probes";
}

const TW = 1100;
const BAND = 200;
const BOX_W = 180;
const BOX_H = 156;
const POP_W = 150;
const SPREAD = 60;

function Topology({ sites, pop }: { sites: OverviewSite[]; pop: OverviewSite | null }) {
  const navigate = useNavigate();
  if (sites.length === 0) return <p className="muted">No sites yet.</p>;
  const left = sites.filter((_, i) => i % 2 === 0);
  const right = sites.filter((_, i) => i % 2 === 1);
  const rows = Math.max(left.length, right.length);
  const H = rows * BAND + 24;
  const popX = (TW - POP_W) / 2;
  const popCy = H / 2;
  const popH = Math.max(BOX_H, rows * 70);
  const go = (e: MouseEvent, to: string) => {
    e.preventDefault();
    navigate(to);
  };

  const placed = [
    ...left.map((s, r) => ({ s, r, side: "l" as const })),
    ...right.map((s, r) => ({ s, r, side: "r" as const })),
  ];

  return (
    <>
      <svg
        className="topo-svg"
        viewBox={`0 0 ${TW} ${H}`}
        role="img"
        aria-label={`Network map: ${sites.length} sites connected to ${pop?.name ?? "the PoP"} over ${sites[0].paths.map((p) => p.label).join(", ")}`}
      >
        {placed.map(({ s, r, side }) => {
          const n = side === "l" ? left.length : right.length;
          const cy = 12 + r * BAND + BAND / 2 + ((rows - n) * BAND) / 2;
          const boxX = side === "l" ? 8 : TW - 8 - BOX_W;
          const x1 = side === "l" ? boxX + BOX_W : boxX;
          const x2 = side === "l" ? popX : popX + POP_W;
          const dir = side === "l" ? 1 : -1;
          const k = s.paths.length;
          return (
            <g key={s.id}>
              {s.paths.map((p, j) => {
                const off = (j - (k - 1) / 2) * SPREAD;
                const y1 = cy + off;
                const y2 = popCy + off * 0.3 + (r - (n - 1) / 2) * 14;
                // Straight out of the site past the label and chips, then curve into the PoP.
                const xs = x1 + dir * 200;
                const dx = Math.abs(x2 - xs) * 0.5;
                const on = classesOn(s, p.path);
                const state = lineState(p);
                const lx = x1 + dir * 14;
                const anchor = side === "l" ? "start" : "end";
                let cx = lx;
                return (
                  <g key={p.path} className={`topo-path ${state}${p.path === "sat" ? " sat" : ""}${on.length ? " busy" : ""}`}>
                    <title>
                      {`${p.label} (${p.carrier}, ${p.underlay_type}) at ${s.name}: ${lineWord(p)}. Latency ${fmt(p.rtt_avg_ms, " ms")}, jitter ${fmt(p.jitter_ms, " ms")}, loss ${fmt(p.loss_pct, "%", 2)}.`}
                    </title>
                    <path className="topo-line" d={`M${x1},${y1} H${xs} C${xs + dir * dx},${y1} ${x2 - dir * dx},${y2} ${x2},${y2}`} />
                    <g transform={`translate(${lx},${y1 - 9})`}>
                      <HealthMark state={state} x={side === "l" ? 4 : -4} />
                      <text className="topo-label" x={dir * 14} textAnchor={anchor}>
                        <tspan className="topo-path-name">{p.label}</tspan>
                        <tspan className="topo-figure" dx={6}>
                          {fmt(p.rtt_avg_ms, " ms", 0)}
                        </tspan>
                        {state !== "ok" && (
                          <tspan className="topo-word" dx={6}>
                            {lineWord(p)}
                          </tspan>
                        )}
                      </text>
                    </g>
                    {on.map((c) => {
                      const w = Math.round(c.class_name.length * 6.6 + 14);
                      const x = side === "l" ? cx : cx - w;
                      cx += dir * (w + 4);
                      return (
                        <g key={c.class_name} className="topo-chip" transform={`translate(${x},${y1 + 6})`}>
                          <rect width={w} height={17} rx={4} />
                          <text x={w / 2} y={12.5} textAnchor="middle">
                            {c.class_name}
                          </text>
                        </g>
                      );
                    })}
                  </g>
                );
              })}
              <a href={`/sites/${s.id}`} onClick={(e) => go(e, `/sites/${s.id}`)} className="topo-site">
                <rect x={boxX} y={cy - BOX_H / 2} width={BOX_W} height={BOX_H} rx={8} />
                <text className="topo-eyebrow" x={boxX + 16} y={cy - BOX_H / 2 + 26}>
                  SITE
                </text>
                <text className="topo-name" x={boxX + 16} y={cy - BOX_H / 2 + 52}>
                  {s.name}
                </text>
                <text className="topo-sub" x={boxX + 16} y={cy - BOX_H / 2 + 74}>
                  {s.location}
                </text>
                <NodeMark site={s} x={boxX + 16} y={cy - BOX_H / 2 + 104} />
                {s.storm_mode && (
                  <g transform={`translate(${boxX + 16},${cy - BOX_H / 2 + 120})`}>
                    <rect className="topo-storm" width={104} height={20} rx={4} />
                    <text className="topo-storm-text" x={52} y={14} textAnchor="middle">
                      Storm Mode
                    </text>
                  </g>
                )}
              </a>
            </g>
          );
        })}
        {pop && (
          <a href={`/sites/${pop.id}`} onClick={(e) => go(e, `/sites/${pop.id}`)} className="topo-site topo-pop">
            <rect x={popX} y={popCy - popH / 2} width={POP_W} height={popH} rx={8} />
            <text className="topo-eyebrow" x={popX + POP_W / 2} y={popCy - 30} textAnchor="middle">
              POP
            </text>
            <text className="topo-name" x={popX + POP_W / 2} y={popCy - 4} textAnchor="middle">
              {pop.location || pop.name}
            </text>
            <text className="topo-sub" x={popX + POP_W / 2} y={popCy + 18} textAnchor="middle">
              {pop.name}
            </text>
            <NodeMark site={pop} x={popX + POP_W / 2} y={popCy + 46} anchor="middle" />
          </a>
        )}
      </svg>

      <ul className="topo-list" aria-label="Paths per site">
        {sites.map((s) => (
          <li key={s.id} className="topo-mini">
            <div className="topo-mini-head">
              <Link to={`/sites/${s.id}`} className="topo-mini-name">
                {s.name}
              </Link>
              <span className="muted small">to {pop?.location || pop?.name || "PoP"}</span>
              <NodeState site={s} />
              {s.storm_mode && <span className="pill storm">Storm Mode</span>}
            </div>
            <ul>
              {s.paths.map((p) => {
                const state = lineState(p);
                return (
                  <li key={p.path} className={`topo-path ${state}${p.path === "sat" ? " sat" : ""}`}>
                    <svg className="topo-swatch" viewBox="0 0 36 10" aria-hidden="true">
                      <line className="topo-line" x1="0" y1="5" x2="36" y2="5" />
                    </svg>
                    <span className="topo-mini-label">{p.label}</span>
                    <span className="mono small">{fmt(p.rtt_avg_ms, " ms", 0)}</span>
                    {p.sent ? <StatusPill health={p.health} /> : <span className="muted small">No probes</span>}
                    <span className="topo-mini-chips">
                      {classesOn(s, p.path).map((c) => (
                        <span key={c.class_name} className="class-chip">
                          {c.class_name}
                        </span>
                      ))}
                    </span>
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>

      <ul className="topo-legend" aria-label="Map key">
        <li>
          <svg viewBox="0 0 28 8" aria-hidden="true"><line className="k-ok" x1="0" y1="4" x2="28" y2="4" /></svg>Within SLA
        </li>
        <li>
          <svg viewBox="0 0 28 8" aria-hidden="true"><line className="k-warn" x1="0" y1="4" x2="28" y2="4" /></svg>At risk
        </li>
        <li>
          <svg viewBox="0 0 28 8" aria-hidden="true"><line className="k-bad" x1="0" y1="4" x2="28" y2="4" /></svg>Down
        </li>
        <li>
          <svg viewBox="0 0 28 8" aria-hidden="true"><line className="k-sat" x1="0" y1="4" x2="28" y2="4" /></svg>Satellite backup
        </li>
        <li>
          <span className="class-chip">voice</span> class on that path
        </li>
      </ul>
    </>
  );
}

/** The same round / diamond / square marks as StatusPill, drawn in SVG. */
function HealthMark({ state, x }: { state: Health | "none"; x: number }) {
  const cls = `topo-mark ${state}`;
  if (state === "ok") return <circle className={cls} cx={x} cy={-4} r={4.5} />;
  if (state === "warn") return <rect className={cls} x={x - 4} y={-8} width={8} height={8} transform={`rotate(45 ${x} -4)`} />;
  return <rect className={cls} x={x - 4.5} y={-8.5} width={9} height={9} />;
}

function NodeMark({ site: s, x, y, anchor = "start" }: { site: SiteSummary; x: number; y: number; anchor?: "start" | "middle" }) {
  const state: Health = !s.node_id ? "warn" : !s.online ? "bad" : s.apply_ok === false ? "warn" : "ok";
  const word = !s.node_id ? "Not enrolled" : !s.online ? "Offline" : s.apply_ok === false ? "Config failed" : "Online";
  const w = word.length * 8 + 16;
  const start = anchor === "middle" ? x - w / 2 : x;
  return (
    <g transform={`translate(${start},${y})`}>
      <HealthMark state={state} x={5} />
      <text className={`topo-state ${state}`} x={16}>
        {word}
      </text>
    </g>
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
                  <div className="sla-sites">
                    {c.sites.map((s) => (
                      <span key={s.site_id}>
                        {s.site} <span className="mono">{s.pct === null ? "–" : `${s.pct.toFixed(1)}%`}</span>
                      </span>
                    ))}
                  </div>
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

// ---- Site cards ----

function SiteCard({ site: s }: { site: OverviewSite }) {
  const { current, busy, error, toggle } = useStormToggle();
  const stormSite = current?.sites.find((x) => x.id === s.id);
  const labels = Object.fromEntries(s.paths.map((p) => [p.path, p.label]));
  return (
    <section className={`card ov-site${s.storm_mode ? " storm" : ""}`} aria-labelledby={`site-${s.id}`}>
      <div className="ov-site-head">
        <div>
          <h2 id={`site-${s.id}`}>
            <Link to={`/sites/${s.id}`}>{s.name}</Link>
          </h2>
          <span className="muted small">{s.location}</span>
        </div>
        <NodeState site={s} />
        {stormSite && (
          <button
            className={`button small ${s.storm_mode ? "storm-on" : "secondary"} ov-storm-btn`}
            aria-pressed={s.storm_mode}
            disabled={busy}
            onClick={() => toggle(stormSite, !s.storm_mode)}
          >
            <span className={`storm-mark${s.storm_mode ? " on" : ""}`} aria-hidden="true" />
            Storm Mode {s.storm_mode ? "on" : "off"}
          </button>
        )}
      </div>
      <ErrorNote error={error} />
      <table className="ov-paths">
        <thead>
          <tr>
            <th scope="col">Path</th>
            <th scope="col" className="col-status">Status</th>
            <th scope="col" className="num">Latency</th>
            <th scope="col" className="num">Jitter</th>
            <th scope="col" className="num">Loss</th>
          </tr>
        </thead>
        <tbody>
          {s.paths.map((p) => (
            <tr key={p.path}>
              <td>
                <span className={`path-key ${p.path}`} aria-hidden="true" />
                {p.label}
                <div className="status-inline">{p.sent ? <StatusPill health={p.health} /> : <span className="muted small">No probes</span>}</div>
              </td>
              <td className="col-status">{p.sent ? <StatusPill health={p.health} /> : <span className="muted small">No probes</span>}</td>
              <td className="num mono">{fmt(p.rtt_avg_ms, " ms")}</td>
              <td className="num mono">{fmt(p.jitter_ms, " ms")}</td>
              <td className="num mono">{fmt(p.loss_pct, "%", 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="ov-sub">Where each class runs <span className="muted small">(moves in the last 24 h)</span></h3>
      <ul className="class-map">
        {s.steering.map((c) => (
          <li key={c.class_name}>
            <span className="class-chip">{c.class_name}</span>
            <span className="class-path">
              {c.paused ? (
                <span className="muted">Paused</span>
              ) : (
                <>
                  {c.intended_label ?? <span className="muted">Not steered yet</span>}
                  {c.failover && <span className="small muted"> (failover)</span>}
                </>
              )}
              {c.actual && (
                <span className="class-actual">
                  Agent reports {c.actual_label ?? labels[c.actual] ?? c.actual}
                </span>
              )}
            </span>
            <span className="class-moves small muted">
              {c.moves_24h ? plural(c.moves_24h, "move") : "No moves"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
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

export function NodeState({ site: s }: { site: SiteSummary }) {
  if (!s.node_id) return <span className="muted">Not enrolled</span>;
  if (!s.online) return <StatusPill health="bad">Offline, seen {ago(s.last_seen)}</StatusPill>;
  if (s.apply_ok === false) return <StatusPill health="warn">Config failed</StatusPill>;
  return <StatusPill health="ok">Online</StatusPill>;
}
