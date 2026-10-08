import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, useApi, type AiStatus } from "../api";
import { ErrorNote } from "../components";
import { PageHead } from "../ui";
import { useCustomer } from "../customer";

interface PlanResult {
  action: number;
  message: string;
  undone?: string;
  order_id?: number;
}

interface Plan {
  id: number;
  status: "draft" | "applied" | "undone" | "cancelled";
  question: string;
  summary: string[];
  problems: string[];
  results: PlanResult[];
  created_at: string;
  applied_by: string | null;
  undone_by: string | null;
}

interface Turn {
  q: string;
  a: string | null;
  plan?: Plan | null;
  error?: string;
}

const SUGGESTIONS = [
  "Why did voice move today?",
  "Put Zoom, Teams and Webex in voice at every site",
  "Voice keeps breaking up in Kingston. Fix it",
  "Are we heading for burst charges this month?",
];

const STATUS: Record<Plan["status"], string> = {
  draft: "Waiting for you",
  applied: "Applied",
  undone: "Undone",
  cancelled: "Not applied",
};

/** One proposed set of changes: nothing happens until someone applies it, and applied changes can be undone. */
function PlanCard({ plan, onChange }: { plan: Plan; onChange: (p: Plan) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const step = async (what: "apply" | "undo" | "cancel") => {
    setBusy(true);
    setError(null);
    try {
      onChange(await api<Plan>(`/ai/plans/${plan.id}/${what}`, { method: "POST" }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const draft = plan.status === "draft";
  const orderId = plan.results.find((r) => r.order_id)?.order_id;
  return (
    <div className="card plan" aria-label="Proposed changes">
      <div className="plan-head">
        <span className="eyebrow">Proposed changes</span>
        <span className={plan.status === "applied" ? "pill ok" : draft ? "pill warn" : "pill"}>{STATUS[plan.status]}</span>
      </div>
      {draft && <div className="small muted">If you apply them, Connect will:</div>}
      <ul className="lines">
        {(draft ? plan.summary : plan.results.map((r) => r.message)).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
      {plan.status === "undone" && (
        <ul className="results small muted">
          {plan.results
            .filter((r) => r.undone)
            .map((r) => (
              <li key={r.action}>Undone: {r.undone}</li>
            ))}
        </ul>
      )}
      {plan.problems.length > 0 && (
        <ul className="problems">
          {plan.problems.map((p, i) => (
            <li key={i}>
              <span className="pill warn">Can't do yet:</span> {p}
            </li>
          ))}
        </ul>
      )}
      <ErrorNote error={error} />
      <div className="actions">
        {draft && (
          <>
            <button className="button" disabled={busy || plan.problems.length > 0} onClick={() => step("apply")}>
              Apply changes
            </button>
            <button className="button secondary" disabled={busy} onClick={() => step("cancel")}>
              Don't apply
            </button>
          </>
        )}
        {plan.status === "applied" && (
          <button className="button secondary" disabled={busy} onClick={() => step("undo")}>
            Undo
          </button>
        )}
        {plan.status === "applied" && orderId && (
          <Link className="button secondary" to="/order">
            Review the order
          </Link>
        )}
      </div>
      {draft && <p className="small muted">Every change is checked first, applied together or not at all, and logged.</p>}
    </div>
  );
}

/** Ask your network: questions answered, and changes proposed, from this organisation's own data. */
export default function Ask() {
  const { current } = useCustomer();
  const status = useApi<AiStatus>("/ai/status", 0);
  const recent = useApi<Plan[]>(current ? `/customers/${current.id}/assistant/plans?limit=10` : null, 0);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [params] = useSearchParams();
  // "Ask what to do" on an insight opens here with the question filled in, to send or edit.
  const [question, setQuestion] = useState(params.get("q")?.slice(0, 500) ?? "");
  const [busy, setBusy] = useState(false);

  const send = async (q: string) => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setQuestion("");
    const history = turns
      .filter((t) => t.a)
      .slice(-4)
      .map((t) => ({ q: t.q, a: t.a ?? "" }));
    setTurns((t) => [...t, { q, a: null }]);
    try {
      const out = await api<{ answer: string; plan: Plan | null }>("/ai/ask", {
        method: "POST",
        body: JSON.stringify({ question: q, customer_id: current?.id, history }),
      });
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, a: out.answer, plan: out.plan } : x)));
      if (out.plan) recent.reload();
    } catch (e) {
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error: (e as Error).message } : x)));
    } finally {
      setBusy(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(question);
  };
  const update = (p: Plan) => {
    setTurns((t) => t.map((x) => (x.plan?.id === p.id ? { ...x, plan: p } : x)));
    recent.reload();
  };

  const enabled = status.data?.ask_enabled;
  const shown = new Set(turns.map((t) => t.plan?.id));
  const earlier = (recent.data ?? []).filter((p) => !shown.has(p.id) && p.status !== "cancelled");
  return (
    <>
      <PageHead eyebrow="Ask your network" title="Ask, or tell it what to change">
        Answers come from {current ? `${current.name}'s` : "your"} own sites, paths, decisions, events and metering. Ask
        it to add a rule, switch Storm Mode or fix a problem, and it proposes the changes for you to apply.
      </PageHead>
      <ErrorNote error={status.error} />
      <section className="card chat" style={{ maxWidth: 820 }}>
        {status.data && !enabled && (
          <p className="muted">Ask your network is switched off on this controller: no AI service key is configured.</p>
        )}
        {turns.length === 0 && enabled && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="button secondary small" onClick={() => send(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className="chat">
            <div className="q">{t.q}</div>
            {t.a !== null ? (
              <>
                <div className="a">{t.a}</div>
                {t.plan && <PlanCard plan={t.plan} onChange={update} />}
              </>
            ) : t.error ? (
              <ErrorNote error={t.error} />
            ) : (
              <div className="a muted">Looking at your network…</div>
            )}
          </div>
        ))}
        <form onSubmit={submit}>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Why did voice move to Carrier B at 2 pm?"
            maxLength={500}
            disabled={!enabled}
            aria-label="Your question or request"
          />
          <button className="button" disabled={!enabled || busy || !question.trim()}>
            Send
          </button>
        </form>
        {status.data?.model && (
          <p className="muted small">
            Answered by {status.data.model}. Nothing changes until you apply it. Questions and changes are in the audit
            log.
          </p>
        )}
      </section>
      {earlier.length > 0 && (
        <section style={{ maxWidth: 820 }}>
          <h2>Earlier changes from Ask</h2>
          {earlier.map((p) => (
            <div key={p.id}>
              <p className="small muted quote">“{p.question}”</p>
              <PlanCard plan={p} onChange={update} />
            </div>
          ))}
        </section>
      )}
    </>
  );
}
