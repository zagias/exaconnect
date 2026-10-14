import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import {
  acceptQuote,
  addCataloguePrice,
  amount,
  approveQuote,
  cancelSalesOrder,
  commercialPaths,
  createCatalogueItem,
  createQuote,
  declineQuote,
  endContractItem,
  orderItemCancel,
  orderItemDone,
  orderItemRetry,
  previewQuote,
  quoteAction,
  quoteDocumentUrl,
  updateCatalogueItem,
  updateQuote,
  useApi,
  PRODUCT_NAMES,
  type CatalogueItem,
  type ChargeType,
  type Contract,
  type OrderRow,
  type PriceModel,
  type Product,
  type Quote,
  type QuoteIn,
  type QuoteLine,
  type QuoteRow,
  type QuoteState,
  type QuoteTotals,
  type SalesOrder,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote, ExampleTag } from "../components";
import { useCustomer, who } from "../customer";
import { Card, EmptyState, PageHead, RowActions, useAction } from "../ui";
import "./billing.css";

/* Catalogue, quotes (CPQ), sales orders and contracts (ADR 0055).
   ExaCarib staff see them under Operations (/admin/...); an organisation sees
   its own under Organisation (/quotes, /orders) and on the Billing page. */

const CHARGED: Record<ChargeType, string> = { recurring: "Monthly", one_off: "Once", usage: "By use" };
const QUOTE_PILL: Record<QuoteState, string> = {
  draft: "off",
  pending_approval: "warn",
  approved: "ok",
  sent: "ok",
  accepted: "ok",
  declined: "bad",
  cancelled: "off",
  expired: "warn",
};
const ORDER_PILL = { acknowledged: "off", in_progress: "warn", completed: "ok", partial: "warn", cancelled: "off" } as const;
const ITEM_PILL = { pending: "warn", done: "ok", failed: "bad", cancelled: "off" } as const;
const ITEM_WORD = { pending: "Waiting", done: "Done", failed: "Failed", cancelled: "Cancelled" } as const;

const day = (iso: string | null | undefined) =>
  iso ? new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–";
const qty = (v: string) => String(Number(v));
const isoIn = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);

function useRoles() {
  const { user } = useAuth();
  const staff = user?.role === "admin";
  const manager = staff || (user?.role === "customer" && (user.org_role === "owner" || user.org_role === "admin"));
  return { staff, manager, base: staff ? "/admin" : "" };
}

/** Quotes and orders for an organisation: list, builder, one quote, orders. */
export default function QuotesAndOrders() {
  return (
    <Routes>
      <Route index element={<QuoteList />} />
      <Route path="new" element={<QuoteBuilder />} />
      <Route path=":id" element={<QuotePage />} />
      <Route path=":id/edit" element={<QuoteBuilder />} />
    </Routes>
  );
}

export function Orders() {
  return (
    <Routes>
      <Route index element={<OrderList />} />
      <Route path=":id" element={<OrderPage />} />
    </Routes>
  );
}

function Head({ title, children }: { title: string; children?: ReactNode }) {
  const { staff } = useRoles();
  // Operations screens get their heading from the admin frame.
  if (staff) return null;
  return <PageHead title={title}>{children}</PageHead>;
}

// ---- quotes ----------------------------------------------------------------------------------

function QuoteList() {
  const { staff, manager, base } = useRoles();
  const { current } = useCustomer();
  const [all, setAll] = useState(staff);
  const cid = current?.id;
  const path = staff && all ? commercialPaths.quotes : cid ? `${commercialPaths.quotes}?customer_id=${cid}` : null;
  const quotes = useApi<{ quotes: QuoteRow[] }>(path, 30_000);
  const rows = quotes.data?.quotes ?? [];
  return (
    <>
      <Head title="Quotes and orders">
        Quotes from ExaCarib, or ones you build from the catalogue. Accepting a quote places the order.{" "}
        <Link to="/orders">Your orders</Link>.
      </Head>
      <ErrorNote error={quotes.error} />
      <Card
        title="Quotes"
        note={
          <div className="row-gap">
            {staff && (
              <div className="segmented" role="group" aria-label="Which organisations">
                <button aria-pressed={all} onClick={() => setAll(true)}>
                  Everyone
                </button>
                <button aria-pressed={!all} onClick={() => setAll(false)}>
                  {current?.name ?? "This organisation"}
                </button>
              </div>
            )}
            {manager && (
              <Link className="button small" to={`${base}/quotes/new`}>
                New quote
              </Link>
            )}
          </div>
        }
      >
        {quotes.data && rows.length === 0 && (
          <EmptyState title="No quotes yet">
            {staff ? "Build one for the organisation you're working on." : manager ? "Build one from the catalogue, or ask ExaCarib." : "Quotes ExaCarib sends appear here."}
          </EmptyState>
        )}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Quote</th>
                  {staff && <th scope="col">Organisation</th>}
                  <th scope="col">State</th>
                  <th scope="col" className="num">
                    Monthly
                  </th>
                  <th scope="col" className="num">
                    Once
                  </th>
                  <th scope="col" className="num">
                    Contract value
                  </th>
                  <th scope="col">Valid until</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((q) => (
                  <tr key={q.id}>
                    <td>
                      <Link to={`${base}/quotes/${q.id}`}>{q.number}</Link> {q.example && <ExampleTag />}
                      {q.title && <div className="muted small">{q.title}</div>}
                    </td>
                    {staff && <td data-label="Organisation">{q.customer}</td>}
                    <td data-label="State">
                      <span className={`pill ${QUOTE_PILL[q.state]}`}>{q.state_label}</span>
                      {q.order_number && (
                        <div className="small">
                          <Link to={`${base}/orders/${q.order_id}`}>{q.order_number}</Link>
                        </div>
                      )}
                    </td>
                    <td className="num mono" data-label="Monthly">
                      {q.currency} {amount(q.monthly_total)}
                    </td>
                    <td className="num mono" data-label="Once">
                      {amount(q.one_off_total)}
                    </td>
                    <td className="num mono" data-label="Contract value">
                      {amount(q.contract_total)}
                      <div className="muted small">{q.term_months} months</div>
                    </td>
                    <td data-label="Valid until" className="nowrap">
                      {day(q.valid_until)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

interface Draft {
  key: number;
  item: string;
  quantity: string;
  discount_pct: string;
  locations: string;
  location: string;
  carrier: string;
  underlay_type: string;
  note: string;
}

let nextKey = 1;
const blankLine = (item = ""): Draft => ({
  key: nextKey++,
  item,
  quantity: "1",
  discount_pct: "0",
  locations: "",
  location: "",
  carrier: "",
  underlay_type: "fibre",
  note: "",
});

function toLine(d: Draft, item: CatalogueItem | undefined): QuoteIn["lines"][number] {
  const config: Record<string, unknown> = {};
  if (item?.fulfilment === "site") {
    const names = d.locations.split(",").map((s) => s.trim()).filter(Boolean);
    if (names.length) config.locations = names;
  } else if (item?.fulfilment === "link") {
    Object.assign(config, { location: d.location.trim(), carrier: d.carrier.trim(), underlay_type: d.underlay_type });
  } else if (d.note.trim()) config.note = d.note.trim();
  return { item: d.item, quantity: d.quantity || "1", discount_pct: Number(d.discount_pct) || 0, config };
}

function fromLine(l: QuoteLine): Draft {
  const c = l.config as Record<string, string | string[] | undefined>;
  return {
    ...blankLine(l.code),
    quantity: qty(l.quantity),
    discount_pct: String(Number(l.discount_pct)),
    locations: Array.isArray(c.locations) ? c.locations.join(", ") : "",
    location: (c.location as string) ?? "",
    carrier: (c.carrier as string) ?? "",
    underlay_type: (c.underlay_type as string) ?? "fibre",
    note: (c.note as string) ?? "",
  };
}

/** Build or change a quote, priced live by the rating engine as you type. */
function QuoteBuilder() {
  const { id } = useParams();
  const { staff, base } = useRoles();
  const { current } = useCustomer();
  const navigate = useNavigate();
  const existing = useApi<Quote>(id ? commercialPaths.quote(id) : null, 0);
  const catalogue = useApi<{ items: CatalogueItem[] }>(commercialPaths.items, 0);
  const items = useMemo(() => catalogue.data?.items ?? [], [catalogue.data]);
  const byCode = useMemo(() => Object.fromEntries(items.map((i) => [i.code, i])), [items]);
  const [head, setHead] = useState({ title: "", currency: "USD", term_months: "12", valid_until: isoIn(30), notes: "" });
  const [lines, setLines] = useState<Draft[]>([blankLine()]);
  const [loaded, setLoaded] = useState(false);
  const [priced, setPriced] = useState<(QuoteTotals & { lines: QuoteLine[] }) | null>(null);
  const [priceError, setPriceError] = useState<string | null>(null);
  const act = useAction();
  const q = existing.data;
  const cid = q?.customer_id ?? current?.id;

  useEffect(() => {
    if (!q || loaded) return;
    setHead({ title: q.title, currency: q.currency, term_months: String(q.term_months), valid_until: q.valid_until, notes: q.notes });
    setLines(q.lines.length ? q.lines.map(fromLine) : [blankLine()]);
    setLoaded(true);
  }, [q, loaded]);

  const body = (): QuoteIn => ({
    customer_id: staff ? cid : undefined,
    title: head.title.trim(),
    currency: head.currency.toUpperCase(),
    term_months: Number(head.term_months) || 12,
    valid_until: head.valid_until,
    notes: head.notes,
    lines: lines.filter((l) => l.item).map((l) => toLine(l, byCode[l.item])),
  });
  const key = JSON.stringify(body());

  useEffect(() => {
    if (!cid || !lines.some((l) => l.item)) {
      setPriced(null);
      return;
    }
    const t = setTimeout(() => {
      previewQuote(JSON.parse(key))
        .then((p) => {
          setPriced(p);
          setPriceError(null);
        })
        .catch((e: Error) => setPriceError(e.message));
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, cid]);

  const set = (k: number, patch: Partial<Draft>) => setLines((ls) => ls.map((l) => (l.key === k ? { ...l, ...patch } : l)));
  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const saved = id ? await updateQuote(id, body()) : await createQuote(body());
      navigate(`${base}/quotes/${saved.id}`);
    });
  };
  if (id && q && !["draft", "pending_approval", "approved"].includes(q.status))
    return <p className="muted">{q.number} has been sent and can't be changed. Copy it instead.</p>;
  const pricedLine = (i: number) => priced?.lines[i];
  const chosen = lines.filter((l) => l.item);

  return (
    <>
      <Head title={id ? `Change ${q?.number ?? "quote"}` : "New quote"}>
        Pick items from the catalogue; prices come from the catalogue and your plan's price list.
      </Head>
      <ErrorNote error={existing.error ?? catalogue.error ?? act.error} />
      <form className="card" onSubmit={save} style={{ marginBottom: 24 }}>
        <div className="card-head">
          <h2>{id ? `${q?.number ?? ""} for ${q?.customer ?? ""}` : `Quote for ${current?.name ?? "…"}`}</h2>
          {items.some((i) => i.example) && <ExampleTag />}
        </div>
        <div className="form">
          <label>
            Title
            <input value={head.title} onChange={(e) => setHead({ ...head, title: e.target.value })} maxLength={120} placeholder="Two branches with managed service" />
          </label>
          <label>
            Contract term (months)
            <input type="number" min={1} max={120} value={head.term_months} onChange={(e) => setHead({ ...head, term_months: e.target.value })} required />
          </label>
          <label>
            Currency
            <input value={head.currency} onChange={(e) => setHead({ ...head, currency: e.target.value })} maxLength={3} pattern="[A-Za-z]{3}" required />
          </label>
          <label>
            Valid until
            <input type="date" value={head.valid_until} onChange={(e) => setHead({ ...head, valid_until: e.target.value })} required />
          </label>
        </div>

        <h3>Items</h3>
        {lines.map((l, i) => {
          const it = byCode[l.item];
          const p = l.item ? pricedLine(chosen.indexOf(l)) : undefined;
          return (
            <div key={l.key} className="form card-inset" style={{ marginBottom: 12 }}>
              <label className="wide">
                Item
                <select value={l.item} onChange={(e) => set(l.key, { item: e.target.value })} aria-label={`Item ${i + 1}`}>
                  <option value="">Choose…</option>
                  {(["connect", "commai"] as Product[]).map((prod) => (
                    <optgroup key={prod} label={PRODUCT_NAMES[prod]}>
                      {items
                        .filter((x) => x.product === prod)
                        .map((x) => (
                          <option key={x.code} value={x.code}>
                            {x.name} ({CHARGED[x.charge_type].toLowerCase()})
                          </option>
                        ))}
                    </optgroup>
                  ))}
                </select>
                {it && <span className="muted small">{it.price_text}</span>}
              </label>
              <label>
                {it?.fulfilment === "link" ? "Commit (Mbps)" : it?.charge_type === "usage" ? `Expected ${it.unit}s a month` : `Quantity${it ? ` (${it.unit})` : ""}`}
                <input type="number" min={0} step="any" value={l.quantity} onChange={(e) => set(l.key, { quantity: e.target.value })} required />
              </label>
              {staff && (
                <label>
                  Discount %{it && <span className="muted small"> up to {Number(it.max_discount_pct)}% without approval</span>}
                  <input type="number" min={0} max={100} step="any" value={l.discount_pct} onChange={(e) => set(l.key, { discount_pct: e.target.value })} />
                </label>
              )}
              {it?.fulfilment === "site" && (
                <label className="wide">
                  Locations, one per site (comma separated, optional)
                  <input value={l.locations} onChange={(e) => set(l.key, { locations: e.target.value })} placeholder="Kingston HQ, Montego Bay" />
                </label>
              )}
              {it?.fulfilment === "link" && (
                <>
                  <label>
                    Location
                    <input value={l.location} onChange={(e) => set(l.key, { location: e.target.value })} placeholder="Kingston HQ" />
                  </label>
                  <label>
                    Carrier
                    <input value={l.carrier} onChange={(e) => set(l.key, { carrier: e.target.value })} placeholder="Flow" />
                  </label>
                  <label>
                    Link type
                    <select value={l.underlay_type} onChange={(e) => set(l.key, { underlay_type: e.target.value })}>
                      {["fibre", "broadband", "lte", "leo", "geo"].map((u) => (
                        <option key={u}>{u}</option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              {it && !["site", "link", "subscription"].includes(it.fulfilment) && (
                <label className="wide">
                  Note (optional)
                  <input value={l.note} onChange={(e) => set(l.key, { note: e.target.value })} maxLength={300} />
                </label>
              )}
              <div className="actions" style={{ justifyContent: "space-between", width: "100%" }}>
                <span className="mono">
                  {p ? (
                    <>
                      {head.currency} {amount(p.amount)} {it && <span className="muted small">{CHARGED[it.charge_type].toLowerCase()}</span>}
                      {Number(p.term_discount_pct) > 0 && <span className="muted small"> · {Number(p.term_discount_pct)}% term discount</span>}
                      {p.needs_approval && <> <span className="pill warn small">Needs approval</span></>}
                    </>
                  ) : (
                    ""
                  )}
                </span>
                <button type="button" className="button secondary small" onClick={() => setLines((ls) => (ls.length > 1 ? ls.filter((x) => x.key !== l.key) : [blankLine()]))}>
                  Remove
                </button>
              </div>
            </div>
          );
        })}
        <button type="button" className="button secondary small" onClick={() => setLines((ls) => [...ls, blankLine()])}>
          Add an item
        </button>

        <div className="form" style={{ marginTop: 16 }}>
          <label className="wide">
            Notes on the quote
            <textarea value={head.notes} onChange={(e) => setHead({ ...head, notes: e.target.value })} maxLength={4000} rows={3} />
          </label>
        </div>

        <ErrorNote error={priceError} />
        {priced && <Totals t={priced} currency={head.currency.toUpperCase()} term={Number(head.term_months) || 12} />}
        <div className="form-actions">
          <button className="button" disabled={act.busy || !cid || !chosen.length}>
            {id ? "Save changes" : "Save draft"}
          </button>
          <Link className="button secondary" to={id ? `${base}/quotes/${id}` : `${base}/quotes`}>
            Cancel
          </Link>
        </div>
      </form>
    </>
  );
}

function Totals({ t, currency, term }: { t: QuoteTotals; currency: string; term: number }) {
  return (
    <dl className="bill-totals">
      <div>
        <dt>Once</dt>
        <dd className="mono">
          {currency} {amount(t.one_off_total)}
        </dd>
      </div>
      <div>
        <dt>Every month</dt>
        <dd className="mono">
          {currency} {amount(t.monthly_total)}
        </dd>
      </div>
      {Number(t.usage_estimate) > 0 && (
        <div>
          <dt>Usage (estimate, a month)</dt>
          <dd className="mono">
            {currency} {amount(t.usage_estimate)}
          </dd>
        </div>
      )}
      <div className="bill-total">
        <dt>Contract value, {term} months</dt>
        <dd className="mono">
          {currency} {amount(t.contract_total)}
        </dd>
      </div>
      <p className="muted small">Before tax. Usage is billed as used.</p>
    </dl>
  );
}

function QuotePage() {
  const { id = "" } = useParams();
  const { staff, manager, base } = useRoles();
  const navigate = useNavigate();
  const quote = useApi<Quote>(commercialPaths.quote(id), 0);
  const act = useAction();
  const [msg, setMsg] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const q = quote.data;
  if (!q) return <ErrorNote error={quote.error} />;
  const done = (m: string) => {
    setMsg(m);
    quote.reload();
  };
  const run = (fn: () => Promise<unknown>, m: string) => act.run(async () => {
    await fn();
    done(m);
  });
  const ownDraft = !staff && ["draft", "approved"].includes(q.status);
  const open = q.state !== "expired";

  return (
    <>
      <Head title={`Quote ${q.number}`}>{q.title}</Head>
      {msg && (
        <p className="pill ok" role="status">
          {msg}
        </p>
      )}
      <ErrorNote error={act.error} />
      <section className="card" style={{ marginBottom: 24 }}>
        <div className="bill-doc-head">
          <div>
            <h2 style={{ margin: 0 }}>
              {q.number} {q.example && <ExampleTag />}
            </h2>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              For {q.customer}. {q.term_months}-month term. Valid until {day(q.valid_until)}.
            </p>
          </div>
          <div className="bill-doc-meta">
            <span className={`pill ${QUOTE_PILL[q.state]}`}>{q.state_label}</span>
            {q.order && (
              <div className="small" style={{ marginTop: 4 }}>
                Order <Link to={`${base}/orders/${q.order.id}`}>{q.order.number}</Link>
              </div>
            )}
          </div>
        </div>
        {q.approved_by && <p className="muted small">Discounts approved by {who(q.approved_by)}: {q.approval_note}</p>}
        {q.status === "draft" && q.approval_note && <p className="pill warn small">Approval refused: {q.approval_note}</p>}
        {q.accepted_at && (
          <p className="small">
            Accepted by {q.accepted_name} on {day(q.accepted_at)}.
          </p>
        )}
        {q.declined_at && <p className="small">Declined on {day(q.declined_at)}{q.decline_reason ? `: ${q.decline_reason}` : "."}</p>}
        <LinesTable lines={q.lines} currency={q.currency} />
        <Totals t={q} currency={q.currency} term={q.term_months} />
        {q.notes && <p className="muted">{q.notes}</p>}
        <div className="form-actions no-print">
          <a className="button secondary" href={quoteDocumentUrl(q.id)} target="_blank" rel="noopener">
            Printable quote
          </a>
          {(staff || ownDraft) && ["draft", "pending_approval", "approved"].includes(q.status) && open && (
            <Link className="button secondary" to={`${base}/quotes/${q.id}/edit`}>
              Change
            </Link>
          )}
          {staff && q.status === "draft" && q.needs_approval && open && (
            <button className="button" disabled={act.busy} onClick={() => run(() => quoteAction(q.id, "submit"), "Sent for approval.")}>
              Ask for approval
            </button>
          )}
          {(staff || ownDraft) && manager && open && (q.status === "approved" || (q.status === "draft" && !q.needs_approval)) && (
            <button className="button" disabled={act.busy} onClick={() => run(() => quoteAction(q.id, "send"), staff ? `Sent to ${q.customer}.` : "Ready to accept.")}>
              {staff ? "Send to the organisation" : "Finish quote"}
            </button>
          )}
          {(staff || manager) && (
            <button
              className="button secondary"
              disabled={act.busy}
              onClick={() =>
                act.run(async () => {
                  const c = await quoteAction(q.id, "copy");
                  navigate(`${base}/quotes/${c.id}`);
                })
              }
            >
              Copy at today's prices
            </button>
          )}
          {(staff || ownDraft) && ["draft", "pending_approval", "approved", "sent"].includes(q.status) && (
            <button className="button secondary" disabled={act.busy} onClick={() => confirm(`Withdraw ${q.number}?`) && run(() => quoteAction(q.id, "cancel"), "Withdrawn.")}>
              Withdraw
            </button>
          )}
        </div>
      </section>

      {staff && q.status === "pending_approval" && (
        <Card title="Approve the discounts">
          <p className="muted small">Some discounts are above what the items allow. Say why you approve or refuse; it goes on record.</p>
          <div className="form">
            <label className="wide">
              Reason
              <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} placeholder="Launch customer, two sites" />
            </label>
            <div className="actions">
              <button className="button" disabled={act.busy || !note.trim()} onClick={() => run(() => approveQuote(q.id, true, note), "Approved. Send it when ready.")}>
                Approve
              </button>
              <button className="button secondary" disabled={act.busy || !note.trim()} onClick={() => run(() => approveQuote(q.id, false, note), "Refused; back to draft.")}>
                Refuse
              </button>
            </div>
          </div>
        </Card>
      )}

      {manager && q.status === "sent" && open && (
        <Card title={staff ? "Accept for the organisation" : "Accept this quote"}>
          <p className="muted small">
            {staff
              ? "Only when the organisation has agreed in writing. "
              : "Accepting places the order at these prices for the full term. "}
            Type the full name of the person accepting.
          </p>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              act.run(async () => {
                const r = await acceptQuote(q.id, name);
                navigate(`${base}/orders/${r.order.id}`);
              });
            }}
          >
            <label>
              Full name
              <input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} maxLength={120} autoComplete="name" />
            </label>
            <div className="actions">
              <button className="button" disabled={act.busy || name.trim().length < 2}>
                Accept and order
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={act.busy}
                onClick={() => {
                  const reason = prompt("Why are you declining? (optional)") ?? null;
                  if (reason !== null) run(() => declineQuote(q.id, reason), "Declined.");
                }}
              >
                Decline
              </button>
            </div>
          </form>
        </Card>
      )}
    </>
  );
}

function LinesTable({ lines, currency }: { lines: QuoteLine[]; currency: string }) {
  return (
    <div className="table-wrap">
      <table className="paths dt stack bill-lines">
        <thead>
          <tr>
            <th scope="col">Item</th>
            <th scope="col">Charged</th>
            <th scope="col" className="num">
              List ({currency})
            </th>
            <th scope="col" className="num">
              Discount
            </th>
            <th scope="col" className="num">
              Price ({currency})
            </th>
          </tr>
        </thead>
        <tbody>
          {lines.map((l, i) => (
            <tr key={l.id ?? i}>
              <td>
                {l.description}
                <div className="muted small">{l.price_text}</div>
                {l.needs_approval && <span className="pill warn small">Above the {Number(l.max_discount_pct)}% limit</span>}
              </td>
              <td data-label="Charged">{l.charge_type === "usage" ? "By use (estimate)" : CHARGED[l.charge_type]}</td>
              <td className="num mono" data-label="List">
                {amount(l.list_amount)}
              </td>
              <td className="num mono" data-label="Discount">
                {[Number(l.term_discount_pct) > 0 && `${Number(l.term_discount_pct)}% term`, Number(l.discount_pct) > 0 && `${Number(l.discount_pct)}%`]
                  .filter(Boolean)
                  .join(", ") || "–"}
              </td>
              <td className="num mono" data-label="Price">
                {amount(l.amount)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- orders ----------------------------------------------------------------------------------

function OrderList() {
  const { staff, base } = useRoles();
  const { current } = useCustomer();
  const [all, setAll] = useState(staff);
  const cid = current?.id;
  const path = staff && all ? commercialPaths.orders : cid ? `${commercialPaths.orders}?customer_id=${cid}` : null;
  const orders = useApi<{ orders: OrderRow[] }>(path, 30_000);
  const rows = orders.data?.orders ?? [];
  return (
    <>
      <Head title="Orders">
        What you ordered and how far it has got. <Link to="/quotes">Quotes</Link>.
      </Head>
      <ErrorNote error={orders.error} />
      <Card
        title="Orders"
        note={
          staff ? (
            <div className="segmented" role="group" aria-label="Which organisations">
              <button aria-pressed={all} onClick={() => setAll(true)}>
                Everyone
              </button>
              <button aria-pressed={!all} onClick={() => setAll(false)}>
                {current?.name ?? "This organisation"}
              </button>
            </div>
          ) : undefined
        }
      >
        {orders.data && rows.length === 0 && <EmptyState title="No orders yet">An order is placed when a quote is accepted.</EmptyState>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Order</th>
                  {staff && <th scope="col">Organisation</th>}
                  <th scope="col">State</th>
                  <th scope="col">Progress</th>
                  <th scope="col">Placed</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((o) => (
                  <tr key={o.id}>
                    <td>
                      <Link to={`${base}/orders/${o.id}`}>{o.number}</Link>
                      {o.quote_number && <div className="muted small">from {o.quote_number}</div>}
                    </td>
                    {staff && <td data-label="Organisation">{o.customer}</td>}
                    <td data-label="State">
                      <span className={`pill ${ORDER_PILL[o.status]}`}>{o.status_label}</span>
                    </td>
                    <td data-label="Progress" className="small">
                      {o.done_count} of {o.item_count} done
                      {o.failed_count > 0 && <span className="pill bad small"> {o.failed_count} failed</span>}
                      {o.waiting_count > 0 && <div className="muted">{o.waiting_count} waiting for ExaCarib</div>}
                    </td>
                    <td data-label="Placed" className="nowrap">
                      {day(o.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

function OrderPage() {
  const { id = "" } = useParams();
  const { staff, base } = useRoles();
  const order = useApi<SalesOrder>(commercialPaths.order(id), 15_000);
  const act = useAction();
  const [fix, setFix] = useState<{ id: string; carrier: string; location: string } | null>(null);
  const o = order.data;
  if (!o) return <ErrorNote error={order.error} />;
  const run = (fn: () => Promise<unknown>) =>
    act.run(async () => {
      await fn();
      order.reload();
    });
  const open = o.items.some((i) => i.status === "pending" || i.status === "failed");
  return (
    <>
      <Head title={`Order ${o.number}`} />
      <ErrorNote error={act.error} />
      <section className="card" style={{ marginBottom: 24 }}>
        <div className="bill-doc-head">
          <div>
            <h2 style={{ margin: 0 }}>{o.number}</h2>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              For {o.customer}, placed {day(o.created_at)}
              {o.quote && (
                <>
                  {" "}
                  from <Link to={`${base}/quotes/${o.quote.id}`}>{o.quote.number}</Link>
                </>
              )}
              .
            </p>
          </div>
          <div className="bill-doc-meta">
            <span className={`pill ${ORDER_PILL[o.status]}`}>{o.status_label}</span>
            {o.completed_at && <div className="small muted">Completed {day(o.completed_at)}</div>}
          </div>
        </div>
        <p className="muted small">Each item's charges start on the day it is done.</p>
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">How</th>
                <th scope="col">State</th>
                {staff && (
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {o.items.map((i) => (
                <tr key={i.id}>
                  <td>
                    {i.description}
                    <div className="muted small">
                      {CHARGED[i.charge_type]}, {o.currency} {amount(i.amount)}
                    </div>
                  </td>
                  <td data-label="How" className="small">
                    {i.automatic ? "Set up automatically" : "Done by ExaCarib staff"}
                  </td>
                  <td data-label="State">
                    <span className={`pill ${ITEM_PILL[i.status]}`}>{ITEM_WORD[i.status]}</span>
                    {i.error && <div className="small">{i.error}</div>}
                    {i.status === "done" && <ResultNote r={i.result} />}
                  </td>
                  {staff && (
                    <td className="actions">
                      {(i.status === "pending" || i.status === "failed") && (
                        <RowActions
                          label={i.name}
                          items={[
                            ...(i.automatic
                              ? [
                                  {
                                    label: i.fulfilment === "link" ? "Fix details and try again" : "Try again",
                                    onSelect: () =>
                                      i.fulfilment === "link"
                                        ? setFix({ id: i.id, carrier: String(i.config.carrier ?? ""), location: String(i.config.location ?? "") })
                                        : run(() => orderItemRetry(i.id)),
                                  },
                                ]
                              : []),
                            {
                              label: "Mark done",
                              onSelect: () => {
                                const note = prompt(i.automatic ? "Done by hand? Say what was done." : "Note (for example the tracking number)", "");
                                if (note !== null) run(() => orderItemDone(i.id, note));
                              },
                            },
                            {
                              label: "Cancel item",
                              danger: true,
                              onSelect: () => {
                                const why = prompt(`Why cancel ${i.name}?`);
                                if (why) run(() => orderItemCancel(i.id, why));
                              },
                            },
                          ]}
                        />
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {fix && (
          <form
            className="form card-inset"
            style={{ marginTop: 16 }}
            onSubmit={(e) => {
              e.preventDefault();
              const item = o.items.find((x) => x.id === fix.id);
              run(() => orderItemRetry(fix.id, { ...item?.config, carrier: fix.carrier, location: fix.location }));
              setFix(null);
            }}
          >
            <h3 className="wide">Link details</h3>
            <label>
              Location
              <input value={fix.location} onChange={(e) => setFix({ ...fix, location: e.target.value })} />
            </label>
            <label>
              Carrier
              <input value={fix.carrier} onChange={(e) => setFix({ ...fix, carrier: e.target.value })} required />
            </label>
            <div className="actions">
              <button className="button" disabled={act.busy}>
                Try again
              </button>
              <button type="button" className="button secondary" onClick={() => setFix(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
        {staff && open && (
          <div className="form-actions">
            <button
              className="button secondary"
              disabled={act.busy}
              onClick={() => {
                const why = prompt(`Why cancel what's left of ${o.number}?`);
                if (why) run(() => cancelSalesOrder(o.id, why));
              }}
            >
              Cancel what's left
            </button>
          </div>
        )}
      </section>
    </>
  );
}

function ResultNote({ r }: { r: Record<string, unknown> }) {
  const sites = r.sites as { site: string; location: string; existing?: boolean }[] | undefined;
  if (sites?.length) return <div className="muted small">{sites.map((s) => `${s.location} (${s.site})`).join(", ")}</div>;
  if (r.site && r.path) return <div className="muted small">{`${r.site}, ${r.path}`}</div>;
  if (r.note) return <div className="muted small">{String(r.note)}</div>;
  return null;
}

// ---- contract (on the Billing page) ----------------------------------------------------------

/** What the organisation has bought through quotes: monthly items, one-offs and usage this month. */
export function ContractCard({ cid }: { cid: string }) {
  const { staff } = useRoles();
  const c = useApi<Contract>(commercialPaths.contract(cid), 60_000);
  const act = useAction();
  const d = c.data;
  if (!d) return <ErrorNote error={c.error} />;
  const active = d.items.filter((i) => i.status === "active");
  if (!active.length && !d.one_off_charges.length) return null;
  return (
    <Card title="What you've bought" note={<Link to={staff ? "/admin/orders" : "/orders"}>Orders</Link>}>
      <ErrorNote error={act.error} />
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">Item</th>
              <th scope="col">From</th>
              <th scope="col">Term ends</th>
              <th scope="col" className="num">
                A month
              </th>
              {staff && (
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {active.map((i) => (
              <tr key={i.id} className={Number(i.monthly_amount) < 0 ? "bill-credit" : undefined}>
                <td>
                  {i.description} <span className="muted small">{PRODUCT_NAMES[i.product]}</span>
                  {i.price_text && <div className="muted small">{i.price_text}</div>}
                </td>
                <td data-label="From" className="nowrap">
                  {day(i.starts_on)}
                </td>
                <td data-label="Term ends" className="nowrap">
                  {day(i.term_ends_on)}
                </td>
                <td className="num mono" data-label="A month">
                  {i.charge_type === "usage" ? "By use" : `${i.currency} ${amount(i.monthly_amount)}`}
                </td>
                {staff && (
                  <td className="actions">
                    <RowActions
                      label={i.description}
                      items={[
                        {
                          label: "End today",
                          danger: true,
                          onSelect: () =>
                            confirm(`Stop charging ${i.description} from today?`) &&
                            act.run(async () => {
                              await endContractItem(cid, i.id);
                              c.reload();
                            }),
                        },
                      ]}
                    />
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {d.usage.length > 0 && (
        <>
          <h3>Usage this month</h3>
          <ul className="small">
            {d.usage.map((u) => (
              <li key={u.contract_item_id}>
                {u.description}: {qty(u.quantity)} {u.unit} ({qty(u.included)} included), {amount(u.amount_so_far)} so far. <span className="muted">{u.price_text}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {d.one_off_charges.length > 0 && (
        <>
          <h3>One-off charges</h3>
          <ul className="small">
            {d.one_off_charges.slice(0, 10).map((o) => (
              <li key={o.id}>
                {day(o.charge_on)}: {o.description}, {o.currency} {amount(o.amount)}
                {o.order_number && <span className="muted"> ({o.order_number})</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

// ---- catalogue (ExaCarib staff) ---------------------------------------------------------------

export function CatalogueAdmin() {
  const items = useApi<{ items: CatalogueItem[] }>(`${commercialPaths.items}?include_inactive=true`, 0);
  const act = useAction();
  const [msg, setMsg] = useState<string | null>(null);
  const [pricing, setPricing] = useState<CatalogueItem | null>(null);
  const rows = items.data?.items ?? [];
  const toggle = (i: CatalogueItem) =>
    act.run(async () => {
      await updateCatalogueItem(i.code, { active: !i.active });
      setMsg(i.active ? `${i.name} is no longer sold.` : `${i.name} is sold again.`);
      items.reload();
    });
  return (
    <>
      {msg && (
        <p className="pill ok" role="status">
          {msg}
        </p>
      )}
      <ErrorNote error={items.error ?? act.error} />
      <Card title="Catalogue" note={rows.some((r) => r.example) ? <ExampleTag /> : undefined}>
        <p className="muted small">
          Plans, sites and link commits take their price from each plan's price list (Billing). Everything else is priced here; a new
          price is a new version, and quotes keep the price they were made with.
        </p>
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">App</th>
                <th scope="col">Charged</th>
                <th scope="col">Price</th>
                <th scope="col" className="num">
                  Max discount
                </th>
                <th scope="col">Sold</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id}>
                  <td>
                    {i.name} {i.example && <ExampleTag />}
                    <div className="muted small mono">{i.code}</div>
                  </td>
                  <td data-label="App">{PRODUCT_NAMES[i.product]}</td>
                  <td data-label="Charged">
                    {CHARGED[i.charge_type]}
                    <div className="muted small">{i.fulfilment === "manual" ? "Staff mark done" : "Automatic"}</div>
                  </td>
                  <td data-label="Price" className="small">
                    {i.price_text}
                    {i.price?.version && <span className="muted"> (v{i.price.version})</span>}
                  </td>
                  <td className="num mono" data-label="Max discount">
                    {Number(i.max_discount_pct)}%
                  </td>
                  <td data-label="Sold">
                    <span className={`pill ${i.active ? "ok" : "off"}`}>{i.active ? "Yes" : "No"}</span>
                  </td>
                  <td className="actions">
                    <RowActions
                      label={i.name}
                      items={[
                        ...(i.plan_priced ? [] : [{ label: "New price", onSelect: () => setPricing(i) }]),
                        { label: i.active ? "Stop selling" : "Sell again", onSelect: () => toggle(i), danger: i.active },
                      ]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {pricing && (
          <PriceForm
            item={pricing}
            onCancel={() => setPricing(null)}
            onDone={(m) => {
              setPricing(null);
              setMsg(m);
              items.reload();
            }}
          />
        )}
        <NewItemForm
          onDone={(m) => {
            setMsg(m);
            items.reload();
          }}
        />
      </Card>
    </>
  );
}

interface PriceDraft {
  model: PriceModel;
  unit_price: string;
  tiers: string;
  included_qty: string;
  minimum: string;
  term_discounts: string;
  effective_from: string;
}

const blankPrice = (): PriceDraft => ({
  model: "per_unit",
  unit_price: "",
  tiers: "",
  included_qty: "0",
  minimum: "0",
  term_discounts: "",
  effective_from: new Date().toISOString().slice(0, 10),
});

/** "10:25, 50:20, :15" → bands; "12:5, 36:10" → term discounts. */
function parsePrice(d: PriceDraft) {
  const pairs = (s: string) =>
    s
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)
      .map((x) => x.split(":").map((y) => y.trim()));
  return {
    model: d.model,
    unit_price: d.unit_price || "0",
    tiers: d.model === "tiered" || d.model === "volume" ? pairs(d.tiers).map(([up, p]) => ({ up_to: up ? up : null, unit_price: p })) : [],
    included_qty: d.included_qty || "0",
    minimum: d.minimum || "0",
    term_discounts: pairs(d.term_discounts).map(([m, p]) => ({ min_months: Number(m), pct: p })),
    effective_from: d.effective_from,
  };
}

function PriceFields({ d, set, recurring }: { d: PriceDraft; set: (p: Partial<PriceDraft>) => void; recurring: boolean }) {
  const banded = d.model === "tiered" || d.model === "volume";
  return (
    <>
      <label>
        Pricing
        <select value={d.model} onChange={(e) => set({ model: e.target.value as PriceModel })}>
          <option value="per_unit">Per unit</option>
          <option value="flat">Flat</option>
          <option value="tiered">Tiered (each band at its price)</option>
          <option value="volume">Volume (all at the band reached)</option>
        </select>
      </label>
      {banded ? (
        <label className="wide">
          Bands: up to:price, last one open (for example 10:25, 50:20, :15)
          <input value={d.tiers} onChange={(e) => set({ tiers: e.target.value })} required pattern="[0-9.: ,]*" />
        </label>
      ) : (
        <label>
          Price
          <input type="number" min={0} step="any" value={d.unit_price} onChange={(e) => set({ unit_price: e.target.value })} required />
        </label>
      )}
      <label>
        Included free
        <input type="number" min={0} step="any" value={d.included_qty} onChange={(e) => set({ included_qty: e.target.value })} />
      </label>
      <label>
        Minimum charge
        <input type="number" min={0} step="any" value={d.minimum} onChange={(e) => set({ minimum: e.target.value })} />
      </label>
      {recurring && (
        <label>
          Term discounts, months:% (for example 12:5, 36:10)
          <input value={d.term_discounts} onChange={(e) => set({ term_discounts: e.target.value })} pattern="[0-9.: ,]*" />
        </label>
      )}
      <label>
        From
        <input type="date" value={d.effective_from} onChange={(e) => set({ effective_from: e.target.value })} required />
      </label>
    </>
  );
}

function PriceForm({ item, onDone, onCancel }: { item: CatalogueItem; onDone: (m: string) => void; onCancel: () => void }) {
  const [d, setD] = useState<PriceDraft>(blankPrice());
  const act = useAction();
  return (
    <form
      className="form card-inset"
      style={{ marginTop: 16 }}
      onSubmit={(e) => {
        e.preventDefault();
        act.run(async () => {
          const p = await addCataloguePrice(item.code, { ...parsePrice(d), currency: item.price?.currency ?? "USD" });
          onDone(`${item.name}: price version ${p.version} from ${day(p.effective_from)}.`);
        });
      }}
    >
      <h3 className="wide">New price for {item.name}</h3>
      <ErrorNote error={act.error} />
      <PriceFields d={d} set={(p) => setD({ ...d, ...p })} recurring={item.charge_type === "recurring"} />
      <div className="actions">
        <button className="button" disabled={act.busy}>
          Save price
        </button>
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function NewItemForm({ onDone }: { onDone: (m: string) => void }) {
  const [f, setF] = useState({
    code: "",
    name: "",
    description: "",
    product: "connect" as Product,
    charge_type: "recurring" as ChargeType,
    fulfilment: "immediate" as "immediate" | "manual",
    unit: "each",
    meter: "",
    max_discount_pct: "10",
  });
  const [p, setP] = useState<PriceDraft>(blankPrice());
  const act = useAction();
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const item = await createCatalogueItem({
        ...f,
        code: f.code.trim().toLowerCase(),
        meter: f.charge_type === "usage" ? f.meter.trim() : undefined,
        max_discount_pct: Number(f.max_discount_pct) || 0,
        price: parsePrice(p),
      });
      onDone(`Added ${item.name}.`);
      setF({ ...f, code: "", name: "", description: "", meter: "" });
      setP(blankPrice());
    });
  };
  return (
    <form className="form card-inset" onSubmit={add} style={{ marginTop: 16 }}>
      <h3 className="wide">New item</h3>
      <ErrorNote error={act.error} />
      <label>
        App
        <select value={f.product} onChange={(e) => setF({ ...f, product: e.target.value as Product })}>
          <option value="connect">Connect</option>
          <option value="commai">Jibsy</option>
        </select>
      </label>
      <label>
        Code
        <input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} required pattern="[a-z0-9][a-z0-9._\-]{1,47}" placeholder="connect.firewall" />
      </label>
      <label>
        Name
        <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required maxLength={120} placeholder="Managed firewall" />
      </label>
      <label>
        Charged
        <select value={f.charge_type} onChange={(e) => setF({ ...f, charge_type: e.target.value as ChargeType, fulfilment: e.target.value === "one_off" ? "manual" : "immediate" })}>
          <option value="recurring">Every month</option>
          <option value="one_off">Once</option>
          <option value="usage">By use</option>
        </select>
      </label>
      <label>
        Unit
        <input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} required maxLength={30} placeholder="site, seat, hour" />
      </label>
      {f.charge_type === "usage" && (
        <label>
          Meter (usage arrives on it)
          <input value={f.meter} onChange={(e) => setF({ ...f, meter: e.target.value })} required pattern="[a-z0-9][a-z0-9._:\-]{0,63}" placeholder="engineer_hours" />
        </label>
      )}
      <label>
        Set up
        <select value={f.fulfilment} onChange={(e) => setF({ ...f, fulfilment: e.target.value as "immediate" | "manual" })}>
          <option value="immediate">Starts when ordered</option>
          <option value="manual">Staff mark it done</option>
        </select>
      </label>
      <label>
        Max discount without approval (%)
        <input type="number" min={0} max={100} value={f.max_discount_pct} onChange={(e) => setF({ ...f, max_discount_pct: e.target.value })} />
      </label>
      <label className="wide">
        Description
        <input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} maxLength={500} />
      </label>
      <PriceFields d={p} set={(x) => setP({ ...p, ...x })} recurring={f.charge_type === "recurring"} />
      <div className="actions">
        <button className="button" disabled={act.busy}>
          Add item
        </button>
      </div>
    </form>
  );
}
