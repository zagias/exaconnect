import { useState } from "react";
import { InsightList } from "./Insights";
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { download, num, useApi, type Insight, type LinkSettlement, type UsagePoint, type UsageRow } from "../api";
import { ErrorNote, Eyebrow, LineChart, clock } from "../components";

const PERIODS = [
  { key: "hours=6", label: "Last 6 hours" },
  { key: "hours=24", label: "Last 24 hours" },
  { key: "", label: "This month" },
];

const mbps = (v: number | null | undefined) => (v == null ? "–" : `${v.toFixed(v >= 100 ? 0 : 1)} Mbps`);
const cost = (v: string | number) => Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Usage per link, the 95th percentile, the commit line and burst (CLAUDE.md §4.5).
 *  Carrier accounts get the same screen, limited to their own links and read only. */
export default function Metering({ carrierView = false }: { carrierView?: boolean }) {
  const { user } = useAuth();
  const [period, setPeriod] = useState(PERIODS[1].key);
  const [selected, setSelected] = useState<string | null>(null);
  const [dlError, setDlError] = useState<string | null>(null);
  const q = period ? `?${period}` : "";
  const { data, error } = useApi<{ start: string; end: string; links: LinkSettlement[] }>(`/metering/links${q}`, 60_000);
  const usage = useApi<{ totals: { gb: number; over_commit_gb: number; satellite_gb: number }; paths: UsageRow[] }>(
    !carrierView && user?.role !== "carrier" ? `/metering/usage${q}` : null,
    60_000,
  );
  const links = data?.links ?? [];
  const current = links.find((l) => l.id === selected) ?? links.find((l) => l.samples > 0) ?? links[0];

  const csv = async (linkId?: string) => {
    setDlError(null);
    const params = new URLSearchParams(period);
    if (linkId) params.set("link_id", linkId);
    try {
      await download(`/metering/settlement.csv?${params}`, `exaconnect-settlement${linkId ? "-link" : ""}.csv`);
    } catch (e) {
      setDlError((e as Error).message);
    }
  };

  const byCarrier = new Map<string, LinkSettlement[]>();
  for (const l of links) byCarrier.set(l.carrier, [...(byCarrier.get(l.carrier) ?? []), l]);

  return (
    <>
      <div className="page-head">
        <Eyebrow>{carrierView ? "Carrier view" : "Metering"}</Eyebrow>
        <h1>{carrierView ? "Your links and the samples behind every charge" : "Usage, 95th percentile and burst"}</h1>
        <p className="muted">
          Bytes are sampled every minute and rolled up to 5-minute averages. For the period, the top 5% of samples are
          discarded and the highest remaining is the billable rate, using the higher of in and out. Burst is the
          billable rate above commit, charged at the burst price.
          {carrierView && " This view is read only."}
        </p>
        <ErrorNote error={error ?? dlError} />
      </div>
      <div className="grid">
        {carrierView && <CarrierAnomalies />}
        <section className="card span-12">
          <div className="card-head">
            <div className="segmented" role="group" aria-label="Period">
              {PERIODS.map((p) => (
                <button key={p.label} aria-pressed={period === p.key} onClick={() => setPeriod(p.key)}>
                  {p.label}
                </button>
              ))}
            </div>
            <button className="button secondary" onClick={() => csv()}>
              Download CSV
            </button>
          </div>
          {data && (
            <p className="muted small">
              {new Date(data.start).toLocaleString("en-GB")} to {new Date(data.end).toLocaleString("en-GB")} (UTC
              buckets)
            </p>
          )}
          {[...byCarrier.entries()].map(([carrier, ls]) => (
            <div key={carrier}>
              <h2>{carrier}</h2>
              <table className="paths">
                <thead>
                  <tr>
                    <th scope="col">Site</th>
                    {!carrierView && user?.role === "admin" && <th scope="col">Customer</th>}
                    <th scope="col" className="num">Samples</th>
                    <th scope="col" className="num">95th in</th>
                    <th scope="col" className="num">95th out</th>
                    <th scope="col" className="num">Commit</th>
                    <th scope="col" className="num">Burst</th>
                    <th scope="col" className="num">Commit charge</th>
                    <th scope="col" className="num">Burst charge</th>
                    <th scope="col" className="num">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {ls.map((l) => (
                    <tr key={l.id} className={current?.id === l.id ? "selected" : ""}>
                      <td>
                        <button className="link" onClick={() => setSelected(l.id)}>
                          {l.site}
                        </button>{" "}
                        <span className="muted small">
                          {l.path_label}, {l.underlay_type}
                        </span>
                      </td>
                      {!carrierView && user?.role === "admin" && <td>{l.customer}</td>}
                      <td className="num mono">
                        {l.samples}
                        {l.discarded > 0 && <span className="muted"> (−{l.discarded})</span>}
                      </td>
                      <td className="num mono">{mbps(l.p95_in_mbps)}</td>
                      <td className="num mono">{mbps(l.p95_out_mbps)}</td>
                      <td className="num mono">{mbps(num(l.commit_mbps))}</td>
                      <td className="num mono">{Number(l.burst_mbps) > 0 ? mbps(Number(l.burst_mbps)) : "–"}</td>
                      <td className="money">{cost(l.commit_charge)}</td>
                      <td className="money">{cost(l.burst_charge)}</td>
                      <td className="money">
                        <strong>{cost(l.total)}</strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {data && links.length === 0 && <p className="muted">No links to show.</p>}
        </section>
        {current && <LinkChart link={current} period={period} onCsv={() => csv(current.id)} />}
        {usage.data && <UsageCard data={usage.data} />}
      </div>
    </>
  );
}

function LinkChart({ link, period, onCsv }: { link: LinkSettlement; period: string; onCsv: () => void }) {
  const q = period ? `?${period}` : "";
  const { data, error } = useApi<{ start: string; end: string; points: UsagePoint[] }>(
    `/metering/links/${link.id}/samples${q}`,
    60_000,
  );
  const from = data ? new Date(data.start).getTime() : Date.now() - 86_400_000;
  const to = data ? new Date(data.end).getTime() : Date.now();
  const pts = data?.points ?? [];
  const series = [
    { key: "in", label: "In", points: pts.map((p) => ({ t: new Date(p.bucket).getTime(), v: p.in_mbps })) },
    { key: "out", label: "Out", points: pts.map((p) => ({ t: new Date(p.bucket).getTime(), v: p.out_mbps })) },
  ];
  const refs = [
    ...(link.billable_mbps != null ? [{ value: link.billable_mbps, label: "95th", kind: "p95" as const }] : []),
    ...(num(link.commit_mbps) ? [{ value: num(link.commit_mbps) as number, label: "Commit", kind: "commit" as const }] : []),
  ];
  return (
    <section className="card span-12">
      <div className="card-head">
        <div>
          <Eyebrow>{link.carrier}</Eyebrow>
          <h2>
            {link.site}, {link.path_label}
          </h2>
        </div>
        <button className="button secondary" onClick={onCsv}>
          CSV for this link
        </button>
      </div>
      <ErrorNote error={error} />
      <div className="chart-wide">
        <LineChart title="5-minute average" unit=" Mbps" series={series} from={from} to={to} refs={refs} />
      </div>
      <p className="muted small">
        {link.samples} samples; {link.discarded} discarded as the top 5%. Billable {mbps(link.billable_mbps)} against a
        commit of {mbps(num(link.commit_mbps))} at {cost(link.cost_per_mbps)} per Mbps; burst at {cost(link.burst_price)}{" "}
        per Mbps.
      </p>
    </section>
  );
}

function UsageCard({ data }: { data: { totals: { gb: number; over_commit_gb: number; satellite_gb: number }; paths: UsageRow[] } }) {
  const pct = (v: number) => (data.totals.gb ? `${((100 * v) / data.totals.gb).toFixed(1)}%` : "–");
  return (
    <section className="card span-12">
      <Eyebrow>Usage by site and path</Eyebrow>
      <h2>Where your traffic went</h2>
      <p className="muted">
        {data.totals.gb.toFixed(2)} GB in total. {pct(data.totals.over_commit_gb)} above commit,{" "}
        {pct(data.totals.satellite_gb)} on satellite.
      </p>
      <table className="paths">
        <thead>
          <tr>
            <th scope="col">Site</th>
            <th scope="col">Path</th>
            <th scope="col" className="num">Data</th>
            <th scope="col" className="num">Above commit</th>
            <th scope="col" className="num">95th</th>
            <th scope="col">Why traffic went there</th>
          </tr>
        </thead>
        <tbody>
          {data.paths.map((p) => (
            <tr key={`${p.site_id}-${p.path}`}>
              <td>
                <Link to={`/sites/${p.site_id}`}>{p.site}</Link>
              </td>
              <td>
                {p.path_label} <span className="muted small">{p.carrier}</span>
              </td>
              <td className="num mono">{p.gb.toFixed(2)} GB</td>
              <td className="num mono">{p.over_commit_gb ? `${p.over_commit_gb.toFixed(2)} GB` : "–"}</td>
              <td className="num mono">{mbps(p.p95_mbps)}</td>
              <td className="small">
                {p.reasons.length === 0 ? (
                  <span className="muted">{p.satellite ? "No moves onto satellite" : "Default routing"}</span>
                ) : (
                  p.reasons.map((r) => (
                    <div key={r.time}>
                      <span className="mono muted">{clock(r.time)}</span> {r.reason}
                    </div>
                  ))
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** Carrier anomalies on the links this view covers (read only). */
function CarrierAnomalies() {
  const list = useApi<Insight[]>("/insights?kind=anomaly", 60_000);
  return (
    <section className="card span-12">
      <div className="card-head">
        <h2>Anomalies on these links</h2>
        <span className="muted small">Paths behaving worse than usual for the hour, measured by ExaConnect probes.</span>
      </div>
      <InsightList items={list.data ?? []} reload={list.reload} readOnly />
    </section>
  );
}
