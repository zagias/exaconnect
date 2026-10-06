import { useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/automation/assistant.py (ADR 0020). */

interface Finding {
  area: string;
  status: "ok" | "problem" | "unknown";
  confidence: "confirmed" | "likely" | "unknown";
  summary: string;
  evidence: string[];
  fix: { id: string; label: string } | null;
}
interface Fix {
  id: string;
  fix_id: string;
  label: string;
  status: "proposed" | "applied" | "failed" | "rejected";
  appliable?: boolean;
  result?: { detail?: string; resolved?: boolean };
}
interface Answer {
  id: string | null;
  question: string;
  answer: string;
  confidence: "confirmed" | "likely" | "unknown";
  findings: Finding[];
  fixes: Fix[];
  source: string;
}
interface Case {
  id: string;
  reference: string;
  subject: string;
  status: string;
  created_at: string;
}

const CONFIDENCE: Record<Answer["confidence"], [string, string]> = {
  confirmed: ["bad", "Cause confirmed"],
  likely: ["warn", "Likely cause"],
  unknown: ["shadow", "Cause unknown"],
};
const SUGGESTED = ["Why has WhatsApp stopped sending?", "Why are bookings failing?", "Are any workflows failing?", "Have we hit a usage limit?"];

export default function Assistant() {
  const base = useCommaiBase();
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const cases = useApi<Case[]>(base && `${base}/support-cases`, 0);
  const act = useAction();
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  const ask = (question: string) =>
    act.run(async () => {
      setAnswer(await api<Answer>(`${base}/assistant/ask`, { method: "POST", body: JSON.stringify({ question }) }));
    });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (q.trim().length >= 2) ask(q.trim());
  };
  return (
    <>
      <PageHead eyebrow="CommAI" title="Assistant">
        Ask about your set-up. The assistant checks your real configuration, shows the evidence, and suggests fixes that apply only when you approve them.
      </PageHead>
      <Card title="Ask">
        <form onSubmit={submit} className="form">
          <label className="wide">
            Your question
            <input value={q} onChange={(e) => setQ(e.target.value)} maxLength={1000} placeholder="Why has WhatsApp stopped sending?" />
          </label>
          <div className="actions">
            <button className="button" disabled={act.busy}>
              {act.busy ? "Checking…" : "Ask"}
            </button>
          </div>
        </form>
        <div className="auto-row" style={{ marginTop: 8 }}>
          {SUGGESTED.map((s) => (
            <button key={s} className="button small secondary" disabled={act.busy} onClick={() => {
              setQ(s);
              ask(s);
            }}>
              {s}
            </button>
          ))}
        </div>
        <p className="auto-secret-note">Never paste passwords or API keys here. Sign-ins and keys go through the Integrations and Channels screens.</p>
        <ErrorNote error={act.error} />
      </Card>
      {answer && <AnswerCard base={base} answer={answer} onCase={cases.reload} />}
      <Card title="Support cases">
        {cases.data && cases.data.length === 0 && <div className="empty">No support cases.</div>}
        <ul className="auto-evidence">
          {(cases.data ?? []).map((c) => (
            <li key={c.id}>
              <span className="mono">{c.reference}</span> · {c.subject} · {c.status} · {when(c.created_at)}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function AnswerCard({ base, answer, onCase }: { base: string; answer: Answer; onCase: () => void }) {
  const [fixes, setFixes] = useState<Fix[]>(answer.fixes);
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const [cls, word] = CONFIDENCE[answer.confidence];
  const apply = (f: Fix) =>
    act.run(async () => {
      const r = await api<Fix & { message: string }>(`${base}/assistant/fixes/${f.id}/apply`, { method: "POST" });
      setFixes((x) => x.map((y) => (y.id === f.id ? r : y)));
      setMsg(r.message);
    });
  const dismiss = (f: Fix) =>
    act.run(async () => {
      const r = await api<Fix>(`${base}/assistant/fixes/${f.id}/reject`, { method: "POST" });
      setFixes((x) => x.map((y) => (y.id === f.id ? r : y)));
    });
  const openCase = () =>
    act.run(async () => {
      const c = await api<Case>(`${base}/support-cases`, {
        method: "POST",
        body: JSON.stringify({ subject: answer.question.slice(0, 200), question: answer.question, answer_id: answer.id }),
      });
      setMsg(`Support case ${c.reference} opened with your configuration, the checks and redacted errors attached.`);
      onCase();
    });
  const problems = answer.findings.filter((f) => f.status === "problem");
  return (
    <Card title="Answer" note={<span className={`pill ${cls}`}>{word}</span>}>
      <p className="auto-answer">{answer.answer}</p>
      {problems.length > 0 && (
        <>
          <h3 className="small">Evidence</h3>
          {problems.map((f, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <strong>{f.summary}</strong> <span className="small muted">({f.confidence})</span>
              <ul className="auto-evidence">
                {f.evidence.map((e, k) => (
                  <li key={k}>{e}</li>
                ))}
              </ul>
            </div>
          ))}
        </>
      )}
      {fixes.length > 0 && (
        <>
          <h3 className="small">Suggested fixes</h3>
          <ul className="auto-evidence">
            {fixes.map((f) => (
              <li key={f.id}>
                {f.label}{" "}
                {f.status === "proposed" ? (
                  <span className="auto-row" style={{ display: "inline-flex" }}>
                    <button className="button small" disabled={act.busy} onClick={() => apply(f)}>
                      Approve and apply
                    </button>
                    <button className="button small secondary" disabled={act.busy} onClick={() => dismiss(f)}>
                      Dismiss
                    </button>
                  </span>
                ) : (
                  <span className={`pill ${f.status === "applied" ? (f.result?.resolved ? "ok" : "warn") : f.status === "failed" ? "bad" : "shadow"}`}>
                    {f.status === "applied" ? (f.result?.resolved ? "Fixed" : "Applied, still failing") : f.status === "rejected" && f.appliable === false ? "Do this on its screen" : f.status}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {msg && <p className="small">{msg}</p>}
      <div className="auto-row" style={{ marginTop: 12 }}>
        <button className="button secondary" disabled={act.busy || !answer.id} onClick={openCase}>
          Open a support case
        </button>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}
