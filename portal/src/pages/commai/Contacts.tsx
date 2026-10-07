import { useEffect, useState, type FormEvent } from "react";
import { Link, Route, Routes, useParams } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { CHANNEL_LABEL, STATE_LABEL, useCommaiBase, when, type Page } from "./lib";
import type { Contact } from "./types";

/** The business's own customers, their channel identities and history. */
export default function Contacts() {
  return (
    <Routes>
      <Route path="/" element={<ContactList />} />
      <Route path="/:id" element={<ContactPage />} />
    </Routes>
  );
}

function ContactList() {
  const base = useCommaiBase();
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const list = useApi<Page<Contact>>(base ? `${base}/contacts?limit=100${query ? `&q=${encodeURIComponent(query)}` : ""}` : null, 30_000);
  const add = useAction();
  const more = useAction();
  const [form, setForm] = useState({ name: "", email: "", phone: "" });
  // Later pages, fetched with the cursor of the page before ("Show more").
  const [extra, setExtra] = useState<Page<Contact>[]>([]);
  useEffect(() => setExtra([]), [query]);
  const next = extra.length ? extra[extra.length - 1].next : list.data?.next;
  const showMore = () =>
    more.run(async () => {
      const pg = await api<Page<Contact>>(
        `${base}/contacts?limit=100&before=${encodeURIComponent(next ?? "")}${query ? `&q=${encodeURIComponent(query)}` : ""}`,
      );
      setExtra((all) => [...all, pg]);
    });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.run(async () => {
      await api(`${base}/contacts`, { method: "POST", body: JSON.stringify(form) });
      setForm({ name: "", email: "", phone: "" });
      list.reload();
    });
  };

  if (!base) return null;
  const items = [...(list.data?.items ?? []), ...extra.flatMap((x) => x.items)];
  return (
    <>
      <PageHead eyebrow="CommAI" title="Contacts">
        Everyone who has written to the business, on any channel.
      </PageHead>
      <Card title="Contacts">
        <form
          role="search"
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            setQuery(q.trim());
          }}
        >
          <label>
            Search
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name, email or phone" />
          </label>
        </form>
        <ErrorNote error={list.error} />
        {list.data && items.length === 0 && (
          <div className="empty">
            <p>No contacts yet. They appear when someone writes in, or add one below.</p>
          </div>
        )}
        {items.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Email</th>
                  <th scope="col">Phone</th>
                  <th scope="col">Conversations</th>
                  <th scope="col">Since</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <Link to={`/commai/contacts/${c.id}`}>{c.name || "No name yet"}</Link>
                    </td>
                    <td data-label="Email">{c.email || "–"}</td>
                    <td data-label="Phone" className="mono">
                      {c.phone || "–"}
                    </td>
                    <td data-label="Conversations" className="mono">
                      {c.conversations ?? 0}
                    </td>
                    <td data-label="Since">{when(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {next && (
          <div className="actions">
            <button className="button secondary" disabled={more.busy} onClick={showMore}>
              Show more
            </button>
          </div>
        )}
        <ErrorNote error={more.error} />
      </Card>
      <Card title="Add a contact">
        <form className="form" onSubmit={submit}>
          <label>
            Name
            <input value={form.name} maxLength={200} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label>
            Email
            <input type="email" value={form.email} maxLength={255} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </label>
          <label>
            Phone (with country code)
            <input value={form.phone} maxLength={40} placeholder="+1 868 555 0100" onChange={(e) => setForm({ ...form, phone: e.target.value })} />
          </label>
          <div className="actions">
            <button className="button" disabled={add.busy}>
              Add contact
            </button>
          </div>
        </form>
        <ErrorNote error={add.error} />
      </Card>
    </>
  );
}

interface ContactFull extends Omit<Contact, "conversations"> {
  identities: { id: string; channel: string; address: string; verified: boolean; opted_out: boolean }[];
  conversations: { id: string; channel: string; state: string; subject: string; created_at: string; last_message_at: string | null }[];
}

interface Identity {
  id: string;
  channel: string;
  address: string;
  verified: boolean;
  opted_out: boolean;
}

interface History {
  calls: { kind: string; id: string; conversation_id?: string; direction: string; started_at: string; seconds: number | null; status?: string }[];
  open_requests: { kind: string; id: string; state: string; subject: string; conversation_id?: string | null; at: string }[];
  linked_records: { app: string; object_type: string; object_id: string; action: string; created_at: string; simulated: boolean }[];
  bookings: { id: string; app: string; action: string; start: string; end: string | null; conversation_id: string | null }[];
}

const REQUEST_KIND: Record<string, string> = { conversation: "Conversation", action: "Request", workflow: "Workflow" };

function ContactPage() {
  const base = useCommaiBase();
  const { id } = useParams();
  const c = useApi<ContactFull>(base && id ? `${base}/contacts/${id}` : null, 30_000);
  const h = useApi<History>(base && id ? `${base}/contacts/${id}/history` : null, 60_000);
  if (!c.data) return <ErrorNote error={c.error} />;
  const d = c.data;
  return (
    <>
      <PageHead eyebrow="Contact" title={d.name || d.email || d.phone || "Contact"}>
        <Link to="/commai/contacts">All contacts</Link>
      </PageHead>
      <Identities base={base!} contactId={d.id} identities={d.identities} onChanged={c.reload} />
      <Card title="Conversations">
        {d.conversations.length === 0 && <p className="muted">None yet.</p>}
        <ul>
          {d.conversations.map((v) => (
            <li key={v.id}>
              <Link to={`/commai/c/${v.id}`}>
                {CHANNEL_LABEL[v.channel] ?? v.channel}, {when(v.last_message_at ?? v.created_at)}
              </Link>{" "}
              · {STATE_LABEL[v.state] ?? v.state}
              {v.subject ? ` · ${v.subject}` : ""}
            </li>
          ))}
        </ul>
      </Card>
      <ErrorNote error={h.error} />
      {h.data && <HistoryCards h={h.data} />}
    </>
  );
}

function HistoryCards({ h }: { h: History }) {
  return (
    <>
      <Card title="Open requests">
        {h.open_requests.length === 0 ? (
          <p className="muted">Nothing open.</p>
        ) : (
          <ul>
            {h.open_requests.map((r) => (
              <li key={`${r.kind}-${r.id}`}>
                {REQUEST_KIND[r.kind] ?? r.kind}: {r.subject || "No subject"} · {r.state.replace(/_/g, " ")} · {when(r.at)}
                {r.conversation_id && (
                  <>
                    {" "}
                    · <Link to={`/commai/c/${r.conversation_id}`}>Open conversation</Link>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Calls">
        {h.calls.length === 0 ? (
          <p className="muted">No calls.</p>
        ) : (
          <ul>
            {h.calls.map((k) => (
              <li key={`${k.kind}-${k.id}`}>
                {k.kind === "ai_browser" ? "Website call with the AI assistant" : k.direction === "outbound" ? "Call out" : "Call in"}, {when(k.started_at)}
                {k.seconds != null && <span className="mono"> · {Math.round(k.seconds / 60) || "<1"} min</span>}
                {k.conversation_id && (
                  <>
                    {" "}
                    · <Link to={`/commai/c/${k.conversation_id}`}>Transcript</Link>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Bookings">
        {h.bookings.length === 0 ? (
          <p className="muted">No bookings.</p>
        ) : (
          <ul>
            {h.bookings.map((b) => (
              <li key={b.id}>
                {when(b.start)}
                {b.end ? ` to ${when(b.end)}` : ""} · {b.app}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Linked records">
        {h.linked_records.length === 0 ? (
          <p className="muted">No records in connected apps yet.</p>
        ) : (
          <ul>
            {h.linked_records.map((r) => (
              <li key={`${r.app}-${r.object_id}`}>
                {r.app}: {r.object_type} <span className="mono">{r.object_id}</span> · {when(r.created_at)}
                {r.simulated && <span className="pill"> Simulated</span>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function Identities({ base, contactId, identities, onChanged }: { base: string; contactId: string; identities: Identity[]; onChanged: () => void }) {
  const act = useAction();
  const [form, setForm] = useState({ channel: "email", address: "", verified: false, evidence: "" });
  const u = `${base}/contacts/${contactId}/identities`;
  const add = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(u, { method: "POST", body: JSON.stringify(form) });
      setForm({ channel: form.channel, address: "", verified: false, evidence: "" });
      onChanged();
    });
  };
  const verify = (i: Identity) => {
    const evidence = window.prompt(`How did you check they own ${i.address}?`, "");
    if (!evidence?.trim()) return;
    act.run(async () => {
      await api(`${u}/${i.id}/verify`, { method: "POST", body: JSON.stringify({ verified: true, evidence }) });
      onChanged();
    });
  };
  const remove = (i: Identity) => {
    if (!window.confirm(`Remove ${i.address} from this contact? Past conversations stay.`)) return;
    act.run(async () => {
      await api(`${u}/${i.id}`, { method: "DELETE" });
      onChanged();
    });
  };
  return (
    <Card title="How to reach them">
      <ul className="identity-list">
        {identities.map((i) => (
          <li key={i.id}>
            {CHANNEL_LABEL[i.channel] ?? i.channel}: <span className="mono">{i.address}</span>
            {i.verified ? " · Verified" : " · Not verified"}
            {i.opted_out && <span className="pill warn"> Opted out</span>}{" "}
            {!i.verified && (
              <button type="button" className="linklike" disabled={act.busy} aria-label={`Mark ${i.address} verified`} onClick={() => verify(i)}>
                Mark verified
              </button>
            )}{" "}
            <button type="button" className="linklike" disabled={act.busy} aria-label={`Remove ${i.address}`} onClick={() => remove(i)}>
              Remove
            </button>
          </li>
        ))}
      </ul>
      <form className="form" onSubmit={add} aria-label="Add an address">
        <label>
          Channel
          <select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>
            {["email", "sms", "whatsapp", "voice", "web"].map((ch) => (
              <option key={ch} value={ch}>
                {CHANNEL_LABEL[ch] ?? ch}
              </option>
            ))}
          </select>
        </label>
        <label>
          Address
          <input value={form.address} required maxLength={255} onChange={(e) => setForm({ ...form, address: e.target.value })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={form.verified} onChange={(e) => setForm({ ...form, verified: e.target.checked })} />
          We have checked they own it
        </label>
        {form.verified && (
          <label>
            How it was checked
            <input value={form.evidence} required maxLength={300} placeholder="Signed in on our app" onChange={(e) => setForm({ ...form, evidence: e.target.value })} />
          </label>
        )}
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Add address
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}
