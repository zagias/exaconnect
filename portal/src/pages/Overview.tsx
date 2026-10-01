import { Link } from "react-router-dom";
import { useApi, useControllerStatus, type Overview as OverviewData, type SiteSummary } from "../api";
import { ErrorNote, Eyebrow, StatTile, StatusPill, ago, fmt } from "../components";

export default function Overview() {
  const controller = useControllerStatus();
  const { data, error } = useApi<OverviewData>("/overview", 10_000);
  const sites = data?.sites ?? [];
  const attention = data?.attention ?? [];
  const enrolled = sites.filter((s) => s.node_id);
  const online = enrolled.filter((s) => s.online);
  const applied = enrolled.filter((s) => s.apply_ok && s.applied_version === s.desired_version);
  const paths = enrolled.filter((s) => s.kind === "site").flatMap((s) => s.paths);
  const healthy = paths.filter((p) => p.health === "ok");

  const headline = !data
    ? "Loading"
    : enrolled.length === 0
      ? "No sites enrolled yet"
      : attention.length === 0
        ? "All sites within SLA"
        : "Needs attention";

  return (
    <>
      <div className="page-head">
        <Eyebrow>Overview</Eyebrow>
        <h1>{headline}</h1>
        <p className="muted">
          {attention.length > 0 && `${attention.join(", ")}. `}
          {controller.ok ? `Controller online, version ${controller.version}.` : "We can't reach the controller."}
        </p>
        <ErrorNote error={error} />
      </div>
      <div className="grid">
        <StatTile label="Sites online" figure={`${online.length} of ${enrolled.length}`} change="Heard from in the last 30 s" />
        <StatTile label="Paths within voice SLA" figure={`${healthy.length} of ${paths.length}`} change="Last 30 s of probes" />
        <StatTile label="Config applied" figure={`${applied.length} of ${enrolled.length}`} change="Agents on the latest version" />
        {sites.map((s) => (
          <SiteCard key={s.id} site={s} />
        ))}
      </div>
    </>
  );
}

function SiteCard({ site: s }: { site: SiteSummary }) {
  return (
    <section className="card span-4">
      <div className="card-head">
        <div>
          <Eyebrow>{s.kind === "pop" ? "PoP" : "Site"}</Eyebrow>
          <h2>
            <Link to={`/sites/${s.id}`}>{s.name}</Link>
          </h2>
          <span className="muted">{s.location}</span>
        </div>
        <NodeState site={s} />
      </div>
      {s.kind === "pop" ? (
        <p className="muted" style={{ margin: 0 }}>
          The PoP reflects probes from every site. Paths are measured from the sites.
        </p>
      ) : (
        <table className="paths">
          <thead>
            <tr>
              <th scope="col">Path</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">Latency</th>
              <th scope="col" className="num">Loss</th>
            </tr>
          </thead>
          <tbody>
            {s.paths.map((p) => (
              <tr key={p.path}>
                <td>{p.label}</td>
                <td>{p.sent ? <StatusPill health={p.health} /> : <span className="muted">No recent probes</span>}</td>
                <td className="num mono">{fmt(p.rtt_avg_ms, " ms")}</td>
                <td className="num mono">{fmt(p.loss_pct, "%", 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
