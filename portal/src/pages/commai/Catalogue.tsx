import { useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { AppsTabs } from "./Integrations";
import { useCommaiBase, when } from "./lib";
import "./automation.css";
import "./catalogue.css";

/* Shapes from controller/exaconnect_controller/commai/api/integrations_catalogue.py and
   automation/catalogue.py (ADR 0034). The page is drawn from the connector registry:
   a new connector appears here with no change to this file. */

interface ActionInfo {
  name: string;
  label: string;
  kind: string;
  sensitive: boolean;
}

interface CatalogueApp {
  app: string;
  label: string;
  description: string;
  auth: string;
  simulated: boolean;
  sign_in_ready: boolean;
  status: string;
  golive_key: string;
  webhooks: string;
  has_simulator: boolean;
  actions: ActionInfo[];
  read_actions: string[];
  write_actions: string[];
  needs: { env: string[]; app_registration: string; reason: string };
  connection: { status: string; auth_method: string } | null;
}

interface Catalogue {
  categories: { id: string; label: string; apps: CatalogueApp[] }[];
  standards: { id: string; name: string; spec: string; what: string }[];
  via_standard: { name: string; category: string; via: string; definition?: string; events_in?: string; events_out?: string }[];
}

interface Operation {
  id: string;
  method: string;
  path: string;
  summary: string;
  kind: string;
  deprecated: boolean;
}

interface RestAction {
  operation: string;
  name: string;
  label: string;
  sensitive: boolean;
  mapping: Record<string, string>;
}

interface RestApp {
  id: string;
  app: string;
  name: string;
  base_url: string;
  status: "draft" | "approved";
  version: number;
  actions: RestAction[];
  auth: { type?: string; client_secret_set?: boolean };
  operation_count: number;
  approved_by: string;
  operations?: Operation[];
  security_schemes?: { scheme: string; type: string }[];
}

interface InboundHook {
  id: string;
  kind: string;
  name: string;
  event_type: string;
  active: boolean;
  url: string;
  received_count: number;
  rejected_count: number;
  last_received_at: string | null;
  secret?: string;
}

interface Endpoint {
  id: string;
  url: string;
}

interface ImportResult {
  rows: number;
  created: number;
  updated: number;
  skipped: number;
  problems: string[];
}

const STATUS_WORD: Record<string, string> = {
  "not connected": "Not connected",
  draft: "Draft",
  authorised: "Signed in",
  testing: "Testing",
  live: "Live",
  paused: "Paused",
  broken: "Needs repair",
};

const AUTH_WORD: Record<string, string> = {
  oauth: "Sign-in page (OAuth)",
  credentials: "Secure entry",
  token: "Token",
  none: "No sign-in",
};

export default function CatalogueScreen() {
  const base = useCommaiBase();
  const { data, error } = useApi<Catalogue>(base && `${base}/integration-catalogue`, 60_000);
  return (
    <>
      <PageHead title="Apps">
        Every app Jibsy can work with, what it may do there, and what ExaCarib still has to set up before it can go
        live. Apps not yet live run on a stand-in with example data, so you can try them safely.
      </PageHead>
      <AppsTabs />
      {!base && <p className="muted">Choose an organisation first.</p>}
      <ErrorNote error={error} />
      {data && (
        <nav className="cat-jump" aria-label="Categories">
          {data.categories.map((c) => (
            <a key={c.id} href={`#cat-${c.id}`}>
              {c.label} <span className="cat-count">{c.apps.length}</span>
            </a>
          ))}
          <a href="#standards">Standards</a>
          <a href="#own-api">Your own API</a>
          <a href="#hooks">Inbound webhooks</a>
          <a href="#exchange">Data exchange</a>
        </nav>
      )}
      {data?.categories.map((c) => (
        <section key={c.id} id={`cat-${c.id}`} className="cat-section" aria-labelledby={`cat-h-${c.id}`}>
          <h2 id={`cat-h-${c.id}`}>{c.label}</h2>
          <div className="auto-grid">
            {c.apps.map((a) => (
              <AppCard key={a.app} a={a} />
            ))}
          </div>
        </section>
      ))}
      {data && <Standards data={data} />}
      {base && <OwnApis base={base} />}
      {base && <InboundHooks base={base} />}
      {base && <WebhookFormat base={base} />}
      {base && <Exchange base={base} />}
    </>
  );
}

function AppCard({ a }: { a: CatalogueApp }) {
  const reads = a.actions.filter((x) => x.kind === "read");
  const writes = a.actions.filter((x) => x.kind !== "read");
  const needs = a.needs;
  const live = a.status === "live" && !a.simulated;
  return (
    <section className="card auto-app" aria-labelledby={`capp-${a.app}`}>
      <div className="card-head" style={{ marginBottom: 0 }}>
        <h3 id={`capp-${a.app}`}>{a.label}</h3>
        <span className={`pill ${live ? "ok" : a.status === "broken" ? "bad" : "warn"}`}>
          {STATUS_WORD[a.status] ?? a.status}
        </span>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        {a.description}
      </p>
      <p className="small" style={{ margin: 0 }}>
        {AUTH_WORD[a.auth] ?? a.auth}
        {a.simulated && (
          <>
            {" · "}
            <span className="tag">Example data</span> on a stand-in
          </>
        )}
      </p>
      {reads.length > 0 && <ActionList title="Reads" items={reads} />}
      {writes.length > 0 && <ActionList title="Changes" items={writes} />}
      {a.webhooks && <p className="small muted" style={{ margin: 0 }}>Tells Jibsy about changes: {a.webhooks}</p>}
      {!a.sign_in_ready && (needs.reason || needs.env.length > 0 || needs.app_registration) && (
        <details className="cat-needs">
          <summary>What ExaCarib still needs</summary>
          {needs.app_registration && <p className="small">{needs.app_registration}</p>}
          {needs.env.length > 0 && (
            <p className="small">
              Server settings:{" "}
              {needs.env.map((e, i) => (
                <span key={e}>
                  {i > 0 && ", "}
                  <code>{e}</code>
                </span>
              ))}
            </p>
          )}
          {needs.reason && <p className="small muted">{needs.reason}</p>}
          {a.golive_key && (
            <p className="small muted">
              Go-live check: <code>{a.golive_key}</code>
            </p>
          )}
        </details>
      )}
      <div>
        <Link className="button small" to={`/commai/integrations/${a.app}`}>
          {a.connection ? "Open" : "Set up"}
        </Link>
      </div>
    </section>
  );
}

function ActionList({ title, items }: { title: string; items: ActionInfo[] }) {
  return (
    <div>
      <div className="cat-label">{title}</div>
      <ul className="auto-actions-list" aria-label={title}>
        {items.map((x) => (
          <li key={x.name}>
            <span className={`auto-kind ${x.kind === "read" ? "" : "write"} ${x.sensitive ? "sensitive" : ""}`}>
              {x.label}
              {x.kind !== "read" ? ` · ${x.kind}` : ""}
              {x.sensitive ? " · needs approval" : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Standards({ data }: { data: Catalogue }) {
  return (
    <section id="standards" className="cat-section">
      <Card title="Standards Jibsy speaks">
        <p className="small muted">
          Any system that speaks one of these connects without a ready-made app. Machine-readable descriptions:{" "}
          <a href="/api/v1/commai/openapi.json">OpenAPI</a> and <a href="/api/v1/commai/asyncapi.json">AsyncAPI</a>.
        </p>
        <dl className="auto-kv">
          {data.standards.map((s) => (
            <div key={s.id} style={{ display: "contents" }}>
              <dt>
                <strong>{s.name}</strong>
                <br />
                <span className="small">{s.spec}</span>
              </dt>
              <dd>{s.what}</dd>
            </div>
          ))}
        </dl>
        <h3 className="cat-sub">Popular apps reached through a standard</h3>
        <ul className="cat-via">
          {data.via_standard.map((v) => (
            <li key={v.name}>
              {v.name} <span className="muted small">via {data.standards.find((s) => s.id === v.via)?.name ?? v.via}</span>
              {v.definition && (
                <span className="muted small">
                  {" "}
                  · Jibsy app definition <code>{v.definition}</code>, events in through <a href="#hooks">inbound webhooks</a>
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </section>
  );
}

// ---- your own REST API -------------------------------------------------------------------

function OwnApis({ base }: { base: string }) {
  const list = useApi<RestApp[]>(`${base}/rest-apps`, 0);
  const [name, setName] = useState("");
  const [doc, setDoc] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const act = useAction();
  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) void f.text().then(setDoc);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const r = await api<RestApp>(`${base}/rest-apps`, { method: "POST", body: JSON.stringify({ name, document: doc }) });
      setName("");
      setDoc("");
      setOpen(r.id);
      list.reload();
    });
  };
  return (
    <section id="own-api" className="cat-section">
      <Card title="Your own API (OpenAPI)">
        <p className="small muted">
          Paste or upload your API's OpenAPI 3 document. Jibsy lists its operations; you choose which become actions
          and how fields map, and a person approves. Nothing in the document runs, and Jibsy only calls operations it
          describes.
        </p>
        <ErrorNote error={list.error ?? act.error} />
        {(list.data ?? []).map((r) => (
          <div key={r.id} className="cat-rest">
            <div className="auto-row" style={{ justifyContent: "space-between" }}>
              <strong>{r.name}</strong>
              <span className={`pill ${r.status === "approved" ? "ok" : "warn"}`}>
                {r.status === "approved" ? `Approved (v${r.version})` : `Draft (v${r.version})`}
              </span>
            </div>
            <p className="small muted" style={{ margin: 0 }}>
              {r.base_url} · {r.actions.length} of {r.operation_count} operations chosen · sign-in {r.auth.type ?? "not set"}
            </p>
            <button className="button small secondary" type="button" onClick={() => setOpen(open === r.id ? null : r.id)}>
              {open === r.id ? "Close" : "Choose actions"}
            </button>
            {open === r.id && <RestEditor base={base} id={r.id} onSaved={list.reload} />}
          </div>
        ))}
        <form onSubmit={submit} className="auto-fields" style={{ marginTop: 16 }}>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
          </label>
          <label>
            OpenAPI file
            <input type="file" accept=".json,.yaml,.yml,application/json" onChange={onFile} />
          </label>
          <label className="wide">
            Or paste the document
            <textarea className="auto-text" value={doc} onChange={(e) => setDoc(e.target.value)} required />
          </label>
          <div className="wide">
            <button className="button" disabled={act.busy || !name || !doc}>
              Import
            </button>
          </div>
        </form>
      </Card>
    </section>
  );
}

function RestEditor({ base, id, onSaved }: { base: string; id: string; onSaved: () => void }) {
  const app = useApi<RestApp>(`${base}/rest-apps/${id}`, 0);
  const [chosen, setChosen] = useState<Record<string, RestAction> | null>(null);
  const [authType, setAuthType] = useState<string | null>(null);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const act = useAction();
  const r = app.data;
  if (!r) return <ErrorNote error={app.error} />;
  const current: Record<string, RestAction> =
    chosen ?? Object.fromEntries(r.actions.map((a) => [a.operation, a]));
  const auth = authType ?? r.auth.type ?? "none";
  const toggle = (op: Operation) => {
    const next = { ...current };
    if (next[op.id]) delete next[op.id];
    else next[op.id] = { operation: op.id, name: op.id, label: op.summary, sensitive: op.kind === "delete", mapping: {} };
    setChosen(next);
  };
  const suggest = () =>
    act.run(async () => {
      const d = await api<{ actions: RestAction[] }>(`${base}/rest-apps/${id}/draft`, { method: "POST" });
      setChosen(Object.fromEntries(d.actions.map((a) => [a.operation, a])));
    });
  const save = () =>
    act.run(async () => {
      const body: Record<string, unknown> = {
        actions: Object.values(current),
        auth: auth === "oauth2_auth_code" ? { type: auth, client_id: clientId } : { type: auth },
      };
      if (clientSecret) body.client_secret = clientSecret;
      await api(`${base}/rest-apps/${id}`, { method: "PUT", body: JSON.stringify(body) });
      setClientSecret("");
      app.reload();
      onSaved();
    });
  const approve = () =>
    act.run(async () => {
      await api(`${base}/rest-apps/${id}/approve`, { method: "POST" });
      app.reload();
      onSaved();
    });
  return (
    <div className="cat-editor">
      <ErrorNote error={act.error} />
      <fieldset>
        <legend>Operations in the document</legend>
        <ul className="cat-ops">
          {(r.operations ?? []).map((op) => (
            <li key={op.id}>
              <label>
                <input type="checkbox" checked={Boolean(current[op.id])} onChange={() => toggle(op)} />{" "}
                <code>
                  {op.method} {op.path}
                </code>{" "}
                {op.summary}
                <span className={`auto-kind ${op.kind === "read" ? "" : "write"} ${op.kind === "delete" ? "sensitive" : ""}`}>
                  {op.kind}
                  {op.kind === "delete" ? " · needs approval" : ""}
                </span>
                {op.deprecated && <span className="muted small"> (deprecated)</span>}
              </label>
              {current[op.id] && Object.keys(current[op.id].mapping).length > 0 && (
                <div className="small muted">
                  Field mapping:{" "}
                  {Object.entries(current[op.id].mapping)
                    .map(([ours, theirs]) => `${ours} → ${theirs}`)
                    .join(", ")}
                </div>
              )}
            </li>
          ))}
        </ul>
      </fieldset>
      <div className="auto-fields">
        <label>
          Sign-in
          <select value={auth} onChange={(e) => setAuthType(e.target.value)}>
            <option value="none">None</option>
            {(r.security_schemes ?? []).map((s) => (
              <option key={s.scheme + s.type} value={s.type}>
                {s.scheme}: {s.type.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        {auth === "oauth2_auth_code" && (
          <>
            <label>
              Your OAuth client id
              <input value={clientId} onChange={(e) => setClientId(e.target.value)} />
            </label>
            <label>
              Client secret {r.auth.client_secret_set ? "(kept; enter to replace)" : ""}
              <input type="password" autoComplete="off" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
            </label>
          </>
        )}
      </div>
      <p className="small muted">
        API keys, passwords and client secrets for the other sign-in methods are entered on the app's set-up page after
        approval, and are kept encrypted.
      </p>
      <div className="auto-row">
        <button className="button small secondary" type="button" onClick={suggest} disabled={act.busy}>
          Suggest a first choice
        </button>
        <button className="button small" type="button" onClick={save} disabled={act.busy}>
          Save (back to draft)
        </button>
        <button className="button small" type="button" onClick={approve} disabled={act.busy || r.status === "approved"}>
          Approve
        </button>
        {r.status === "approved" && (
          <Link className="button small secondary" to={`/commai/integrations/${r.app}`}>
            Set up
          </Link>
        )}
      </div>
    </div>
  );
}

// ---- inbound webhooks --------------------------------------------------------------------

function InboundHooks({ base }: { base: string }) {
  const list = useApi<InboundHook[]>(`${base}/inbound-hooks`, 30_000);
  const [name, setName] = useState("");
  const [eventType, setEventType] = useState("");
  const [created, setCreated] = useState<InboundHook | null>(null);
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const h = await api<InboundHook>(`${base}/inbound-hooks`, {
        method: "POST",
        body: JSON.stringify({ name, event_type: eventType }),
      });
      setCreated(h);
      setName("");
      setEventType("");
      list.reload();
    });
  };
  const toggle = (h: InboundHook) =>
    act.run(async () => {
      await api(`${base}/inbound-hooks/${h.id}`, { method: "PATCH", body: JSON.stringify({ active: !h.active }) });
      list.reload();
    });
  return (
    <section id="hooks" className="cat-section">
      <Card title="Inbound webhooks">
        <p className="small muted">
          An address Zapier, Make, n8n or any system can send events to. Deliveries must be signed (Standard Webhooks
          or the ExaCarib v1 signature); each is accepted once and can start a workflow
          (<code>inbound_webhook.received</code>).
        </p>
        <ErrorNote error={list.error ?? act.error} />
        {created?.secret && (
          <div className="auto-banner ok" role="status">
            <strong>{created.name}</strong>: address <code className="cat-wrap">{created.url}</code>, signing secret{" "}
            <code className="cat-wrap">{created.secret}</code>. Copy the secret now; it is not shown again.
          </div>
        )}
        <ul className="auto-log">
          {(list.data ?? [])
            .filter((h) => h.kind === "generic")
            .map((h) => (
              <li key={h.id}>
                <span className="auto-status">{h.active ? "On" : "Off"}</span>
                <span>
                  <strong>{h.name}</strong> {h.event_type && <code>{h.event_type}</code>} · {h.received_count} received,{" "}
                  {h.rejected_count} refused{h.last_received_at ? ` · last ${when(h.last_received_at)}` : ""}{" "}
                  <button className="button small secondary" type="button" onClick={() => toggle(h)}>
                    {h.active ? "Switch off" : "Switch on"}
                  </button>
                </span>
              </li>
            ))}
        </ul>
        <form onSubmit={submit} className="auto-fields" style={{ marginTop: 12 }}>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
          </label>
          <label>
            Event type (optional)
            <input value={eventType} onChange={(e) => setEventType(e.target.value)} placeholder="order.created" />
          </label>
          <div className="wide">
            <button className="button" disabled={act.busy || !name}>
              Create address
            </button>
          </div>
        </form>
      </Card>
    </section>
  );
}

function WebhookFormat({ base }: { base: string }) {
  const eps = useApi<Endpoint[]>(`${base}/webhooks`, 0);
  const [id, setId] = useState("");
  const [format, setFormat] = useState("cloudevents");
  const [standard, setStandard] = useState(true);
  const [secret, setSecret] = useState<string | null>(null);
  const act = useAction();
  if (!eps.data?.length) return null;
  const save = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const r = await api<{ standard_secret?: string }>(`${base}/webhooks/${id}/format`, {
        method: "PUT",
        body: JSON.stringify({ format, standard_headers: standard }),
      });
      setSecret(r.standard_secret ?? null);
    });
  };
  return (
    <Card title="Event webhooks: format">
      <p className="small muted">
        Send events as CloudEvents 1.0 and add the Standard Webhooks headers, so standard tools can verify them.
      </p>
      <ErrorNote error={act.error} />
      {secret && (
        <div className="auto-banner ok" role="status">
          Standard Webhooks secret: <code className="cat-wrap">{secret}</code>
        </div>
      )}
      <form onSubmit={save} className="auto-fields">
        <label>
          Endpoint
          <select value={id} onChange={(e) => setId(e.target.value)} required>
            <option value="">Choose…</option>
            {eps.data.map((x) => (
              <option key={x.id} value={x.id}>
                {x.url}
              </option>
            ))}
          </select>
        </label>
        <label>
          Format
          <select value={format} onChange={(e) => setFormat(e.target.value)}>
            <option value="cloudevents">CloudEvents 1.0</option>
            <option value="exacarib">ExaCarib JSON</option>
          </select>
        </label>
        <label>
          <span>
            <input type="checkbox" checked={standard} onChange={(e) => setStandard(e.target.checked)} /> Standard Webhooks
            headers
          </span>
        </label>
        <div className="wide">
          <button className="button" disabled={act.busy || !id}>
            Save
          </button>
        </div>
      </form>
    </Card>
  );
}

// ---- data exchange -----------------------------------------------------------------------

function Exchange({ base }: { base: string }) {
  const feed = useApi<{ published: boolean; since: string | null }>(`${base}/ical-feed`, 0);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [feedUrl, setFeedUrl] = useState<string | null>(null);
  const act = useAction();
  const upload = (kind: "csv" | "vcf") => (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    act.run(async () => {
      const data = await f.text();
      setResult(
        await api<ImportResult>(`${base}/imports/contacts.${kind}`, { method: "POST", body: JSON.stringify({ data }) }),
      );
    });
    e.target.value = "";
  };
  const publish = () =>
    act.run(async () => {
      const r = await api<{ url: string }>(`${base}/ical-feed`, { method: "POST" });
      setFeedUrl(r.url);
      feed.reload();
    });
  const dl = `/api/v1${base}/exports`;
  return (
    <section id="exchange" className="cat-section">
      <Card title="Data exchange">
        <ErrorNote error={act.error} />
        <div className="auto-fields">
          <div>
            <div className="cat-label">Download</div>
            <ul className="cat-via">
              <li>
                <a href={`${dl}/contacts.csv`}>Contacts (CSV)</a>
              </li>
              <li>
                <a href={`${dl}/contacts.vcf`}>Contacts (vCard)</a>
              </li>
              <li>
                <a href={`${dl}/conversations.csv`}>Conversations (CSV, one row per message)</a>
              </li>
            </ul>
          </div>
          <label>
            Import contacts from CSV
            <input type="file" accept=".csv,text/csv" onChange={upload("csv")} disabled={act.busy} />
            <span className="small muted">Columns: name, email, phone, language, external_ref.</span>
          </label>
          <label>
            Import contacts from vCard
            <input type="file" accept=".vcf,text/vcard" onChange={upload("vcf")} disabled={act.busy} />
          </label>
        </div>
        {result && (
          <div className="auto-banner ok" role="status">
            {result.rows} rows: {result.created} added, {result.updated} updated, {result.skipped} skipped.
            {result.problems.length > 0 && (
              <ul className="auto-warnings">
                {result.problems.slice(0, 10).map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <h3 className="cat-sub">Bookings calendar</h3>
        <p className="small muted">
          A private address any calendar app can subscribe to. Confirmed bookings carry an invite (.ics) link.
          {feed.data?.published ? ` Published ${when(feed.data.since)}.` : " Not published."}
        </p>
        {feedUrl && (
          <div className="auto-banner ok" role="status">
            Calendar address: <code className="cat-wrap">{feedUrl}</code>. Copy it now; it is not shown again.
          </div>
        )}
        <button className="button small" type="button" onClick={publish} disabled={act.busy}>
          {feed.data?.published ? "Make a new address" : "Publish"}
        </button>
      </Card>
    </section>
  );
}
