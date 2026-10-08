import { useId, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/automation/reports.py (ADR 0020). */

interface Figure {
  value: number | null;
  source: string;
}
interface Times {
  count: number;
  average_s: number | null;
  median_s: number | null;
  p90_s: number | null;
  source: string;
}
interface Outcomes {
  conversations: Figure;
  messages_received: Figure;
  first_response: Times;
  missed_contacts: Figure & { late_first_replies: number };
  backlog: { open: number; unanswered: number; overdue: number };
  resolution_time: Times;
  resolved: Figure;
  reopened: Figure;
  confirmed_bookings: Figure;
  verified_actions: Figure;
  integration_failures: Figure & { by_app: { app: string; cause: string; n: number }[] };
  ai: { handled: number; kept: number; verifiably_resolved: number; definitions: { kept: string; verifiably_resolved: string } };
  workflows: { runs: number; failed: number };
}
interface Usage {
  meters: { meter: string; quantity: number; records: number; example_unit_price: number | null; example_cost: number | null }[];
  by_channel: { channel: string; quantity: number; example_cost: number }[];
  by_workflow: { workflow_id: string; name: string; meter: string; quantity: number; example_cost: number }[];
  example_total: number;
  prices: string;
  currency: string;
}
interface Limit {
  meter: string;
  monthly_alert: number | null;
  monthly_hard: number | null;
  used_this_month: number;
  state: "ok" | "alert" | "stopped";
}

const PERIODS: [string, number][] = [
  ["Last 7 days", 7],
  ["Last 30 days", 30],
  ["Last 90 days", 90],
];

function dur(s: number | null): string {
  if (s === null) return "–";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

/** A figure with where it comes from: shown on hover and focus, and read out by screen readers. */
function Stat({ label, figure, source }: { label: string; figure: string | number; source?: string }) {
  const id = useId();
  return (
    <div className="auto-stat" title={source} tabIndex={source ? 0 : undefined} aria-describedby={source ? id : undefined} role="group" aria-label={label}>
      <div className="label" aria-hidden="true">
        {label}
      </div>
      <div className="figure">{figure}</div>
      {source && (
        <div id={id} className="sr-only">
          Counted from: {source}
        </div>
      )}
    </div>
  );
}

export default function Reports() {
  const base = useCommaiBase();
  const [days, setDays] = useState(30);
  // Fixed when the period is chosen: a time taken on every render changes the
  // address on every render, and the page then fetches without end.
  const from = useMemo(() => new Date(Date.now() - days * 86400_000).toISOString(), [days]);
  const q = `?from=${encodeURIComponent(from)}`;
  const out = useApi<Outcomes>(base && `${base}/reports/outcomes${q}`, 60_000);
  const use = useApi<Usage>(base && `${base}/reports/usage${q}`, 60_000);
  const limits = useApi<Limit[]>(base && `${base}/usage-limits`, 60_000);
  const head = (
    <PageHead title="Reports">
      Every figure is counted from recorded events, never estimated. Hover over or tab to a figure to see where it comes from.
    </PageHead>
  );
  if (!base)
    return (
      <>
        {head}
        <p className="muted">Choose an organisation first.</p>
      </>
    );
  const o = out.data;
  return (
    <>
      {head}
      <div className="segmented" role="group" aria-label="Period" style={{ marginBottom: 16 }}>
        {PERIODS.map(([l, d]) => (
          <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}>
            {l}
          </button>
        ))}
      </div>
      <p className="sr-only" role="status" aria-live="polite">
        {o ? `Showing the ${PERIODS.find((x) => x[1] === days)?.[0].toLowerCase()}` : "Loading"}
      </p>
      <ErrorNote error={out.error} />
      {o && (
        <>
          <h2 className="sr-only">Outcomes</h2>
          <div className="auto-stats">
            <Stat label="Conversations" figure={o.conversations.value ?? 0} source={o.conversations.source} />
            <Stat label="First reply (median)" figure={dur(o.first_response.median_s)} source={o.first_response.source} />
            <Stat label="Missed contacts" figure={o.missed_contacts.value ?? 0} source={o.missed_contacts.source} />
            <Stat label="Backlog now" figure={o.backlog.open} source="Conversations not resolved" />
            <Stat label="Resolution (median)" figure={dur(o.resolution_time.median_s)} source={o.resolution_time.source} />
            <Stat label="Reopened" figure={o.reopened.value ?? 0} source={o.reopened.source} />
            <Stat label="Confirmed bookings" figure={o.confirmed_bookings.value ?? 0} source={o.confirmed_bookings.source} />
            <Stat label="Verified actions" figure={o.verified_actions.value ?? 0} source={o.verified_actions.source} />
            <Stat label="Integration failures" figure={o.integration_failures.value ?? 0} source={o.integration_failures.source} />
          </div>
          <Card title="AI: kept and resolved">
            <div className="auto-stats">
              <Stat label="AI replied" figure={o.ai.handled} />
              <Stat label="AI kept" figure={o.ai.kept} />
              <Stat label="Verifiably resolved" figure={o.ai.verifiably_resolved} />
            </div>
            <dl className="auto-kv">
              <dt>Kept</dt>
              <dd>{o.ai.definitions.kept}</dd>
              <dt>Verifiably resolved</dt>
              <dd>{o.ai.definitions.verifiably_resolved}</dd>
            </dl>
          </Card>
          {o.integration_failures.by_app.length > 0 && (
            <Card title="Integration failures">
              <div className="table-wrap">
                <table className="paths dt stack">
                  <caption className="sr-only">Integration failures by app and cause</caption>
                  <thead>
                    <tr>
                      <th scope="col">App</th>
                      <th scope="col">Cause</th>
                      <th scope="col">Failures</th>
                    </tr>
                  </thead>
                  <tbody>
                    {o.integration_failures.by_app.map((f) => (
                      <tr key={`${f.app}-${f.cause}`}>
                        <th scope="row">{f.app}</th>
                        <td data-label="Cause">{f.cause.replace("_", " ") || "unknown"}</td>
                        <td data-label="Failures" className="mono">
                          {f.n}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </>
      )}
      <UsageCard usage={use.data} error={use.error} />
      <Limits base={base} limits={limits.data ?? []} reload={limits.reload} />
    </>
  );
}

function UsageCard({ usage, error }: { usage: Usage | null; error: string | null }) {
  return (
    <Card title="Usage and cost" note={<span className="tag">Example data: example prices</span>}>
      <ErrorNote error={error} />
      {usage && (
        <>
          <p className="small muted">
            {usage.prices}. Quantities are exact; costs use example prices in {usage.currency}.
          </p>
          {usage.meters.length === 0 && <div className="empty">No metered usage in this period.</div>}
          {usage.meters.length > 0 && (
            <div className="table-wrap">
              <table className="paths dt stack">
                <caption className="sr-only">Usage by meter with example costs</caption>
                <thead>
                  <tr>
                    <th scope="col">Meter</th>
                    <th scope="col">Quantity</th>
                    <th scope="col">Example unit price</th>
                    <th scope="col">Example cost</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.meters.map((m) => (
                    <tr key={m.meter}>
                      <th scope="row" className="mono">
                        {m.meter}
                      </th>
                      <td data-label="Quantity" className="mono">
                        {m.quantity}
                      </td>
                      <td data-label="Example unit price" className="mono">
                        {m.example_unit_price ?? "–"}
                      </td>
                      <td data-label="Example cost" className="mono">
                        {m.example_cost?.toFixed(2) ?? "–"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {usage.by_channel.length > 0 && (
            <p className="small">
              By channel:{" "}
              {usage.by_channel.map((c) => `${c.channel} ${c.quantity} (${c.example_cost.toFixed(2)})`).join(" · ")}
            </p>
          )}
          {usage.by_workflow.length > 0 && (
            <p className="small">
              By workflow: {usage.by_workflow.map((w) => `${w.name}: ${w.quantity} ${w.meter.replace("_", " ")}s`).join(" · ")}
            </p>
          )}
          <p className="small">
            Example total: <span className="mono">{usage.example_total.toFixed(2)}</span>. The bill itself, priced on your rate card, is on{" "}
            <Link to="/commai/bill">Usage and bill</Link>.
          </p>
        </>
      )}
    </Card>
  );
}

function Limits({ base, limits, reload }: { base: string; limits: Limit[]; reload: () => void }) {
  const [meter, setMeter] = useState("ai_reply");
  const [alert, setAlert] = useState("");
  const [hard, setHard] = useState("");
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/usage-limits/${encodeURIComponent(meter)}`, {
        method: "PUT",
        body: JSON.stringify({ monthly_alert: alert === "" ? null : Number(alert), monthly_hard: hard === "" ? null : Number(hard) }),
      });
      reload();
    });
  };
  const word = { ok: ["ok", "Within budget"], alert: ["warn", "Alert level passed"], stopped: ["bad", "Stopped: hard limit reached"] } as const;
  return (
    <Card title="Quantity limits">
      <p className="muted small">
        Limits on counts (replies, messages, minutes). An alert warns you. A hard limit stops that kind of work until the month ends or you raise it. Money budgets per channel and
        workflow are on <Link to="/commai/bill">Usage and bill</Link>.
      </p>
      {limits.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <caption className="sr-only">Quantity limits this month</caption>
            <thead>
              <tr>
                <th scope="col">Meter</th>
                <th scope="col">Used this month</th>
                <th scope="col">Alert at</th>
                <th scope="col">Hard limit</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {limits.map((l) => (
                <tr key={l.meter}>
                  <th scope="row" className="mono">
                    {l.meter}
                  </th>
                  <td data-label="Used" className="mono">
                    {l.used_this_month}
                  </td>
                  <td data-label="Alert at" className="mono">
                    {l.monthly_alert ?? "–"}
                  </td>
                  <td data-label="Hard limit" className="mono">
                    {l.monthly_hard ?? "–"}
                  </td>
                  <td data-label="State">
                    <span className={`pill ${word[l.state][0]}`}>{word[l.state][1]}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="form" onSubmit={submit} style={{ marginTop: 12 }} aria-label="Set a quantity limit">
        <label>
          Meter
          <input value={meter} onChange={(e) => setMeter(e.target.value)} list="meters" required maxLength={60} />
          <datalist id="meters">
            {["ai_reply", "ai_tokens", "copilot", "message_out:whatsapp", "message_out:sms", "message_out:email", "voice_minute", "ai_voice_minute", "workflow_run"].map(
              (m) => (
                <option key={m} value={m} />
              ),
            )}
          </datalist>
        </label>
        <label>
          Alert at
          <input type="number" min={0} inputMode="decimal" value={alert} onChange={(e) => setAlert(e.target.value)} />
        </label>
        <label>
          Hard limit
          <input type="number" min={0} inputMode="decimal" value={hard} onChange={(e) => setHard(e.target.value)} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Save limit
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}
