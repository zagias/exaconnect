import { useState, type CSSProperties, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, useAction } from "../../ui";
import { useCommaiBase } from "../commai/lib";
import type { HelpHome } from "./api";
import { Home } from "./HelpCentre";
import "./help.css";

type Show = HelpHome["show"];
type Day = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

interface HelpSettings {
  title: string;
  intro: string;
  colour: string;
  show: Show;
  hours: Partial<Record<Day, [string, string] | null>>;
  whatsapp: string;
  phone: string;
  email: string;
  widget_key_id: string;
  reschedule: boolean;
  cancel: boolean;
}

interface ArticleRow {
  source_id: string;
  title: string;
  approved: boolean;
  category: string;
  published: boolean;
  live: boolean;
  needs_republish: boolean;
}

interface DataRequest {
  id: string;
  kind: "download" | "delete";
  status: string;
  contact_name: string;
  contact_email: string;
  created_at: string;
  reason?: string;
}

interface Admin {
  slug: string;
  enabled: boolean;
  url: string;
  settings: HelpSettings;
  articles: ArticleRow[];
  preview: HelpHome;
  widget_keys: { id: string; name: string }[];
  open_data_requests: number;
}

const SHOW_LABEL: Record<keyof Show, string> = {
  articles: "Help articles",
  search: "Search",
  ask: "Ask (the AI agent)",
  contact: "Contact form",
  hours: "Opening hours",
  channels: "WhatsApp, phone and email links",
  signin: "Customer sign-in",
  bookings: "Bookings (signed-in customers)",
  data_requests: "Data requests (signed-in customers)",
};
const DAYS: [Day, string][] = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
];

/** Settings tab: the help centre for the business's own customers (ADR 0031). */
export default function HelpAdmin() {
  const base = useCommaiBase();
  const a = useApi<Admin>(base ? `${base}/help-centre` : null, 0);
  if (!base) return null;
  if (!a.data) return <ErrorNote error={a.error} />;
  return (
    <>
      <Basics base={base} data={a.data} onSaved={a.reload} />
      <Articles base={base} rows={a.data.articles} onSaved={a.reload} />
      <DataRequests base={base} onChange={a.reload} />
      <Card title="Preview" note={<span className="tag">{a.data.enabled ? "As customers see it" : "Not public yet"}</span>}>
        <p className="muted small">Buttons are off in the preview.</p>
        <div className="help-preview help" style={{ "--help-brand": a.data.preview.colour, minHeight: 0 } as CSSProperties}>
          <div className="help-head">
            <div className="help-wrap help-head-row">
              <span className="help-brand">
                {a.data.preview.business}
                <span className="help-brand-sub">{a.data.preview.title}</span>
              </span>
            </div>
          </div>
          <div className="help-wrap" style={{ padding: 16 }}>
            <Home slug={a.data.slug} home={a.data.preview} preview />
          </div>
        </div>
      </Card>
    </>
  );
}

function Basics({ base, data, onSaved }: { base: string; data: Admin; onSaved: () => void }) {
  const [enabled, setEnabled] = useState(data.enabled);
  const [slug, setSlug] = useState(data.slug);
  const [s, setS] = useState<HelpSettings>(data.settings);
  const [saved, setSaved] = useState(false);
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      await api(`${base}/help-centre`, { method: "PATCH", body: JSON.stringify({ enabled, slug, settings: s }) });
      setSaved(true);
      onSaved();
    });
  };
  const setDay = (d: Day, i: 0 | 1, v: string) => {
    const cur = s.hours[d] ?? ["09:00", "17:00"];
    const next: [string, string] = i === 0 ? [v, cur[1]] : [cur[0], v];
    setS({ ...s, hours: { ...s.hours, [d]: next } });
  };
  const toggleDay = (d: Day, open: boolean) => setS({ ...s, hours: { ...s.hours, [d]: open ? ["09:00", "17:00"] : null } });
  const anyHours = Object.keys(s.hours).length > 0;
  return (
    <Card title="Help centre">
      <p className="muted small">
        A public page where your own customers find answers, ask, contact you, and (once signed in) see their conversations
        and bookings. It stays off until you switch it on.
      </p>
      <form className="form" onSubmit={submit}>
        <label className="check wide">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Switched on (public)
        </label>
        <label>
          Address
          <input value={slug} maxLength={50} pattern="[a-z0-9][a-z0-9-]{1,48}[a-z0-9]" onChange={(e) => setSlug(e.target.value.toLowerCase())} />
        </label>
        <p className="wide small">
          Customers open <a href={data.url} target="_blank" rel="noreferrer">{data.url}</a>
        </p>
        <label>
          Title
          <input value={s.title} maxLength={80} onChange={(e) => setS({ ...s, title: e.target.value })} />
        </label>
        <label>
          Colour (white text must be readable on it)
          <input value={s.colour} placeholder="#155EEF" maxLength={7} onChange={(e) => setS({ ...s, colour: e.target.value })} />
        </label>
        <label className="wide">
          Introduction
          <textarea rows={2} maxLength={400} value={s.intro} onChange={(e) => setS({ ...s, intro: e.target.value })} />
        </label>
        <fieldset className="wide">
          <legend>Show</legend>
          {(Object.keys(SHOW_LABEL) as (keyof Show)[]).map((k) => (
            <label key={k} className="check">
              <input type="checkbox" checked={s.show[k]} onChange={(e) => setS({ ...s, show: { ...s.show, [k]: e.target.checked } })} />
              {SHOW_LABEL[k]}
            </label>
          ))}
        </fieldset>
        <fieldset className="wide">
          <legend>Opening hours ({anyHours ? "by day" : "none set: shown as open every day"})</legend>
          {DAYS.map(([d, label]) => {
            const span = s.hours[d];
            return (
              <div key={d} className="help-row" style={{ alignItems: "center" }}>
                <label className="check" style={{ minWidth: 130 }}>
                  <input type="checkbox" checked={!!span} onChange={(e) => toggleDay(d, e.target.checked)} /> {label}
                </label>
                {span && (
                  <>
                    <input type="time" aria-label={`${label} opens`} value={span[0]} onChange={(e) => setDay(d, 0, e.target.value)} />
                    <span aria-hidden="true">to</span>
                    <input type="time" aria-label={`${label} closes`} value={span[1]} onChange={(e) => setDay(d, 1, e.target.value)} />
                  </>
                )}
              </div>
            );
          })}
        </fieldset>
        <label>
          WhatsApp number
          <input value={s.whatsapp} placeholder="Your live WhatsApp line" onChange={(e) => setS({ ...s, whatsapp: e.target.value })} />
        </label>
        <label>
          Phone
          <input value={s.phone} onChange={(e) => setS({ ...s, phone: e.target.value })} />
        </label>
        <label>
          Email
          <input type="email" value={s.email} onChange={(e) => setS({ ...s, email: e.target.value })} />
        </label>
        <label>
          Sign-in from your own website
          <select value={s.widget_key_id} onChange={(e) => setS({ ...s, widget_key_id: e.target.value })}>
            <option value="">{data.widget_keys.length ? "First website chat key" : "No website chat key yet"}</option>
            {data.widget_keys.map((k) => (
              <option key={k.id} value={k.id}>
                {k.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="wide">
          <legend>Signed-in customers may</legend>
          <label className="check">
            <input type="checkbox" checked={s.reschedule} onChange={(e) => setS({ ...s, reschedule: e.target.checked })} /> Change a booking's time
          </label>
          <label className="check">
            <input type="checkbox" checked={s.cancel} onChange={(e) => setS({ ...s, cancel: e.target.checked })} /> Ask to cancel a booking (you approve it)
          </label>
        </fieldset>
        <p className="muted small wide">
          Empty numbers use your live channel accounts. Your site signs customers in with the same token as website chat, then
          sends them to the address above with <code>#user_token=…</code>.
        </p>
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            Save
          </button>
          <span role="status" aria-live="polite" className="ok-note">
            {saved ? "✓ Saved" : ""}
          </span>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Articles({ base, rows, onSaved }: { base: string; rows: ArticleRow[]; onSaved: () => void }) {
  const act = useAction();
  const [cats, setCats] = useState<Record<string, string>>({});
  const put = (r: ArticleRow, published: boolean) =>
    act.run(async () => {
      await api(`${base}/help-centre/articles/${r.source_id}`, {
        method: "PUT",
        body: JSON.stringify({ published, category: cats[r.source_id] ?? r.category }),
      });
      onSaved();
    });
  return (
    <Card title="Articles">
      <p className="muted small">
        Only knowledge you approved (on AI agents) can be published. Editing a source hides its article until you publish it
        again.
      </p>
      {rows.length === 0 && <p className="muted">No knowledge sources yet. Add them on the AI agents screen.</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Category</th>
              <th scope="col">Status</th>
              <th scope="col">
                <span className="sr-only">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.source_id}>
                <td>{r.title}</td>
                <td>
                  <input
                    aria-label={`Category for ${r.title}`}
                    value={cats[r.source_id] ?? r.category}
                    maxLength={60}
                    onChange={(e) => setCats({ ...cats, [r.source_id]: e.target.value })}
                  />
                </td>
                <td>
                  {r.live ? (
                    <span className="pill ok">Published</span>
                  ) : r.needs_republish ? (
                    <span className="pill warn">Edited: publish again</span>
                  ) : !r.approved ? (
                    <span className="pill shadow">Not approved</span>
                  ) : (
                    <span className="pill shadow">Not published</span>
                  )}
                </td>
                <td>
                  {r.live ? (
                    <button type="button" className="button secondary small" disabled={act.busy} onClick={() => put(r, false)}>
                      Withdraw<span className="sr-only"> {r.title}</span>
                    </button>
                  ) : (
                    <button type="button" className="button small" disabled={act.busy || !r.approved} onClick={() => put(r, true)}>
                      Publish<span className="sr-only"> {r.title}</span>
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function DataRequests({ base, onChange }: { base: string; onChange: () => void }) {
  const list = useApi<DataRequest[]>(`${base}/help-centre/data-requests`, 0);
  const act = useAction();
  const [exported, setExported] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const handle = (id: string, status: "done" | "refused", mode: "delete" | "anonymise" = "delete") =>
    act.run(async () => {
      if (status === "done" && mode && list.data?.find((r) => r.id === id)?.kind === "delete") {
        const what = mode === "delete" ? "delete this customer and all their conversations" : "remove everything that identifies this customer";
        if (!window.confirm(`This will ${what}. It can't be undone. Continue?`)) return;
      }
      const out = await api<DataRequest>(`${base}/help-centre/data-requests/${id}`, {
        method: "POST",
        body: JSON.stringify({ status, mode }),
      });
      setRefused(status === "done" && out.status === "refused" ? out.reason || "A legal hold stops it." : null);
      list.reload();
      onChange();
    });
  const exportOne = (id: string) =>
    act.run(async () => {
      const data = await api<unknown>(`${base}/help-centre/data-requests/${id}/export`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `customer-data-${id.slice(0, 8)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setExported(id);
    });
  return (
    <Card title="Customers' data requests">
      <p className="muted small">
        From signed-in customers. For a copy, download their data (no private notes are included), send it to them and mark it
        done. A deletion is carried out here, through your data rules: a contact on legal hold can't be deleted.
      </p>
      {list.data && list.data.length === 0 && <p className="muted">No open requests.</p>}
      <ul className="me-list" style={{ listStyle: "none", padding: 0 }}>
        {(list.data ?? []).map((r) => (
          <li key={r.id} style={{ padding: "8px 0", borderBottom: "1px solid var(--line)" }}>
            <strong>{r.kind === "download" ? "Copy of data" : "Delete data"}</strong> for {r.contact_name || r.contact_email || "a customer"}{" "}
            <span className="muted small">{new Date(r.created_at).toLocaleDateString("en-GB")}</span>
            <div className="form-actions" style={{ marginTop: 6 }}>
              {r.kind === "download" && (
                <button type="button" className="button secondary small" disabled={act.busy} onClick={() => exportOne(r.id)}>
                  Download their data{exported === r.id ? " ✓" : ""}
                </button>
              )}
              {r.kind === "delete" ? (
                <>
                  <button type="button" className="button small" disabled={act.busy} onClick={() => handle(r.id, "done", "delete")}>
                    Delete their data
                  </button>
                  <button type="button" className="button secondary small" disabled={act.busy} onClick={() => handle(r.id, "done", "anonymise")}>
                    Anonymise instead
                  </button>
                </>
              ) : (
                <button type="button" className="button small" disabled={act.busy} onClick={() => handle(r.id, "done")}>
                  Mark done
                </button>
              )}
              <button type="button" className="button danger-text small" disabled={act.busy} onClick={() => handle(r.id, "refused")}>
                Refuse
              </button>
            </div>
          </li>
        ))}
      </ul>
      {refused && (
        <p className="small" role="status">
          ⚠ Not deleted: {refused}
        </p>
      )}
      <ErrorNote error={act.error ?? list.error} />
    </Card>
  );
}
