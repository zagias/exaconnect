import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useApi, type DecisionRow, type SiteSummary } from "../api";
import { ErrorNote, Eyebrow, Stamp } from "../components";
import { useAuth } from "../auth";
import { useCustomer } from "../customer";

const KIND_WORDS: Record<DecisionRow["kind"], string> = {
  move: "Moved",
  move_back: "Moved back",
  failover: "Failover",
  hold: "Kept",
};

const METRIC_UNITS: Record<string, string> = { loss: "%", latency: " ms", jitter: " ms" };

/** Every routing decision with its reason, newest first, filterable by site and class. */
export default function Decisions() {
  const { current } = useCustomer();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const site = params.get("site_id") ?? "";
  const cls = params.get("class_name") ?? "";
  const [holds, setHolds] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const q = new URLSearchParams({ limit: "200", include_holds: String(holds) });
  const scope = user?.role === "admin" && current ? `customer_id=${current.id}` : "";
  const ready = user?.role !== "admin" || !!current;
  if (scope && current) q.set("customer_id", current.id);
  if (site) q.set("site_id", site);
  if (cls) q.set("class_name", cls);
  const { data, error } = useApi<DecisionRow[]>(ready ? `/decisions?${q}` : null, 10_000);
  const sites = useApi<{ sites: SiteSummary[] }>(ready ? `/overview${scope ? `?${scope}` : ""}` : null, 0);
  const classes = useApi<{ name: string }[]>(current ? `/classes?customer_id=${current.id}` : null, 0);
  const filter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <>
      <div className="page-head">
        <Eyebrow>Routing moves</Eyebrow>
        <h1>Why traffic moved</h1>
        <p className="muted">
          See when Connect changed a traffic path, which site was affected and why. Filter by site or traffic class to investigate a change.
        </p>
        <ErrorNote error={error ?? sites.error ?? classes.error} />
      </div>
      <section className="card">
        <div className="filters">
          <label>
            Site{" "}
            <select value={site} onChange={(e) => filter("site_id", e.target.value)}>
              <option value="">All sites</option>
              {(sites.data?.sites ?? [])
                .filter((s) => s.kind === "site")
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Class{" "}
            <select value={cls} onChange={(e) => filter("class_name", e.target.value)}>
              <option value="">All classes</option>
              {(classes.data ?? []).map((c) => (
                <option key={c.name} value={c.name}>{c.name}</option>
              ))}
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={holds} onChange={(e) => setHolds(e.target.checked)} /> Show decisions to
            stay put
          </label>
        </div>
        {data?.length === 0 && (
          <p className="muted">
            {site || cls
              ? "No routing decisions match these filters."
              : "No routing decisions to show yet. Decisions appear here when the network records them."}
          </p>
        )}
        {data && data.length >= 200 && <p className="small muted" role="status">Showing the latest 200 decisions. Filter by site and class to narrow the history.</p>}
        <ol className="timeline">
          {(data ?? []).map((d) => (
            <li key={d.id} className={`decision ${d.kind}`}>
              <div className="decision-head">
                <Stamp iso={d.time} seconds />
                <span className={`pill ${d.kind === "failover" ? "bad" : d.kind === "hold" ? "warn" : "ok"}`}>
                  {KIND_WORDS[d.kind]}
                </span>
                {d.shadow && <span className="pill shadow">Shadow, not applied</span>}
                <Link to={`/sites/${d.site_id}`}>{d.site}</Link>
                <span className="mono">{d.class_name}</span>
              </div>
              <p className="reason">{d.reason.replace(/^\[Shadow\] /, "")}</p>
              <button className="link small" onClick={() => setOpen(open === d.id ? null : d.id)} aria-expanded={open === d.id}>
                {open === d.id ? "Hide the numbers" : "Show the numbers"}
              </button>
              {open === d.id && <Inputs d={d} />}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}

function Inputs({ d }: { d: DecisionRow }) {
  const paths = d.inputs.paths ?? {};
  const p = d.inputs.policy;
  return (
    <div className="inputs">
      {p && (
        <p className="muted small">
          Forecast {p.horizon_s} s ahead · hold {p.hold_s} s · move back after {p.return_after_s / 60} min healthy
          {d.inputs.storm ? " · Storm Mode on" : ""} · engine {d.engine}
        </p>
      )}
      <table className="paths">
        <thead>
          <tr>
            <th scope="col">Path</th>
            <th scope="col">Metric</th>
            <th scope="col" className="num">Now</th>
            <th scope="col" className="num">Forecast</th>
            <th scope="col" className="num">Trend a minute</th>
            <th scope="col" className="num">SLA</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(paths).flatMap(([name, e]) =>
            Object.entries(e.metrics).map(([m, v], i) => (
              <tr key={`${name}-${m}`}>
                <td>{i === 0 ? `${name}${e.up ? "" : " (down)"}` : ""}</td>
                <td>{m}</td>
                <td className="num mono">{v.now.toFixed(m === "loss" ? 2 : 0)}{METRIC_UNITS[m]}</td>
                <td className="num mono">
                  {v.ahead.toFixed(m === "loss" ? 2 : 0)}
                  {METRIC_UNITS[m]} <span className="muted">± {v.se.toFixed(m === "loss" ? 2 : 0)}</span>
                </td>
                <td className="num mono">
                  {v.slope_per_min >= 0 ? "+" : ""}
                  {v.slope_per_min.toFixed(m === "loss" ? 2 : 1)}
                  {METRIC_UNITS[m]}
                </td>
                <td className="num mono">{v.limit}{METRIC_UNITS[m]}</td>
              </tr>
            )),
          )}
        </tbody>
      </table>
    </div>
  );
}
