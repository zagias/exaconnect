import { useState, type FormEvent } from "react";
import { api, useApi, type AiStatus } from "../api";
import { ErrorNote } from "../components";
import { PageHead } from "../ui";
import { useCustomer } from "../customer";

interface Turn {
  q: string;
  a: string | null;
  error?: string;
}

const SUGGESTIONS = [
  "Why did voice move today?",
  "Which carrier has been least reliable this week?",
  "Are we heading for burst charges this month?",
  "Is any site in Storm Mode, and why?",
];

/** Ask your network: questions answered from this organisation's own data. */
export default function Ask() {
  const { current } = useCustomer();
  const status = useApi<AiStatus>("/ai/status", 0);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async (q: string) => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setQuestion("");
    setTurns((t) => [...t, { q, a: null }]);
    try {
      const out = await api<{ answer: string }>("/ai/ask", {
        method: "POST",
        body: JSON.stringify({ question: q, customer_id: current?.id }),
      });
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, a: out.answer } : x)));
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

  const enabled = status.data?.ask_enabled;
  return (
    <>
      <PageHead eyebrow="Ask your network" title="Ask in plain English">
        Answers come from {current ? `${current.name}'s` : "your"} own sites, paths, decisions, events and metering. Check
        anything important against the screen it names.
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
              <div className="a">{t.a}</div>
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
            aria-label="Your question"
          />
          <button className="button" disabled={!enabled || busy || !question.trim()}>
            Ask
          </button>
        </form>
        {status.data?.model && <p className="muted small">Answered by {status.data.model}. Questions are logged in the audit log.</p>}
      </section>
    </>
  );
}
