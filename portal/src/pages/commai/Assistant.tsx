import { useState, type FormEvent } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import { CASE_STATUS, ReplyForm, Thread, type CaseFull } from "./SupportQueue";
import { PriceLines } from "./voice/ChangeBox";
import { SpeechButton } from "./voice/SpeechInput";
import type { PriceImpact } from "./voice/types";
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
  /** A phone change typed to the assistant: the same proposal and confirm step as Voice (ADR 0033). */
  voice_change?: {
    understood: boolean;
    message?: string;
    id?: string;
    summary?: string;
    scope?: "self" | "admin";
    price_impact?: PriceImpact;
  };
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
const SUGGESTED = [
  "Why has WhatsApp stopped sending?",
  "Why are bookings failing?",
  "Are any workflows failing?",
  "Have we hit a budget?",
  "Why are calls failing?",
  "Why does the AI agent hand over so much?",
  "Why can't people sign in?",
];

export default function Assistant() {
  const base = useCommaiBase();
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [openCase, setOpenCase] = useState<string | null>(null);
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
        <form onSubmit={submit} className="form" aria-describedby="assistant-hint">
          <label className="wide" htmlFor="assistant-question">
            Your question, or a phone change
          </label>
          <input
            id="assistant-question"
            className="wide"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            maxLength={1000}
            placeholder="Why has WhatsApp stopped sending?"
          />
          <div className="actions">
            <SpeechButton onText={setQ} label="Say your question instead of typing it" />
            <button className="button" disabled={act.busy} aria-busy={act.busy}>
              {act.busy ? "Checking…" : "Ask"}
            </button>
          </div>
        </form>
        <p id="assistant-hint" className="small muted">
          You can also ask for a phone change, such as “forward my calls to my mobile until 5”. You see the exact change first and nothing changes until you confirm.
        </p>
        <div className="auto-row" style={{ marginTop: 8 }} role="group" aria-label="Suggested questions">
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
      <div aria-live="polite">
        {answer && <AnswerCard key={answer.id ?? answer.question} base={base} answer={answer} onCase={cases.reload} />}
      </div>
      <Card title="Support cases">
        {cases.data && cases.data.length === 0 && <div className="empty">No support cases.</div>}
        <ul className="auto-evidence" aria-label="Your support cases">
          {(cases.data ?? []).map((c) => (
            <li key={c.id}>
              <button className="linklike" onClick={() => setOpenCase(openCase === c.id ? null : c.id)} aria-expanded={openCase === c.id}>
                <span className="mono">{c.reference}</span> · {c.subject}
              </button>{" "}
              · <span className={`pill ${CASE_STATUS[c.status]?.[0] ?? "shadow"}`}>{CASE_STATUS[c.status]?.[1] ?? c.status}</span> · {when(c.created_at)}
              {openCase === c.id && <CaseThread base={base} id={c.id} onChange={cases.reload} />}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function CaseThread({ base, id, onChange }: { base: string; id: string; onChange: () => void }) {
  const c = useApi<CaseFull>(`${base}/support-cases/${id}/thread`, 30_000);
  if (!c.data) return <ErrorNote error={c.error} />;
  return (
    <div style={{ margin: "8px 0 16px" }}>
      <Thread replies={c.data.replies} />
      <ReplyForm
        path={`${base}/support-cases/${id}/replies`}
        onSent={() => {
          c.reload();
          onChange();
        }}
      />
    </div>
  );
}

/** A phone change from the assistant: confirm or cancel at /voice/say, exactly as on the Voice screen. */
function VoiceChange({ base, vc }: { base: string; vc: NonNullable<Answer["voice_change"]> }) {
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();
  if (!vc.understood || !vc.id) return null;
  const confirm = () =>
    act.run(async () => {
      const p = vc.price_impact;
      const body = p?.changes_bill ? { accepted_price: { monthly_delta: p.monthly_delta, one_time: p.one_time } } : {};
      await api(`${base}/voice/say/${vc.id}/confirm`, { method: "POST", body: JSON.stringify(body) });
      setDone("Done: the change is live.");
    });
  const cancel = () =>
    act.run(async () => {
      await api(`${base}/voice/say/${vc.id}/cancel`, { method: "POST" });
      setDone("Cancelled. Nothing changed.");
    });
  return (
    <div className="voice-panel" role="group" aria-label="Proposed phone change">
      <p>
        <strong>{vc.summary}</strong>
      </p>
      {vc.price_impact && <PriceLines price={vc.price_impact} />}
      {done ? (
        <p className="pill ok" role="status">
          {done}
        </p>
      ) : (
        <div className="auto-row">
          <button className="button" onClick={confirm} disabled={act.busy}>
            Confirm the change
          </button>
          <button className="button secondary" onClick={cancel} disabled={act.busy}>
            Cancel
          </button>
        </div>
      )}
      <ErrorNote error={act.error} />
    </div>
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
      {answer.voice_change && <VoiceChange base={base} vc={answer.voice_change} />}
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
                    <button className="button small" disabled={act.busy} onClick={() => apply(f)} aria-label={`Approve and apply: ${f.label}`}>
                      Approve and apply
                    </button>
                    <button className="button small secondary" disabled={act.busy} onClick={() => dismiss(f)} aria-label={`Dismiss: ${f.label}`}>
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
      {msg && (
        <p className="small" role="status">
          {msg}
        </p>
      )}
      <div className="auto-row" style={{ marginTop: 12 }}>
        <button className="button secondary" disabled={act.busy || !answer.id || answer.source === "voice"} onClick={openCase}>
          Open a support case
        </button>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}
