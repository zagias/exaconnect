import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { useCustomer } from "../../../customer";
import { Card, PageHead, useAction } from "../../../ui";
import { when } from "../lib";
import { contrast } from "./brand";
import "../automation.css";
import "./partner.css";

/* Shapes from controller/exaconnect_controller/commai/api/partners.py and developer.py (ADR 0025). */

interface Me {
  partner: { id: string; name: string; kind: string } | null;
  role: "admin" | "member" | null;
  acting_for: { customer_id: string; customer_name: string; partner_name: string; scopes: string[] } | null;
}
interface Health {
  state: "ok" | "warn" | "bad";
  open: number;
  unassigned: number;
  overdue: number;
  failed_jobs_24h: number;
  broken_channels: number;
}
interface Linked {
  id: string;
  customer_id: string;
  customer_name: string;
  status: "pending" | "active" | "declined" | "revoked";
  requested_scopes: string[];
  scopes: string[];
  markup_pct: string | null;
  health: Health | null;
  usage: { example_total: number; example_total_with_markup: number; markup_pct: number } | null;
}
interface BrandRow {
  id: string;
  product_name: string;
  colour: string;
  ink: string;
  support_email: string;
  logo_url: string | null;
}
interface Domain {
  id: string;
  purpose: "portal" | "widget";
  domain: string;
  status: "pending" | "verified" | "failed";
  last_error: string;
  dns: { type: string; name: string; value: string };
  cname: { name: string; points_to: string };
}
interface Link {
  id: string;
  partner_name: string;
  partner_kind: string;
  status: Linked["status"];
  requested_scopes: string[];
  scopes: string[];
  note: string;
  requested_at: string;
}
interface Client {
  client_id: string;
  name: string;
  redirect_uris: string[];
  scopes: string[];
  confidential: boolean;
  active_grants: number;
}
interface Grant {
  id: string;
  app: string;
  partner_name: string;
  scopes: string[];
  approved_by: string;
  created_at: string;
}

export const SCOPE_WORDS: Record<string, string> = {
  connect: "Connect network (sites, paths, routing)",
  "commai:read": "Read contacts, conversations and messages",
  "commai:write": "Reply to customers and change contacts and conversations",
  "commai:notes": "Read and write private notes",
  "commai:admin": "Change settings, channels, webhooks, integrations and voice",
};
const SCOPES = Object.keys(SCOPE_WORDS);
const HEALTH: Record<Health["state"], [string, string]> = { ok: ["ok", "Healthy"], warn: ["warn", "Needs attention"], bad: ["bad", "Problem"] };
const STATUS: Record<Linked["status"], [string, string]> = {
  pending: ["warn", "Waiting for the business"],
  active: ["ok", "Active"],
  declined: ["off", "Declined"],
  revoked: ["off", "Ended"],
};

async function switchTo(customerId: string) {
  await api("/commai/partners/switch", { method: "POST", body: JSON.stringify({ customer_id: customerId }) });
  window.location.assign("/commai");
}

/** Partners (ADR 0025): a partner's businesses, brand, apps and statements; for a business, its partner and apps. */
export default function Partner() {
  const me = useApi<Me>("/commai/partners/me", 0);
  const act = useAction();
  if (!me.data) return <ErrorNote error={me.error} />;
  const m = me.data;
  if (m.acting_for)
    return (
      <>
        <PageHead title={`Acting for ${m.acting_for.customer_name}`}>
          You are working in this business for {m.acting_for.partner_name}, with only what it granted.
        </PageHead>
        <Card title="What you can do here">
          <ul className="partner-scopes">
            {m.acting_for.scopes.map((s) => (
              <li key={s}>{SCOPE_WORDS[s] ?? s}</li>
            ))}
          </ul>
          <button
            className="button"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                await api("/commai/partners/switch-back", { method: "POST" });
                window.location.assign("/commai/partner");
              })
            }
          >
            Back to {m.acting_for.partner_name}
          </button>
          <ErrorNote error={act.error} />
        </Card>
      </>
    );
  if (m.partner) return <PartnerHome partner={m.partner} admin={m.role === "admin"} />;
  return <BusinessSide />;
}

/* ---------------------------------------------------------------- the partner's view */

function PartnerHome({ partner, admin }: { partner: NonNullable<Me["partner"]>; admin: boolean }) {
  const base = `/commai/partners/${partner.id}`;
  const list = useApi<Linked[]>(`${base}/customers`, 60_000);
  const act = useAction();
  return (
    <>
      <PageHead eyebrow="Partner" title={partner.name}>
        The businesses you look after. Switch into one to work in it with the permissions it granted you.
      </PageHead>
      <Card title="Your businesses" note={<span className="tag">Example prices</span>}>
        <ErrorNote error={list.error ?? act.error} />
        {list.data && list.data.length === 0 && <div className="empty">No businesses yet. Ask one to accept a link.</div>}
        {list.data && list.data.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Business</th>
                  <th scope="col">Link</th>
                  <th scope="col">Health</th>
                  <th scope="col">Open</th>
                  <th scope="col">Usage this month</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((l) => (
                  <tr key={l.id}>
                    <td>{l.customer_name}</td>
                    <td data-label="Link">
                      <span className={`pill ${STATUS[l.status][0]}`}>{STATUS[l.status][1]}</span>
                    </td>
                    <td data-label="Health">{l.health ? <span className={`pill ${HEALTH[l.health.state][0]}`}>{HEALTH[l.health.state][1]}</span> : "–"}</td>
                    <td data-label="Open" className="mono">
                      {l.health ? `${l.health.open} (${l.health.unassigned} unassigned)` : "–"}
                    </td>
                    <td data-label="Usage" className="mono">
                      {l.usage ? `${l.usage.example_total.toFixed(2)} → ${l.usage.example_total_with_markup.toFixed(2)} (+${l.usage.markup_pct}%)` : "–"}
                    </td>
                    <td>
                      {l.status === "active" && (
                        <button className="button secondary" disabled={act.busy} onClick={() => act.run(() => switchTo(l.customer_id))}>
                          Switch to
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {admin && <RequestLink base={base} reload={list.reload} />}
      <BrandCard brandUrl={`${base}/branding`} canEdit={admin} />
      {admin && <Statements base={base} />}
      <Apps base={base} admin={admin} />
    </>
  );
}

function ScopePicker({ value, onChange, allowed = SCOPES }: { value: string[]; onChange: (v: string[]) => void; allowed?: string[] }) {
  return (
    <fieldset className="partner-scopes">
      <legend>Permissions</legend>
      {allowed.map((s) => (
        <label key={s} className="check">
          <input type="checkbox" checked={value.includes(s)} onChange={(e) => onChange(e.target.checked ? [...value, s] : value.filter((x) => x !== s))} />
          {SCOPE_WORDS[s] ?? s}
        </label>
      ))}
    </fieldset>
  );
}

function RequestLink({ base, reload }: { base: string; reload: () => void }) {
  const [customer, setCustomer] = useState("");
  const [scopes, setScopes] = useState<string[]>(["commai:read", "commai:write"]);
  const [note, setNote] = useState("");
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/links`, { method: "POST", body: JSON.stringify({ customer_id: customer.trim(), scopes, note }) });
      setCustomer("");
      reload();
    });
  };
  return (
    <Card title="Ask to manage a business">
      <p className="muted small">The business gives you its id from CommAI. It then accepts in its own portal and chooses what you may do.</p>
      <form className="form" onSubmit={submit}>
        <label>
          Business id
          <input value={customer} onChange={(e) => setCustomer(e.target.value)} required maxLength={64} className="mono" />
        </label>
        <ScopePicker value={scopes} onChange={setScopes} />
        <label>
          Note to the business
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy || scopes.length === 0}>
            Send request
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Statements({ base }: { base: string }) {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const rows = useApi<{ customer_name: string; period: string; base_amount: string; markup_pct: string; total_amount: string; currency: string }[]>(
    `${base}/statements?month=${month}`,
    0,
  );
  const act = useAction();
  return (
    <Card title="Statements" note={<span className="tag">Example prices</span>}>
      <p className="muted small">Usage at example prices plus your markup, recorded for billing. No payment is taken here.</p>
      <div className="form-row">
        <label>
          Month
          <input type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
        </label>
        <button
          className="button"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              await api(`${base}/statements`, { method: "POST", body: JSON.stringify({ month }) });
              rows.reload();
            })
          }
        >
          Record this month
        </button>
      </div>
      <ErrorNote error={act.error ?? rows.error} />
      {rows.data && rows.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Business</th>
                <th scope="col">Usage</th>
                <th scope="col">Markup</th>
                <th scope="col">Total</th>
              </tr>
            </thead>
            <tbody>
              {rows.data.map((r) => (
                <tr key={r.customer_name}>
                  <td>{r.customer_name}</td>
                  <td data-label="Usage" className="mono">
                    {Number(r.base_amount).toFixed(2)} {r.currency}
                  </td>
                  <td data-label="Markup" className="mono">
                    {Number(r.markup_pct)}%
                  </td>
                  <td data-label="Total" className="mono">
                    {Number(r.total_amount).toFixed(2)} {r.currency}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/* ---------------------------------------------------------------- branding and domains */

export function BrandCard({ brandUrl, canEdit }: { brandUrl: string; canEdit: boolean }) {
  const brand = useApi<BrandRow | { own: BrandRow | null } | null>(brandUrl, 0);
  const current: BrandRow | null = brand.data && "own" in brand.data ? brand.data.own : (brand.data as BrandRow | null);
  const [form, setForm] = useState<{ product_name: string; colour: string; ink: string; support_email: string } | null>(null);
  const f = form ?? {
    product_name: current?.product_name ?? "",
    colour: current?.colour ?? "#155EEF",
    ink: current?.ink ?? "#10213D",
    support_email: current?.support_email ?? "",
  };
  const act = useAction();
  const ok = (c: string) => /^#[0-9a-fA-F]{6}$/.test(c) && contrast(c, "#FFFFFF") >= 4.5;
  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(brandUrl, { method: "PUT", body: JSON.stringify(f) });
      setForm(null);
      brand.reload();
    });
  };
  const upload = (file: File | undefined) =>
    file &&
    current &&
    act.run(async () => {
      const r = await fetch(`/api/v1/commai/brands/${current.id}/logo`, {
        method: "PUT",
        body: file,
        headers: { "Content-Type": file.type, "X-Requested-With": "exa-portal" },
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `The controller answered ${r.status}.`);
      brand.reload();
    });
  return (
    <Card title="Branding">
      <p className="muted small">Your people and the businesses you manage see this name and colour in the portal and the chat widget.</p>
      <form className="form" onSubmit={save}>
        <label>
          Product name
          <input value={f.product_name} onChange={(e) => setForm({ ...f, product_name: e.target.value })} required maxLength={60} disabled={!canEdit} />
        </label>
        {(["colour", "ink"] as const).map((k) => (
          <label key={k}>
            {k === "colour" ? "Brand colour (buttons and links)" : "Text colour"}
            <span className="partner-colour">
              <input type="color" value={ok(f[k]) || /^#[0-9a-fA-F]{6}$/.test(f[k]) ? f[k] : "#155EEF"} onChange={(e) => setForm({ ...f, [k]: e.target.value.toUpperCase() })} disabled={!canEdit} aria-label={`${k} picker`} />
              <input className="mono" value={f[k]} onChange={(e) => setForm({ ...f, [k]: e.target.value })} maxLength={7} disabled={!canEdit} />
              {/^#[0-9a-fA-F]{6}$/.test(f[k]) && (
                <span className={`pill ${ok(f[k]) ? "ok" : "bad"}`}>
                  {contrast(f[k], "#FFFFFF").toFixed(1)}:1 on white {ok(f[k]) ? "" : "(needs 4.5:1)"}
                </span>
              )}
            </span>
          </label>
        ))}
        <label>
          Support email
          <input type="email" value={f.support_email} onChange={(e) => setForm({ ...f, support_email: e.target.value })} maxLength={255} disabled={!canEdit} />
        </label>
        {canEdit && (
          <div className="actions">
            <button className="button" disabled={act.busy || !ok(f.colour) || !ok(f.ink)}>
              Save branding
            </button>
          </div>
        )}
      </form>
      {current && (
        <div className="partner-logo">
          {current.logo_url ? <img src={current.logo_url} alt={`${current.product_name} logo`} /> : <span className="muted small">No logo yet.</span>}
          {canEdit && (
            <label className="button secondary">
              Upload logo (PNG, JPEG or WebP, 256 KB at most)
              <input type="file" className="sr-only" accept="image/png,image/jpeg,image/webp" onChange={(e) => upload(e.target.files?.[0])} />
            </label>
          )}
        </div>
      )}
      <ErrorNote error={act.error ?? brand.error} />
      {current && <Domains brandId={current.id} canEdit={canEdit} />}
    </Card>
  );
}

function Domains({ brandId, canEdit }: { brandId: string; canEdit: boolean }) {
  const url = `/commai/brands/${brandId}/domains`;
  const list = useApi<Domain[]>(url, 0);
  const [domain, setDomain] = useState("");
  const [purpose, setPurpose] = useState<"portal" | "widget">("portal");
  const act = useAction();
  return (
    <div className="partner-domains">
      <h3>Custom domains</h3>
      <p className="muted small">
        Add the TXT record shown at your DNS host, then verify. A certificate is issued only after the domain is verified. Point the domain at ExaCarib with a CNAME.
      </p>
      {list.data?.map((d) => (
        <div key={d.id} className="partner-domain">
          <div>
            <strong>{d.domain}</strong> <span className="muted small">({d.purpose === "portal" ? "portal" : "chat widget"})</span>{" "}
            <span className={`pill ${d.status === "verified" ? "ok" : d.status === "failed" ? "bad" : "warn"}`}>{d.status === "verified" ? "Verified" : d.status === "failed" ? "Failed" : "Waiting for DNS"}</span>
          </div>
          {d.status !== "verified" && (
            <dl className="auto-kv small">
              <dt>TXT name</dt>
              <dd className="mono">{d.dns.name}</dd>
              <dt>TXT value</dt>
              <dd className="mono">{d.dns.value}</dd>
              <dt>CNAME</dt>
              <dd className="mono">
                {d.cname.name} → {d.cname.points_to}
              </dd>
            </dl>
          )}
          {d.last_error && <p className="small muted">{d.last_error}</p>}
          {canEdit && d.status !== "verified" && (
            <button className="button secondary" disabled={act.busy} onClick={() => act.run(async () => void (await api(`${url}/${d.id}/verify`, { method: "POST" }), list.reload()))}>
              Verify now
            </button>
          )}
        </div>
      ))}
      {canEdit && (
        <form
          className="form-row"
          onSubmit={(e) => {
            e.preventDefault();
            act.run(async () => {
              await api(url, { method: "POST", body: JSON.stringify({ domain, purpose }) });
              setDomain("");
              list.reload();
            });
          }}
        >
          <label>
            Domain
            <input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="portal.example.com" required maxLength={253} />
          </label>
          <label>
            For
            <select value={purpose} onChange={(e) => setPurpose(e.target.value as "portal" | "widget")}>
              <option value="portal">Portal</option>
              <option value="widget">Chat widget</option>
            </select>
          </label>
          <button className="button" disabled={act.busy}>
            Add domain
          </button>
        </form>
      )}
      <ErrorNote error={act.error ?? list.error} />
    </div>
  );
}

/* ---------------------------------------------------------------- OAuth apps */

function Apps({ base, admin }: { base: string; admin: boolean }) {
  const list = useApi<Client[]>(`${base}/oauth-clients`, 0);
  const [name, setName] = useState("");
  const [redirect, setRedirect] = useState("");
  const [scopes, setScopes] = useState<string[]>(["commai:read"]);
  const [confidential, setConfidential] = useState(false);
  const [shown, setShown] = useState<{ client_id: string; client_secret: string | null } | null>(null);
  const act = useAction();
  return (
    <Card title="Apps (OAuth)">
      <p className="muted small">
        Your apps ask a business for access with OAuth 2.0 (authorisation code with PKCE, S256). The business approves on its own consent page and can revoke at any time.
      </p>
      {list.data && list.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">App</th>
                <th scope="col">Client id</th>
                <th scope="col">Scopes</th>
                <th scope="col">Businesses</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((c) => (
                <tr key={c.client_id}>
                  <td>{c.name}</td>
                  <td data-label="Client id" className="mono">
                    {c.client_id}
                  </td>
                  <td data-label="Scopes" className="mono">
                    {c.scopes.join(" ")}
                  </td>
                  <td data-label="Businesses" className="mono">
                    {c.active_grants}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {shown && (
        <p className="pill ok" role="status">
          Client id <span className="mono">{shown.client_id}</span>
          {shown.client_secret && (
            <>
              {" "}
              and secret <span className="mono">{shown.client_secret}</span>. Copy the secret now: it is not shown again.
            </>
          )}
        </p>
      )}
      {admin && (
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            act.run(async () => {
              const out = await api<{ client_id: string; client_secret: string | null }>(`${base}/oauth-clients`, {
                method: "POST",
                body: JSON.stringify({ name, redirect_uris: [redirect], scopes, confidential }),
              });
              setShown(out);
              setName("");
              list.reload();
            });
          }}
        >
          <label>
            App name
            <input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} maxLength={100} />
          </label>
          <label>
            Redirect URI
            <input value={redirect} onChange={(e) => setRedirect(e.target.value)} required placeholder="https://app.example.com/callback" />
          </label>
          <ScopePicker value={scopes} onChange={setScopes} />
          <label className="check">
            <input type="checkbox" checked={confidential} onChange={(e) => setConfidential(e.target.checked)} />
            Runs on a server and can keep a secret
          </label>
          <div className="actions">
            <button className="button" disabled={act.busy || scopes.length === 0}>
              Register app
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={act.error ?? list.error} />
    </Card>
  );
}

/* ---------------------------------------------------------------- the business's view */

function BusinessSide() {
  const { current } = useCustomer();
  if (!current) return <p className="muted">Choose an organisation first.</p>;
  const base = `/commai/customers/${current.id}`;
  return (
    <>
      <PageHead title="Partners and apps">
        Who may manage {current.name || "this business"}, which apps can reach it, and its sandbox for developers.
      </PageHead>
      <Card title="Your business id">
        <p className="small">
          Give this to a partner that asks to manage your business: <span className="mono">{current.id}</span>
        </p>
      </Card>
      <PartnerLinks base={base} />
      <Grants base={base} />
      <Sandbox base={base} />
      <DataLocation base={base} />
    </>
  );
}

function PartnerLinks({ base }: { base: string }) {
  const list = useApi<Link[]>(`${base}/partner-links`, 0);
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const act = useAction();
  const call = (path: string, init: RequestInit) => act.run(async () => void (await api(path, init), list.reload()));
  return (
    <Card title="Partners">
      {list.data?.length === 0 && <div className="empty">No partner has asked to manage this business.</div>}
      {list.data?.map((l) => {
        const sc = picked[l.id] ?? (l.status === "active" ? l.scopes : l.requested_scopes);
        return (
          <div key={l.id} className="partner-link">
            <div>
              <strong>{l.partner_name}</strong> <span className="muted small">({l.partner_kind === "msp" ? "managed-service provider" : "reseller"})</span>{" "}
              <span className={`pill ${STATUS[l.status][0]}`}>{l.status === "pending" ? "Asking to manage this business" : STATUS[l.status][1]}</span>
            </div>
            {l.note && <p className="small">“{l.note}”</p>}
            {(l.status === "pending" || l.status === "active") && (
              <>
                <ScopePicker value={sc} onChange={(v) => setPicked({ ...picked, [l.id]: v })} allowed={l.requested_scopes} />
                <div className="actions">
                  {l.status === "pending" ? (
                    <>
                      <button className="button" disabled={act.busy || sc.length === 0} onClick={() => call(`${base}/partner-links/${l.id}/accept`, { method: "POST", body: JSON.stringify({ scopes: sc }) })}>
                        Accept
                      </button>
                      <button className="button secondary" disabled={act.busy} onClick={() => call(`${base}/partner-links/${l.id}/decline`, { method: "POST" })}>
                        Decline
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="button secondary" disabled={act.busy || sc.length === 0} onClick={() => call(`${base}/partner-links/${l.id}/scopes`, { method: "PUT", body: JSON.stringify({ scopes: sc }) })}>
                        Save permissions
                      </button>
                      <button className="button danger" disabled={act.busy} onClick={() => confirm(`End ${l.partner_name}'s access now?`) && call(`${base}/partner-links/${l.id}`, { method: "DELETE" })}>
                        Revoke access
                      </button>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        );
      })}
      <ErrorNote error={act.error ?? list.error} />
    </Card>
  );
}

function Grants({ base }: { base: string }) {
  const list = useApi<Grant[]>(`${base}/oauth-grants`, 0);
  const act = useAction();
  return (
    <Card title="Connected apps">
      {list.data?.length === 0 && <div className="empty">No apps have access.</div>}
      {list.data?.map((g) => (
        <div key={g.id} className="partner-link">
          <div>
            <strong>{g.app}</strong> <span className="muted small">by {g.partner_name}, approved by {g.approved_by} {when(g.created_at)}</span>
          </div>
          <p className="small mono">{g.scopes.join(" ")}</p>
          <button className="button danger" disabled={act.busy} onClick={() => act.run(async () => void (await api(`${base}/oauth-grants/${g.id}`, { method: "DELETE" }), list.reload()))}>
            Revoke
          </button>
        </div>
      ))}
      <ErrorNote error={act.error ?? list.error} />
    </Card>
  );
}

function Sandbox({ base }: { base: string }) {
  const sb = useApi<{ sandbox: { id: string; name: string } | null; keys: { id: number; name: string; prefix: string; scopes: string[]; created_at: string }[] }>(`${base}/sandbox`, 0);
  const [token, setToken] = useState<string | null>(null);
  const act = useAction();
  return (
    <Card title="Sandbox">
      <p className="muted small">A copy of this business for developers. Every channel is simulated: nothing is sent to real people and no numbers are ordered.</p>
      {sb.data && !sb.data.sandbox && (
        <button className="button" disabled={act.busy} onClick={() => act.run(async () => void (await api(`${base}/sandbox`, { method: "POST" }), sb.reload()))}>
          Make a sandbox
        </button>
      )}
      {sb.data?.sandbox && (
        <>
          <p className="small">
            Sandbox id <span className="mono">{sb.data.sandbox.id}</span>. Use it in API paths with a sandbox key.
          </p>
          {sb.data.keys.map((k) => (
            <div key={k.id} className="form-row">
              <span className="mono">{k.prefix}…</span> {k.name}
              <button className="button secondary" disabled={act.busy} onClick={() => act.run(async () => void (await api(`${base}/sandbox/keys/${k.id}`, { method: "DELETE" }), sb.reload()))}>
                Revoke
              </button>
            </div>
          ))}
          {token && (
            <p className="pill ok" role="status">
              New sandbox key: <span className="mono">{token}</span>. Copy it now: it is not shown again.
            </p>
          )}
          <button
            className="button"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                const out = await api<{ token: string }>(`${base}/sandbox/keys`, { method: "POST", body: JSON.stringify({ name: "Sandbox key" }) });
                setToken(out.token);
                sb.reload();
              })
            }
          >
            New sandbox key
          </button>
        </>
      )}
      <ErrorNote error={act.error ?? sb.error} />
    </Card>
  );
}

function DataLocation({ base }: { base: string }) {
  const loc = useApi<{ region_name: string; location: string; items: { dependency: string; label: string; provider: string | null; provider_region: string | null; source: string }[] }>(
    `${base}/data-location`,
    0,
  );
  if (!loc.data) return <ErrorNote error={loc.error} />;
  return (
    <Card title="Where your data is kept">
      <p className="small">
        Home region: <strong>{loc.data.region_name}</strong> ({loc.data.location})
      </p>
      <dl className="auto-kv small">
        {loc.data.items.map((i) => (
          <div key={i.dependency} style={{ display: "contents" }}>
            <dt>{i.label}</dt>
            <dd>
              {i.provider ?? "Not recorded"} {i.provider_region ? `· ${i.provider_region}` : ""}{" "}
              <span className="muted">({i.source === "recorded" ? "recorded by ExaCarib" : i.source === "configuration" ? "from the server's set-up" : "not recorded"})</span>
            </dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}
