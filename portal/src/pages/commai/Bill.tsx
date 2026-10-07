import { useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase, when, type Page } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/bill.py, usage.py, ai_supplier.py and
   api/entitlements.py (ADR 0033). Money arrives as strings and is shown as given. */

interface UsageLine {
  family: string;
  meter: string;
  label: string;
  channel: string;
  rate_card_version: number;
  quantity: string;
  unit_price: string;
  amount: string;
}
interface Usage {
  period: string;
  currency: string;
  example_prices: boolean;
  lines: UsageLine[];
  voice: { kind: string; charges: number; amount: string }[];
  total: string;
}
interface Budget {
  scope: "total" | "channel" | "workflow" | "ai";
  key: string;
  label: string;
  monthly_alert: string | null;
  monthly_hard: string | null;
  spent_this_month: string;
  state: "ok" | "alert" | "stopped";
}
interface BillRow {
  id: string;
  kind: "invoice" | "credit_note";
  number: string | null;
  status: "draft" | "issued";
  period_start: string;
  currency: string;
  total: string;
  credits_number: string | null;
  issued_at: string | null;
}
interface BillLine {
  id: number;
  family: string;
  description: string;
  quantity: string;
  unit_price: string | null;
  amount: string;
  rate_card_version: number | null;
}
interface BillFull extends BillRow {
  lines: BillLine[];
  by_family: Record<string, string>;
  reason: string;
}
interface Module {
  module: string;
  label: string;
  enabled: boolean;
}
interface Reconcile {
  period: string;
  simulated: boolean;
  billed_ai: string;
  supplier_cost: string;
  margin: string;
  margin_pct: string | null;
  issues: { day: string; model: string; issue: string }[];
}

const STATE: Record<Budget["state"], [string, string]> = {
  ok: ["ok", "Within budget"],
  alert: ["warn", "Past alert level"],
  stopped: ["bad", "Limit reached: stopped"],
};
const FAMILY: Record<string, string> = { messaging: "Messaging", ai: "AI", automation: "Workflows", voice: "Voice" };

function thisMonth(): string {
  return new Date().toISOString().slice(0, 7);
}

/** Usage priced on the rate cards, money budgets, the single bill and the plan. */
export default function Bill() {
  const base = useCommaiBase();
  const { user } = useAuth();
  const exacarib = user?.role === "admin";
  const [period, setPeriod] = useState(thisMonth());
  const usage = useApi<Usage>(base && `${base}/bill/usage?period=${period}`, 60_000);
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  const u = usage.data;
  return (
    <>
      <PageHead title="Usage and bill">
        Messaging, AI, workflows and voice on one bill, priced on versioned rate cards. Budgets warn you, and a hard limit stops the work it covers.
      </PageHead>
      <Card
        title="Usage this period"
        note={
          <label className="small">
            Month{" "}
            <input type="month" value={period} onChange={(e) => setPeriod(e.target.value || thisMonth())} />
          </label>
        }
      >
        <ErrorNote error={usage.error} />
        {u?.example_prices && <p className="tag">Example data: example prices until ExaCarib sets your price list</p>}
        {u && u.lines.length === 0 && u.voice.length === 0 && <div className="empty">No charges this month.</div>}
        {u && (u.lines.length > 0 || u.voice.length > 0) && (
          <table className="paths dt stack">
            <caption className="sr-only">Charges for {u.period}</caption>
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Quantity</th>
                <th scope="col">Unit price</th>
                <th scope="col">Rate card</th>
                <th scope="col">Amount ({u.currency})</th>
              </tr>
            </thead>
            <tbody>
              {u.lines.map((l, i) => (
                <tr key={i}>
                  <td data-label="Item">
                    {FAMILY[l.family] ?? l.family}: {l.label}
                  </td>
                  <td data-label="Quantity" className="mono">{l.quantity}</td>
                  <td data-label="Unit price" className="mono">{l.unit_price}</td>
                  <td data-label="Rate card">v{l.rate_card_version}</td>
                  <td data-label="Amount" className="mono">{l.amount}</td>
                </tr>
              ))}
              {u.voice.map((v) => (
                <tr key={`voice-${v.kind}`}>
                  <td data-label="Item">Voice: {v.kind.replace(/_/g, " ")}</td>
                  <td data-label="Quantity" className="mono">{v.charges}</td>
                  <td data-label="Unit price">Per call or fee</td>
                  <td data-label="Rate card">Voice card</td>
                  <td data-label="Amount" className="mono">{v.amount}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={4}>
                  Total so far
                </th>
                <td className="mono">{u.total}</td>
              </tr>
            </tfoot>
          </table>
        )}
      </Card>
      <Budgets base={base} currency={u?.currency ?? ""} />
      <Bills base={base} exacarib={exacarib} />
      <Plan base={base} exacarib={exacarib} />
      {exacarib && <Supplier base={base} />}
    </>
  );
}

function Budgets({ base, currency }: { base: string; currency: string }) {
  const list = useApi<Budget[]>(`${base}/budgets`, 60_000);
  const [scope, setScope] = useState<Budget["scope"]>("total");
  const [key, setKey] = useState("");
  const [alert, setAlert] = useState("");
  const [hard, setHard] = useState("");
  const act = useAction();
  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/budgets/${scope}`, {
        method: "PUT",
        body: JSON.stringify({ key, monthly_alert: alert || null, monthly_hard: hard || null }),
      });
      setAlert("");
      setHard("");
      list.reload();
    });
  };
  const remove = (b: Budget) =>
    act.run(async () => {
      await api(`${base}/budgets/${b.scope}?key=${encodeURIComponent(b.key)}`, { method: "DELETE" });
      list.reload();
    });
  return (
    <Card title="Budgets">
      <ErrorNote error={list.error} />
      {list.data && list.data.length === 0 && <div className="empty">No budgets. Set one for the whole bill, a channel, AI or a workflow.</div>}
      {list.data && list.data.length > 0 && (
        <table className="paths dt stack">
          <caption className="sr-only">Money budgets this month</caption>
          <thead>
            <tr>
              <th scope="col">Covers</th>
              <th scope="col">Spent</th>
              <th scope="col">Alert at</th>
              <th scope="col">Hard limit</th>
              <th scope="col">State</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((b) => (
              <tr key={`${b.scope}:${b.key}`}>
                <td data-label="Covers">{b.label}</td>
                <td data-label="Spent" className="mono">{b.spent_this_month}</td>
                <td data-label="Alert at" className="mono">{b.monthly_alert ?? "None"}</td>
                <td data-label="Hard limit" className="mono">{b.monthly_hard ?? "None"}</td>
                <td data-label="State">
                  <span className={`pill ${STATE[b.state][0]}`}>{STATE[b.state][1]}</span>
                </td>
                <td>
                  <button className="button small secondary" disabled={act.busy} onClick={() => remove(b)} aria-label={`Remove the budget for ${b.label}`}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form onSubmit={save} className="auto-fields" style={{ marginTop: 12 }} aria-label="Set a budget">
        <label>
          Covers
          <select value={scope} onChange={(e) => setScope(e.target.value as Budget["scope"])}>
            <option value="total">The whole bill</option>
            <option value="channel">One channel</option>
            <option value="ai">AI</option>
            <option value="workflow">One workflow</option>
          </select>
        </label>
        {(scope === "channel" || scope === "workflow") && (
          <label>
            {scope === "channel" ? "Channel (whatsapp, sms, email, web, voice)" : "Workflow id"}
            <input value={key} onChange={(e) => setKey(e.target.value.trim())} maxLength={60} required />
          </label>
        )}
        <label>
          Alert at{currency ? ` (${currency})` : ""}
          <input inputMode="decimal" value={alert} onChange={(e) => setAlert(e.target.value)} placeholder="e.g. 150" />
        </label>
        <label>
          Hard limit{currency ? ` (${currency})` : ""}
          <input inputMode="decimal" value={hard} onChange={(e) => setHard(e.target.value)} placeholder="e.g. 200" />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Save budget
          </button>
        </div>
      </form>
      <p className="small muted">At the hard limit, the work the budget covers stops until next month or until you raise it. Replies people send are never blocked by an AI budget.</p>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Bills({ base, exacarib }: { base: string; exacarib: boolean }) {
  const list = useApi<Page<BillRow>>(`${base}/bills?limit=24`, 0);
  const [open, setOpen] = useState<BillFull | null>(null);
  const [period, setPeriod] = useState(thisMonth());
  const [reason, setReason] = useState("");
  const act = useAction();
  const show = (id: string) =>
    act.run(async () => {
      setOpen(await api<BillFull>(`${base}/bills/${id}`));
    });
  const draft = () =>
    act.run(async () => {
      setOpen(await api<BillFull>(`${base}/bills/draft`, { method: "POST", body: JSON.stringify({ period }) }));
      list.reload();
    });
  const issue = (b: BillFull) =>
    act.run(async () => {
      setOpen(await api<BillFull>(`${base}/bills/${b.id}/issue`, { method: "POST" }));
      list.reload();
    });
  const credit = (b: BillFull, line: BillLine) =>
    act.run(async () => {
      if (reason.trim().length < 3) throw new Error("Give a reason for the credit first.");
      setOpen(
        await api<BillFull>(`${base}/bills/${b.id}/credit`, {
          method: "POST",
          body: JSON.stringify({ lines: [{ line_id: line.id }], reason }),
        }),
      );
      list.reload();
    });
  return (
    <Card title="Bills">
      <ErrorNote error={list.error} />
      {exacarib && (
        <div className="auto-row" style={{ marginBottom: 12 }}>
          <label className="small">
            Month <input type="month" value={period} onChange={(e) => setPeriod(e.target.value || thisMonth())} />
          </label>
          <button className="button small" disabled={act.busy} onClick={draft}>
            Build the draft bill
          </button>
        </div>
      )}
      {list.data && list.data.items.length === 0 && <div className="empty">No bills yet. ExaCarib issues one bill a month for every module you use.</div>}
      <ul className="auto-evidence" aria-label="Bills">
        {(list.data?.items ?? []).map((b) => (
          <li key={b.id}>
            <button className="linklike" onClick={() => show(b.id)}>
              {b.number ?? "Draft"} · {b.kind === "credit_note" ? `Credit note for ${b.credits_number}` : `Bill for ${b.period_start.slice(0, 7)}`}
            </button>{" "}
            · <span className="mono">{b.currency} {b.total}</span> · {b.status === "issued" ? `issued ${when(b.issued_at)}` : "draft"}
          </li>
        ))}
      </ul>
      {open && (
        <section aria-labelledby="bill-open" style={{ marginTop: 16 }}>
          <h3 id="bill-open">
            {open.number ?? "Draft bill"} <span className={`pill ${open.status === "issued" ? "ok" : "shadow"}`}>{open.status === "issued" ? "Issued, frozen" : "Draft"}</span>
          </h3>
          {open.reason && <p className="small">Reason: {open.reason}</p>}
          <table className="paths dt stack">
            <caption className="sr-only">Lines</caption>
            <thead>
              <tr>
                <th scope="col">Line</th>
                <th scope="col">Quantity</th>
                <th scope="col">Unit price</th>
                <th scope="col">Amount</th>
                {exacarib && open.status === "issued" && open.kind === "invoice" && (
                  <th scope="col">
                    <span className="sr-only">Credit</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {open.lines.map((l) => (
                <tr key={l.id}>
                  <td data-label="Line">
                    {FAMILY[l.family] ?? l.family}: {l.description}
                    {l.rate_card_version ? <span className="small muted"> (card v{l.rate_card_version})</span> : null}
                  </td>
                  <td data-label="Quantity" className="mono">{l.quantity}</td>
                  <td data-label="Unit price" className="mono">{l.unit_price ?? ""}</td>
                  <td data-label="Amount" className="mono">{l.amount}</td>
                  {exacarib && open.status === "issued" && open.kind === "invoice" && (
                    <td>
                      <button className="button small secondary" disabled={act.busy} onClick={() => credit(open, l)} aria-label={`Credit ${l.description}`}>
                        Credit
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={3}>
                  Total
                </th>
                <td className="mono">
                  {open.currency} {open.total}
                </td>
              </tr>
            </tfoot>
          </table>
          {exacarib && open.status === "draft" && (
            <button className="button" disabled={act.busy} onClick={() => issue(open)}>
              Issue (it can't change afterwards)
            </button>
          )}
          {exacarib && open.status === "issued" && open.kind === "invoice" && (
            <label className="small">
              Reason for a credit{" "}
              <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} />
            </label>
          )}
        </section>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Plan({ base, exacarib }: { base: string; exacarib: boolean }) {
  const plan = useApi<{ modules: Module[] }>(`${base}/entitlements`, 0);
  const act = useAction();
  const toggle = (m: Module) =>
    act.run(async () => {
      await api(`${base}/entitlements`, { method: "PUT", body: JSON.stringify({ modules: { [m.module]: !m.enabled } }) });
      plan.reload();
    });
  return (
    <Card title="Plan">
      <ErrorNote error={plan.error} />
      <ul className="auto-evidence" aria-label="Modules">
        {(plan.data?.modules ?? []).map((m) => (
          <li key={m.module}>
            {m.label}: <span className={`pill ${m.enabled ? "ok" : "shadow"}`}>{m.enabled ? "Included" : "Not included"}</span>{" "}
            {exacarib && (
              <button className="button small secondary" disabled={act.busy} onClick={() => toggle(m)}>
                {m.enabled ? "Switch off" : "Switch on"}
              </button>
            )}
          </li>
        ))}
      </ul>
      {!exacarib && <p className="small muted">Messaging, voice, AI agents and automation are sold separately. Ask ExaCarib to add a module.</p>}
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Supplier({ base }: { base: string }) {
  const [period, setPeriod] = useState(thisMonth());
  const rec = useApi<Reconcile>(`${base}/ai-supplier/reconcile?period=${period}`, 0);
  const act = useAction();
  const importCosts = () =>
    act.run(async () => {
      await api(`/commai/exacarib/ai-supplier/import`, { method: "POST", body: JSON.stringify({ period, source: "" }) });
      rec.reload();
    });
  const r = rec.data;
  return (
    <Card title="AI supplier costs (ExaCarib only)">
      <div className="auto-row">
        <label className="small">
          Month <input type="month" value={period} onChange={(e) => setPeriod(e.target.value || thisMonth())} />
        </label>
        <button className="button small secondary" disabled={act.busy} onClick={importCosts}>
          Import the supplier's usage
        </button>
      </div>
      <ErrorNote error={rec.error || act.error} />
      {r && (
        <>
          {r.simulated && <p className="tag">Example data: simulated supplier usage</p>}
          <dl className="auto-kv">
            <dt>Billed for AI</dt>
            <dd className="mono">{r.billed_ai}</dd>
            <dt>Share of supplier cost</dt>
            <dd className="mono">{r.supplier_cost}</dd>
            <dt>Margin</dt>
            <dd className="mono">
              {r.margin}
              {r.margin_pct ? ` (${r.margin_pct}%)` : ""}
            </dd>
          </dl>
          {r.issues.length > 0 && (
            <>
              <h3 className="small">Differences to check</h3>
              <ul className="auto-evidence">
                {r.issues.slice(0, 20).map((i, k) => (
                  <li key={k}>
                    {i.day} · {i.model}: {i.issue}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </Card>
  );
}
