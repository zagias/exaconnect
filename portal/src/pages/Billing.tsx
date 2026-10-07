import { useState, type ReactNode } from "react";
import { Link, Route, Routes, useParams, useSearchParams } from "react-router-dom";
import {
  amount,
  billingPaths,
  download,
  monthKey,
  payInvoice,
  simulatePayment,
  useApi,
  PRODUCT_NAMES,
  type BillingCharges,
  type BillingLine,
  type BillingTotals,
  type CustomerPlans,
  type Invoice,
  type InvoiceStatus,
  type PlanCharges,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Eyebrow, ExampleTag, when } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, RowActions, useAction } from "../ui";
import "./billing.css";

/** Billing (ADR 0022): each plan's running charges this month, invoices, and one
 *  invoice with its lines and credits, printable and as CSV. Read only for customers,
 *  apart from paying an issued invoice when online payment is switched on. */
export default function Billing() {
  return (
    <Routes>
      <Route index element={<BillingHome />} />
      <Route path="invoices/:id" element={<InvoicePage />} />
    </Routes>
  );
}

const STATUS: Record<InvoiceStatus, { cls: string; word: string }> = {
  draft: { cls: "warn", word: "Draft" },
  issued: { cls: "ok", word: "Issued" },
  void: { cls: "off", word: "Void" },
};

export function InvoiceStatusPill({ inv }: { inv: Pick<Invoice, "status" | "paid_at"> }) {
  if (inv.status === "issued" && inv.paid_at) return <span className="pill ok">Paid</span>;
  const s = STATUS[inv.status];
  return <span className={`pill ${s.cls}`}>{s.word}</span>;
}

const fileName = (inv: Invoice) => inv.number ?? `draft-${inv.period}-${inv.product}`;

export async function invoiceCsv(inv: Invoice): Promise<void> {
  await download(`/billing/invoices/${inv.id}/csv`, `exacarib-invoice-${fileName(inv)}.csv`);
}

/** The printable page opens in a new tab (the browser's print or save as PDF). */
export const printUrl = (inv: Invoice) => `/api/v1/billing/invoices/${inv.id}/print`;

const day = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const lastDay = (isoExclusive: string) => day(new Date(Date.parse(`${isoExclusive}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10));

function BillingHome() {
  const { current } = useCustomer();
  const { user } = useAuth();
  const [back, setBack] = useState(0);
  const [dlError, setDlError] = useState<string | null>(null);
  const cid = current?.id;
  const held = useApi<CustomerPlans>(cid ? billingPaths.customerPlans(cid) : null, 60_000);
  const charges = useApi<BillingCharges>(cid ? billingPaths.charges(cid, monthKey(back)) : null, 60_000);
  const invoices = useApi<Invoice[]>(cid ? `${billingPaths.invoices}?customer_id=${cid}` : null, 60_000);
  const csv = (inv: Invoice) => invoiceCsv(inv).catch((e: Error) => setDlError(e.message));
  const c = charges.data;
  const products = held.data?.products ?? [];

  return (
    <>
      <PageHead eyebrow="Billing" title="Plans, charges and invoices">
        Connect and Jibsy are separate plans, each with its own invoice. Months run on UTC.
      </PageHead>
      <ErrorNote error={held.error ?? charges.error ?? invoices.error ?? dlError} />

      <Card title="Your plans">
        {held.data && held.data.subscriptions.length === 0 && <p className="muted">No plans yet.</p>}
        {held.data && held.data.subscriptions.length > 0 && (
          <>
            <p className="muted small">
              Holding today: {products.length ? products.map((p) => PRODUCT_NAMES[p]).join(" and ") : "nothing"}.
            </p>
            <SubscriptionTable subs={held.data.subscriptions} />
          </>
        )}
      </Card>

      <Card
        title={back === 0 ? "This month so far" : "Last month"}
        note={
          <div className="segmented" role="group" aria-label="Month">
            <button aria-pressed={back === 0} onClick={() => setBack(0)}>
              This month
            </button>
            <button aria-pressed={back === 1} onClick={() => setBack(1)}>
              Last month
            </button>
          </div>
        }
      >
        {c && c.plans.length === 0 && <p className="muted">No plan covered {c.label}.</p>}
        {c && c.totals.length > 0 && (
          <p className="bill-running">
            {c.label}:{" "}
            {c.totals.map((t) => (
              <strong key={t.currency} className="mono">
                {t.currency} {amount(t.total)}{" "}
              </strong>
            ))}
            <span className="muted small">including tax, across {c.plans.length === 1 ? "one plan" : `${c.plans.length} plans`}</span>{" "}
            {c.example && <ExampleTag />}
          </p>
        )}
      </Card>
      {c?.plans.map((p) => <PlanBlock key={p.subscription_id} p={p} />)}

      <Card title="Invoices">
        <InvoiceTable
          invoices={invoices.data ?? []}
          showCustomer={false}
          actions={(inv) => (
            <RowActions
              label={inv.number ?? `draft for ${inv.label}`}
              primary={
                <Link className="button secondary small" to={`/billing/invoices/${inv.id}`}>
                  View
                </Link>
              }
              items={[
                { label: "Download CSV", onSelect: () => csv(inv) },
                { label: "Printable page", onSelect: () => window.open(printUrl(inv), "_blank", "noopener") },
              ]}
            />
          )}
        />
        {invoices.data?.length === 0 && (
          <p className="muted">
            No invoices yet.{" "}
            {user?.role === "admin" ? "Generate drafts in Admin, Billing." : "Invoices appear here when ExaCarib issues them."}
          </p>
        )}
      </Card>
    </>
  );
}

export function SubscriptionTable({ subs, actions }: { subs: CustomerPlans["subscriptions"]; actions?: (s: CustomerPlans["subscriptions"][number]) => ReactNode }) {
  return (
    <div className="table-wrap">
      <table className="paths dt stack">
        <thead>
          <tr>
            <th scope="col">Plan</th>
            <th scope="col">Product</th>
            <th scope="col">From</th>
            <th scope="col">Until</th>
            <th scope="col">State</th>
            {actions && (
              <th scope="col" className="actions">
                <span className="sr-only">Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {subs.map((s) => (
            <tr key={s.id}>
              <td>{s.plan}</td>
              <td data-label="Product">{PRODUCT_NAMES[s.product]}</td>
              <td data-label="From" className="nowrap">
                {day(s.starts_on)} {s.starts_on.slice(0, 4)}
              </td>
              <td data-label="Until" className="nowrap">
                {s.ends_on ? `${lastDay(s.ends_on)} ${s.ends_on.slice(0, 4)}` : <span className="muted">Open</span>}
              </td>
              <td data-label="State">
                <span className={`pill ${s.state === "active" ? "ok" : s.state === "scheduled" ? "warn" : "off"}`}>
                  {s.state === "active" ? "Active" : s.state === "scheduled" ? "Starts later" : "Ended"}
                </span>
              </td>
              {actions && <td className="actions">{actions(s)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlanBlock({ p }: { p: PlanCharges }) {
  const partial = p.covered_from !== p.period_start || p.covered_to !== p.period_end;
  return (
    <Card title={p.plan} note={p.example ? <ExampleTag /> : undefined}>
      <p className="muted small bill-note">
        {PRODUCT_NAMES[p.product]}, {p.label}
        {partial && ` (${day(p.covered_from)} to ${lastDay(p.covered_to)})`}, priced with{" "}
        {p.price_list.customer_id ? "your own prices" : "the plan's prices"} (version {p.price_list.version}, from{" "}
        {day(p.price_list.effective_from)}).{" "}
        {p.complete ? "The month has ended; the invoice follows." : "Worked out from usage so far; it changes until the month ends."}
      </p>
      <LinesTable lines={p.lines} currency={p.currency} />
      <Totals t={p} />
    </Card>
  );
}

export function InvoiceTable({
  invoices,
  showCustomer,
  actions,
}: {
  invoices: Invoice[];
  showCustomer: boolean;
  actions: (inv: Invoice) => ReactNode;
}) {
  if (invoices.length === 0) return null;
  return (
    <div className="table-wrap">
      <table className="paths dt stack">
        <thead>
          <tr>
            <th scope="col">Invoice</th>
            {showCustomer && <th scope="col">Customer</th>}
            <th scope="col">Plan</th>
            <th scope="col">Month</th>
            <th scope="col">Status</th>
            <th scope="col" className="num">
              Total
            </th>
            <th scope="col" className="actions">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {invoices.map((inv) => (
            <tr key={inv.id}>
              <td className="mono">
                {inv.number ?? <span className="muted">Not numbered</span>}
                {inv.example && (
                  <>
                    {" "}
                    <ExampleTag />
                  </>
                )}
              </td>
              {showCustomer && <td data-label="Customer">{inv.customer}</td>}
              <td data-label="Plan">{inv.plan}</td>
              <td data-label="Month" className="nowrap">
                {inv.label}
              </td>
              <td data-label="Status">
                <InvoiceStatusPill inv={inv} />
              </td>
              <td data-label="Total" className="money">
                {inv.currency} {amount(inv.total)}
              </td>
              <td className="actions">{actions(inv)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const INPUT_LABELS: Record<string, string> = {
  days_active: "Days billed",
  days_in_month: "Days in month",
  price_list_version: "Price list version",
  commit_mbps: "Commit (Mbps)",
  underlay_type: "Underlay",
  carrier: "Carrier",
  samples: "5-minute samples",
  discarded: "Discarded as the top 5%",
  p95_in_mbps: "95th percentile in (Mbps)",
  p95_out_mbps: "95th percentile out (Mbps)",
  billable_mbps: "Billable rate (Mbps)",
  windows: "10-second windows measured",
  met: "Windows within SLA",
  met_pct: "SLA met (%)",
  below_pct: "Threshold (%)",
  credit_pct: "Credit (% of site fee)",
  site_fee: "Site fee",
  cap: "Credit cap",
  price_per_mbps_month: "Price per Mbps a month",
  hours_per_month: "Hours in a billing month",
  meter: "Meter",
  records: "Usage records",
  price_key: "Priced as",
  charges: "Rated charges",
  rated_amount: "Rated by voice billing",
  already_on_voice_invoices: "Already on a voice invoice",
};
const HIDDEN = new Set(["site", "path", "class_name", "source", "capped", "samples_csv", "circuit", "metered", "voice_kind"]);

function show(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/\.?0+$/, "");
  return String(v);
}

/** The inputs behind a line, so a person can see why it is there. */
function Inputs({ line }: { line: BillingLine }) {
  const entries = Object.entries(line.inputs).filter(([k, v]) => !HIDDEN.has(k) && v !== null && typeof v !== "object");
  const segments = Array.isArray(line.inputs.segments)
    ? (line.inputs.segments as { mbps: number; from: string; to: string; hours: number; amount: number }[])
    : [];
  if (entries.length === 0 && segments.length === 0) return null;
  return (
    <details className="bill-why">
      <summary>How this was worked out</summary>
      <dl>
        {entries.map(([k, v]) => (
          <div key={k}>
            <dt>{INPUT_LABELS[k] ?? k.replace(/_/g, " ")}</dt>
            <dd className="mono">{show(v)}</dd>
          </div>
        ))}
      </dl>
      {segments.length > 0 && (
        <ul className="note-list small">
          {segments.map((s) => (
            <li key={s.from}>
              <span className="mono">{s.mbps} Mbps</span> from {when(s.from)} to {when(s.to)}: <span className="mono">{s.hours} h</span>
            </li>
          ))}
        </ul>
      )}
      {line.kind === "burst" && line.link_id && <p className="small muted">The samples are in the metering CSV for this link and month.</p>}
    </details>
  );
}

export function LinesTable({ lines, currency }: { lines: BillingLine[]; currency: string }) {
  if (lines.length === 0) return <p className="muted">Nothing to charge for this month.</p>;
  const charges = lines.filter((l) => l.kind !== "credit");
  const credits = lines.filter((l) => l.kind === "credit");
  const rows = (ls: BillingLine[]) =>
    ls.map((l) => (
      <tr key={l.position} className={l.kind === "credit" ? "bill-credit" : undefined}>
        <td>
          {l.description}
          <div className="muted small">{l.plan}</div>
          <Inputs line={l} />
        </td>
        <td className="num mono" data-label="Quantity">
          {Number(l.quantity).toLocaleString("en-GB", { maximumFractionDigits: 3 })} <span className="muted small">{l.unit}</span>
        </td>
        <td className="money" data-label="Unit price">
          {l.kind === "credit" ? "" : Number(l.unit_price).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 6 })}
        </td>
        <td className="money" data-label="Amount">
          {amount(l.amount)}
        </td>
      </tr>
    ));
  return (
    <div className="table-wrap">
      <table className="paths dt stack bill-lines">
        <thead>
          <tr>
            <th scope="col">Description</th>
            <th scope="col" className="num">
              Quantity
            </th>
            <th scope="col" className="num">
              Unit price <span className="unit">{currency}</span>
            </th>
            <th scope="col" className="num">
              Amount <span className="unit">{currency}</span>
            </th>
          </tr>
        </thead>
        <tbody>{rows(charges)}</tbody>
        {credits.length > 0 && (
          <tbody className="group">
            <tr>
              <th scope="colgroup" colSpan={4}>
                SLA credits
              </th>
            </tr>
            {rows(credits)}
          </tbody>
        )}
      </table>
    </div>
  );
}

export function Totals({ t }: { t: BillingTotals }) {
  return (
    <dl className="bill-totals">
      <div>
        <dt>Charges</dt>
        <dd className="money">{amount(t.charges)}</dd>
      </div>
      <div>
        <dt>SLA credits</dt>
        <dd className="money">{amount(t.credits)}</dd>
      </div>
      <div>
        <dt>Subtotal</dt>
        <dd className="money">{amount(t.subtotal)}</dd>
      </div>
      <div>
        <dt>Tax at {Number(t.tax_rate_pct)}%</dt>
        <dd className="money">{amount(t.tax)}</dd>
      </div>
      <div className="bill-total">
        <dt>Total</dt>
        <dd className="money">
          {t.currency} {amount(t.total)}
        </dd>
      </div>
    </dl>
  );
}

function InvoicePage() {
  const { id } = useParams();
  const { data: inv, error, reload } = useApi<Invoice>(id ? billingPaths.invoice(id) : null, 0);
  const [dlError, setDlError] = useState<string | null>(null);
  return (
    <>
      <div className="bill-toolbar no-print">
        <Link to="/billing">Back to billing</Link>
        {inv && (
          <div className="form-actions">
            <button className="button secondary" onClick={() => invoiceCsv(inv).catch((e: Error) => setDlError(e.message))}>
              Download CSV
            </button>
            <a className="button secondary" href={printUrl(inv)} target="_blank" rel="noopener">
              Printable page
            </a>
            <button className="button" onClick={() => window.print()}>
              Print
            </button>
          </div>
        )}
      </div>
      <ErrorNote error={error ?? dlError} />
      {inv && <PayPanel inv={inv} onChange={reload} />}
      {inv && <InvoiceDocument inv={inv} />}
    </>
  );
}

/** Online payment, when ExaCarib has switched it on. In simulated mode the
 *  provider's page is stood in for here, and completing it sends the provider's
 *  signed event through the same webhook a real payment uses. */
function PayPanel({ inv, onChange }: { inv: Invoice; onChange: () => void }) {
  const [params, setParams] = useSearchParams();
  const act = useAction();
  const simulated = params.get("simulated");
  const paymentId = params.get("payment");
  if (inv.status !== "issued" || inv.paid_at) return null;
  const pay = (provider: "stripe" | "hosted") =>
    act.run(async () => {
      const r = await payInvoice(inv.id, provider);
      window.location.assign(r.url);
    });
  const finish = (approved: boolean) =>
    act.run(async () => {
      if (!paymentId) return;
      await simulatePayment(paymentId, approved);
      setParams({});
      onChange();
    });
  return (
    <section className="card no-print bill-pay" style={{ marginBottom: 24 }}>
      {simulated && paymentId ? (
        <>
          <Eyebrow>Simulated payment page</Eyebrow>
          <p>
            No money moves: online payment is in simulated mode. Choose what the payer does on the provider's page.{" "}
            <ExampleTag />
          </p>
          <div className="form-actions">
            <button className="button" disabled={act.busy} onClick={() => finish(true)}>
              Pay {inv.currency} {amount(inv.total)}
            </button>
            <button className="button secondary" disabled={act.busy} onClick={() => finish(false)}>
              Decline
            </button>
          </div>
        </>
      ) : (
        <>
          <Eyebrow>Pay online</Eyebrow>
          <div className="form-actions">
            <button className="button" disabled={act.busy} onClick={() => pay("stripe")}>
              Pay by card (Stripe)
            </button>
            <button className="button secondary" disabled={act.busy} onClick={() => pay("hosted")}>
              Pay on the bank's page
            </button>
          </div>
          <p className="muted small">If online payment is switched off, pay by bank transfer as on the invoice.</p>
        </>
      )}
      <ErrorNote error={act.error} />
    </section>
  );
}

/** The invoice as a document: what prints. */
export function InvoiceDocument({ inv }: { inv: Invoice }) {
  return (
    <article className="card bill-doc">
      <header className="bill-doc-head">
        <div>
          <img src="/brand/exacarib-wordmark.png" alt="ExaCarib" width={160} height={29} className="bill-logo" />
          <div className="muted small">{inv.plan}</div>
        </div>
        <div className="bill-doc-meta">
          <Eyebrow>{inv.status === "draft" ? "Draft invoice" : "Invoice"}</Eyebrow>
          <h1 className="mono">{inv.number ?? "Not yet numbered"}</h1>
          <InvoiceStatusPill inv={inv} /> {inv.example && <ExampleTag />}
        </div>
      </header>
      <dl className="bill-facts">
        <div>
          <dt>Customer</dt>
          <dd>{inv.customer}</dd>
        </div>
        <div>
          <dt>Plan</dt>
          <dd>
            {inv.plan} <span className="muted small">({PRODUCT_NAMES[inv.product]})</span>
          </dd>
        </div>
        <div>
          <dt>Period</dt>
          <dd>
            {inv.label}
            {(inv.covered_from !== inv.period_start || inv.covered_to !== inv.period_end) &&
              `, ${day(inv.covered_from)} to ${lastDay(inv.covered_to)}`}{" "}
            <span className="muted small">(UTC)</span>
          </dd>
        </div>
        <div>
          <dt>{inv.issued_at ? "Issued" : "Worked out"}</dt>
          <dd>{when(inv.issued_at ?? inv.generated_at)}</dd>
        </div>
        <div>
          <dt>Prices</dt>
          <dd>
            {inv.own_list ? "Your own" : "The plan's"}, version {inv.price_list_version}
          </dd>
        </div>
      </dl>
      {inv.status === "void" && (
        <p className="callout">
          Void{inv.voided_at ? ` since ${when(inv.voided_at)}` : ""}
          {inv.void_reason ? `: ${inv.void_reason}` : "."}
        </p>
      )}
      {inv.paid_at && <p className="pill ok">Paid {when(inv.paid_at)}</p>}
      {inv.example && <p className="small muted">Priced with example prices. These are not real prices.</p>}
      <LinesTable lines={inv.lines ?? []} currency={inv.currency} />
      <Totals t={inv} />
      <p className="small muted bill-foot">
        {inv.product === "connect"
          ? "Burst is the 95th percentile of the month's 5-minute samples above commit, the same samples as the metering screen and CSV. SLA credits come from Connect's probe measurements for each class."
          : "Usage comes from Jibsy's metered records; voice is rated by Jibsy Voice's rate card and appears here as rated."}
      </p>
    </article>
  );
}
