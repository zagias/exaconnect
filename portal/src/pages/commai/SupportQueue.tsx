import { useId, useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, EmptyState, PageHead, useAction } from "../../ui";
import { when } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/automation/support.py (ADR 0033). */

export interface Reply {
  id: number;
  author: string;
  from_exacarib: boolean;
  internal: boolean;
  body: string;
  created_at: string;
}
export interface CaseFull {
  id: string;
  customer_id: string;
  reference: string;
  subject: string;
  question: string;
  status: string;
  priority: string;
  assignee: string;
  created_by: string;
  created_at: string;
  diagnostics: { area: string; status: string; summary: string }[];
  errors: { source: string; at: string; error: string }[];
  correlation_ids: string[];
  replies: Reply[];
}
interface QueueRow {
  id: string;
  business: string;
  reference: string;
  subject: string;
  status: string;
  priority: string;
  assignee: string;
  created_at: string;
  replies: number;
  last_customer_reply: string | null;
}

export const CASE_STATUS: Record<string, [string, string]> = {
  open: ["warn", "Open"],
  in_progress: ["shadow", "In progress"],
  waiting_on_customer: ["shadow", "Waiting on the business"],
  closed: ["ok", "Closed"],
};

/** ExaCarib's support queue: every business's cases, with assignment, status, replies and internal notes. */
export default function SupportQueue() {
  const { user } = useAuth();
  const [status, setStatus] = useState("active");
  const [mine, setMine] = useState(false);
  const [q, setQ] = useState("");
  const params = new URLSearchParams({ status });
  if (mine && user?.email) params.set("assignee", user.email);
  if (q.trim()) params.set("q", q.trim());
  const list = useApi<QueueRow[]>(user?.role === "admin" ? `/commai/exacarib/support/cases?${params}` : null, 30_000);
  const [open, setOpen] = useState<string | null>(null);
  if (user?.role !== "admin") return <p className="muted">The support queue is for ExaCarib staff.</p>;
  return (
    <>
      <PageHead title="Support queue">
        Cases businesses opened from the assistant, with their configuration, checks and redacted errors attached.
      </PageHead>
      <Card title="Cases">
        <form className="auto-row" onSubmit={(e) => e.preventDefault()} aria-label="Filter cases">
          <label className="small">
            Status{" "}
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="active">Not closed</option>
              <option value="open">Open</option>
              <option value="in_progress">In progress</option>
              <option value="waiting_on_customer">Waiting on the business</option>
              <option value="closed">Closed</option>
            </select>
          </label>
          <label className="small">
            <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> Assigned to me
          </label>
          <label className="small">
            <span className="sr-only">Search by reference or subject</span>
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Reference or subject" />
          </label>
        </form>
        <ErrorNote error={list.error} />
        {list.data && list.data.length === 0 && <EmptyState title="No cases match">Try another status, or clear the search.</EmptyState>}
        {list.data && list.data.length > 0 && (
          <table className="paths dt stack">
            <caption className="sr-only">Support cases</caption>
            <thead>
              <tr>
                <th scope="col">Case</th>
                <th scope="col">Business</th>
                <th scope="col">Status</th>
                <th scope="col">Priority</th>
                <th scope="col">Assigned to</th>
                <th scope="col">Opened</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((c) => (
                <tr key={c.id} aria-selected={open === c.id}>
                  <td data-label="Case">
                    <button className="linklike" onClick={() => setOpen(c.id)}>
                      <span className="mono">{c.reference}</span> {c.subject}
                    </button>
                  </td>
                  <td data-label="Business">{c.business}</td>
                  <td data-label="Status">
                    <span className={`pill ${CASE_STATUS[c.status]?.[0] ?? "shadow"}`}>{CASE_STATUS[c.status]?.[1] ?? c.status}</span>
                  </td>
                  <td data-label="Priority">{c.priority}</td>
                  <td data-label="Assigned to">{c.assignee || "Nobody"}</td>
                  <td data-label="Opened">{when(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {open && <QueueCase id={open} onChange={list.reload} />}
    </>
  );
}

function QueueCase({ id, onChange }: { id: string; onChange: () => void }) {
  const { user } = useAuth();
  const c = useApi<CaseFull>(`/commai/exacarib/support/cases/${id}`, 0);
  const act = useAction();
  const patch = (body: Record<string, string>) =>
    act.run(async () => {
      await api(`/commai/exacarib/support/cases/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      c.reload();
      onChange();
    });
  if (!c.data) return <ErrorNote error={c.error} />;
  const d = c.data;
  return (
    <Card title={`${d.reference}: ${d.subject}`}>
      <div className="auto-row">
        <label className="small">
          Status{" "}
          <select value={d.status} disabled={act.busy} onChange={(e) => patch({ status: e.target.value })}>
            {Object.entries(CASE_STATUS).map(([k, [, label]]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="small">
          Priority{" "}
          <select value={d.priority} disabled={act.busy} onChange={(e) => patch({ priority: e.target.value })}>
            {["low", "normal", "high", "urgent"].map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>
        <span className="small">Assigned to {d.assignee || "nobody"}</span>
        {user?.email && d.assignee !== user.email && (
          <button className="button small secondary" disabled={act.busy} onClick={() => patch({ assignee: user.email })}>
            Assign to me
          </button>
        )}
      </div>
      {d.question && <p>{d.question}</p>}
      <details>
        <summary>Checks, errors and correlation ids</summary>
        <ul className="auto-evidence">
          {d.diagnostics.filter((f) => f.status === "problem").map((f, i) => (
            <li key={`d${i}`}>
              {f.area}: {f.summary}
            </li>
          ))}
          {d.errors.map((e, i) => (
            <li key={`e${i}`}>
              {e.source} at {e.at}: {e.error}
            </li>
          ))}
        </ul>
        <p className="small mono">{d.correlation_ids.join(" ")}</p>
      </details>
      <Thread replies={d.replies} />
      <ReplyForm path={`/commai/exacarib/support/cases/${id}/replies`} exacarib onSent={() => { c.reload(); onChange(); }} />
      <ErrorNote error={act.error} />
    </Card>
  );
}

export function Thread({ replies }: { replies: Reply[] }) {
  if (replies.length === 0) return <p className="small muted">No replies yet.</p>;
  return (
    <ol className="auto-log" aria-label="Replies">
      {replies.map((r) => (
        <li key={r.id}>
          <span className="small muted">
            {r.from_exacarib ? "ExaCarib" : r.author.replace(/^user:/, "")} · {when(r.created_at)}
            {r.internal && <span className="tag"> Internal note</span>}
          </span>
          <span style={{ whiteSpace: "pre-wrap" }}>{r.body}</span>
        </li>
      ))}
    </ol>
  );
}

export function ReplyForm({ path, exacarib = false, onSent }: { path: string; exacarib?: boolean; onSent: () => void }) {
  const [body, setBody] = useState("");
  const [internal, setInternal] = useState(false);
  const [status, setStatus] = useState("");
  const act = useAction();
  const send = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(path, {
        method: "POST",
        body: JSON.stringify(exacarib ? { body, internal, status: status || null } : { body }),
      });
      setBody("");
      onSent();
    });
  };
  const fid = useId();
  return (
    <form onSubmit={send} className="auto-fields" style={{ marginTop: 12 }}>
      <label className="wide" htmlFor={fid}>
        {exacarib ? "Reply or note" : "Your reply"}
      </label>
      <textarea id={fid} className="auto-text wide" value={body} onChange={(e) => setBody(e.target.value)} maxLength={4000} required />
      {exacarib && (
        <>
          <label>
            <span>
              <input type="checkbox" checked={internal} onChange={(e) => setInternal(e.target.checked)} /> Internal note (the business never sees it)
            </span>
          </label>
          <label>
            Then set status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Leave as it is</option>
              {Object.entries(CASE_STATUS).map(([k, [, label]]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy || !body.trim()}>
          {exacarib && internal ? "Add note" : "Send reply"}
        </button>
      </div>
      <ErrorNote error={act.error} />
    </form>
  );
}
