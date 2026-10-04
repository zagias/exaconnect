import { useState } from "react";
import { InsightList } from "./Insights";
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { download, num, useApi, type Insight, type LinkSettlement, type UsagePoint, type UsageRow } from "../api";
import { ErrorNote, Eyebrow, LineChart, rangeLabel, when } from "../components";

const PERIODS = [
  { key: "hours=6", label: "Last 6 hours" },
  { key: "hours=24", label: "Last 24 hours" },
  { key: "", label: "This month" },
];

const mbps = (v: number | null | undefined) => (v == null ? "–" : `${v.toFixed(v >= 100 ? 0 : 1)} Mbps`);
const rate = (v: number | null | undefined) => (v == null ? "–" : v.toFixed(v >= 100 ? 0 : 1));
const cost = (v: string | number) => Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** "1 Oct to now" for a period still running; full start and end otherwise. */
function periodLabel(start: string, end: string): string {
  const to = Math.min(new Date(end).getTime(), Date.now());
  return rangeLabel(new Date(start).getTime(), to);
}

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

  const showCustomer = !carrierView && user?.role === "admin";
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
            <p className="muted small period-note">
              {periodLabel(data.start, data.end)}, in 5-minute UTC buckets. Charges in US dollars.
            </p>
          )}
          {links.length > 0 && (
            <table className="paths settle">
              <colgroup>
                <col className="c-site" />
                {showCustomer && <col className="c-cust" />}
                <col className="c-samples" />
                <col span={4} className="c-rate" />
                <col span={3} className="c-money" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">Site and path</th>
                  {showCustomer && <th scope="col">Customer</th>}
                  <th scope="col" className="num" title="5-minute samples, with the number discarded as the top 5%">
                    Samples
                  </th>
                  <th scope="col" className="num">
                    95th in <span className="unit">Mbps</span>
                  </th>
                  <th scope="col" className="num">
                    95th out <span className="unit">Mbps</span>
                  </th>
                  <th scope="col" className="num">
                    Commit <span className="unit">Mbps</span>
                  </th>
                  <th scope="col" className="num">
                    Burst <span className="unit">Mbps</span>
                  </th>
                  <th scope="col" className="num">
                    Commit <span className="unit">US$</span>
                  </th>
                  <th scope="col" className="num">
                    Burst <span className="unit">US$</span>
                  </th>
                  <th scope="col" className="num">
                    Total <span className="unit">US$</span>
                  </th>
                </tr>
              </thead>
              {[...byCarrier.entries()].map(([carrier, ls]) => (
                <tbody key={carrier} className="group">
                  <tr>
                    <th scope="colgroup" colSpan={showCustomer ? 10 : 9}>
                      {carrier}
                    </th>
                  </tr>
                  {ls.map((l) => (
                    <tr key={l.id} className={current?.id === l.id ? "selected" : ""}>
                      <td className="site-cell">
                        <button className="link" onClick={() => setSelected(l.id)} aria-pressed={current?.id === l.id}>
                          {l.site}
                        </button>{" "}
                        <span className="muted small">{l.underlay_type}</span>
                      </td>
                      {showCustomer && <td className="site-cell">{l.customer}</td>}
                      <td className="num mono">
                        {l.samples}
                        {l.discarded > 0 && <span className="muted"> (−{l.discarded})</span>}
                      </td>
                      <td className="num mono">{rate(l.p95_in_mbps)}</td>
                      <td className="num mono">{rate(l.p95_out_mbps)}</td>
                      <td className="num mono">{rate(num(l.commit_mbps))}</td>
                      <td className="num mono">{Number(l.burst_mbps) > 0 ? rate(Number(l.burst_mbps)) : "–"}</td>
                      <td className="money">{cost(l.commit_charge)}</td>
                      <td className="money">{cost(l.burst_charge)}</td>
                      <td className="money">
                        <strong>{cost(l.total)}</strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          )}
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
  // A period still in progress ends at now (or the last sample), not at the end of the month.
  const last = data?.points.length ? new Date(data.points[data.points.length - 1].bucket).getTime() + 5 * 60_000 : 0;
  const to = data ? Math.min(new Date(data.end).getTime(), Math.max(Date.now(), last)) : Date.now();
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
      <LineChart
        title="Throughput"
        unit=" Mbps"
        series={series}
        from={from}
        to={to}
        refs={refs}
        height={260}
        gapMs={15 * 60_000}
        note={rangeLabel(from, to)}
      />
      <p className="muted small">
        {link.samples} samples; {link.discarded} discarded as the top 5%. Billable {mbps(link.billable_mbps)} against a
        commit of {mbps(num(link.commit_mbps))} at US$ {cost(link.cost_per_mbps)} per Mbps; burst at US${" "}
        {cost(link.burst_price)} per Mbps.
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
      <table className="paths usage">
        <thead>
          <tr>
            <th scope="col" className="nowrap">Site</th>
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
              <td className="nowrap" data-label="Site">
                <Link to={`/sites/${p.site_id}`}>{p.site}</Link>
              </td>
              <td className="nowrap" data-label="Path">
                {p.path_label}
                {p.carrier !== p.path_label && <span className="muted small"> {p.carrier}</span>}
              </td>
              <td className="num mono" data-label="Data">{p.gb.toFixed(2)} GB</td>
              <td className="num mono" data-label="Above commit">{p.over_commit_gb ? `${p.over_commit_gb.toFixed(2)} GB` : "–"}</td>
              <td className="num mono" data-label="95th">{mbps(p.p95_mbps)}</td>
              <td className="small reasons" data-label="Why traffic went there">
                {p.reasons.length === 0 ? (
                  <span className="muted">{p.satellite ? "No moves onto satellite" : "Default routing"}</span>
                ) : (
                  p.reasons.map((r, i) => (
                    <div key={`${r.time}-${r.class_name}-${i}`}>
                      <span className="mono muted">{when(r.time)}</span> {r.reason}
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
