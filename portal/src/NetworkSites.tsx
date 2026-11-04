import { useMemo } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import type { OverviewSite, SiteSummary } from "./api";
import { useAuth } from "./auth";
import { ago, StatusPill } from "./components";
import "./network-sites.css";

export function siteHealth(site: SiteSummary): { key: string; label: string; rank: number; tone: "ok" | "warn" | "bad" } {
  if (!site.node_id) return { key: "waiting", label: "Not connected", rank: 3, tone: "warn" };
  if (!site.last_seen) return { key: "waiting", label: "Waiting for first report", rank: 3, tone: "warn" };
  if (!site.online) return { key: "offline", label: "Offline", rank: 0, tone: "bad" };
  if (site.apply_ok === false) return { key: "attention", label: "Configuration issue", rank: 1, tone: "warn" };
  const measured = site.paths.filter((path) => path.sent);
  if (measured.some((path) => path.health === "bad")) return { key: "attention", label: "Connection down", rank: 1, tone: "bad" };
  if (measured.some((path) => path.health === "warn")) return { key: "attention", label: "Performance at risk", rank: 1, tone: "warn" };
  if (!measured.length || measured.length < site.paths.length) return { key: "unmeasured", label: "Waiting for measurements", rank: 2, tone: "warn" };
  return { key: "healthy", label: "Within SLA", rank: 4, tone: "ok" };
}

/** A bounded site inventory; detailed paths and charts belong on the selected site. */
export function NetworkSites({ sites, embedded = false }: { sites: OverviewSite[]; embedded?: boolean }) {
  const { user } = useAuth();
  const manage = user?.role === "admin" || user?.org_role === "owner" || user?.org_role === "admin";
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const search = (params.get("site_search") ?? "").slice(0, 200);
  const requestedStatus = params.get("site_status") ?? "all";
  const status = ["all", "attention", "offline", "healthy", "unmeasured", "waiting"].includes(requestedStatus) ? requestedStatus : "all";
  const sort = params.get("site_sort") === "name" ? "name" : "attention";
  const requestedPage = params.get("site_page") ?? "0";
  const page = /^\d{1,6}$/.test(requestedPage) ? Number(requestedPage) : 0;
  const requestedSize = Number(params.get("site_rows") ?? 10);
  const size = [10, 25, 50].includes(requestedSize) ? requestedSize : 10;
  const update = (values: Record<string, string | null>) => setParams((previous) => {
    const next = new URLSearchParams(previous);
    for (const [key, value] of Object.entries(values)) {
      if (value === null || value === "") next.delete(key); else next.set(key, value);
    }
    return next;
  }, { replace: true });
  const filtered = useMemo(() => {
    const q = search.trim().toLocaleLowerCase();
    return sites.filter((site) => {
      const health = siteHealth(site);
      return (!q || `${site.name} ${site.location ?? ""}`.toLocaleLowerCase().includes(q)) &&
        (status === "all" || (status === "attention" ? health.key !== "healthy" : health.key === status));
    }).sort((a, b) => (sort === "attention" ? siteHealth(a).rank - siteHealth(b).rank : 0) || a.name.localeCompare(b.name, undefined, { numeric: true }));
  }, [sites, search, status, sort]);
  const pages = Math.max(1, Math.ceil(filtered.length / size));
  const current = Math.min(page, pages - 1);
  const shown = filtered.slice(current * size, (current + 1) * size);
  return (
    <section className="card network-sites" id="network-sites" aria-labelledby="network-sites-title">
      <div className="network-sites-head">
        <div><h2 id="network-sites-title">{embedded ? `${sites.length} ${sites.length === 1 ? "site" : "sites"}` : <>Sites <span className="network-total">{sites.length}</span></>}</h2>{!embedded && <p className="muted small">Connection health and latest reports.</p>}</div>
        {manage && <Link className="button secondary small" to="/locations">Add or connect a location</Link>}
      </div>
      <div className="network-filters">
        <label className="network-search">Search sites<input type="search" value={search} placeholder="Site name or location" maxLength={200} onChange={(e) => update({ site_search: e.target.value, site_page: null })} /></label>
        <label>Site status<select value={status} onChange={(e) => update({ site_status: e.target.value, site_page: null })}><option value="all">All sites</option><option value="attention">Needs attention</option><option value="offline">Offline</option><option value="healthy">Within SLA</option><option value="unmeasured">Waiting for measurements</option><option value="waiting">Not connected yet</option></select></label>
        <label>Sort by<select value={sort} onChange={(e) => update({ site_sort: e.target.value, site_page: null })}><option value="attention">Attention first</option><option value="name">Site name</option></select></label>
      </div>
      <div className="table-wrap"><table className="paths dt stack network-table"><thead><tr><th scope="col">Site</th><th scope="col">Status</th><th scope="col">Connections within SLA</th><th scope="col">Traffic classes</th><th scope="col">Last report</th></tr></thead><tbody>
        {shown.map((site) => { const health = siteHealth(site); const measured = site.online ? site.paths.filter((path) => path.sent) : []; return <tr key={site.id}><th scope="row"><Link to={`/sites/${site.id}`} state={{ siteList: location.pathname + location.search }}>{site.name}</Link><span className="sub muted">{site.location || "Location not set"}</span></th><td data-label="Status"><StatusPill health={health.tone}>{health.label}</StatusPill>{site.storm_mode && <span className="sub muted">Storm Mode on</span>}</td><td data-label="Connections within SLA"><span className="mono">{measured.length ? `${measured.filter((path) => path.health === "ok").length} / ${site.paths.length}` : "—"}</span><span className="sub muted">{measured.length ? `${measured.length} measured` : "No recent measurements"}</span></td><td data-label="Traffic classes"><span className="small">{site.steering.map((item) => item.class_name).join(" · ") || "None configured"}</span></td><td data-label="Last report"><span className="small muted">{site.last_seen ? ago(site.last_seen) : "Not reported yet"}</span></td></tr>; })}
      </tbody></table></div>
      {!shown.length && <p className="network-empty">{sites.length ? "No sites match these filters." : "No sites connected yet. Start by adding a location."}</p>}
      <div className="network-pagination"><span className="small muted" role="status">{filtered.length ? `${current * size + 1}–${Math.min((current + 1) * size, filtered.length)} of ${filtered.length} ${filtered.length === 1 ? "site" : "sites"}` : "0 sites"}</span><label className="small">Rows per page<select value={size} onChange={(e) => update({ site_rows: e.target.value, site_page: null })}><option value={10}>10</option><option value={25}>25</option><option value={50}>50</option></select></label><button className="button secondary small" disabled={current === 0} onClick={() => update({ site_page: String(current - 1) })}>Previous</button><button className="button secondary small" disabled={current + 1 >= pages} onClick={() => update({ site_page: String(current + 1) })}>Next</button></div>
    </section>
  );
}
