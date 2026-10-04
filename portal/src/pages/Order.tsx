import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  PSK_HINT,
  PSK_PATTERN,
  cancelOrder,
  circuitPaths,
  confirmOrder,
  createOrder,
  draftOrder,
  num,
  orderPaths,
  useApi,
  type Circuit,
  type Order,
  type OrderAction,
  type OrderEngine,
  type OrderNeed,
  type OrderStatus,
  type Partner,
  type PartnerCategory,
  type StormSite,
} from "../api";
import { ErrorNote, ExampleTag, Eyebrow } from "../components";
import { useCustomer, who } from "../customer";
import { Card, useAction } from "../ui";

// Partner directory and plain-English ordering (ADR 0011, docs/ordering-contract.md).
// A person describes what they need, or picks a partner from the directory; the
// controller drafts an order, and nothing changes until the person confirms it.

export const ORDER_STATUS: Record<OrderStatus, { cls: string; word: string }> = {
  done: { cls: "ok", word: "Done" },
  pending_partner: { cls: "warn", word: "Waiting for partner" },
  draft: { cls: "shadow", word: "Draft" },
  cancelled: { cls: "shadow", word: "Cancelled" },
  failed: { cls: "bad", word: "Failed" },
};

export function OrderStatusPill({ status }: { status: OrderStatus }) {
  const s = ORDER_STATUS[status] ?? { cls: "shadow", word: status };
  return <span className={`pill ${s.cls}`}>{s.word}</span>;
}

const ENGINE: Record<OrderEngine, string> = {
  ai: "Drafted by AI: check it carefully",
  rules: "Drafted by rules",
  form: "Drafted from your choices",
};

export const CATEGORY_LABEL: Record<PartnerCategory, string> = {
  cloud: "Cloud",
  saas: "Software",
  payments: "Payments",
  internet: "Internet",
  security: "Security",
  content: "Content",
  other: "Other",
};

const CATEGORIES: { value: PartnerCategory | ""; label: string }[] = [
  { value: "", label: "All" },
  ...(Object.keys(CATEGORY_LABEL) as PartnerCategory[]).map((c) => ({ value: c, label: CATEGORY_LABEL[c] })),
];

export const usd = (v: unknown) => {
  const n = num(v);
  return n === null ? "–" : `US$ ${n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–";

export const split = (s: string) =>
  s
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

/** "Adds about US$ 100.00 a month", "Saves about …", or nothing when there's no estimate. */
function estimateText(v: unknown): string | null {
  const n = num(v);
  if (n === null) return null;
  if (Math.abs(n) < 0.005) return "No change to your monthly charge";
  return `${n > 0 ? "Adds" : "Saves"} about ${usd(Math.abs(n))} a month`;
}

/** A web address safe to link to. */
export const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : null);

/** The partner a partner_connection order refers to, if any. */
export function partnerOf(o: Order): string | null {
  for (const a of o.actions) if (a.action === "partner_connection") return a.partner;
  return null;
}

export default function OrderPage() {
  const { current } = useCustomer();
  if (!current) return null;
  // Keyed so a different customer starts with a clean draft.
  return <OrderScreen key={current.id} customerId={current.id} customerName={current.name} sites={current.sites} />;
}

function OrderScreen({ customerId, customerName, sites }: { customerId: string; customerName: string; sites: StormSite[] }) {
  const orders = useApi<Order[]>(orderPaths.list(customerId), 10_000);
  const [draft, setDraft] = useState<Order | null>(null);
  const top = useRef<HTMLDivElement>(null);
  const show = (o: Order) => {
    setDraft(o);
    orders.reload();
    requestAnimationFrame(() => top.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  return (
    <>
      <div className="page-head">
        <Eyebrow>Order</Eyebrow>
        <h1>Order connections</h1>
        <p className="muted">
          Ask for circuits, bandwidth changes and internet access in plain English, or connect to a partner from the
          directory. We draft the order for you to check; nothing changes until you confirm it.
        </p>
      </div>
      <div ref={top} style={{ scrollMarginTop: 16 }}>
        <Describe
          customerId={customerId}
          sites={sites}
          draft={draft}
          onDraft={(o) => {
            setDraft(o);
            orders.reload();
          }}
          onUpdate={(o) => {
            setDraft(o);
            orders.reload();
          }}
          onClose={() => setDraft(null)}
        />
      </div>
      <Directory customerId={customerId} sites={sites} onDrafted={show} />
      <Orders customerName={customerName} orders={orders.data} error={orders.error} onOpen={show} />
    </>
  );
}

// ---- (a) Tell us what you need ----

/** Example phrasings, using this customer's own site and circuit names where there are some. */
function examples(sites: StormSite[], circuits: string[]) {
  const a = sites[0]?.name ?? "Kingston";
  const b = sites[1]?.name ?? "Montego Bay";
  const c = circuits[0] ?? "Amazon Web Services us-east-1";
  return [
    { label: "A cloud circuit", text: `Connect ${a} to our Azure VNet in East US at 100 Mbps for 10.200.0.0/16` },
    { label: "A site-to-site VLAN", text: `Join ${a} VLAN 100 to ${b} VLAN 200 at 20 Mbps` },
    { label: "A bandwidth change", text: `Raise ${c} to 100 Mbps` },
    { label: "Internet straight out", text: `Send ${b}'s internet straight out at the site` },
  ];
}

function Describe({
  customerId,
  sites,
  draft,
  onDraft,
  onUpdate,
  onClose,
}: {
  customerId: string;
  sites: StormSite[];
  draft: Order | null;
  onDraft: (o: Order) => void;
  onUpdate: (o: Order) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [rulesOnly, setRulesOnly] = useState(false);
  const circuits = useApi<Circuit[]>(circuitPaths.list(customerId), 0);
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    act.run(async () => {
      onDraft(await draftOrder(customerId, text.trim(), rulesOnly ? "rules" : "auto"));
    });
  };
  return (
    <Card title="Tell us what you need">
      <form className="form" onSubmit={submit}>
        <label className="wide">
          What would you like to change?
          <textarea
            className="order-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            minLength={3}
            maxLength={500}
            rows={3}
            placeholder="Connect Kingston to our AWS VPC in us-east-1 at 50 Mbps for 10.100.0.0/16"
          />
        </label>
        <div className="wide">
          <div className="small muted" style={{ marginBottom: 4 }}>
            For example (click to use):
          </div>
          <div className="suggestions">
            {examples(sites, (circuits.data ?? []).map((c) => c.name)).map((x) => (
              <button key={x.label} type="button" className="button secondary small" title={x.text} onClick={() => setText(x.text)}>
                {x.label}
              </button>
            ))}
          </div>
        </div>
        <label className="check wide">
          <input type="checkbox" checked={rulesOnly} onChange={(e) => setRulesOnly(e.target.checked)} /> Draft with rules only, without
          AI
        </label>
        <div className="actions wide">
          <button className="button" disabled={act.busy || text.trim().length < 3}>
            {act.busy ? "Drafting…" : "Draft the order"}
          </button>
          <span className="small muted">Drafting changes nothing.</span>
        </div>
      </form>
      <ErrorNote error={act.error} />
      {draft && <DraftView key={draft.id} customerId={customerId} order={draft} onUpdate={onUpdate} onClose={onClose} />}
    </Card>
  );
}

const needKey = (n: OrderNeed) => `${n.action}.${n.field}`;
const isOptional = (n: OrderNeed) => n.optional === true || n.field === "inside_cidr";
const NUMERIC = /(^|_)(asn|vlan|mbps)$/;

/** One inputs object per action, `{}` where none are needed. Blank optional values go as null. */
function buildInputs(order: Order, values: Record<string, string>): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = order.actions.map(() => ({}));
  for (const n of order.needs) {
    const target = out[n.action];
    if (!target) continue;
    const raw = (values[needKey(n)] ?? "").trim();
    let v: unknown = raw === "" ? null : raw;
    if (raw && (typeof n.default === "number" || NUMERIC.test(n.field)) && /^\d+$/.test(raw)) v = Number(raw);
    target[n.field] = v;
  }
  return out;
}

function DraftView({
  customerId,
  order,
  onUpdate,
  onClose,
}: {
  customerId: string;
  order: Order;
  onUpdate: (o: Order) => void;
  onClose: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(order.needs.map((n) => [needKey(n), n.default == null ? "" : String(n.default)])),
  );
  const act = useAction();
  const isDraft = order.status === "draft";
  const missing = order.needs.some((n) => !isOptional(n) && !(values[needKey(n)] ?? "").trim());
  const blocked = order.problems.length > 0 || order.actions.length === 0;
  const estimate = estimateText(order.monthly_estimate);
  const grouped = order.actions.length > 1;

  const confirm = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const done = await confirmOrder(customerId, order.id, buildInputs(order, values));
      // Secrets go straight to the circuit; don't keep them in the page.
      setValues((v) => Object.fromEntries(Object.entries(v).map(([k, x]) => [k, order.needs.find((n) => needKey(n) === k)?.secret ? "" : x])));
      onUpdate(done);
    });
  };
  const cancel = () =>
    act.run(async () => {
      onUpdate(await cancelOrder(customerId, order.id));
    });

  const needInput = (n: OrderNeed) => (
    <label key={needKey(n)}>
      {n.label}
      {isOptional(n) && <span className="muted"> (optional)</span>}
      <input
        type={n.secret ? "password" : "text"}
        autoComplete="off"
        value={values[needKey(n)] ?? ""}
        onChange={(e) => setValues({ ...values, [needKey(n)]: e.target.value })}
        required={!isOptional(n)}
        inputMode={NUMERIC.test(n.field) || typeof n.default === "number" ? "numeric" : undefined}
        pattern={n.field === "psk" ? PSK_PATTERN : undefined}
        title={n.field === "psk" ? PSK_HINT : undefined}
        disabled={!isDraft}
      />
    </label>
  );

  return (
    <div className="card-inset draft">
      <div className="draft-head">
        <h3>Order {order.id}</h3>
        {!isDraft && <OrderStatusPill status={order.status} />}
        <span className={order.engine === "ai" ? "pill warn" : "small muted"}>{ENGINE[order.engine] ?? `Drafted by ${order.engine}`}</span>
      </div>
      {order.text && <p className="small muted quote">“{order.text}”</p>}

      {order.summary.length > 0 && (isDraft || !(order.results ?? []).length) && (
        <>
          <div className="small muted">{isDraft ? "If you confirm, we will:" : "What it does:"}</div>
          <ul className="lines">
            {order.summary.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </>
      )}
      {order.problems.length > 0 && (
        <ul className="problems">
          {order.problems.map((p, i) => (
            <li key={i}>
              <span className="pill warn">Can't do yet:</span> {p}
            </li>
          ))}
        </ul>
      )}
      {estimate && <p className="estimate">{estimate}</p>}

      {isDraft ? (
        <form className="form" onSubmit={confirm}>
          {grouped
            ? order.actions.map((_, i) => {
                const needs = order.needs.filter((n) => n.action === i);
                if (needs.length === 0) return null;
                return (
                  <fieldset key={i} className="wide">
                    <legend>{order.summary.length === order.actions.length ? order.summary[i] : `Item ${i + 1}`}</legend>
                    <div className="form">{needs.map(needInput)}</div>
                  </fieldset>
                );
              })
            : order.needs.map(needInput)}
          <p className="small wide" style={{ margin: 0 }}>
            <strong>Nothing changes until you press Confirm.</strong>
            {blocked && order.actions.length > 0 ? " Sort out what we can't do yet first: edit your request and draft it again." : ""}
          </p>
          <div className="actions wide">
            <button className="button" disabled={act.busy || blocked || missing}>
              {act.busy ? "Working…" : "Confirm"}
            </button>
            <button type="button" className="button secondary" disabled={act.busy} onClick={cancel}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <OutcomeNote order={order} />
          <Results order={order} />
          <div className="actions" style={{ marginTop: 12 }}>
            <button type="button" className="button secondary small" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function OutcomeNote({ order }: { order: Order }) {
  const text: Partial<Record<OrderStatus, string>> = {
    done: "Done. Your sites pick up the change within 10 seconds.",
    pending_partner: "Confirmed. The partner's side is set up next; this order shows Waiting for partner until it is ready.",
    cancelled: "Cancelled. Nothing was changed.",
  };
  const t = text[order.status];
  if (!t) return null;
  return (
    <p className="ok-note small" role="status" style={{ margin: "8px 0" }}>
      {t}
    </p>
  );
}

function Results({ order }: { order: Order }) {
  const results = order.results ?? [];
  if (results.length === 0) return null;
  return (
    <ul className="results">
      {results.map((r, i) => (
        <li key={i}>
          {r.pending ? (
            <span className="pill warn">Waiting for partner</span>
          ) : (
            <span className={`pill ${r.ok ? "ok" : "bad"}`}>{r.ok ? "Done" : "Failed"}</span>
          )}{" "}
          {r.message}
          {r.circuit_id != null && (
            <>
              {" "}
              <Link to="/fabric">See it in Fabric</Link>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

// ---- (b) Partner directory ----

function Directory({ customerId, sites, onDrafted }: { customerId: string; sites: StormSite[]; onDrafted: (o: Order) => void }) {
  const [category, setCategory] = useState<PartnerCategory | "">("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);
  const list = useApi<Partner[]>(orderPaths.partners(category, query), 0);
  const [connecting, setConnecting] = useState<number | null>(null);
  const partners = list.data ?? [];
  return (
    <Card title="Partner directory">
      <p className="small muted" style={{ marginTop: 0 }}>
        Clouds and services you can reach privately through ExaCarib's PoP. Pick one and we draft the order for you to
        confirm.
      </p>
      <div className="filters">
        <div className="chips" role="group" aria-label="Category">
          {CATEGORIES.map((c) => (
            <button key={c.value || "all"} type="button" aria-pressed={category === c.value} onClick={() => setCategory(c.value)}>
              {c.label}
            </button>
          ))}
        </div>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search partners" aria-label="Search partners" />
      </div>
      <ErrorNote error={list.error} />
      {list.data && partners.length === 0 ? (
        <p className="muted">No partners match. Try another category or search.</p>
      ) : (
        <div className="partner-grid">
          {partners.map((p) => (
            <PartnerCard
              key={p.id}
              p={p}
              customerId={customerId}
              sites={sites}
              open={connecting === p.id}
              onToggle={() => setConnecting(connecting === p.id ? null : p.id)}
              onDrafted={(o) => {
                setConnecting(null);
                onDrafted(o);
              }}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

function PartnerCard({
  p,
  customerId,
  sites,
  open,
  onToggle,
  onDrafted,
}: {
  p: Partner;
  customerId: string;
  sites: StormSite[];
  open: boolean;
  onToggle: () => void;
  onDrafted: (o: Order) => void;
}) {
  const site = safeUrl(p.website);
  return (
    <article className="card-inset partner">
      <div className="partner-head">
        <h3>{p.name}</h3>
        {p.example && <ExampleTag />}
      </div>
      <div className="small muted">
        {CATEGORY_LABEL[p.category] ?? p.category} ·{" "}
        {p.kind === "cloud" ? "You set up the VPN in your cloud console" : "The partner's side is set up for you"}
      </div>
      {p.description && <p className="small">{p.description}</p>}
      {p.regions.length > 0 && (
        <div className="small">
          <span className="muted">Regions: </span>
          {p.regions.join(", ")}
        </div>
      )}
      {p.kind === "service" && p.prefixes.length > 0 && (
        <div className="small">
          <span className="muted">Reach: </span>
          <span className="mono wrap">{p.prefixes.join(", ")}</span>
        </div>
      )}
      <div className="small">
        <span className="mono">{usd(p.price_per_mbps_month)}</span> per Mbps a month
      </div>
      <div className="form-actions" style={{ alignItems: "center", marginTop: "auto", paddingTop: 4 }}>
        <button className="button small" aria-expanded={open} onClick={onToggle}>
          Connect
        </button>
        {site && (
          <a href={site} target="_blank" rel="noopener noreferrer" className="small">
            Website
          </a>
        )}
      </div>
      {open && <ConnectForm p={p} customerId={customerId} sites={sites} onDrafted={onDrafted} onCancel={onToggle} />}
    </article>
  );
}

function ConnectForm({
  p,
  customerId,
  sites,
  onDrafted,
  onCancel,
}: {
  p: Partner;
  customerId: string;
  sites: StormSite[];
  onDrafted: (o: Order) => void;
  onCancel: () => void;
}) {
  const cloud = p.kind === "cloud";
  const [f, setF] = useState({ site: "", bandwidth: cloud ? "50" : "10", region: p.regions[0] ?? "", prefixes: "" });
  const act = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const action: OrderAction = {
        action: "partner_connection",
        partner: p.slug,
        site: f.site || null,
        bandwidth_mbps: Number(f.bandwidth),
        ...(cloud ? { region: f.region.trim() || null, cloud_prefixes: split(f.prefixes) } : {}),
      };
      onDrafted(await createOrder(customerId, [action]));
    });
  };
  return (
    <form className="form" onSubmit={submit} style={{ marginTop: 8 }}>
      <label>
        From
        <select value={f.site} onChange={set("site")}>
          <option value="">All sites</option>
          {sites.map((s) => (
            <option key={s.id} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Bandwidth (Mbps)
        <input type="number" min={1} max={1000} step={1} required value={f.bandwidth} onChange={set("bandwidth")} />
      </label>
      {cloud && (
        <>
          <label>
            Region
            {p.regions.length > 0 ? (
              <select value={f.region} onChange={set("region")}>
                {p.regions.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            ) : (
              <input value={f.region} onChange={set("region")} placeholder="us-east-1" />
            )}
          </label>
          <label className="wide">
            Cloud subnets
            <input value={f.prefixes} onChange={set("prefixes")} placeholder="10.100.0.0/16, 10.101.0.0/16" />
          </label>
        </>
      )}
      <p className="small muted wide" style={{ margin: 0 }}>
        This drafts the order at the top of the page for you to check. Nothing changes yet.
      </p>
      <div className="actions wide">
        <button className="button small" disabled={act.busy}>
          Draft the order
        </button>
        <button type="button" className="button secondary small" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

// ---- (c) Your orders ----

const SHOW = 10;

function Orders({
  customerName,
  orders,
  error,
  onOpen,
}: {
  customerName: string;
  orders: Order[] | null;
  error: string | null;
  onOpen: (o: Order) => void;
}) {
  const [all, setAll] = useState(false);
  const list = orders ?? [];
  const shown = all ? list : list.slice(0, SHOW);
  return (
    <Card title="Your orders" note={<span className="small muted">{customerName}, newest first</span>}>
      <ErrorNote error={error} />
      {orders && list.length === 0 ? (
        <p className="muted">No orders yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="paths">
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Status</th>
                <th scope="col">By</th>
                <th scope="col">Results</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((o) => (
                <tr key={o.id}>
                  <td style={{ minWidth: 220 }}>
                    <div className="small muted">Order {o.id}</div>
                    {o.text ? <div>{o.text}</div> : null}
                    {o.summary.length > 0 && !(o.results ?? []).length && (
                      <div className={o.text ? "small muted" : undefined}>{o.summary.join(" ")}</div>
                    )}
                    {!o.text && o.summary.length === 0 && <span className="muted">–</span>}
                  </td>
                  <td>
                    <OrderStatusPill status={o.status} />
                  </td>
                  <td className="small">
                    <div>{who(o.created_by)}</div>
                    <div className="muted">{when(o.created_at)}</div>
                    {o.confirmed_by && (
                      <div className="muted">
                        Confirmed by {who(o.confirmed_by)}, {when(o.confirmed_at)}
                      </div>
                    )}
                  </td>
                  <td className="small" style={{ minWidth: 200 }}>
                    {(o.results ?? []).length > 0 ? <Results order={o} /> : <span className="muted">–</span>}
                  </td>
                  <td>
                    {o.status === "draft" && (
                      <button className="button secondary small" onClick={() => onOpen(o)}>
                        Open
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {list.length > SHOW && (
        <button className="link small" onClick={() => setAll(!all)} style={{ marginTop: 8 }}>
          {all ? "Show fewer" : `Show all ${list.length}`}
        </button>
      )}
    </Card>
  );
}
