import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api";
import { ErrorNote } from "../../components";
import { useAuth } from "../../auth";
import type { Plan } from "./types";

const STATUS: Record<Plan["status"], string> = {
  draft: "Awaiting review", applied: "Applied", undone: "Undone", cancelled: "Not applied",
};

function valueText(value: unknown): string {
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value ?? "—");
}

/** Approval and execution are separate user actions, both bound to the review on screen. */
export function PlanReview({ plan, canOperate, onChange }: { plan: Plan; canOperate: boolean; onChange: (p: Plan) => void }) {
  const { user } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retry = useRef<{ revision: string; key: string } | null>(null);
  const draft = plan.status === "draft";
  const approved = !!plan.revision && plan.approval?.revision === plan.revision && plan.approval.approved_by === `user:${user?.email}`;
  const expired = !plan.expires_at || new Date(plan.expires_at).getTime() <= Date.now();
  const reviewable = !!plan.revision && !expired && plan.problems.length === 0;
  const orderId = plan.results.find((r) => r.order_id)?.order_id;

  const step = async (what: "approve" | "execute" | "cancel" | "undo") => {
    if (busy || !canOperate) return;
    if ((what === "approve" || what === "execute") && !reviewable) return;
    if (what === "execute" && (!approved || plan.can_execute !== true)) return;
    setBusy(true);
    setError(null);
    try {
      let body: Record<string, unknown> | undefined;
      if (what === "approve") body = { revision: plan.revision };
      if (what === "execute") {
        if (retry.current?.revision !== plan.revision) retry.current = { revision: plan.revision!, key: crypto.randomUUID() };
        body = { revision: plan.revision, idempotency_key: retry.current!.key };
      }
      const updated = await api<Plan>(`/assistant/plans/${plan.id}/${what}`, {
        method: "POST", ...(body ? { body: JSON.stringify(body) } : {}),
      });
      onChange(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    setBusy(true);
    setError(null);
    try { onChange(await api<Plan>(`/assistant/plans/${plan.id}`)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <section className="assistant-proposal" aria-label={`Proposed changes ${plan.id}`}>
      <div className="assistant-proposal-head">
        <h3>{draft ? "Review proposed changes" : "Change record"}</h3>
        <span className={`pill ${plan.status === "applied" ? "ok" : ""}`}>{draft && !canOperate ? "View only" : approved && draft ? "Revision approved" : STATUS[plan.status]}</span>
      </div>
      <ul className="assistant-changes">
        {(draft ? plan.summary : plan.results.map((r) => r.message)).map((line, i) => <li key={i}>{line}</li>)}
      </ul>
      {!!plan.actions?.length && (
        <details className="assistant-details">
          <summary>Configuration details</summary>
          {plan.actions.map((action, i) => (
            <dl className="assistant-values" key={i}>
              {Object.entries(action).map(([key, value]) => (
                <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{valueText(value)}</dd></div>
              ))}
            </dl>
          ))}
        </details>
      )}
      {plan.revision && <p className="small muted">Revision <span className="mono" title={plan.revision}>{plan.revision.slice(0, 12)}</span>{draft && plan.expires_at ? ` · Review expires ${new Date(plan.expires_at).toLocaleString("en-GB")}` : ""}</p>}
      {plan.problems.length > 0 && <ul className="assistant-blockers">{plan.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
      {draft && !reviewable && plan.problems.length === 0 && <p className="small muted">{expired ? "This proposal needs a fresh review before it can be approved." : "This proposal does not have a reviewed revision yet."}</p>}
      {plan.status === "undone" && <ul className="small muted">{plan.results.filter((r) => r.undone).map((r) => <li key={r.action}>{r.undone}</li>)}</ul>}
      <ErrorNote error={error} />
      <div className="assistant-actions">
        {canOperate && draft && (
          <>
            {approved ? (
              <button className="button" disabled={busy || !reviewable || plan.can_execute !== true} onClick={() => step("execute")}>Apply approved changes</button>
            ) : (
              <button className="button" disabled={busy || !reviewable} onClick={() => step("approve")}>Approve this revision</button>
            )}
            <button className="button secondary" disabled={busy} onClick={() => step("cancel")}>Don't apply</button>
            <button className="linklike" disabled={busy} onClick={refresh}>Refresh review</button>
          </>
        )}
        {canOperate && plan.status === "applied" && <button className="button secondary" disabled={busy || plan.problems.length > 0} onClick={() => step("undo")}>Undo</button>}
        {plan.status === "applied" && orderId && <Link className="button secondary" to="/order">Review the order</Link>}
      </div>
      {draft && <p className="small muted">{canOperate ? approved ? "Approval is recorded. Apply runs only this reviewed revision and checks your access again." : "Approval records your review. You choose when to apply the approved changes." : "You can review this proposal. Your account cannot apply, dismiss or undo changes."}</p>}
    </section>
  );
}
