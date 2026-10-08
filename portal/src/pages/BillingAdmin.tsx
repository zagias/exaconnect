import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  amount,
  billingPaths,
  changePlan,
  createPlan,
  createPriceList,
  download,
  endPlan,
  generateInvoices,
  issueInvoice,
  monthKey,
  setPartnerCost,
  subscribe,
  updatePlan,
  useApi,
  voidInvoice,
  PRODUCT_NAMES,
  type BillingMargin,
  type CustomerPlans,
  type Invoice,
  type Payables,
  type PaymentStatus,
  type Plan,
  type PriceList,
  type Product,
} from "../api";
import { ErrorNote, ExampleTag } from "../components";
import { useCustomer } from "../customer";
import { Card, RowActions, useAction } from "../ui";
import { InvoiceTable, SubscriptionTable, invoiceCsv, printUrl } from "./Billing";
import "./billing.css";

const today = () => new Date().toISOString().slice(0, 10);

function MonthPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="segmented" role="group" aria-label="Month">
      {[2, 1, 0].map((b) => (
        <button key={b} aria-pressed={value === monthKey(b)} onClick={() => onChange(monthKey(b))}>
          {monthKey(b)}
        </button>
      ))}
    </div>
  );
}

/** Admin, Billing (ADR 0022): plans and subscriptions, price lists, drafts, issue
 *  and void, payables to carriers and partners, and margin. */
export function BillingAdmin() {
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <>
      {msg && (
        <p className="pill ok" role="status">
          {msg}
        </p>
      )}
      <PlansCard onDone={setMsg} />
      <SubscriptionsCard onDone={setMsg} />
      <PriceListsCard onDone={setMsg} />
      <InvoicesCard onDone={setMsg} />
      <PayablesCard onDone={setMsg} />
      <MarginCard />
      <PaymentsCard />
    </>
  );
}

function PlansCard({ onDone }: { onDone: (m: string) => void }) {
  const plans = useApi<Plan[]>(billingPaths.plans, 0);
  const act = useAction();
  const [f, setF] = useState({ product: "connect" as Product, name: "", description: "" });
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await createPlan({ product: f.product, name: f.name.trim(), description: f.description.trim() });
      onDone(`Added ${f.name.trim()}. Give it a price list next.`);
      setF({ ...f, name: "", description: "" });
      plans.reload();
    });
  };
  const toggle = (p: Plan) =>
    act.run(async () => {
      await updatePlan(p.id, { active: !p.active });
      onDone(p.active ? `${p.name} is no longer offered.` : `${p.name} is offered again.`);
      plans.reload();
    });
  return (
    <Card title="Plans" note={<span className="muted small">Connect and Jibsy are sold as separate plans.</span>}>
      <ErrorNote error={plans.error ?? act.error} />
      {plans.data && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Plan</th>
                <th scope="col">Product</th>
                <th scope="col">Prices in effect</th>
                <th scope="col" className="num">
                  Organisations
                </th>
                <th scope="col">Offered</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {plans.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    {p.name} {p.example && <ExampleTag />}
                    <div className="muted small">{p.description}</div>
                  </td>
                  <td data-label="Product">{PRODUCT_NAMES[p.product]}</td>
                  <td data-label="Prices">
                    {p.price_list ? (
                      <span className="small">
                        v{p.price_list.version}, {p.price_list.currency}
                        {Number(p.price_list.monthly_fee) > 0 && `, ${amount(p.price_list.monthly_fee)} a month`}
                        {p.product === "connect" && `, site ${amount(p.price_list.site_monthly)}`}
                      </span>
                    ) : (
                      <span className="pill warn">No prices yet</span>
                    )}
                  </td>
                  <td className="num mono" data-label="Organisations">
                    {p.subscribers}
                  </td>
                  <td data-label="Offered">
                    <span className={`pill ${p.active ? "ok" : "off"}`}>{p.active ? "Yes" : "No"}</span>
                  </td>
                  <td className="actions">
                    <RowActions
                      label={p.name}
                      items={[{ label: p.active ? "Stop offering" : "Offer again", onSelect: () => toggle(p), danger: p.active }]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="form card-inset" onSubmit={add} style={{ marginTop: 16 }}>
        <h3 className="wide">New plan</h3>
        <label>
          Product
          <select value={f.product} onChange={(e) => setF({ ...f, product: e.target.value as Product })}>
            <option value="connect">Connect</option>
            <option value="commai">Jibsy</option>
          </select>
        </label>
        <label>
          Name
          <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={80} placeholder="Connect Plus" />
        </label>
        <label className="wide">
          Description
          <input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} maxLength={300} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Add plan
          </button>
        </div>
      </form>
    </Card>
  );
}

function SubscriptionsCard({ onDone }: { onDone: (m: string) => void }) {
  const { current } = useCustomer();
  const cid = current?.id;
  const held = useApi<CustomerPlans>(cid ? billingPaths.customerPlans(cid) : null, 0);
  const plans = useApi<Plan[]>(billingPaths.plans, 0);
  const act = useAction();
  const [f, setF] = useState({ plan_id: "", starts_on: today() });
  const offered = (plans.data ?? []).filter((p) => p.active);
  const add = (e: FormEvent) => {
    e.preventDefault();
    if (!cid) return;
    act.run(async () => {
      const s = await subscribe(cid, { plan_id: f.plan_id, starts_on: f.starts_on });
      onDone(`${current?.name} now holds ${s.plan} from ${s.starts_on}.`);
      held.reload();
    });
  };
  const change = (sid: string, product: Product) =>
    act.run(async () => {
      if (!cid) return;
      const choices = offered.filter((p) => p.product === product);
      const name = window.prompt(`Change to which ${PRODUCT_NAMES[product]} plan? ${choices.map((p) => p.name).join(", ")}`);
      const plan = choices.find((p) => p.name === name?.trim());
      if (!plan) return;
      const on = window.prompt("From which day (YYYY-MM-DD)? The old plan ends that day.", today());
      if (!on) return;
      await changePlan(cid, sid, { plan_id: plan.id, on });
      onDone(`Changed to ${plan.name} from ${on}.`);
      held.reload();
    });
  const end = (sid: string, plan: string) =>
    act.run(async () => {
      if (!cid) return;
      const on = window.prompt(`End ${plan} on which day (YYYY-MM-DD)? It is held until the day before.`, today());
      if (!on) return;
      await endPlan(cid, sid, { on });
      onDone(`${plan} ends on ${on}.`);
      held.reload();
    });
  return (
    <Card title={`Subscriptions${current ? `: ${current.name}` : ""}`}>
      <ErrorNote error={held.error ?? act.error} />
      {held.data && (
        <p className="muted small">
          Holds today: {held.data.products.length ? held.data.products.map((p) => PRODUCT_NAMES[p]).join(" and ") : "nothing"}.
        </p>
      )}
      {held.data && held.data.subscriptions.length > 0 && (
        <SubscriptionTable
          subs={held.data.subscriptions}
          actions={(s) =>
            s.state === "ended" ? null : (
              <RowActions
                label={s.plan}
                items={[
                  { label: "Change plan", onSelect: () => change(s.id, s.product) },
                  { label: "End plan", onSelect: () => end(s.id, s.plan), danger: true },
                ]}
              />
            )
          }
        />
      )}
      <form className="form card-inset" onSubmit={add} style={{ marginTop: 16 }}>
        <h3 className="wide">Add a plan</h3>
        <label>
          Plan
          <select value={f.plan_id} onChange={(e) => setF({ ...f, plan_id: e.target.value })} required>
            <option value="">Choose…</option>
            {offered.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({PRODUCT_NAMES[p.product]})
              </option>
            ))}
          </select>
        </label>
        <label>
          From
          <input type="date" value={f.starts_on} onChange={(e) => setF({ ...f, starts_on: e.target.value })} required />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy || !cid}>
            Add plan
          </button>
        </div>
      </form>
    </Card>
  );
}

/** "ai_reply=0.02" lines to {ai_reply: "0.02"}. */
function parsePairs(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\n|,/)) {
    const [k, v] = line.split("=").map((s) => s?.trim());
    if (k && v) out[k] = v;
  }
  return out;
}

function PriceListsCard({ onDone }: { onDone: (m: string) => void }) {
  const { current } = useCustomer();
  const plans = useApi<Plan[]>(billingPaths.plans, 0);
  const lists = useApi<{ plans: PriceList[]; customer: PriceList[] }>(
    `${billingPaths.priceLists}${current ? `?customer_id=${current.id}` : ""}`,
    0,
  );
  const act = useAction();
  const [f, setF] = useState({
    plan_id: "",
    own: false,
    effective_from: `${monthKey(0)}-01`,
    label: "",
    currency: "USD",
    tax_rate_pct: "0",
    monthly_fee: "0",
    site_monthly: "0",
    commit_per_mbps: "0",
    burst_per_mbps: "0",
    satellite_per_gb: "0",
    circuit_per_mbps_month: "",
    meters: "ai_reply=0.02\nmessage_out:*=0.01",
    credits: "99.9=5\n99.5=10\n99=25",
    credit_cap_pct: "50",
  });
  const plan = plans.data?.find((p) => p.id === f.plan_id);
  const connect = plan?.product !== "commai";
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const credits = Object.entries(parsePairs(f.credits)).map(([below, credit]) => ({
        below_pct: Number(below),
        credit_pct: Number(credit),
      }));
      const body: Record<string, unknown> = {
        plan_id: f.plan_id,
        customer_id: f.own ? current?.id : null,
        effective_from: f.effective_from,
        label: f.label,
        currency: f.currency,
        tax_rate_pct: f.tax_rate_pct,
        monthly_fee: f.monthly_fee,
      };
      if (connect) {
        Object.assign(body, {
          site_monthly: f.site_monthly,
          commit_per_mbps: f.commit_per_mbps,
          burst_per_mbps: f.burst_per_mbps,
          satellite_per_gb: f.satellite_per_gb,
          circuit_per_mbps_month: f.circuit_per_mbps_month || null,
          sla_credits: credits,
          credit_cap_pct: f.credit_cap_pct,
        });
      } else body.meter_prices = parsePairs(f.meters);
      const pl = await createPriceList(body);
      onDone(`Saved ${pl.plan} prices, version ${pl.version}, from ${pl.effective_from}.`);
      lists.reload();
      plans.reload();
    });
  };
  const all = [...(lists.data?.customer ?? []), ...(lists.data?.plans ?? [])];
  return (
    <Card title="Price lists" note={<span className="muted small">Every save is a new version. Old versions never change.</span>}>
      <ErrorNote error={lists.error ?? act.error} />
      {all.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Plan</th>
                <th scope="col">For</th>
                <th scope="col" className="num">
                  Version
                </th>
                <th scope="col">From</th>
                <th scope="col">Prices</th>
              </tr>
            </thead>
            <tbody>
              {all.map((p) => (
                <tr key={p.id}>
                  <td>
                    {p.plan} {p.example && <ExampleTag />}
                  </td>
                  <td data-label="For">{p.customer_id ? current?.name ?? "One organisation" : "Everyone on the plan"}</td>
                  <td className="num mono" data-label="Version">
                    {p.version}
                  </td>
                  <td data-label="From" className="nowrap">
                    {p.effective_from}
                  </td>
                  <td data-label="Prices" className="small">
                    {p.currency}, tax {Number(p.tax_rate_pct)}%
                    {Number(p.monthly_fee) > 0 && `, plan ${amount(p.monthly_fee)}`}
                    {p.product === "connect"
                      ? `, site ${amount(p.site_monthly)}, commit ${amount(p.commit_per_mbps)}/Mbps, burst ${amount(p.burst_per_mbps)}/Mbps, satellite ${amount(p.satellite_per_gb)}/GB`
                      : `, ${Object.entries(p.meter_prices)
                          .map(([k, v]) => `${k} ${v}`)
                          .join(", ")}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="form card-inset" onSubmit={save} style={{ marginTop: 16 }}>
        <h3 className="wide">New version</h3>
        <label>
          Plan
          <select value={f.plan_id} onChange={set("plan_id")} required>
            <option value="">Choose…</option>
            {(plans.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={f.own} onChange={(e) => setF({ ...f, own: e.target.checked })} disabled={!current} />
          Only for {current?.name ?? "the chosen organisation"}
        </label>
        <label>
          Takes effect
          <input type="date" value={f.effective_from} onChange={set("effective_from")} required />
        </label>
        <label>
          Label
          <input value={f.label} onChange={set("label")} maxLength={120} placeholder="2027 contract" />
        </label>
        <label>
          Currency
          <input value={f.currency} onChange={set("currency")} pattern="[A-Za-z]{3}" required />
        </label>
        <label>
          Tax (%)
          <input value={f.tax_rate_pct} onChange={set("tax_rate_pct")} inputMode="decimal" />
        </label>
        <label>
          Plan fee a month
          <input value={f.monthly_fee} onChange={set("monthly_fee")} inputMode="decimal" />
        </label>
        {connect ? (
          <>
            <label>
              Site fee a month
              <input value={f.site_monthly} onChange={set("site_monthly")} inputMode="decimal" />
            </label>
            <label>
              Commit per Mbps
              <input value={f.commit_per_mbps} onChange={set("commit_per_mbps")} inputMode="decimal" />
            </label>
            <label>
              Burst per Mbps over commit
              <input value={f.burst_per_mbps} onChange={set("burst_per_mbps")} inputMode="decimal" />
            </label>
            <label>
              Satellite per GB
              <input value={f.satellite_per_gb} onChange={set("satellite_per_gb")} inputMode="decimal" />
            </label>
            <label>
              Circuits per Mbps a month
              <input value={f.circuit_per_mbps_month} onChange={set("circuit_per_mbps_month")} placeholder="Each circuit's own" />
            </label>
            <label className="wide">
              SLA credits (SLA met below % = credit % of the site fee, one per line)
              <textarea rows={3} value={f.credits} onChange={set("credits")} />
            </label>
            <label>
              Credit cap (% of site fee)
              <input value={f.credit_cap_pct} onChange={set("credit_cap_pct")} inputMode="decimal" />
            </label>
          </>
        ) : (
          <label className="wide">
            Meter prices (meter = price per unit, one per line; message_out:* covers every channel)
            <textarea rows={4} value={f.meters} onChange={set("meters")} />
          </label>
        )}
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Save version
          </button>
        </div>
      </form>
    </Card>
  );
}

function InvoicesCard({ onDone }: { onDone: (m: string) => void }) {
  const { current } = useCustomer();
  const [month, setMonth] = useState(monthKey(1));
  const invoices = useApi<Invoice[]>(`${billingPaths.invoices}?period=${month}`, 0);
  const act = useAction();
  const gen = (all: boolean) =>
    act.run(async () => {
      const r = await generateInvoices({ period: month, customer_id: all ? undefined : current?.id });
      const skipped = r.skipped.map((s) => `${s.customer} ${s.plan}: ${s.reason}`).join(" ");
      onDone(`${r.drafts.length} draft${r.drafts.length === 1 ? "" : "s"} ready for ${month}.${skipped ? ` Skipped: ${skipped}` : ""}`);
      invoices.reload();
    });
  const issue = (inv: Invoice) =>
    act.run(async () => {
      if (!window.confirm(`Issue the ${inv.plan} invoice for ${inv.customer}, ${inv.label}? It gets the next number and can't be changed afterwards.`))
        return;
      const r = await issueInvoice(inv.id);
      onDone(`Issued ${r.number}.`);
      invoices.reload();
    });
  const voidIt = (inv: Invoice) =>
    act.run(async () => {
      const reason = window.prompt(inv.status === "issued" ? `Why is ${inv.number} void?` : "Reason (optional)", "");
      if (reason === null) return;
      await voidInvoice(inv.id, reason);
      onDone(`${inv.number ?? "The draft"} is void.`);
      invoices.reload();
    });
  const exp = (inv: Invoice, fmt: "xero" | "quickbooks" | "csv") =>
    act.run(() => download(`/billing/invoices/${inv.id}/export/${fmt}`, `accounting-${inv.number}-${fmt}.${fmt === "csv" ? "csv" : "json"}`));
  return (
    <Card title="Invoices" note={<MonthPicker value={month} onChange={setMonth} />}>
      <ErrorNote error={invoices.error ?? act.error} />
      <div className="form-actions" style={{ marginBottom: 12 }}>
        <button className="button" disabled={act.busy} onClick={() => gen(true)}>
          Generate drafts for everyone
        </button>
        <button className="button secondary" disabled={act.busy || !current} onClick={() => gen(false)}>
          Generate for {current?.name ?? "this organisation"}
        </button>
      </div>
      <p className="muted small">Drafts can be generated again at will: the same invoices get fresh lines. Issued invoices never change.</p>
      <InvoiceTable
        invoices={invoices.data ?? []}
        showCustomer
        actions={(inv) => (
          <RowActions
            label={inv.number ?? `draft ${inv.customer} ${inv.plan}`}
            primary={
              <Link className="button secondary small" to={`/billing/invoices/${inv.id}`}>
                View
              </Link>
            }
            items={[
              ...(inv.status === "draft" ? [{ label: "Issue", onSelect: () => issue(inv) }] : []),
              { label: "Download CSV", onSelect: () => act.run(() => invoiceCsv(inv)) },
              { label: "Printable page", onSelect: () => window.open(printUrl(inv), "_blank", "noopener") },
              ...(inv.status === "issued"
                ? [
                    { label: "Export for Xero", onSelect: () => exp(inv, "xero") },
                    { label: "Export for QuickBooks", onSelect: () => exp(inv, "quickbooks") },
                    { label: "Export accounting CSV", onSelect: () => exp(inv, "csv") },
                  ]
                : []),
              ...(inv.status !== "void" && !inv.paid_at ? [{ label: "Void", onSelect: () => voidIt(inv), danger: true }] : []),
            ]}
          />
        )}
      />
      {invoices.data?.length === 0 && <p className="muted">No invoices for {month} yet.</p>}
    </Card>
  );
}

function PayablesCard({ onDone }: { onDone: (m: string) => void }) {
  const [month, setMonth] = useState(monthKey(1));
  const p = useApi<Payables>(`/billing/payables?period=${month}`, 0);
  const act = useAction();
  const cost = (id: number, name: string, now: string | null) =>
    act.run(async () => {
      const v = window.prompt(`What ExaCarib pays ${name} per Mbps a month (blank: not agreed yet)`, now ?? "");
      if (v === null) return;
      await setPartnerCost(id, v.trim() || null);
      onDone(`Saved ${name}'s cost.`);
      p.reload();
    });
  return (
    <Card title="Payables" note={<MonthPicker value={month} onChange={setMonth} />}>
      <p className="muted small">
        What ExaCarib owes carriers (commit plus burst per link, from the same settlement as the carrier view) and Fabric
        partners (per circuit, hour by hour). Kept apart from customer prices.
      </p>
      <ErrorNote error={p.error ?? act.error} />
      {p.data && (
        <>
          <dl className="bill-figures">
            <div>
              <dt>Total owed, {p.data.label}</dt>
              <dd>
                {p.data.currency} {amount(p.data.total)}
              </dd>
            </div>
          </dl>
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Carrier or partner</th>
                  <th scope="col" className="num">
                    Commit
                  </th>
                  <th scope="col" className="num">
                    Burst
                  </th>
                  <th scope="col" className="num">
                    Total
                  </th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {p.data.carriers.map((c) => (
                  <tr key={c.carrier_id}>
                    <td>
                      {c.carrier} <span className="muted small">({c.links.length} links)</span>
                    </td>
                    <td className="money" data-label="Commit">
                      {amount(c.commit)}
                    </td>
                    <td className="money" data-label="Burst">
                      {amount(c.burst)}
                    </td>
                    <td className="money" data-label="Total">
                      {amount(c.total)}
                    </td>
                    <td className="actions" />
                  </tr>
                ))}
                {p.data.partners.map((x) => (
                  <tr key={x.partner_id}>
                    <td>
                      {x.partner} <span className="muted small">(Fabric partner, {x.circuits.length} circuits)</span>
                      {x.unpriced > 0 && <span className="pill warn">{x.unpriced} without an agreed cost</span>}
                    </td>
                    <td className="money" data-label="Commit">
                      –
                    </td>
                    <td className="money" data-label="Burst">
                      –
                    </td>
                    <td className="money" data-label="Total">
                      {amount(x.total)}
                    </td>
                    <td className="actions">
                      <RowActions
                        label={x.partner}
                        items={[{ label: "Set cost per Mbps", onSelect: () => cost(x.partner_id, x.partner, x.circuits[0]?.cost_per_mbps_month ?? null) }]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

function MarginCard() {
  const [month, setMonth] = useState(monthKey(1));
  const m = useApi<BillingMargin>(`/billing/margin?period=${month}`, 0);
  const SOURCE: Record<string, string> = { issued: "issued", draft: "draft", estimate: "worked out now", unavailable: "unavailable" };
  return (
    <Card title="Margin" note={<MonthPicker value={month} onChange={setMonth} />}>
      <p className="muted small">Billed less what ExaCarib owes carriers and partners, in USD. Jibsy supply cost is not counted yet.</p>
      <ErrorNote error={m.error} />
      {m.data && (
        <>
          <h3>By service</h3>
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Service</th>
                  <th scope="col" className="num">
                    Billed
                  </th>
                  <th scope="col" className="num">
                    Cost
                  </th>
                  <th scope="col" className="num">
                    Margin
                  </th>
                </tr>
              </thead>
              <tbody>
                {m.data.services.map((s) => (
                  <tr key={s.service}>
                    <td>{s.label}</td>
                    <td className="money" data-label="Billed">
                      {amount(s.revenue)}
                    </td>
                    <td className="money" data-label="Cost">
                      {amount(s.cost)}
                    </td>
                    <td className="money" data-label="Margin">
                      {amount(s.margin)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>By organisation</h3>
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Organisation</th>
                  <th scope="col">Plans</th>
                  <th scope="col" className="num">
                    Billed
                  </th>
                  <th scope="col" className="num">
                    Carrier cost
                  </th>
                  <th scope="col" className="num">
                    Partner cost
                  </th>
                  <th scope="col" className="num">
                    Margin
                  </th>
                </tr>
              </thead>
              <tbody>
                {m.data.customers.map((c) => (
                  <tr key={c.customer_id}>
                    <td>
                      {c.customer} {c.example && <ExampleTag />}
                    </td>
                    <td data-label="Plans" className="small">
                      {c.plans.map((p) => `${p.plan} (${p.number ?? SOURCE[p.source]})`).join(", ") || "None"}
                    </td>
                    <td className="money" data-label="Billed">
                      {c.currency} {amount(c.revenue)}
                    </td>
                    <td className="money" data-label="Carrier cost">
                      {amount(c.carrier_cost)}
                    </td>
                    <td className="money" data-label="Partner cost">
                      {amount(c.partner_cost)}
                      {c.unpriced_circuits > 0 && <span className="pill warn">+{c.unpriced_circuits} unpriced</span>}
                    </td>
                    <td className="money" data-label="Margin">
                      {c.margin == null ? "Not in USD" : `${amount(c.margin)}${c.margin_pct ? ` (${c.margin_pct}%)` : ""}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

function PaymentsCard() {
  const s = useApi<PaymentStatus>("/billing/payments/status", 0);
  const WORD = { off: "Off", simulated: "Simulated", live: "Live" } as const;
  return (
    <Card title="Online payment and accounting">
      <ErrorNote error={s.error} />
      {s.data && (
        <>
          <p className="muted small">
            Payment is {WORD[s.data.mode].toLowerCase()}. Simulated mode makes no network calls. An invoice is marked
            paid only by the provider's signed event. Accounting exports (Xero, QuickBooks Online, CSV) are on each issued
            invoice; nothing is sent.
          </p>
          <ul className="note-list">
            {s.data.providers.map((p) => (
              <li key={p.key}>
                <span className={`pill ${p.mode === "live" ? "ok" : p.mode === "simulated" ? "warn" : "off"}`}>{WORD[p.mode]}</span>{" "}
                {p.label}
                {p.missing_env.length > 0 && <span className="muted small"> To go live, set: {p.missing_env.join(", ")}.</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
