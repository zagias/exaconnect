import { useState } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import "../../identity.css";
import { Card, useAction } from "../../../ui";

interface DataSettings {
  categories: { key: string; label: string }[];
  rules: Record<string, number | null>;
  hold: { hold_all: boolean; hold_reason: string };
  held_contacts: { id: string; name: string; email: string; legal_hold_reason: string }[];
  runs: { id: string; trigger: string; counts: Record<string, number>; held: Record<string, number>; started_at: string }[];
  requests: { id: string; kind: string; status: string; reason: string; contact_name: string | null; requested_by: string; created_at: string }[];
}
interface Export {
  id: string;
  status: "queued" | "ready" | "failed";
  include_notes: boolean;
  size: number;
  created_at: string;
  expires_at: string;
}
interface Processing {
  rows: { what: string; who: string; where: string; status: string }[];
  note: string;
}
interface Contact {
  id: string;
  name: string;
  email: string;
  phone: string;
}

function stamp(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

const STATUS_WORD: Record<string, string> = {
  live: "✓ Live",
  simulated: "Simulated",
  not_configured: "Not set up",
};

/** Retention, legal hold, subject requests, business export and where data is processed (ADR 0030). */
export default function Data({ base }: { base: string }) {
  const s = useApi<DataSettings>(`${base}/data`, 30_000);
  const exports = useApi<Export[]>(`${base}/data/exports`, 10_000);
  const proc = useApi<Processing>(`${base}/data/processing`, 0);
  const act = useAction();
  const [draft, setDraft] = useState<{ rules: Record<string, string>; hold_all: boolean; hold_reason: string } | null>(null);
  if (!s.data) return <ErrorNote error={s.error} />;
  const d = s.data;
  const f = draft ?? {
    rules: Object.fromEntries(Object.entries(d.rules).map(([k, v]) => [k, v == null ? "" : String(v)])),
    hold_all: d.hold.hold_all,
    hold_reason: d.hold.hold_reason,
  };
  const save = () =>
    act.run(async () => {
      await api(`${base}/data/retention`, {
        method: "PUT",
        body: JSON.stringify({
          rules: Object.fromEntries(Object.entries(f.rules).map(([k, v]) => [k, v ? Number(v) : null])),
          hold_all: f.hold_all,
          hold_reason: f.hold_reason,
        }),
      });
      setDraft(null);
      s.reload();
    });
  const post = (path: string, body?: unknown, reload: () => void = s.reload) =>
    act.run(async () => {
      await api(`${base}${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      reload();
    });

  return (
    <>
      <ErrorNote error={act.error} />
      <Card title="How long we keep your data">
        <p className="muted small">
          Once a day, anything older than its period is removed: message and call text is blanked (the conversation stays
          in your reports), notes and AI logs are deleted, and calls lose their recording. Contacts under legal hold are
          never touched. Leave a period empty to keep that data.
        </p>
        <div className="form">
          {d.categories.map((c) => (
            <label key={c.key}>
              {c.label} (days)
              <input
                type="number"
                min={1}
                placeholder="Keep"
                value={f.rules[c.key] ?? ""}
                onChange={(e) => setDraft({ ...f, rules: { ...f.rules, [c.key]: e.target.value } })}
              />
            </label>
          ))}
          <fieldset className="wide">
            <legend>Legal hold</legend>
            <label className="check">
              <input type="checkbox" checked={f.hold_all} onChange={(e) => setDraft({ ...f, hold_all: e.target.checked })} />
              Hold everything: nothing is deleted or anonymised until this is off
            </label>
            {f.hold_all && (
              <label>
                Reason
                <input value={f.hold_reason} maxLength={300} onChange={(e) => setDraft({ ...f, hold_reason: e.target.value })} />
              </label>
            )}
            {d.held_contacts.length > 0 && (
              <p className="small">
                Contacts on hold: {d.held_contacts.map((c) => c.name || c.email || c.id.slice(0, 8)).join(", ")}
              </p>
            )}
          </fieldset>
          <div className="actions wide">
            <button className="button" disabled={act.busy || !draft} onClick={save}>
              Save
            </button>
            <button className="button secondary" disabled={act.busy} onClick={() => post("/data/retention/run")}>
              Run now
            </button>
          </div>
        </div>
        {d.runs.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <caption className="muted small" style={{ textAlign: "left" }}>
                Recent runs
              </caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Removed</th>
                  <th scope="col">Kept for legal hold</th>
                </tr>
              </thead>
              <tbody>
                {d.runs.map((r) => (
                  <tr key={r.id}>
                    <td className="mono small">
                      {stamp(r.started_at)} {r.trigger === "manual" ? "(on request)" : ""}
                    </td>
                    <td data-label="Removed" className="small">
                      {Object.entries(r.counts).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(", ") || "Nothing"}
                    </td>
                    <td data-label="Held" className="small">
                      {r.held.all ? "Everything (legal hold)" : Object.entries(r.held).filter(([, v]) => v).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(", ") || "None"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <SubjectRequests base={base} busy={act.busy} run={act.run} onDone={s.reload} requests={d.requests} />
      <Card title="Export everything">
        <p className="muted small">A ZIP with one file per kind of record, for your organisation only. Keys and secrets are never included. Kept for seven days.</p>
        <div className="actions">
          <button className="button" disabled={act.busy} onClick={() => post("/data/exports", { include_notes: true }, exports.reload)}>
            Export with private notes
          </button>
          <button className="button secondary" disabled={act.busy} onClick={() => post("/data/exports", { include_notes: false }, exports.reload)}>
            Export without notes
          </button>
        </div>
        <ul>
          {(exports.data ?? []).map((x) => (
            <li key={x.id} className="small">
              {stamp(x.created_at)} {x.include_notes ? "with notes" : "without notes"}:{" "}
              {x.status === "ready" ? (
                <a href={`/api/v1${base}/data/exports/${x.id}/download`}>Download ({Math.max(1, Math.round(x.size / 1024))} KB)</a>
              ) : x.status === "failed" ? (
                "✕ failed"
              ) : (
                "being prepared…"
              )}
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Where your data is processed">
        <ErrorNote error={proc.error} />
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">What</th>
                <th scope="col">Who</th>
                <th scope="col">Where</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {(proc.data?.rows ?? []).map((r, i) => (
                <tr key={i}>
                  <td>{r.what}</td>
                  <td data-label="Who">{r.who}</td>
                  <td data-label="Where">{r.where}</td>
                  <td data-label="State">{STATUS_WORD[r.status] ?? r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {proc.data && <p className="muted small">{proc.data.note}</p>}
      </Card>
    </>
  );
}

function SubjectRequests({
  base,
  busy,
  run,
  onDone,
  requests,
}: {
  base: string;
  busy: boolean;
  run: (fn: () => Promise<void>) => Promise<void>;
  onDone: () => void;
  requests: DataSettings["requests"];
}) {
  const [q, setQ] = useState("");
  const found = useApi<{ items: Contact[] }>(q.length >= 2 ? `${base}/contacts?q=${encodeURIComponent(q)}&limit=10` : null, 0);
  const erase = (c: Contact, mode: "anonymise" | "delete") => {
    const what = mode === "delete" ? "Delete this contact and all their conversations" : "Anonymise this contact";
    if (!window.confirm(`${what}? This can't be undone.`)) return;
    run(async () => {
      await api(`${base}/data/contacts/${c.id}/erase`, { method: "POST", body: JSON.stringify({ mode, confirm: c.id }) });
      onDone();
    });
  };
  const hold = (c: Contact, on: boolean) =>
    run(async () => {
      const reason = on ? (window.prompt("Reason for the legal hold") ?? "") : "";
      await api(`${base}/data/contacts/${c.id}/hold`, { method: "PUT", body: JSON.stringify({ on, reason }) });
      onDone();
    });
  return (
    <Card title="Requests from a person">
      <p className="muted small">
        Export everything held about one person, or anonymise or delete them when they ask. A legal hold blocks anonymising
        and deleting.
      </p>
      <label>
        Find a contact
        <input value={q} placeholder="Name, email or phone" onChange={(e) => setQ(e.target.value)} />
      </label>
      <ul>
        {(found.data?.items ?? []).map((c) => (
          <li key={c.id}>
            {c.name || "No name"} <span className="muted small">{c.email || c.phone}</span>{" "}
            <a href={`/api/v1${base}/data/contacts/${c.id}/export?format=zip`}>Export</a>{" "}
            <button className="link" disabled={busy} onClick={() => hold(c, true)}>
              Hold
            </button>{" "}
            <button className="link" disabled={busy} onClick={() => hold(c, false)}>
              Release hold
            </button>{" "}
            <button className="link" disabled={busy} onClick={() => erase(c, "anonymise")}>
              Anonymise
            </button>{" "}
            <button className="link" disabled={busy} onClick={() => erase(c, "delete")}>
              Delete
            </button>
          </li>
        ))}
      </ul>
      {requests.length > 0 && (
        <ul className="small">
          {requests.map((r) => (
            <li key={r.id}>
              {stamp(r.created_at)} {r.kind} {r.contact_name ? `for ${r.contact_name}` : ""} by {r.requested_by.replace(/^user:/, "")}:{" "}
              {r.status === "refused" ? `✕ refused (${r.reason})` : "✓ done"}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
