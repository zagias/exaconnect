import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote, ExampleTag } from "../../../components";
import { Card, useAction } from "../../../ui";
import { when } from "../lib";
import type { VoiceOverview } from "./types";

/* Voice stage 5 screens (ADR 0033): numbers by country, port orders,
   emergency addresses, fraud protection and (ExaCarib only) carriers. */

interface Country {
  code: string;
  name: string;
  calling_code: string;
  areas: string[];
  numbers: boolean;
  porting: boolean;
}
interface FoundNumber {
  e164: string;
  country: string;
  area: string;
}
interface Price {
  currency: string;
  monthly_delta: string;
  one_time: string;
}
interface PortRow {
  id: string;
  e164: string;
  status: string;
  losing_carrier: string;
  foc_date: string | null;
  rejection_reason: string;
  documents: number;
  created_at: string;
}
interface PortDetail extends Omit<PortRow, "documents"> {
  account_name: string;
  account_number: string;
  requested_date: string | null;
  missing_documents: string[];
  price: Price;
  timeline: { status: string; detail: string; actor: string; at: string }[];
  documents: PortDoc[];
  test_calls: { e164: string; ok: boolean; detail: string; at: string }[];
}
interface PortDoc {
  id: string;
  doc_type: string;
  filename: string;
  size_bytes: number;
  uploaded_at: string;
}
interface Rule {
  country: string;
  numbers: string[];
  location_delivery: boolean;
  requires_validated_address: boolean;
  notice: string;
  status: string;
}
interface EmergencySite {
  id: string;
  name: string;
  address_line1: string;
  city: string;
  island: string;
  country: string;
  postcode: string;
  emergency_status: string;
  emergency_reason: string;
  rules: Rule;
}
interface EmergencyPerson {
  id: string;
  name: string;
  extension: string;
  site: string | null;
  emergency_own: boolean;
  emergency_status: string;
  emergency_reason: string;
  notice_acknowledged: boolean;
  notice_country: string;
}
interface EmergencyNumber {
  id: string;
  e164: string;
  status: string;
  outbound_enabled: boolean;
  outbound_reason: string;
}
interface EmergencyOverview {
  sites: EmergencySite[];
  people: EmergencyPerson[];
  numbers: EmergencyNumber[];
}
interface Notice {
  key: string;
  text: string;
  country: string;
  numbers: string[];
  acknowledged_at: string | null;
  status: string;
}
interface Notification {
  id: number;
  kind: string;
  title: string;
  body: string;
  at: string;
  read_at: string | null;
}

const PORT_STATUS: Record<string, [string, string]> = {
  draft: ["off", "Draft"],
  submitted: ["warn", "With the losing carrier"],
  documents_needed: ["warn", "Documents needed"],
  accepted: ["warn", "Accepted"],
  scheduled: ["warn", "Switch-over booked"],
  cutting_over: ["warn", "Switching over"],
  completed: ["ok", "Live"],
  rolled_back: ["bad", "Rolled back"],
  rejected: ["bad", "Rejected"],
  cancelled: ["off", "Cancelled"],
};
const ADDRESS_STATUS: Record<string, [string, string]> = {
  not_registered: ["off", "Not checked"],
  pending: ["warn", "Being checked"],
  registered: ["ok", "Validated"],
  rejected: ["bad", "Not accepted"],
  site: ["off", "Uses site address"],
};
const DOC_LABEL: Record<string, string> = { loa: "Letter of authorisation", bill: "Recent bill", id: "Proof of identity", other: "Other" };

function Pill({ map, k }: { map: Record<string, [string, string]>; k: string }) {
  const [cls, label] = map[k] ?? ["off", k];
  return <span className={`pill small ${cls}`}>{label}</span>;
}

const post = (path: string, body?: unknown, method = "POST") =>
  api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

/** Uploads a file as multipart form data (the JSON helper sets its own content type). */
async function upload(path: string, form: FormData): Promise<void> {
  const r = await fetch(`/api/v1${path}`, {
    method: "POST",
    body: form,
    credentials: "same-origin",
    headers: { "X-Requested-With": "exa-portal" },
  });
  if (!r.ok) {
    let msg = `The controller answered ${r.status}.`;
    try {
      const b = await r.json();
      if (typeof b.detail === "string") msg = b.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(msg);
  }
}

// ---- notices ---------------------------------------------------------------------------

export function VoiceNotices({ base }: { base: string }) {
  const list = useApi<Notification[]>(`${base}/voice/notifications?limit=20`, 30_000);
  const act = useAction();
  const unread = (list.data ?? []).filter((n) => !n.read_at);
  if (unread.length === 0) return null;
  return (
    <div className="voice-inline" role="status">
      <div className="row-between">
        <strong>{unread.length === 1 ? "1 new notice" : `${unread.length} new notices`}</strong>
        <button className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => (await post(`${base}/voice/notifications/read`), list.reload()))}>
          Mark as read
        </button>
      </div>
      <ul className="plain-list">
        {unread.slice(0, 5).map((n) => (
          <li key={n.id}>
            {n.title} <span className="muted small">{when(n.at)}</span>
            {n.body && <div className="small muted">{n.body}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---- numbers by country ----------------------------------------------------------------

export function Numbers({ base, v }: { base: string; v: VoiceOverview }) {
  const list = useApi<Country[]>(`${base}/voice/countries`, 0);
  const [country, setCountry] = useState("TT");
  const [area, setArea] = useState("");
  const [contains, setContains] = useState("");
  const [found, setFound] = useState<FoundNumber[] | null>(null);
  const [example, setExample] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [made, setMade] = useState<string | null>(null);
  const act = useAction();
  const c = (list.data ?? []).find((x) => x.code === country);

  const search = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const q = new URLSearchParams({ country, area, contains });
      const r = await api<{ numbers: FoundNumber[]; example: boolean }>(`${base}/voice/numbers/search?${q}`);
      setFound(r.numbers);
      setExample(r.example);
      setPicked([]);
    });
  };
  const order = () =>
    act.run(async () => {
      const o = await api<{ id: string }>(`${base}/voice/orders`, {
        method: "POST",
        body: JSON.stringify({ items: { numbers: { new: picked.length, country, area: area || undefined, choose: picked } } }),
      });
      setMade(o.id);
      setPicked([]);
    });

  return (
    <>
      <Card title="Countries" note={<span className="muted small">ExaCarib switches each country on after its written go-live checks pass.</span>}>
        <ErrorNote error={list.error} />
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Country</th>
              <th scope="col">Area codes</th>
              <th scope="col">New numbers</th>
              <th scope="col">Porting</th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((x) => (
              <tr key={x.code}>
                <td>
                  {x.name} <span className="muted small">+{x.calling_code}</span>
                </td>
                <td className="mono">{x.areas.join(", ") || "—"}</td>
                <td>{x.numbers ? <span className="pill small ok">On</span> : <span className="pill small off">Not yet</span>}</td>
                <td>{x.porting ? <span className="pill small ok">On</span> : <span className="pill small off">Not yet</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title="Find and order numbers">
        <form className="voice-when" onSubmit={search}>
          <label>
            Country
            <select value={country} onChange={(e) => (setCountry(e.target.value), setArea(""), setFound(null))}>
              {(list.data ?? []).map((x) => (
                <option key={x.code} value={x.code}>
                  {x.name}
                  {x.numbers ? "" : " (not on yet)"}
                </option>
              ))}
            </select>
          </label>
          {c && c.areas.length > 1 && (
            <label>
              Area code
              <select value={area} onChange={(e) => setArea(e.target.value)}>
                <option value="">Any</option>
                {c.areas.map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
            </label>
          )}
          <label>
            Contains
            <input value={contains} onChange={(e) => setContains(e.target.value)} inputMode="numeric" maxLength={12} />
          </label>
          <button className="button" disabled={act.busy || !c?.numbers}>
            Search
          </button>
        </form>
        {c && !c.numbers && <p className="muted small">Numbers in {c.name} are not switched on yet.</p>}
        <ErrorNote error={act.error} />
        {found && (
          <>
            <p className="small">{example && <ExampleTag />} Pick the numbers you want, then order them. Each one goes live only after a test call works.</p>
            {found.length === 0 && <p className="muted">No numbers free there.</p>}
            <div className="voice-checks">
              {found.map((n) => (
                <label key={n.e164} className="mono">
                  <input
                    type="checkbox"
                    checked={picked.includes(n.e164)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, n.e164] : picked.filter((p) => p !== n.e164))}
                  />{" "}
                  {n.e164}
                </label>
              ))}
            </div>
            <p>
              <button className="button" disabled={!picked.length || act.busy || !v.is_admin} onClick={order}>
                Make an order for {picked.length} number{picked.length === 1 ? "" : "s"}
              </button>{" "}
              {made && <span className="small">Order made: approve its price under Orders.</span>}
            </p>
          </>
        )}
      </Card>
    </>
  );
}

// ---- port orders -----------------------------------------------------------------------

export function Ports({ base, v }: { base: string; v: VoiceOverview }) {
  const list = useApi<PortRow[]>(`${base}/voice/port-orders`, 15_000);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <NewPort base={base} onMade={(id) => (list.reload(), setOpen(id))} />
      <Card title="Port orders">
        <ErrorNote error={list.error} />
        {list.data && list.data.length === 0 && <p className="muted">No port orders yet.</p>}
        <ul className="plain-list">
          {(list.data ?? []).map((p) => (
            <li key={p.id}>
              <div className="row-between">
                <span>
                  <Pill map={PORT_STATUS} k={p.status} /> <span className="mono">{p.e164}</span> from {p.losing_carrier || "the losing carrier"}
                  {p.foc_date && p.status === "scheduled" && <span className="small"> on {p.foc_date}</span>}
                  <span className="muted small"> {when(p.created_at)}</span>
                </span>
                <button className="button secondary small" aria-expanded={open === p.id} onClick={() => setOpen(open === p.id ? null : p.id)}>
                  {open === p.id ? "Hide" : "Details"}
                </button>
              </div>
              {open === p.id && <PortPanel base={base} id={p.id} v={v} onChange={list.reload} />}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function NewPort({ base, onMade }: { base: string; onMade: (id: string) => void }) {
  const [f, setF] = useState({ e164: "", losing_carrier: "", account_name: "", account_number: "", requested_date: "" });
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const p = await api<{ id: string }>(`${base}/voice/port-orders`, {
        method: "POST",
        body: JSON.stringify({ ...f, requested_date: f.requested_date || null }),
      });
      setF({ e164: "", losing_carrier: "", account_name: "", account_number: "", requested_date: "" });
      onMade(p.id);
    });
  };
  const field = (k: keyof typeof f, label: string, type = "text") => (
    <label>
      {label}
      <input type={type} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} required={k === "e164" || k === "losing_carrier"} />
    </label>
  );
  return (
    <Card title="Bring a number to Jibsy">
      <p className="muted small">The number keeps working with your current provider until the agreed switch-over date.</p>
      <form className="voice-when" onSubmit={submit}>
        {field("e164", "Number (+country code)")}
        {field("losing_carrier", "Current provider")}
        {field("account_name", "Account name")}
        {field("account_number", "Account number")}
        {field("requested_date", "Preferred date", "date")}
        <button className="button" disabled={act.busy}>
          Start a port order
        </button>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function PortPanel({ base, id, v, onChange }: { base: string; id: string; v: VoiceOverview; onChange: () => void }) {
  const p = useApi<PortDetail>(`${base}/voice/port-orders/${id}`, 10_000);
  const act = useAction();
  const [docType, setDocType] = useState("loa");
  const [date, setDate] = useState("");
  const [account, setAccount] = useState("");
  const d = p.data;
  if (!d) return <ErrorNote error={p.error} />;
  const path = `${base}/voice/port-orders/${id}`;
  const done = () => (p.reload(), onChange());
  const send = (file: File | undefined) =>
    file &&
    act.run(async () => {
      const form = new FormData();
      form.append("doc_type", docType);
      form.append("file", file);
      await upload(`${path}/documents`, form);
      done();
    });
  const canDocs = ["draft", "documents_needed", "rejected", "submitted"].includes(d.status);
  return (
    <div className="voice-panel">
      {d.status === "rejected" && (
        <p className="voice-error">
          Rejected: {d.rejection_reason} Correct the details below and submit again, or cancel.
        </p>
      )}
      <h3 className="voice-sub">Timeline</h3>
      <ol className="voice-steps" aria-label="Port timeline">
        {d.timeline.map((t, i) => (
          <li key={i}>
            <Pill map={PORT_STATUS} k={t.status} /> {t.detail} <span className="muted small">{when(t.at)}</span>
          </li>
        ))}
      </ol>
      <h3 className="voice-sub">Documents</h3>
      {d.missing_documents.length > 0 && <p className="small">Still needed: {d.missing_documents.join(", ").toLowerCase()}.</p>}
      <ul className="plain-list">
        {d.documents.map((doc) => (
          <li key={doc.id} className="small">
            {DOC_LABEL[doc.doc_type]}:{" "}
            <a href={`/api/v1${path}/documents/${doc.id}`}>{doc.filename}</a>{" "}
            <span className="muted">{Math.ceil(doc.size_bytes / 1024)} KB</span>
          </li>
        ))}
      </ul>
      {canDocs && (
        <div className="voice-when">
          <label>
            Type
            <select value={docType} onChange={(e) => setDocType(e.target.value)}>
              {Object.entries(DOC_LABEL).map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <label>
            File (PDF, PNG or JPEG, up to 10 MB)
            <input type="file" accept="application/pdf,image/png,image/jpeg" onChange={(e) => send(e.target.files?.[0])} />
          </label>
        </div>
      )}
      {["draft", "rejected"].includes(d.status) && (
        <>
          {d.status === "rejected" && (
            <div className="voice-when">
              <label>
                Account number
                <input value={account} placeholder={d.account_number} onChange={(e) => setAccount(e.target.value)} />
              </label>
              <button className="button secondary" disabled={!account || act.busy} onClick={() => act.run(async () => (await post(path, { account_number: account }, "PATCH"), done()))}>
                Correct
              </button>
            </div>
          )}
          <p>
            {!v.can_spend && <span className="pill warn">Someone with spend permission must submit this port.</span>}{" "}
            <button
              className="button"
              disabled={!v.can_spend || act.busy || d.missing_documents.length > 0}
              onClick={() => act.run(async () => (await post(`${path}/submit`, { accepted_price: { monthly_delta: d.price.monthly_delta, one_time: d.price.one_time } }), done()))}
            >
              Submit: {d.price.currency} {d.price.monthly_delta} a month and {d.price.one_time} once
            </button>
          </p>
        </>
      )}
      {["scheduled", "rolled_back"].includes(d.status) && (
        <div className="voice-when">
          <label>
            New switch-over date
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <button className="button secondary" disabled={!date || act.busy} onClick={() => act.run(async () => (await post(`${path}/reschedule`, { date }), done()))}>
            Reschedule
          </button>
        </div>
      )}
      {(d.test_calls ?? []).length > 0 && (
        <>
          <h3 className="voice-sub">Test calls</h3>
          <ul className="plain-list">
            {d.test_calls.map((t, i) => (
              <li key={i} className="small">
                <span className={`pill small ${t.ok ? "ok" : "bad"}`}>{t.ok ? "Worked" : "Failed"}</span> {t.detail} <span className="muted">{when(t.at)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {!["completed", "cancelled", "cutting_over"].includes(d.status) && (
        <p>
          <button className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => (await post(`${path}/cancel`), done()))}>
            Cancel port
          </button>
        </p>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

// ---- emergency addresses ---------------------------------------------------------------

export function Emergency({ base }: { base: string }) {
  const ov = useApi<EmergencyOverview>(`${base}/voice/emergency`, 15_000);
  const act = useAction();
  const [editing, setEditing] = useState<string | null>(null);
  const [addr, setAddr] = useState({ address_line1: "", city: "", island: "", country: "TT", postcode: "" });
  const d = ov.data;
  if (!d) return <ErrorNote error={ov.error} />;
  const blocked = d.numbers.filter((n) => n.status === "active" && !n.outbound_enabled);
  return (
    <>
      {blocked.length > 0 && (
        <div className="voice-inline" role="alert">
          <strong>
            {blocked.length} number{blocked.length === 1 ? " can't" : "s can't"} call out yet
          </strong>
          <ul className="plain-list">
            {blocked.map((n) => (
              <li key={n.id} className="small">
                <span className="mono">{n.e164}</span>: {n.outbound_reason}
              </li>
            ))}
          </ul>
          <p className="small muted">Emergency calls always work.</p>
        </div>
      )}
      <Card title="Sites" note={<span className="muted small">Each address is checked with the provider. The rules differ by island.</span>}>
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Site</th>
              <th scope="col">Address</th>
              <th scope="col">Status</th>
              <th scope="col">Emergency numbers</th>
              <th scope="col">Address sent with calls</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {d.sites.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td className="small">
                  {[s.address_line1, s.city, s.island, s.postcode, s.country].filter(Boolean).join(", ")}
                  {s.emergency_reason && <div className="voice-error">{s.emergency_reason}</div>}
                </td>
                <td>
                  <Pill map={ADDRESS_STATUS} k={s.emergency_status} />
                </td>
                <td className="mono">{s.rules.numbers.join(", ")}</td>
                <td className="small">
                  {s.rules.location_delivery ? "Yes" : "No: callers say where they are"}
                  {s.rules.requires_validated_address && <div className="muted">Outbound waits for a validated address</div>}
                </td>
                <td>
                  <button className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => (await post(`${base}/voice/emergency/sites/${s.id}/validate`), ov.reload()))}>
                    Check again
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">Change a site's address under Phone system. A person moved to another site takes that site's address, and it is checked again.</p>
      </Card>
      <Card title="People">
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Person</th>
              <th scope="col">Address</th>
              <th scope="col">Notice read</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {d.people.map((p) => (
              <tr key={p.id}>
                <td>
                  {p.name} <span className="muted small">ext {p.extension}</span>
                </td>
                <td className="small">
                  {p.emergency_own ? (
                    <>
                      Own address <Pill map={ADDRESS_STATUS} k={p.emergency_status} />
                    </>
                  ) : (
                    <>{p.site ?? "No site"}</>
                  )}
                  {p.emergency_reason && <div className="voice-error">{p.emergency_reason}</div>}
                </td>
                <td>{p.notice_acknowledged ? <span className="pill small ok">Yes</span> : <span className="pill small warn">Not yet</span>}</td>
                <td>
                  <button className="button secondary small" onClick={() => setEditing(editing === p.id ? null : p.id)}>
                    {editing === p.id ? "Close" : "Home address"}
                  </button>
                  {p.emergency_own && (
                    <button className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => (await post(`${base}/voice/emergency/users/${p.id}`, { address: null }, "PUT"), ov.reload()))}>
                      Use site
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {editing && (
          <form
            className="voice-when"
            onSubmit={(e) => {
              e.preventDefault();
              act.run(async () => {
                await post(`${base}/voice/emergency/users/${editing}`, { address: addr }, "PUT");
                setEditing(null);
                ov.reload();
              });
            }}
          >
            {(["address_line1", "city", "island", "postcode"] as const).map((k) => (
              <label key={k}>
                {{ address_line1: "Street", city: "Town or city", island: "Island", postcode: "Postcode" }[k]}
                <input value={addr[k]} onChange={(e) => setAddr({ ...addr, [k]: e.target.value })} required={k === "address_line1"} />
              </label>
            ))}
            <label>
              Country
              <input value={addr.country} maxLength={2} onChange={(e) => setAddr({ ...addr, country: e.target.value.toUpperCase() })} />
            </label>
            <button className="button" disabled={act.busy}>
              Save and check
            </button>
          </form>
        )}
        <ErrorNote error={act.error} />
      </Card>
    </>
  );
}

/** The notice each person must read about emergency calling on their island. */
export function EmergencyNotice({ base }: { base: string }) {
  const n = useApi<Notice>(`${base}/voice/me/emergency-notice`, 0);
  const act = useAction();
  if (!n.data) return null;
  const d = n.data;
  return (
    <section className="card" style={{ marginBottom: 24 }} aria-label="Emergency calling">
      <div className="card-head">
        <h2>Emergency calling</h2>
        {d.acknowledged_at ? <span className="pill small ok">Read</span> : <span className="pill small warn">Please read</span>}
      </div>
      <p>{d.text}</p>
      {!d.acknowledged_at && (
        <button className="button" disabled={act.busy} onClick={() => act.run(async () => (await post(`${base}/voice/me/emergency-notice`, { key: d.key }), n.reload()))}>
          I have read this
        </button>
      )}
      <ErrorNote error={act.error} />
    </section>
  );
}

// ---- fraud -------------------------------------------------------------------------------

interface FraudStatus {
  settings: {
    origin: string;
    intl_suspended: boolean;
    suspended_at: string | null;
    suspended_reason: string;
    intl_daily_cap: string | null;
    intl_daily_calls: number;
    after_hours_international: string;
    hours_id: string | null;
    spike_factor: string;
    spike_min_calls: number;
    allowed_high_risk: string[];
    restored_by: string;
    restored_at: string | null;
  };
  today: { calls: number; spend: string };
  rates: { last_hour: number; hourly_baseline: number };
  high_risk: { origin: string; prefix: string; name: string; reason: string }[];
  open_now: boolean;
}

export function Fraud({ base, v }: { base: string; v: VoiceOverview }) {
  const st = useApi<FraudStatus>(`${base}/voice/fraud`, 15_000);
  const act = useAction();
  const [draft, setDraft] = useState<Partial<FraudStatus["settings"]>>({});
  const [note, setNote] = useState("");
  const d = st.data;
  if (!d) return <ErrorNote error={st.error} />;
  const s = { ...d.settings, ...draft };
  const save = () =>
    act.run(async () => {
      await post(`${base}/voice/fraud`, draft, "PUT");
      setDraft({});
      st.reload();
    });
  const allow = (prefix: string, on: boolean) =>
    act.run(async () => {
      const list = on ? [...s.allowed_high_risk, prefix] : s.allowed_high_risk.filter((p) => p !== prefix);
      await post(`${base}/voice/fraud`, { allowed_high_risk: list }, "PUT");
      st.reload();
    });
  return (
    <>
      {s.intl_suspended && (
        <div className="voice-inline" role="alert">
          <strong>International calling is suspended.</strong> <span className="small">{s.suspended_reason}</span>
          <div className="voice-when">
            <label>
              What you checked
              <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
            </label>
            <button className="button" disabled={!v.can_spend || act.busy} onClick={() => act.run(async () => (await post(`${base}/voice/fraud/restore`, { note }), st.reload()))}>
              Restore international calling
            </button>
          </div>
          {!v.can_spend && <p className="small">Someone with spend permission must restore it.</p>}
        </div>
      )}
      <Card title="International calling" note={<span className="muted small">Calls from {s.origin} to any other country count as international.</span>}>
        <div className="voice-figures">
          <div>
            <div className="muted small">International calls today</div>
            <div className="mono">{d.today.calls}</div>
          </div>
          <div>
            <div className="muted small">International spend today</div>
            <div className="mono">{d.today.spend}</div>
          </div>
          <div>
            <div className="muted small">Last hour, against the usual hourly rate</div>
            <div className="mono">
              {d.rates.last_hour} / {d.rates.hourly_baseline.toFixed(1)}
            </div>
          </div>
        </div>
        <div className="voice-when">
          <label>
            Calls a day
            <input type="number" min={1} value={s.intl_daily_calls} onChange={(e) => setDraft({ ...draft, intl_daily_calls: Number(e.target.value) })} />
          </label>
          <label>
            Spend a day
            <input inputMode="decimal" value={s.intl_daily_cap ?? ""} placeholder="No cap" onChange={(e) => setDraft({ ...draft, intl_daily_cap: e.target.value || null })} />
          </label>
          <label>
            Outside business hours
            <select value={s.after_hours_international} onChange={(e) => setDraft({ ...draft, after_hours_international: e.target.value })}>
              <option value="allow">Allow</option>
              <option value="alert">Allow and alert</option>
              <option value="block">Block</option>
            </select>
          </label>
          <label>
            Business hours
            <select value={s.hours_id ?? ""} onChange={(e) => setDraft({ ...draft, hours_id: e.target.value || null })}>
              <option value="">Mon to Fri, 07:00 to 19:00</option>
              {(v.hours ?? []).map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Suspend at this many times the usual rate
            <input inputMode="decimal" value={s.spike_factor} onChange={(e) => setDraft({ ...draft, spike_factor: e.target.value })} />
          </label>
          <label>
            Once there are at least (calls an hour)
            <input type="number" min={3} value={s.spike_min_calls} onChange={(e) => setDraft({ ...draft, spike_min_calls: Number(e.target.value) })} />
          </label>
          <button className="button" disabled={act.busy || Object.keys(draft).length === 0} onClick={save}>
            Save
          </button>
        </div>
        <p className="muted small">Emergency calls are never stopped. Now: {d.open_now ? "within business hours" : "outside business hours"}.</p>
        <ErrorNote error={act.error} />
      </Card>
      <Card title="High-risk destinations" note={<ExampleTag />}>
        <p className="muted small">
          ExaCarib keeps this list for callers in {s.origin}. These destinations are often used for revenue share fraud, so calls to them are blocked unless you allow one. Allowing one needs spend permission.
        </p>
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Prefix</th>
              <th scope="col">Destination</th>
              <th scope="col">Why</th>
              <th scope="col">Calls</th>
            </tr>
          </thead>
          <tbody>
            {d.high_risk.map((h) => {
              const allowed = s.allowed_high_risk.includes(h.prefix);
              return (
                <tr key={`${h.origin}${h.prefix}`}>
                  <td className="mono">+{h.prefix}</td>
                  <td>{h.name}</td>
                  <td className="small">{h.reason}</td>
                  <td>
                    <label className="small">
                      <input type="checkbox" checked={allowed} disabled={!v.can_spend || act.busy} onChange={(e) => allow(h.prefix, e.target.checked)} /> {allowed ? "Allowed" : "Blocked"}
                    </label>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </>
  );
}

// ---- carriers (ExaCarib admins) ---------------------------------------------------------------------

interface Carrier {
  id: string;
  key: string;
  name: string;
  adapter: string;
  health: string;
  consecutive_failures: number;
  last_options_at: string | null;
  last_rtt_ms: number | null;
  rate_version: number;
  rates: { prefix: string; name: string; per_minute: string }[];
  golive: { status: string; pilots: string[] };
  quality: { score: number; success_pct: number | null; avg_rtt_ms: number | null; samples: number };
  outbound: { host?: string; port?: number; transport?: string };
}
interface Plan {
  mode: string;
  route: string[];
  carriers: { key: string; name: string; health: string; per_minute: string | null; quality: number; usable: boolean; reason: string }[];
}
interface CarrierRec {
  rows: number;
  charged: string;
  expected_from_rates: string;
  difference: string;
  billed_to_customers: string;
  margin: string;
  calls_not_on_carrier_records: number;
  issues: { call_ref: string; issue: string }[];
}

const HEALTH: Record<string, [string, string]> = {
  unknown: ["off", "Not checked"],
  up: ["ok", "Up"],
  degraded: ["warn", "Degraded"],
  down: ["bad", "Down"],
};
const A = "/commai/voice-admin";

export function Carriers({ customerId }: { customerId: string }) {
  const list = useApi<Carrier[]>(`${A}/carriers`, 15_000);
  const rules = useApi<{ prefix: string; mode: string }[]>(`${A}/routing`, 0);
  const act = useAction();
  const [to, setTo] = useState("+1 876 555 0100");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [rule, setRule] = useState({ prefix: "", mode: "quality" });
  const [rec, setRec] = useState<Record<string, CarrierRec>>({});
  return (
    <>
      <Card title="Carriers" note={<span className="muted small">Each carrier carries calls only once it is switched on in the go-live registry.</span>}>
        <ErrorNote error={list.error} />
        <table className="paths dt">
          <thead>
            <tr>
              <th scope="col">Carrier</th>
              <th scope="col">Go-live</th>
              <th scope="col">Trunk</th>
              <th scope="col">Quality</th>
              <th scope="col">Rates</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((c) => (
              <tr key={c.key}>
                <td>
                  {c.name} {c.adapter === "simulated" && <span className="tag">Simulated</span>}
                  <div className="muted small mono">{c.outbound.host ? `${c.outbound.host}:${c.outbound.port} ${c.outbound.transport}` : c.key}</div>
                </td>
                <td>
                  <span className={`pill small ${c.golive.status === "on" ? "ok" : c.golive.status === "pilot" ? "warn" : "off"}`}>{c.golive.status}</span>
                </td>
                <td>
                  <Pill map={HEALTH} k={c.health} />
                  <div className="muted small">
                    {c.last_rtt_ms != null ? `${c.last_rtt_ms} ms` : ""} {c.last_options_at ? when(c.last_options_at) : ""}
                  </div>
                </td>
                <td className="mono">
                  {c.quality.score}
                  <div className="muted small">{c.quality.success_pct != null ? `${c.quality.success_pct}% answered` : "no OPTIONS yet"}</div>
                </td>
                <td className="small">
                  v{c.rate_version}: {c.rates.slice(0, 3).map((r) => `${r.prefix || "*"} ${r.per_minute}`).join(", ")}
                  {c.rates.length > 3 && "…"}
                </td>
                <td>
                  <button className="button secondary small" disabled={act.busy} onClick={() => act.run(async () => (await post(`${A}/carriers/${c.key}/options`), list.reload()))}>
                    Send OPTIONS
                  </button>{" "}
                  <button
                    className="button secondary small"
                    disabled={act.busy}
                    onClick={() =>
                      act.run(async () => {
                        await post(`${A}/carriers/${c.key}/import`);
                        setRec({ ...rec, [c.key]: await api<CarrierRec>(`${A}/carriers/${c.key}/reconcile`) });
                      })
                    }
                  >
                    Import and reconcile
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {Object.entries(rec).map(([k, r]) => (
          <div key={k} className="voice-panel small">
            <strong>{k}</strong>: {r.rows} calls, charged <span className="mono">{r.charged}</span>, rate sheet{" "}
            <span className="mono">{r.expected_from_rates}</span> (difference <span className="mono">{r.difference}</span>), billed{" "}
            <span className="mono">{r.billed_to_customers}</span>, margin <span className="mono">{r.margin}</span>.
            {r.calls_not_on_carrier_records > 0 && ` ${r.calls_not_on_carrier_records} of our calls are missing from the carrier's records.`}
            {r.issues.length > 0 && (
              <ul>
                {r.issues.slice(0, 10).map((i) => (
                  <li key={i.call_ref}>
                    <span className="mono">{i.call_ref}</span>: {i.issue}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        <ErrorNote error={act.error} />
      </Card>
      <Card title="Routing">
        <p className="muted small">Least cost puts the cheapest carrier first. Quality puts the healthiest trunk first. A carrier that refuses a call passes it to the next one.</p>
        <ul className="plain-list">
          {(rules.data ?? []).map((r) => (
            <li key={r.prefix} className="small">
              <span className="mono">{r.prefix ? `+${r.prefix}` : "Everywhere else"}</span>: {r.mode === "lcr" ? "least cost" : "quality"}
            </li>
          ))}
        </ul>
        <div className="voice-when">
          <label>
            Prefix
            <input value={rule.prefix} onChange={(e) => setRule({ ...rule, prefix: e.target.value })} placeholder="1876" inputMode="numeric" />
          </label>
          <label>
            Routing
            <select value={rule.mode} onChange={(e) => setRule({ ...rule, mode: e.target.value })}>
              <option value="lcr">Least cost</option>
              <option value="quality">Quality</option>
            </select>
          </label>
          <button className="button secondary" disabled={act.busy} onClick={() => act.run(async () => (await post(`${A}/routing`, rule, "PUT"), rules.reload()))}>
            Save rule
          </button>
        </div>
        <form
          className="voice-when"
          onSubmit={(e) => {
            e.preventDefault();
            act.run(async () => setPlan(await api<Plan>(`${A}/route?${new URLSearchParams({ customer_id: customerId, to })}`)));
          }}
        >
          <label>
            Try a number
            <input value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          <button className="button">Show the route</button>
        </form>
        {plan && (
          <ol className="voice-steps">
            {plan.carriers.map((c) => (
              <li key={c.key} className="small">
                <span className={`pill small ${c.usable ? "ok" : "off"}`}>{c.usable ? `${plan.route.indexOf(c.key) + 1}` : "Skipped"}</span> {c.name}{" "}
                <span className="mono">{c.per_minute ?? "—"}</span> a minute, quality {c.quality} {c.reason && <span className="muted">({c.reason})</span>}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </>
  );
}
