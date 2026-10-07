import { useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, EmptyState, PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/api/approvals.py and impact.py (ADR 0033). */

interface Check {
  label: string;
  ok: boolean | null;
  detail: string;
}
interface Preview {
  summary?: string;
  changes?: { label: string; value: string }[];
  checks?: Check[];
  notes?: string[];
  reversible?: boolean;
  at?: string;
}
interface PendingAction {
  id: string;
  app: string;
  action: string;
  role: string;
  inputs: Record<string, unknown>;
  preview: Preview;
  proposed_by: string;
  own_proposal: boolean;
  conversation_id: string | null;
  conversation_subject: string | null;
  test: boolean;
  created_at: string;
}
interface PendingStep {
  id: string;
  workflow_id: string;
  workflow: string;
  conversation_id: string | null;
  prompt: string | null;
  since: string | null;
  expires_at: string | null;
}
interface Queue {
  actions: PendingAction[];
  workflow_steps: PendingStep[];
  count: number;
  next: string | null;
}

function who(actor: string): string {
  if (actor.startsWith("ai:")) return "The AI agent";
  if (actor.startsWith("workflow:")) return "A workflow";
  return actor.replace(/^user:/, "");
}

/** Everything waiting for a person: sensitive actions with what they will do, and workflow approval steps. */
export default function Approvals() {
  const base = useCommaiBase();
  const q = useApi<Queue>(base && `${base}/approvals`, 15_000);
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  const data = q.data;
  return (
    <>
      <PageHead title="Approvals">
        Sensitive actions and workflow steps wait here for a person. Each shows what it will change, worked out without changing anything.
      </PageHead>
      <ErrorNote error={q.error} />
      <p className="sr-only" role="status" aria-live="polite">
        {data ? `${data.count} waiting for approval` : ""}
      </p>
      <Card title="Actions" note={data && <span className="pill shadow">{data.actions.length} waiting</span>}>
        {data && data.actions.length === 0 && <EmptyState title="Nothing is waiting">Sensitive actions appear here when the AI, a workflow or a colleague proposes one.</EmptyState>}
        <ul className="auto-steps" aria-label="Actions waiting for approval">
          {(data?.actions ?? []).map((a) => (
            <ActionItem key={a.id} base={base} a={a} done={q.reload} />
          ))}
        </ul>
      </Card>
      <Card title="Workflow steps">
        {data && data.workflow_steps.length === 0 && <EmptyState title="No workflow is waiting">A workflow step that needs a person stops here until someone approves it.</EmptyState>}
        <ul className="auto-steps" aria-label="Workflow steps waiting for approval">
          {(data?.workflow_steps ?? []).map((s) => (
            <StepItem key={s.id} base={base} s={s} done={q.reload} />
          ))}
        </ul>
      </Card>
    </>
  );
}

function ActionItem({ base, a, done }: { base: string; a: PendingAction; done: () => void }) {
  const [preview, setPreview] = useState<Preview>(a.preview || {});
  const [reason, setReason] = useState("");
  const act = useAction();
  const call = (path: string, body?: unknown) =>
    act.run(async () => {
      await api(`${base}/actions/${a.id}/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      done();
    });
  const refresh = () =>
    act.run(async () => {
      setPreview(await api<Preview>(`${base}/actions/${a.id}/preview`, { method: "POST" }));
    });
  const headId = `act-${a.id}`;
  return (
    <li className="auto-step" aria-labelledby={headId}>
      <div className="auto-step-head">
        <h3 id={headId} className="small" style={{ margin: 0 }}>
          {preview.summary || `${a.app}: ${a.action.replace(/_/g, " ")}`}
        </h3>
        <span className="small muted">
          {who(a.proposed_by)} · {when(a.created_at)}
          {a.test && <span className="tag"> Test</span>}
        </span>
      </div>
      {a.conversation_id && (
        <p className="small">
          From the conversation <Link to={`/commai/c/${a.conversation_id}`}>{a.conversation_subject || "open it"}</Link>
        </p>
      )}
      {(preview.changes ?? []).length > 0 && (
        <dl className="auto-kv">
          {preview.changes!.map((c) => (
            <div key={c.label} style={{ display: "contents" }}>
              <dt>{c.label}</dt>
              <dd>{c.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {(preview.checks ?? []).length > 0 && (
        <>
          <h4 className="small">Checked before approving</h4>
          <ul className="auto-evidence">
            {preview.checks!.map((c, i) => (
              <li key={i}>
                <span className={`pill ${c.ok === true ? "ok" : c.ok === false ? "bad" : "shadow"}`}>
                  {c.ok === true ? "OK" : c.ok === false ? "Problem" : "Couldn't check"}
                </span>{" "}
                {c.label}
                {c.detail ? `: ${c.detail}` : ""}
              </li>
            ))}
          </ul>
        </>
      )}
      {(preview.notes ?? []).map((n, i) => (
        <p key={i} className="small muted">
          {n}
        </p>
      ))}
      <p className="small muted">
        {preview.reversible === false ? "This can't be undone once done." : ""} {preview.at ? `Worked out ${when(preview.at)}.` : ""}
      </p>
      <div className="auto-row">
        <button className="button small" disabled={act.busy || a.own_proposal} onClick={() => call("approve")}
          title={a.own_proposal ? "Someone else must approve what you proposed." : undefined}>
          Approve
        </button>
        <label className="small">
          <span className="sr-only">Reason for rejecting</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} placeholder="Reason (optional)" />
        </label>
        <button className="button small secondary" disabled={act.busy} onClick={() => call("reject", { reason })}>
          Reject
        </button>
        <button className="button small secondary" disabled={act.busy} onClick={refresh}>
          Check again
        </button>
      </div>
      {a.own_proposal && <p className="small muted">You proposed this, so someone else approves it.</p>}
      <ErrorNote error={act.error} />
    </li>
  );
}

function StepItem({ base, s, done }: { base: string; s: PendingStep; done: () => void }) {
  const [note, setNote] = useState("");
  const act = useAction();
  const decide = (approve: boolean) =>
    act.run(async () => {
      await api(`${base}/workflow-runs/${s.id}/${approve ? "approve" : "reject"}`, {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      done();
    });
  return (
    <li className="auto-step">
      <div className="auto-step-head">
        <h3 className="small" style={{ margin: 0 }}>
          <Link to={`/commai/workflows/${s.workflow_id}`}>{s.workflow}</Link>
        </h3>
        <span className="small muted">
          {s.since ? `Waiting since ${when(s.since)}` : ""} {s.expires_at ? `· gives up ${new Date(s.expires_at).toLocaleString("en-GB")}` : ""}
        </span>
      </div>
      <p>{s.prompt || "Approve this step?"}</p>
      <div className="auto-row">
        <label className="small">
          <span className="sr-only">Note</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} placeholder="Note (optional)" />
        </label>
        <button className="button small" disabled={act.busy} onClick={() => decide(true)}>
          Approve
        </button>
        <button className="button small secondary" disabled={act.busy} onClick={() => decide(false)}>
          Reject
        </button>
      </div>
      <ErrorNote error={act.error} />
    </li>
  );
}
