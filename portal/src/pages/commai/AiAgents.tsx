import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, RowActions, Tabs, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./ai.css";

/** AI agents (ADR 0019): the customer AI agent's profile and mode, the tools
 * each AI role may use, approved knowledge, knowledge gaps and a browser call. */
export default function AiAgents() {
  const base = useCommaiBase();
  if (!base) return null;
  return (
    <>
      <PageHead eyebrow="CommAI" title="AI agents">
        How the AI answers your customers, what it may do, and what it knows.
      </PageHead>
      <Tabs label="AI agent sections">
        <NavLink to="/commai/ai" end>
          Agent profile and mode
        </NavLink>
        <NavLink to="/commai/ai/tools">Tools it may use</NavLink>
        <NavLink to="/commai/ai/knowledge">Knowledge</NavLink>
        <NavLink to="/commai/ai/gaps">Knowledge gaps</NavLink>
        <NavLink to="/commai/ai/call">Try a browser call</NavLink>
      </Tabs>
      <Routes>
        <Route path="/" element={<Profile base={base} />} />
        <Route path="/tools" element={<Tools base={base} />} />
        <Route path="/knowledge" element={<Knowledge base={base} />} />
        <Route path="/gaps" element={<Gaps base={base} />} />
        <Route path="/call" element={<TryCall base={base} />} />
      </Routes>
    </>
  );
}

// ---- types -------------------------------------------------------------------------

interface AiStatus {
  model: string;
  live: boolean;
  note: string;
  ai_available: boolean;
  enabled: boolean;
  mode: Mode;
  speech: { browser: boolean; server: boolean; note: string };
}

type Mode = "ai_first" | "human_first" | "human_only";

interface AiProfile {
  enabled: boolean;
  name: string;
  tone: string;
  greeting: string;
  business_language: string;
  languages: string[];
  memory: boolean;
  holding_reply: string;
  escalation: { keywords: string[]; max_ai_replies: number; on_missing_knowledge: "escalate" | "answer" };
}

interface RoleInfo {
  role: string;
  label: string;
  serves: string;
  may_read: string;
  may_do: string;
  tools: string[];
  usable: string[];
}

interface CatalogueTool {
  tool: string;
  app: string;
  app_label: string;
  label: string;
  kind: string;
  sensitive: boolean;
  roles: string[];
}

interface Source {
  id: string;
  title: string;
  source_url: string;
  approved: boolean;
  approved_by: string;
  created_by: string;
  updated_at: string;
  characters: number;
  chunks: number;
}

interface SourceDetail extends Omit<Source, "chunks"> {
  body: string;
}

interface Hit {
  chunk_id: number;
  title: string;
  source_url: string;
  text: string;
  coverage: number;
}

interface Lookup {
  hits: Hit[];
  contradictory: boolean;
  contradiction: string;
}

interface Gap {
  id: string;
  question: string;
  reason: "missing" | "contradictory";
  detail: string;
  conversation_id: string | null;
  times: number;
  status: "open" | "resolved";
  last_seen: string;
}

const LANGUAGES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  fr: "French",
  pt: "Portuguese",
  nl: "Dutch",
  ht: "Haitian Creole",
  pap: "Papiamento",
};

const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: "ai_first", label: "AI first", hint: "The AI agent answers; it hands over to a person when it should." },
  { value: "human_first", label: "People first", hint: "People answer; the AI helps staff with the copilot." },
  { value: "human_only", label: "People only", hint: "No AI replies to customers." },
];

// ---- profile and mode -----------------------------------------------------------------

function ModelStatus({ s }: { s: AiStatus }) {
  return (
    <div className="ai-status" role="status">
      <span className={`pill ${s.live ? "ok" : "warn"}`}>{s.live ? `AI service: ${s.model}` : "Simulated AI"}</span>
      <span className={`pill ${s.enabled ? "ok" : "off"}`}>{s.enabled ? "Agent switched on" : "Agent switched off"}</span>
      {s.note && <p className="muted small">{s.note}</p>}
    </div>
  );
}

function Profile({ base }: { base: string }) {
  const status = useApi<AiStatus>(`${base}/ai/status`, 0);
  const prof = useApi<AiProfile>(`${base}/ai/profile`, 0);
  const save = useAction();
  const saveMode = useAction();
  const [draft, setDraft] = useState<AiProfile | null>(null);
  const [keywords, setKeywords] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const d = draft ?? prof.data;
  if (!d) return <ErrorNote error={prof.error} />;
  const set = (patch: Partial<AiProfile>) => {
    setSaved(false);
    setDraft({ ...d, ...patch });
  };
  const kw = keywords ?? d.escalation.keywords.join(", ");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.run(async () => {
      const body = {
        ...d,
        escalation: {
          ...d.escalation,
          keywords: kw
            .split(",")
            .map((k) => k.trim())
            .filter(Boolean),
        },
      };
      await api(`${base}/ai/profile`, { method: "PUT", body: JSON.stringify(body) });
      setDraft(null);
      setKeywords(null);
      setSaved(true);
      prof.reload();
      status.reload();
    });
  };

  const setMode = (mode: Mode) =>
    saveMode.run(async () => {
      await api(`${base}/settings`, { method: "PATCH", body: JSON.stringify({ mode }) });
      status.reload();
    });

  const toggleLang = (code: string, on: boolean) =>
    set({ languages: on ? [...new Set([...d.languages, code])] : d.languages.filter((x) => x !== code) });

  return (
    <>
      <Card title="Who answers first">
        {status.data && <ModelStatus s={status.data} />}
        <fieldset className="ai-modes" disabled={saveMode.busy}>
          <legend className="sr-only">New conversations go to</legend>
          {MODES.map((m) => (
            <label key={m.value} className="ai-mode">
              <input type="radio" name="mode" checked={status.data?.mode === m.value} onChange={() => setMode(m.value)} />
              <span>
                <strong>{m.label}</strong>
                <span className="muted small"> {m.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <ErrorNote error={saveMode.error ?? status.error} />
      </Card>

      <Card title="Agent profile">
        <form className="form" onSubmit={submit}>
          <label className="check wide">
            <input type="checkbox" checked={d.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
            The AI agent answers customers (when a conversation is AI first)
          </label>
          <label>
            Name customers see
            <input value={d.name} maxLength={60} required onChange={(e) => set({ name: e.target.value })} />
          </label>
          <label>
            Tone
            <input value={d.tone} maxLength={200} onChange={(e) => set({ tone: e.target.value })} />
          </label>
          <label>
            Business language
            <select value={d.business_language} onChange={(e) => set({ business_language: e.target.value })}>
              {Object.entries(LANGUAGES).map(([code, name]) => (
                <option key={code} value={code}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="wide">
            Greeting on its first reply (optional)
            <textarea rows={2} maxLength={500} value={d.greeting} onChange={(e) => set({ greeting: e.target.value })} />
          </label>
          <label className="wide">
            Holding reply when it hands over
            <textarea
              rows={2}
              maxLength={500}
              required
              value={d.holding_reply}
              onChange={(e) => set({ holding_reply: e.target.value })}
            />
          </label>
          <fieldset className="wide ai-langs">
            <legend>Languages it answers in</legend>
            {Object.entries(LANGUAGES).map(([code, name]) => (
              <label key={code} className="check">
                <input
                  type="checkbox"
                  checked={d.languages.includes(code) || code === d.business_language}
                  disabled={code === d.business_language}
                  onChange={(e) => toggleLang(code, e.target.checked)}
                />
                {name}
              </label>
            ))}
            <p className="muted small">
              Without an AI service it can't translate: it says so in the customer's language and answers in the business
              language. Other languages go straight to a person.
            </p>
          </fieldset>
          <label className="check wide">
            <input type="checkbox" checked={d.memory} onChange={(e) => set({ memory: e.target.checked })} />
            Remember facts about customers whose identity is verified (staff can see and delete them)
          </label>
          <fieldset className="wide">
            <legend>When to hand over to a person</legend>
            <div className="form">
              <label className="wide">
                Words that always go to a person (comma separated)
                <input value={kw} onChange={(e) => setKeywords(e.target.value)} />
              </label>
              <label>
                After this many AI replies
                <input
                  type="number"
                  min={0}
                  max={50}
                  value={d.escalation.max_ai_replies}
                  onChange={(e) => set({ escalation: { ...d.escalation, max_ai_replies: Number(e.target.value) } })}
                />
              </label>
              <label>
                When knowledge doesn't cover it
                <select
                  value={d.escalation.on_missing_knowledge}
                  onChange={(e) =>
                    set({ escalation: { ...d.escalation, on_missing_knowledge: e.target.value as "escalate" | "answer" } })
                  }
                >
                  <option value="escalate">Hand over to a person</option>
                  <option value="answer">Let the AI answer anyway</option>
                </select>
              </label>
            </div>
          </fieldset>
          <div className="actions wide">
            <button className="button" disabled={save.busy}>
              {save.busy ? "Saving…" : "Save profile"}
            </button>
            {saved && (
              <span className="pill ok" role="status">
                Saved
              </span>
            )}
          </div>
        </form>
        <ErrorNote error={save.error} />
      </Card>
    </>
  );
}

// ---- tools ----------------------------------------------------------------------------

function Tools({ base }: { base: string }) {
  const { data, error, reload } = useApi<{ roles: RoleInfo[]; catalogue: CatalogueTool[] }>(`${base}/ai/roles`, 0);
  if (!data) return <ErrorNote error={error} />;
  return (
    <>
      <p className="muted ai-intro">
        Each AI role may use only the tools ticked here. Anything else is refused by the action service, whatever the AI
        asks for. A tool also has to be switched on in <Link to="/commai/integrations">Integrations</Link>. Sensitive
        actions always wait for a person to approve.
      </p>
      {data.roles.map((r) => (
        <RoleTools key={r.role} base={base} role={r} catalogue={data.catalogue.filter((t) => t.roles.includes(r.role))} onSaved={reload} />
      ))}
    </>
  );
}

function RoleTools({
  base,
  role,
  catalogue,
  onSaved,
}: {
  base: string;
  role: RoleInfo;
  catalogue: CatalogueTool[];
  onSaved: () => void;
}) {
  const save = useAction();
  const [picked, setPicked] = useState<string[] | null>(null);
  const current = picked ?? role.tools;
  const has = (t: CatalogueTool) => current.includes(t.tool) || current.includes(`${t.app}.*`);
  const toggle = (t: CatalogueTool, on: boolean) => {
    // Ticking one tool of an "all actions" grant narrows it to the ticked ones.
    const expanded = current.flatMap((x) =>
      x === `${t.app}.*` ? catalogue.filter((c) => c.app === t.app).map((c) => c.tool) : [x],
    );
    setPicked(on ? [...new Set([...expanded, t.tool])] : expanded.filter((x) => x !== t.tool));
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.run(async () => {
      await api(`${base}/ai/roles/${role.role}/tools`, { method: "PUT", body: JSON.stringify({ tools: current }) });
      setPicked(null);
      onSaved();
    });
  };
  return (
    <Card title={role.label}>
      <dl className="ai-role">
        <dt>Serves</dt>
        <dd>{role.serves}</dd>
        <dt>May read</dt>
        <dd>{role.may_read}</dd>
        <dt>May do</dt>
        <dd>{role.may_do}</dd>
      </dl>
      {catalogue.length === 0 ? (
        <p className="muted">This role has no tools to choose from.</p>
      ) : (
        <form onSubmit={submit}>
          <fieldset className="ai-tools">
            <legend className="sr-only">Tools the {role.label.toLowerCase()} may use</legend>
            {catalogue.map((t) => (
              <label key={t.tool} className="ai-tool">
                <input type="checkbox" checked={has(t)} onChange={(e) => toggle(t, e.target.checked)} />
                <span>
                  <strong>{t.label}</strong> <span className="muted small">· {t.app_label}</span>
                  {t.sensitive && <span className="tag">Needs approval</span>}
                  {has(t) && (
                    <span className={`pill small ${role.usable.includes(t.tool) ? "ok" : "off"}`}>
                      {role.usable.includes(t.tool) ? "Usable now" : "Not switched on in Integrations"}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </fieldset>
          <div className="ai-actions">
            <button className="button" disabled={save.busy || picked === null}>
              {save.busy ? "Saving…" : "Save tools"}
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={save.error} />
    </Card>
  );
}

// ---- knowledge --------------------------------------------------------------------------

function Knowledge({ base }: { base: string }) {
  const list = useApi<Source[]>(`${base}/ai/knowledge`, 0);
  const act = useAction();
  const [editing, setEditing] = useState<SourceDetail | null>(null);

  const approve = (s: Source, approved: boolean) =>
    act.run(async () => {
      await api(`${base}/ai/knowledge/${s.id}/approve`, { method: "POST", body: JSON.stringify({ approved }) });
      list.reload();
    });
  const remove = (s: Source) => {
    if (!window.confirm(`Delete "${s.title}"? The AI stops using it straight away.`)) return;
    act.run(async () => {
      await api(`${base}/ai/knowledge/${s.id}`, { method: "DELETE" });
      list.reload();
    });
  };
  const edit = (s: Source) =>
    act.run(async () => {
      setEditing(await api<SourceDetail>(`${base}/ai/knowledge/${s.id}`));
    });

  const sources = list.data ?? [];
  return (
    <>
      <Card title="Approved knowledge">
        <p className="muted small ai-intro">
          The AI answers only from approved sources, and staff see which source each answer came from. Editing a source
          withdraws its approval until someone approves it again.
        </p>
        <ErrorNote error={list.error ?? act.error} />
        {list.data && sources.length === 0 && (
          <div className="empty">
            <p>No knowledge yet. Add your opening hours, prices and policies below.</p>
          </div>
        )}
        {sources.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Status</th>
                  <th scope="col">Pieces</th>
                  <th scope="col">Updated</th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.id}>
                    <td className="cell-wrap">
                      <strong>{s.title}</strong>
                      {s.source_url && (
                        <span className="sub">
                          <a href={s.source_url} target="_blank" rel="noreferrer">
                            {s.source_url}
                          </a>
                        </span>
                      )}
                    </td>
                    <td data-label="Status">
                      <span className={`pill ${s.approved ? "ok" : "warn"}`}>{s.approved ? "Approved" : "Waiting for approval"}</span>
                    </td>
                    <td data-label="Pieces" className="mono">
                      {s.chunks}
                    </td>
                    <td data-label="Updated" className="mono">
                      {when(s.updated_at)}
                    </td>
                    <td className="actions">
                      <RowActions
                        label={`source ${s.title}`}
                        disabled={act.busy}
                        items={[
                          s.approved
                            ? { label: "Withdraw approval", onSelect: () => approve(s, false) }
                            : { label: "Approve", onSelect: () => approve(s, true) },
                          { label: "Edit", onSelect: () => edit(s) },
                          { label: "Delete", danger: true, onSelect: () => remove(s) },
                        ]}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <SourceForm
        key={editing?.id ?? "new"}
        base={base}
        editing={editing}
        onDone={() => {
          setEditing(null);
          list.reload();
        }}
      />
      <SearchTest base={base} />
    </>
  );
}

function SourceForm({ base, editing, onDone }: { base: string; editing: SourceDetail | null; onDone: () => void }) {
  const save = useAction();
  const [title, setTitle] = useState(editing?.title ?? "");
  const [url, setUrl] = useState(editing?.source_url ?? "");
  const [body, setBody] = useState(editing?.body ?? "");
  const [approved, setApproved] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.run(async () => {
      if (editing) {
        await api(`${base}/ai/knowledge/${editing.id}`, {
          method: "PATCH",
          body: JSON.stringify({ title, body, source_url: url }),
        });
      } else {
        await api(`${base}/ai/knowledge`, {
          method: "POST",
          body: JSON.stringify({ title, body, source_url: url, approved }),
        });
      }
      setTitle("");
      setUrl("");
      setBody("");
      setApproved(false);
      onDone();
    });
  };
  return (
    <Card title={editing ? `Edit "${editing.title}"` : "Add a source"}>
      <form className="form" onSubmit={submit}>
        <label>
          Title
          <input value={title} maxLength={200} required onChange={(e) => setTitle(e.target.value)} placeholder="Opening hours" />
        </label>
        <label>
          Link to the original (optional)
          <input type="url" value={url} maxLength={500} onChange={(e) => setUrl(e.target.value)} placeholder="https://" />
        </label>
        <label className="wide">
          Text
          <textarea rows={8} required value={body} onChange={(e) => setBody(e.target.value)} />
        </label>
        {!editing && (
          <label className="check wide">
            <input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} />
            Approve it now (the AI starts using it straight away)
          </label>
        )}
        {editing && <p className="muted small wide">Saving an edit withdraws approval until someone approves it again.</p>}
        <div className="actions wide">
          <button className="button" disabled={save.busy}>
            {save.busy ? "Saving…" : editing ? "Save changes" : "Add source"}
          </button>
          {editing && (
            <button type="button" className="button secondary" onClick={onDone}>
              Cancel
            </button>
          )}
        </div>
      </form>
      <ErrorNote error={save.error} />
    </Card>
  );
}

function SearchTest({ base }: { base: string }) {
  const run = useAction();
  const [q, setQ] = useState("");
  const [res, setRes] = useState<Lookup | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run.run(async () => setRes(await api<Lookup>(`${base}/ai/knowledge/search?q=${encodeURIComponent(q)}`)));
  };
  return (
    <Card title="What would the AI find?">
      <form className="form" onSubmit={submit}>
        <label className="wide">
          A customer's question
          <input value={q} maxLength={500} required onChange={(e) => setQ(e.target.value)} placeholder="When are you open on Saturday?" />
        </label>
        <div className="actions">
          <button className="button secondary" disabled={run.busy}>
            Search approved knowledge
          </button>
        </div>
      </form>
      <ErrorNote error={run.error} />
      {res && (
        <div aria-live="polite">
          {res.contradictory && <p className="pill warn">Sources disagree: {res.contradiction} The AI would hand over.</p>}
          {res.hits.length === 0 ? (
            <p className="pill warn">Nothing relevant. The AI would hand over and record a knowledge gap.</p>
          ) : (
            <ol className="ai-hits">
              {res.hits.map((h) => (
                <li key={h.chunk_id}>
                  <strong>{h.title}</strong> <span className="muted small">· covers {Math.round(h.coverage * 100)}% of the question</span>
                  <p>{h.text}</p>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </Card>
  );
}

// ---- gaps -----------------------------------------------------------------------------

function Gaps({ base }: { base: string }) {
  const [status, setStatus] = useState<"open" | "resolved" | "all">("open");
  const list = useApi<Gap[]>(`${base}/ai/gaps?status=${status}`, 30_000);
  const act = useAction();
  const resolve = (g: Gap) =>
    act.run(async () => {
      await api(`${base}/ai/gaps/${g.id}/resolve`, { method: "POST" });
      list.reload();
    });
  const gaps = list.data ?? [];
  return (
    <Card
      title="Questions the AI couldn't answer"
      note={
        <div className="segmented" role="group" aria-label="Show">
          {(["open", "resolved", "all"] as const).map((s) => (
            <button key={s} type="button" aria-pressed={status === s} onClick={() => setStatus(s)}>
              {s === "open" ? "Open" : s === "resolved" ? "Resolved" : "All"}
            </button>
          ))}
        </div>
      }
    >
      <p className="muted small ai-intro">
        Each one went to a person. Add or fix a source in Knowledge, then mark the gap resolved.
      </p>
      <ErrorNote error={list.error ?? act.error} />
      {list.data && gaps.length === 0 && (
        <div className="empty">
          <p>No {status === "all" ? "" : status} gaps.</p>
        </div>
      )}
      {gaps.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Question</th>
                <th scope="col">Why</th>
                <th scope="col">Times</th>
                <th scope="col">Last asked</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {gaps.map((g) => (
                <tr key={g.id}>
                  <td className="cell-wrap">
                    {g.question}
                    {g.conversation_id && (
                      <span className="sub">
                        <Link to={`/commai/c/${g.conversation_id}`}>Open the conversation</Link>
                      </span>
                    )}
                  </td>
                  <td data-label="Why" className="cell-wrap">
                    <span className={`pill ${g.reason === "missing" ? "warn" : "bad"}`}>
                      {g.reason === "missing" ? "No source covers it" : "Sources disagree"}
                    </span>
                    {g.detail && <span className="sub">{g.detail}</span>}
                  </td>
                  <td data-label="Times" className="mono">
                    {g.times}
                  </td>
                  <td data-label="Last asked" className="mono">
                    {when(g.last_seen)}
                  </td>
                  <td className="actions">
                    {g.status === "open" ? (
                      <button type="button" className="button secondary small" disabled={act.busy} onClick={() => resolve(g)}>
                        Mark resolved
                      </button>
                    ) : (
                      <span className="pill ok">Resolved</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---- browser call -----------------------------------------------------------------------

interface Line {
  from: "caller" | "ai" | "business";
  text: string;
}

interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

function recognitionClass(): (new () => Recognition) | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const BCP47: Record<string, string> = { en: "en-GB", es: "es-ES", fr: "fr-FR", pt: "pt-PT", nl: "nl-NL", ht: "fr-HT", pap: "nl-NL" };

function TryCall({ base }: { base: string }) {
  const status = useApi<AiStatus>(`${base}/ai/status`, 0);
  const prof = useApi<AiProfile>(`${base}/ai/profile`, 0);
  const act = useAction();
  const [callId, setCallId] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [handedOver, setHandedOver] = useState(false);
  const [lines, setLines] = useState<Line[]>([]);
  const [typed, setTyped] = useState("");
  const [listening, setListening] = useState(false);
  const [speak, setSpeak] = useState(true);
  const rec = useRef<Recognition | null>(null);
  const SR = recognitionClass();
  const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;
  const lang = BCP47[prof.data?.business_language ?? "en"] ?? "en-GB";

  useEffect(
    () => () => {
      rec.current?.stop();
      if (canSpeak) window.speechSynthesis.cancel();
    },
    [canSpeak],
  );

  const say = (text: string) => {
    if (!speak || !canSpeak || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang;
    window.speechSynthesis.speak(u);
  };

  const start = () =>
    act.run(async () => {
      const r = await api<{ conversation_id: string; greeting: string; handler: string }>(`${base}/ai/calls`, {
        method: "POST",
        body: JSON.stringify({ caller: "Test call from the portal" }),
      });
      setCallId(r.conversation_id);
      setEnded(false);
      setHandedOver(r.handler !== "ai");
      setLines([{ from: r.handler === "ai" ? "ai" : "business", text: r.greeting }]);
      say(r.greeting);
    });

  const send = (text: string) => {
    const t = text.trim();
    if (!t || !callId) return;
    setLines((l) => [...l, { from: "caller", text: t }]);
    act.run(async () => {
      const r = await api<{ reply: string; handed_over: boolean; messages: { author_kind: string; body: string }[] }>(
        `${base}/ai/calls/${callId}/turns`,
        { method: "POST", body: JSON.stringify({ text: t }) },
      );
      setLines((l) => [...l, ...r.messages.map((m) => ({ from: (m.author_kind === "ai" ? "ai" : "business") as Line["from"], text: m.body }))]);
      setHandedOver(r.handed_over);
      say(r.reply);
    });
  };

  const listen = () => {
    if (!SR) return;
    if (listening) {
      rec.current?.stop();
      return;
    }
    if (canSpeak) window.speechSynthesis.cancel();
    const r = new SR();
    r.lang = lang;
    r.interimResults = false;
    r.continuous = false;
    r.onresult = (e) => {
      const res = e.results[e.results.length - 1];
      if (res && res.isFinal) send(res[0].transcript);
    };
    r.onerror = (e) => act.setError(e.error === "not-allowed" ? "The browser was not allowed to use the microphone." : `Speech recognition stopped (${e.error}).`);
    r.onend = () => setListening(false);
    rec.current = r;
    setListening(true);
    r.start();
  };

  const end = () =>
    act.run(async () => {
      rec.current?.stop();
      if (canSpeak) window.speechSynthesis.cancel();
      await api(`${base}/ai/calls/${callId}/end`, { method: "POST" });
      setEnded(true);
    });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    send(typed);
    setTyped("");
  };

  const live = callId && !ended;
  return (
    <Card title="Try a browser call with the AI agent">
      <p className="muted small ai-intro">
        Speech is recognised and spoken by your browser, at no cost. The call is a conversation in the inbox, so a person
        can take over. If your browser has no speech recognition, type instead.
      </p>
      {status.data && <p className="muted small">{status.data.speech.note}</p>}
      {status.data && !status.data.live && <p className="pill warn">Simulated AI: answers come from approved knowledge only.</p>}
      {!live ? (
        <div className="ai-actions">
          <button type="button" className="button" disabled={act.busy} onClick={start}>
            {ended ? "Start another call" : "Start a call"}
          </button>
          {ended && callId && <Link to={`/commai/c/${callId}`}>Open the call in the inbox</Link>}
        </div>
      ) : (
        <div className="ai-actions">
          {SR ? (
            <button type="button" className="button" aria-pressed={listening} onClick={listen} disabled={act.busy && !listening}>
              {listening ? "Listening… press to stop" : "Press to speak"}
            </button>
          ) : (
            <span className="muted small">This browser has no speech recognition: type below.</span>
          )}
          {canSpeak && (
            <label className="check small">
              <input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> Speak replies aloud
            </label>
          )}
          <button type="button" className="button danger-text" onClick={end} disabled={act.busy}>
            End call
          </button>
        </div>
      )}
      {handedOver && live && <p className="pill warn">Handed to a person. The AI has stopped answering on this call.</p>}
      {lines.length > 0 && (
        <ol className="ai-transcript" aria-label="Call transcript" aria-live="polite">
          {lines.map((l, i) => (
            <li key={i} className={`ai-line ${l.from}`}>
              <span className="small muted">{l.from === "caller" ? "You" : l.from === "ai" ? "AI agent" : "Business"}</span>
              <span>{l.text}</span>
            </li>
          ))}
        </ol>
      )}
      {live && (
        <form className="form" onSubmit={submit}>
          <label className="wide">
            Or type what the caller says
            <input value={typed} maxLength={2000} onChange={(e) => setTyped(e.target.value)} />
          </label>
          <div className="actions">
            <button className="button secondary" disabled={act.busy || !typed.trim()}>
              Send
            </button>
          </div>
        </form>
      )}
      <ErrorNote error={act.error ?? status.error} />
    </Card>
  );
}
