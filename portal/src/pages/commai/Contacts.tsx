import { useState, type FormEvent } from "react";
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
  const [form, setForm] = useState({ name: "", email: "", phone: "" });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.run(async () => {
      await api(`${base}/contacts`, { method: "POST", body: JSON.stringify(form) });
      setForm({ name: "", email: "", phone: "" });
      list.reload();
    });
  };

  if (!base) return null;
  const items = list.data?.items ?? [];
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

function ContactPage() {
  const base = useCommaiBase();
  const { id } = useParams();
  const c = useApi<ContactFull>(base && id ? `${base}/contacts/${id}` : null, 30_000);
  if (!c.data) return <ErrorNote error={c.error} />;
  const d = c.data;
  return (
    <>
      <PageHead eyebrow="Contact" title={d.name || d.email || d.phone || "Contact"}>
        <Link to="/commai/contacts">All contacts</Link>
      </PageHead>
      <Card title="How to reach them">
        <ul>
          {d.identities.map((i) => (
            <li key={i.id}>
              {CHANNEL_LABEL[i.channel] ?? i.channel}: <span className="mono">{i.address}</span>
              {i.verified ? " · Verified" : " · Not verified"}
              {i.opted_out && <span className="pill warn"> Opted out</span>}
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Conversations">
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
    </>
  );
}
