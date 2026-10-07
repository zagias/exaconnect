import { useState } from "react";
import { Link, Route, Routes, useParams } from "react-router-dom";
import {
  amount,
  download,
  monthKey,
  useApi,
  type BillingCharges,
  type BillingLine,
  type Invoice,
  type InvoiceStatus,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Eyebrow, ExampleTag, when } from "../components";
import { useCustomer } from "../customer";
import { Card, PageHead, RowActions } from "../ui";
import "./billing.css";

/** Billing (ADR 0024): the month's running charges, invoices, and one invoice
 *  with its lines and credits, printable and as CSV. Read only for customers. */
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

export function InvoiceStatusPill({ status }: { status: InvoiceStatus }) {
  const s = STATUS[status];
  return <span className={`pill ${s.cls}`}>{s.word}</span>;
}

export async function invoiceCsv(inv: Invoice): Promise<void> {
  await download(`/billing/invoices/${inv.id}/csv`, `exacarib-invoice-${inv.number ?? `draft-${inv.period}`}.csv`);
}

function BillingHome() {
  const { current } = useCustomer();
  const { user } = useAuth();
  const [back, setBack] = useState(0);
  const [dlError, setDlError] = useState<string | null>(null);
  const cid = current?.id;
  const charges = useApi<BillingCharges>(cid ? `/billing/charges?customer_id=${cid}&period=${monthKey(back)}` : null, 60_000);
  const invoices = useApi<Invoice[]>(cid ? `/billing/invoices?customer_id=${cid}` : null, 60_000);
  const csv = (inv: Invoice) => invoiceCsv(inv).catch((e: Error) => setDlError(e.message));
  const c = charges.data;

  return (
    <>
      <PageHead eyebrow="Billing" title="Charges and invoices">
        What Connect charges for each month: site fees, commit, burst at the 95th percentile, satellite data and
        virtual circuits, less automatic credits when a class misses its SLA. Months run on UTC.
      </PageHead>
      <ErrorNote error={charges.error ?? invoices.error ?? dlError} />

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
        {c && (
          <>
            <p className="muted small bill-note">
              {c.label}, priced with {c.price_list.customer_id ? "your price list" : "the standard price list"} (version{" "}
              {c.price_list.version}, from {c.price_list.effective_from}).{" "}
              {c.complete ? "The month has ended; the invoice follows." : "Worked out from usage so far; it changes until the month ends."}{" "}
              {c.example && <ExampleTag />}
            </p>
            <LinesTable lines={c.lines} currency={c.currency} />
            <Totals t={c} />
          </>
        )}
      </Card>

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
              items={[{ label: "Download CSV", onSelect: () => csv(inv) }]}
            />
          )}
        />
        {invoices.data?.length === 0 && (
          <p className="muted">
            No invoices yet. {user?.role === "admin" ? "Generate drafts in Admin, Billing." : "Invoices appear here when ExaCarib issues them."}
          </p>
        )}
      </Card>
    </>
  );
}

export function InvoiceTable({
  invoices,
  showCustomer,
  actions,
}: {
  invoices: Invoice[];
  showCustomer: boolean;
  actions: (inv: Invoice) => React.ReactNode;
}) {
  if (invoices.length === 0) return null;
  return (
    <div className="table-wrap">
      <table className="paths dt stack">
        <thead>
          <tr>
            <th scope="col">Invoice</th>
            {showCustomer && <th scope="col">Customer</th>}
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
              <td data-label="Month" className="nowrap">
                {inv.label}
              </td>
              <td data-label="Status">
                <InvoiceStatusPill status={inv.status} />
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
};
const HIDDEN = new Set(["site", "path", "class_name", "source", "capped", "samples_csv", "circuit"]);

function show(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/\.?0+$/, "");
  return String(v);
}

/** The inputs behind a line, so a person can see why it is there. */
function Inputs({ line }: { line: BillingLine }) {
  const entries = Object.entries(line.inputs).filter(([k, v]) => !HIDDEN.has(k) && v !== null && typeof v !== "object");
  const segments = Array.isArray(line.inputs.segments) ? (line.inputs.segments as { mbps: number; from: string; to: string; hours: number; amount: number }[]) : [];
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
              <span className="mono">{s.mbps} Mbps</span> from {when(s.from)} to {when(s.to)}:{" "}
              <span className="mono">{s.hours} h</span>
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

export function Totals({ t }: { t: { currency: string; charges: string; credits: string; subtotal: string; tax_rate_pct: string; tax: string; total: string } }) {
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
  const { data: inv, error } = useApi<Invoice>(id ? `/billing/invoices/${id}` : null, 0);
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
            <button className="button" onClick={() => window.print()}>
              Print
            </button>
          </div>
        )}
      </div>
      <ErrorNote error={error ?? dlError} />
      {inv && <InvoiceDocument inv={inv} />}
    </>
  );
}

/** The invoice as a document: what prints. */
export function InvoiceDocument({ inv }: { inv: Invoice }) {
  return (
    <article className="card bill-doc">
      <header className="bill-doc-head">
        <div>
          <img src="/brand/exacarib-wordmark.png" alt="ExaCarib" width={160} height={29} className="bill-logo" />
          <div className="muted small">Connect</div>
        </div>
        <div className="bill-doc-meta">
          <Eyebrow>{inv.status === "draft" ? "Draft invoice" : "Invoice"}</Eyebrow>
          <h1 className="mono">{inv.number ?? "Not yet numbered"}</h1>
          <InvoiceStatusPill status={inv.status} /> {inv.example && <ExampleTag />}
        </div>
      </header>
      <dl className="bill-facts">
        <div>
          <dt>Customer</dt>
          <dd>{inv.customer}</dd>
        </div>
        <div>
          <dt>Month</dt>
          <dd>
            {inv.label} <span className="muted small">(UTC)</span>
          </dd>
        </div>
        <div>
          <dt>{inv.issued_at ? "Issued" : "Worked out"}</dt>
          <dd>{when(inv.issued_at ?? inv.generated_at)}</dd>
        </div>
        <div>
          <dt>Price list</dt>
          <dd>
            {inv.default_list ? "Standard" : "Customer"}, version {inv.price_list_version}
          </dd>
        </div>
      </dl>
      {inv.status === "void" && (
        <p className="callout">
          Void{inv.voided_at ? ` since ${when(inv.voided_at)}` : ""}
          {inv.void_reason ? `: ${inv.void_reason}` : "."}
        </p>
      )}
      {inv.example && <p className="small muted">Priced with the example price list. These are not real prices.</p>}
      <LinesTable lines={inv.lines ?? []} currency={inv.currency} />
      <Totals t={inv} />
      <p className="small muted bill-foot">
        Burst is the 95th percentile of the month's 5-minute samples above commit, the same samples as the metering
        screen and CSV. SLA credits come from Connect's probe measurements for each class.
      </p>
    </article>
  );
}
