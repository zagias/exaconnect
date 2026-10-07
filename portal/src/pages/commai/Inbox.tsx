import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { useAction } from "../../ui";
import CopilotPanel from "./CopilotPanel";
import { AttachmentReadings, NoteFiles, SavedViews, uploadNoteFiles, type Filters } from "./InboxExtras";
import { CHANNEL_LABEL, STATE_LABEL, useCommaiBase, when, type Page } from "./lib";
import { useT } from "./i18n";
import { useLive } from "./live";
import type { Conversation, ConversationDetail, Member, Note, Team } from "./types";
import "./inbox.css";

const VIEWS: { id: string; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "mine", label: "Mine" },
  { id: "unassigned", label: "Unassigned" },
  { id: "ai", label: "With the AI" },
  { id: "overdue", label: "Overdue" },
  { id: "all", label: "All" },
];

const PRIORITY_LABEL: Record<string, string> = { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" };

/** The shared inbox: every channel in one list, one conversation at a time. */
export default function Inbox() {
  const base = useCommaiBase();
  const { id } = useParams();
  const navigate = useNavigate();
  const { t } = useT();
  const [view, setView] = useState("open");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [extra, setExtra] = useState<Filters>({});
  const params = new URLSearchParams({ ...extra, view, limit: "100" });
  if (query) params.set("q", query);
  const list = useApi<Page<Conversation>>(base ? `${base}/conversations?${params}` : null, 15_000);
  const [tick, setTick] = useState(0);

  const reloadList = list.reload;
  useLive(
    base,
    useCallback(
      (type: string) => {
        if (type.startsWith("conversation.") || type.startsWith("message.") || type === "note.created") {
          reloadList();
          setTick((t) => t + 1);
        }
      },
      [reloadList],
    ),
  );

  if (!base) return <p className="muted">Choose a business first.</p>;
  const items = list.data?.items ?? [];

  return (
    <div className={`inbox ${id ? "has-open" : ""}`}>
      <section className="inbox-list card" aria-labelledby="inbox-title">
        <div className="inbox-list-head">
          <h1 id="inbox-title">{t("inbox.title")}</h1>
          <form
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              setQuery(q.trim());
            }}
          >
            <label className="sr-only" htmlFor="inbox-q">
              Search conversations
            </label>
            <input id="inbox-q" type="search" placeholder="Search name, subject, number" value={q} onChange={(e) => setQ(e.target.value)} />
          </form>
          <div className="segmented inbox-views" role="group" aria-label="Show">
            {VIEWS.map((v) => (
              <button key={v.id} type="button" aria-pressed={view === v.id} onClick={() => setView(v.id)}>
                {t(`inbox.view.${v.id}`)}
              </button>
            ))}
          </div>
          <SavedViews
            base={base}
            current={{ ...extra, view, ...(query ? { q: query } : {}) }}
            onApply={(f) => {
              const { view: v, q: fq, ...rest } = f;
              setView(v || "all");
              setQ(fq ?? "");
              setQuery(fq ?? "");
              setExtra(rest);
            }}
          />
          {Object.keys(extra).length > 0 && (
            <button className="button secondary small" type="button" onClick={() => setExtra({})}>
              {t("inbox.clearFilters", { filters: Object.entries(extra).map(([k, v]) => `${k}: ${v}`).join(", ") })}
            </button>
          )}
        </div>
        <ErrorNote error={list.error} />
        {list.data && items.length === 0 && (
          <div className="empty">
            <p>Nothing here. New chats, WhatsApp messages and emails arrive in this list.</p>
          </div>
        )}
        <ul className="inbox-items">
          {items.map((c) => (
            <li key={c.id}>
              <Link to={`/commai/c/${c.id}`} className={`inbox-item ${c.id === id ? "current" : ""}`} aria-current={c.id === id ? "true" : undefined}>
                <div className="inbox-item-top">
                  <strong>{c.contact_name || c.contact_address || "Visitor"}</strong>
                  <span className="muted small">{when(c.last_message_at ?? c.created_at)}</span>
                </div>
                <div className="inbox-item-preview">{c.preview || c.subject || "No messages yet"}</div>
                <div className="inbox-item-meta small">
                  <span className="tag">{CHANNEL_LABEL[c.channel] ?? c.channel}</span>
                  <span>{STATE_LABEL[c.state] ?? c.state}</span>
                  {c.handler === "ai" && <span className="tag ai">AI handling</span>}
                  {(c.priority === "urgent" || c.priority === "high") && <span className="pill warn">{PRIORITY_LABEL[c.priority]}</span>}
                  {(c.first_reply_overdue || c.resolve_overdue) && <span className="pill bad">Overdue</span>}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
      {id ? (
        <ConversationPane key={id} base={base} id={id} tick={tick} onChanged={reloadList} onClose={() => navigate("/commai")} />
      ) : (
        <section className="inbox-empty card">
          <p className="muted">Choose a conversation to read and reply.</p>
        </section>
      )}
    </div>
  );
}

function ConversationPane({
  base,
  id,
  tick,
  onChanged,
  onClose,
}: {
  base: string;
  id: string;
  tick: number;
  onChanged: () => void;
  onClose: () => void;
}) {
  const { user } = useAuth();
  const conv = useApi<ConversationDetail>(`${base}/conversations/${id}`, 10_000);
  const notes = useApi<Note[]>(`${base}/conversations/${id}/notes`, 10_000);
  const teams = useApi<Team[]>(`${base}/teams`, 0);
  const members = useApi<Member[]>(`${base}/members`, 60_000);
  const act = useAction();
  const [draft, setDraft] = useState<{ text: string; n: number } | null>(null);
  const reload = conv.reload;
  const reloadNotes = notes.reload;

  useEffect(() => {
    if (tick) {
      reload();
      reloadNotes();
    }
  }, [tick, reload, reloadNotes]);

  const refresh = () => {
    reload();
    reloadNotes();
    onChanged();
  };

  const post = (path: string, body?: unknown) =>
    act.run(async () => {
      await api(`${base}/conversations/${id}${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      refresh();
    });

  const timeline = useMemo(() => {
    const m = (conv.data?.messages ?? []).map((x) => ({ kind: "message" as const, at: x.created_at, m: x }));
    const n = (notes.data ?? []).map((x) => ({ kind: "note" as const, at: x.created_at, n: x }));
    return [...m, ...n].sort((a, b) => a.at.localeCompare(b.at));
  }, [conv.data, notes.data]);

  const c = conv.data;
  if (!c) {
    return (
      <section className="inbox-conv card">
        <ErrorNote error={conv.error} />
        {!conv.error && <p className="muted">Loading the conversation…</p>}
      </section>
    );
  }
  const internal = c.your_seat === "internal";
  const handledByOther = c.handler === "human" && c.handler_user_id && user && c.handler_email !== user.email;

  return (
    <>
      <section className="inbox-conv card" aria-labelledby="conv-title">
        <header className="conv-head">
          <button type="button" className="button secondary small conv-back" onClick={onClose}>
            Back to the list
          </button>
          <div>
            <h2 id="conv-title">{c.contact_name || c.contact_address || "Visitor"}</h2>
            <div className="muted small">
              {CHANNEL_LABEL[c.channel] ?? c.channel}
              {c.contact_address ? ` · ${c.contact_address}` : ""}
              {c.identity_verified ? " · Verified" : " · Not verified"}
              {c.subject ? ` · ${c.subject}` : ""}
            </div>
          </div>
          <Handler c={c} mine={c.handler === "human" && !!user && c.handler_email === user.email} internal={internal} busy={act.busy} onTakeOver={() => post("/takeover")} onHandBack={() => post("/handback")} />
        </header>

        {c.handovers.length > 0 && <HandoverNote h={c.handovers[c.handovers.length - 1]} />}

        <ol className="timeline" aria-label="Messages and private notes">
          {timeline.map((t) =>
            t.kind === "message" ? (
              <li key={`m${t.m.id}`} className={`bubble ${t.m.direction} by-${t.m.author_kind}`}>
                <div className="bubble-who small">
                  {t.m.direction === "in" ? c.contact_name || "Customer" : authorLabel(t.m.author_kind, t.m.author)}
                  <span className="muted"> · {when(t.m.created_at)}</span>
                  {t.m.direction === "out" && <span className={`msg-status ${t.m.status}`}> · {statusLabel(t.m.status)}</span>}
                </div>
                <div className="bubble-body">{t.m.template ? `Template: ${t.m.template}` : t.m.body}</div>
                {t.m.original_body && t.m.original_body !== t.m.body && (
                  <div className="bubble-original small">
                    Original{t.m.original_language ? ` (${t.m.original_language})` : ""}: {t.m.original_body}
                  </div>
                )}
                {t.m.error && <div className="small pill bad">{t.m.error}</div>}
              </li>
            ) : (
              <li key={`n${t.n.id}`} className="bubble note">
                <div className="bubble-who small">
                  Private note · {t.n.author}
                  <span className="muted"> · {when(t.n.created_at)}</span>
                </div>
                <div className="bubble-body">{t.n.body}</div>
                <NoteFiles base={base} files={t.n.attachments} />
              </li>
            ),
          )}
        </ol>

        <Composer
          key={id}
          base={base}
          id={id}
          internal={internal}
          handledByOther={!!handledByOther}
          handlerEmail={c.handler_email}
          draft={draft}
          onSent={refresh}
        />
        <ErrorNote error={act.error} />
      </section>

      <aside className="inbox-side card" aria-label="Conversation details">
        <Details c={c} teams={teams.data ?? []} members={members.data ?? []} base={base} internal={internal} onChanged={refresh} />
        <AttachmentReadings base={base} conversationId={c.id} />
        <CopilotPanel base={base} conversationId={c.id} onDraft={(text) => setDraft({ text, n: Date.now() })} />
      </aside>
    </>
  );
}

function authorLabel(kind: string, author: string): string {
  if (kind === "ai") return "AI agent";
  if (kind === "system") return "CommAI";
  if (kind === "workflow") return "Workflow";
  return author;
}

function statusLabel(s: string): string {
  return (
    { queued: "Sending", sent: "Sent", delivered: "Delivered", read: "Read", failed: "Not delivered", blocked: "Blocked" }[s] ?? s
  );
}

function Handler({
  c,
  mine,
  internal,
  busy,
  onTakeOver,
  onHandBack,
}: {
  c: ConversationDetail;
  mine: boolean;
  internal: boolean;
  busy: boolean;
  onTakeOver: () => void;
  onHandBack: () => void;
}) {
  const label =
    c.handler === "ai" ? "The AI agent is handling this" : c.handler === "human" ? (mine ? "You are handling this" : `${c.handler_email ?? "Someone"} is handling this`) : "Nobody is handling this yet";
  return (
    <div className="conv-handler">
      <span className={`pill ${c.handler === "ai" ? "warn" : c.handler === "human" ? "ok" : ""}`}>{label}</span>
      {!internal && (
        <div className="conv-handler-actions">
          {!mine && (
            <button type="button" className="button small" disabled={busy} onClick={onTakeOver}>
              Take over
            </button>
          )}
          {c.handler !== "ai" && (
            <button type="button" className="button secondary small" disabled={busy} onClick={onHandBack}>
              Hand to the AI
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function HandoverNote({ h }: { h: { at: string; reason: string; packet: Record<string, unknown> } }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="callout small handover">
      <strong>Handed over by the AI {when(h.at)}:</strong> {h.reason}{" "}
      <button type="button" className="linklike" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide details" : "Show what it collected"}
      </button>
      {open && <pre className="code small">{JSON.stringify(h.packet, null, 2)}</pre>}
    </div>
  );
}

function Composer({
  base,
  id,
  internal,
  handledByOther,
  handlerEmail,
  draft,
  onSent,
}: {
  base: string;
  id: string;
  internal: boolean;
  handledByOther: boolean;
  handlerEmail: string | null;
  draft: { text: string; n: number } | null;
  onSent: () => void;
}) {
  const [mode, setMode] = useState<"reply" | "note">(internal ? "note" : "reply");
  const [text, setText] = useState("");
  const [template, setTemplate] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const { t } = useT();
  const send = useAction();
  const key = useRef(crypto.randomUUID());
  // A draft from the copilot lands in the box; a person still presses Send.
  useEffect(() => {
    if (draft && !internal) {
      setMode("reply");
      setText(draft.text);
    }
  }, [draft, internal]);

  const submit = (e: FormEvent, takeOver = false) => {
    e.preventDefault();
    const body = text.trim();
    if (!body && !template) return;
    send.run(async () => {
      if (mode === "note") {
        const n = await api<{ id: string }>(`${base}/conversations/${id}/notes`, { method: "POST", body: JSON.stringify({ body }) });
        if (files.length) await uploadNoteFiles(base, n.id, files);
        setFiles([]);
      } else {
        await api(`${base}/conversations/${id}/messages`, {
          method: "POST",
          headers: { "Idempotency-Key": key.current },
          body: JSON.stringify({ body, template, take_over: takeOver }),
        });
      }
      key.current = crypto.randomUUID();
      setText("");
      setTemplate("");
      onSent();
    });
  };

  return (
    <form className={`composer ${mode}`} onSubmit={submit}>
      <div className="segmented" role="group" aria-label="Write">
        <button type="button" aria-pressed={mode === "reply"} disabled={internal} onClick={() => setMode("reply")}>
          Reply to customer
        </button>
        <button type="button" aria-pressed={mode === "note"} onClick={() => setMode("note")}>
          Private note
        </button>
      </div>
      {internal && <p className="muted small">Your seat writes private notes only. Customers never see them.</p>}
      {mode === "reply" && handledByOther && (
        <p className="callout warn small">{handlerEmail ?? "Someone else"} is handling this conversation. Sending will take it over.</p>
      )}
      <label className="sr-only" htmlFor="composer-text">
        {mode === "note" ? "Private note" : "Reply"}
      </label>
      <textarea
        id="composer-text"
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={mode === "note" ? "Only your team sees this. Use @name to mention someone." : "Write a reply"}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e as unknown as FormEvent, handledByOther);
        }}
      />
      {mode === "note" && (
        <label className="small note-attach">
          {t("inbox.attachToNote")}
          <input
            type="file"
            multiple
            accept=".txt,.csv,.pdf,image/png,image/jpeg,image/gif,image/webp"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, 5))}
          />
        </label>
      )}
      {mode === "reply" && (
        <details className="small">
          <summary>Send an approved template instead</summary>
          <label>
            Template name
            <input value={template} onChange={(e) => setTemplate(e.target.value)} maxLength={200} placeholder="e.g. order_update" />
          </label>
          <p className="muted">WhatsApp only allows approved templates once 24 hours have passed since the customer last wrote.</p>
        </details>
      )}
      <div className="actions">
        <button className="button" disabled={send.busy} onClick={(e) => submit(e, handledByOther)}>
          {send.busy ? "Sending…" : mode === "note" ? "Add note" : handledByOther ? "Take over and send" : "Send"}
        </button>
      </div>
      <ErrorNote error={send.error} />
    </form>
  );
}

function Details({
  c,
  teams,
  members,
  base,
  internal,
  onChanged,
}: {
  c: ConversationDetail;
  teams: Team[];
  members: Member[];
  base: string;
  internal: boolean;
  onChanged: () => void;
}) {
  const act = useAction();
  const call = (path: string, body: unknown, method = "POST") =>
    act.run(async () => {
      await api(`${base}/conversations/${c.id}${path}`, { method, body: JSON.stringify(body) });
      onChanged();
    });
  const [tags, setTags] = useState(c.tags.join(", "));
  const agents = members.filter((m) => m.seat === "agent");

  return (
    <div className="details">
      <h3>Details</h3>
      <label>
        State
        <select value={c.state} disabled={internal || act.busy} onChange={(e) => e.target.value !== "snoozed" && call("/state", { state: e.target.value })}>
          {Object.entries(STATE_LABEL)
            .filter(([k]) => k !== "snoozed" || c.state === "snoozed")
            .map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
        </select>
      </label>
      {!internal && (
        <button
          type="button"
          className="button secondary small"
          disabled={act.busy}
          onClick={() => call("/state", { state: "snoozed", snoozed_until: new Date(Date.now() + 24 * 3600_000).toISOString() })}
        >
          Snooze until tomorrow
        </button>
      )}
      <label>
        Priority
        <select value={c.priority} disabled={internal || act.busy} onChange={(e) => call("", { priority: e.target.value }, "PATCH")}>
          {Object.entries(PRIORITY_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <label>
        Team
        <select value={c.team_id ?? ""} disabled={internal || act.busy} onChange={(e) => e.target.value && call("/assign", { team_id: e.target.value, reason: "moved to team" })}>
          <option value="">No team</option>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Assigned to
        <select
          value={c.assignee_id ?? ""}
          disabled={internal || act.busy}
          onChange={(e) => e.target.value && call("/assign", { assignee_id: e.target.value, reason: "assigned by hand" })}
        >
          <option value="">Nobody</option>
          {agents.map((m) => (
            <option key={m.id} value={m.id}>
              {m.email}
              {m.available ? "" : " (away)"}
            </option>
          ))}
        </select>
      </label>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          call("", { tags: tags.split(",").map((t) => t.trim()).filter(Boolean) }, "PATCH");
        }}
      >
        <label>
          Tags
          <input value={tags} disabled={internal} onChange={(e) => setTags(e.target.value)} placeholder="billing, vip" />
        </label>
      </form>
      <dl className="small facts">
        <dt>First reply due</dt>
        <dd>
          {c.first_reply_at ? "Replied" : c.first_reply_due ? new Date(c.first_reply_due).toLocaleString("en-GB") : "–"}
          {c.first_reply_overdue && <span className="pill bad"> Overdue</span>}
        </dd>
        <dt>Resolve by</dt>
        <dd>
          {c.resolve_due ? new Date(c.resolve_due).toLocaleString("en-GB") : "–"}
          {c.resolve_overdue && <span className="pill bad"> Overdue</span>}
        </dd>
        {c.contact_id && (
          <>
            <dt>Customer</dt>
            <dd>
              <Link to={`/commai/contacts/${c.contact_id}`}>History and details</Link>
            </dd>
          </>
        )}
      </dl>
      <ErrorNote error={act.error} />
      <details className="small">
        <summary>History</summary>
        <ol className="history">
          {c.log.map((l, i) => (
            <li key={i}>
              {when(l.at)}: {l.kind} {l.from_value ? `${l.from_value} → ` : ""}
              {l.to_value} {l.reason ? `(${l.reason})` : ""} · {l.actor}
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
