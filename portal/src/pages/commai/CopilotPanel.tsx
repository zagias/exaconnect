import { useState } from "react";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { useAction } from "../../ui";
import { when } from "./lib";
import "./ai.css";

interface SourceRef {
  chunk_id: number;
  source_id: string;
  title: string;
  source_url: string;
}

interface AiRun {
  id: string;
  role: string;
  task: string;
  model: string;
  outcome: string;
  reason: string;
  sources: SourceRef[];
  tool_calls: { tool: string; status: string }[];
  created_at: string;
}

interface Result {
  task: string;
  model: string;
  reason: string;
  summary?: string;
  draft?: string;
  sources?: SourceRef[];
  items?: string[];
  text?: string;
  translated?: boolean;
  original?: string;
}

interface Msg {
  direction: "in" | "out";
  body: string;
}

type Task = "summary" | "draft" | "missing" | "next_steps" | "translate";

const TASKS: { task: Task; label: string }[] = [
  { task: "summary", label: "Summarise" },
  { task: "draft", label: "Draft a reply" },
  { task: "missing", label: "Missing details" },
  { task: "next_steps", label: "Next steps" },
  { task: "translate", label: "Translate latest" },
];

const OUTCOME: Record<string, { word: string; cls: string }> = {
  replied: { word: "Answered", cls: "ok" },
  proposed: { word: "Proposed an action", cls: "ok" },
  escalated: { word: "Handed over", cls: "warn" },
  failed: { word: "Failed", cls: "bad" },
};

function Sources({ sources }: { sources: SourceRef[] }) {
  if (!sources.length) return null;
  return (
    <p className="small copilot-sources">
      Sources:{" "}
      {sources.map((s, i) => (
        <span key={s.chunk_id}>
          {i > 0 && ", "}
          {s.source_url ? (
            <a href={s.source_url} target="_blank" rel="noreferrer">
              {s.title}
            </a>
          ) : (
            s.title
          )}
        </span>
      ))}
    </p>
  );
}

/** The employee copilot beside a conversation (ADR 0019). It helps; a person sends. */
export default function CopilotPanel({
  base,
  conversationId,
  onDraft,
}: {
  base: string;
  conversationId: string;
  onDraft: (text: string) => void;
}) {
  const runs = useApi<AiRun[]>(`${base}/ai/conversations/${conversationId}/runs`, 15_000);
  const act = useAction();
  const [result, setResult] = useState<Result | null>(null);

  const run = (task: Task) =>
    act.run(async () => {
      setResult(null);
      const body: { text?: string } = {};
      if (task === "translate") {
        const msgs = await api<Msg[]>(`${base}/conversations/${conversationId}/messages`);
        const last = [...msgs].reverse().find((m) => m.direction === "in");
        if (!last) throw new Error("There is no customer message to translate yet.");
        body.text = last.body;
      }
      setResult(
        await api<Result>(`${base}/ai/conversations/${conversationId}/copilot/${task}`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      );
      runs.reload();
    });

  const answers = (runs.data ?? []).filter((r) => r.role === "customer_agent");

  return (
    <aside className="copilot card" aria-labelledby={`copilot-${conversationId}`}>
      <h2 id={`copilot-${conversationId}`} className="copilot-title">
        Copilot
      </h2>
      <p className="muted small">It suggests; nothing is sent until you send it.</p>
      <div className="copilot-buttons" role="group" aria-label="Copilot tasks">
        {TASKS.map((t) => (
          <button key={t.task} type="button" className="button secondary small" disabled={act.busy} onClick={() => run(t.task)}>
            {t.label}
          </button>
        ))}
      </div>
      <ErrorNote error={act.error} />
      {act.busy && <p className="muted small">Working…</p>}
      {result && (
        <div className="copilot-result" aria-live="polite">
          {result.summary && <p>{result.summary}</p>}
          {result.draft !== undefined && (
            <>
              <p className="copilot-draft">{result.draft}</p>
              <Sources sources={result.sources ?? []} />
              {result.draft && (
                <button type="button" className="button small" onClick={() => onDraft(result.draft ?? "")}>
                  Use this draft
                </button>
              )}
            </>
          )}
          {result.items && (
            <ul className="copilot-items">
              {result.items.length ? result.items.map((x) => <li key={x}>{x}</li>) : <li>Nothing to flag.</li>}
            </ul>
          )}
          {result.task === "translate" && (
            <>
              <p className="copilot-draft">{result.text}</p>
              {!result.translated && <p className="pill warn small">{result.reason}</p>}
            </>
          )}
          {result.model === "simulated" && <p className="muted small">Simulated AI: no AI service is configured.</p>}
        </div>
      )}
      {answers.length > 0 && (
        <details className="copilot-runs">
          <summary>What the AI agent did ({answers.length})</summary>
          <ol>
            {answers.map((r) => {
              const o = OUTCOME[r.outcome] ?? { word: r.outcome, cls: "off" };
              return (
                <li key={r.id}>
                  <span className={`pill small ${o.cls}`}>{o.word}</span> <span className="muted small">{when(r.created_at)}</span>
                  {r.reason && <p className="small">{r.reason}</p>}
                  {r.tool_calls.map((c) => (
                    <p key={c.tool} className="small">
                      {c.tool}: {c.status.replace(/_/g, " ")}
                    </p>
                  ))}
                  <Sources sources={r.sources} />
                </li>
              );
            })}
          </ol>
        </details>
      )}
    </aside>
  );
}
