import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { useAuth } from "../../auth";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { DraftLabel, useT } from "./i18n";
import { useCommaiBase } from "./lib";
import "./quality.css";

/* Shapes from controller/exaconnect_controller/commai/api/team.py (ADR 0032). */
interface Chat {
  id: string;
  kind: "direct" | "group";
  name: string;
  members: string[];
  unread: number;
  last_message_at: string | null;
}
interface ChatMessage {
  id: string;
  author: string;
  body: string;
  created_at: string;
}
interface Notification {
  id: string;
  kind: string;
  title: string;
  body: string;
  conversation_id: string | null;
  read_at: string | null;
  created_at: string;
}
interface Person {
  id: string;
  email: string;
}

/** Team: notifications and staff chat. Internal only: customers never see any of it. */
export default function Team() {
  const base = useCommaiBase();
  const { t } = useT();
  if (!base) return <p className="muted">{t("common.chooseBusiness")}</p>;
  return (
    <>
      <PageHead title={t("team.title")}>
        {t("team.intro")}
      </PageHead>
      <DraftLabel />
      <div className="q-team">
        <Notifications base={base} />
        <StaffChat base={base} />
      </div>
    </>
  );
}

function Notifications({ base }: { base: string }) {
  const i = useT();
  const { t } = i;
  const n = useApi<{ items: Notification[]; unread: number }>(`${base}/notifications`, 15_000);
  const act = useAction();
  const readAll = () =>
    act.run(async () => {
      await api(`${base}/notifications/read`, { method: "POST" });
      n.reload();
    });
  const readOne = (id: string) => api(`${base}/notifications/${id}/read`, { method: "POST" }).then(n.reload, () => {});
  return (
    <Card
      title={t("team.notifications")}
      note={
        n.data && n.data.unread > 0 ? (
          <button className="button secondary small" type="button" onClick={readAll}>
            {t("team.markAllRead")}
          </button>
        ) : undefined
      }
    >
      <ErrorNote error={n.error ?? act.error} />
      {n.data && n.data.items.length === 0 && <p className="muted">{t("team.noNotifications")}</p>}
      <ul className="q-list">
        {(n.data?.items ?? []).map((x) => (
          <li key={x.id} className={x.read_at ? "off" : ""}>
            <span>
              <span className="tag">{t(`team.kind.${x.kind}`)}</span> <strong>{x.title}</strong>
              {x.body && <span className="muted small"> · {x.body}</span>}
              <span className="muted small"> · {i.date(x.created_at)}</span>
            </span>
            <span className="q-row-actions">
              {x.conversation_id && (
                <Link className="button secondary small" to={`/commai/c/${x.conversation_id}`} onClick={() => readOne(x.id)}>
                  {t("team.open")}
                </Link>
              )}
              {!x.read_at && (
                <button className="button secondary small" type="button" onClick={() => readOne(x.id)}>
                  {t("team.markRead")}
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function StaffChat({ base }: { base: string }) {
  const i = useT();
  const { t } = i;
  const { user } = useAuth();
  const chats = useApi<Chat[]>(`${base}/staff-chat`, 10_000);
  const people = useApi<Person[]>(`${base}/people`, 0);
  const [open, setOpen] = useState<string | null>(null);
  const [kind, setKind] = useState<"direct" | "group">("direct");
  const [picked, setPicked] = useState<string[]>([]);
  const [name, setName] = useState("");
  const act = useAction();
  const others = (people.data ?? []).filter((p) => p.email !== user?.email);
  const label = (c: Chat) => (c.kind === "group" ? c.name : c.members.filter((m) => m !== user?.email).join(", ") || c.members.join(", "));

  const start = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const c = await api<{ id: string }>(`${base}/staff-chat`, { method: "POST", body: JSON.stringify({ kind, members: picked, name }) });
      setPicked([]);
      setName("");
      chats.reload();
      setOpen(c.id);
    });
  };

  return (
    <Card title={t("team.chat")}>
      <p className="muted small">{t("team.chatHint")}</p>
      <ErrorNote error={chats.error} />
      <ul className="q-list">
        {(chats.data ?? []).map((c) => (
          <li key={c.id}>
            <button type="button" className="q-chat-link" aria-pressed={open === c.id} onClick={() => setOpen(c.id)}>
              <strong>{label(c)}</strong>
              {c.unread > 0 && <span className="pill warn"> {t("team.unread", { n: c.unread })}</span>}
            </button>
            <span className="muted small">{i.date(c.last_message_at)}</span>
          </li>
        ))}
      </ul>
      {open && <ChatThread base={base} id={open} onRead={chats.reload} />}
      <details className="q-new-chat">
        <summary>{t("team.newChat")}</summary>
        <form className="form" onSubmit={start}>
          <div className="segmented wide" role="group" aria-label={t("team.newChat")}>
            <button type="button" aria-pressed={kind === "direct"} onClick={() => setKind("direct")}>
              {t("team.direct")}
            </button>
            <button type="button" aria-pressed={kind === "group"} onClick={() => setKind("group")}>
              {t("team.group")}
            </button>
          </div>
          {kind === "group" && (
            <label className="wide">
              {t("team.groupName")}
              <input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
            </label>
          )}
          <fieldset className="wide">
            <legend>{t("team.with")}</legend>
            {others.map((p) => (
              <label key={p.id} className="check">
                <input
                  type={kind === "direct" ? "radio" : "checkbox"}
                  name="chat-people"
                  checked={picked.includes(p.id)}
                  onChange={(e) =>
                    setPicked(kind === "direct" ? [p.id] : e.target.checked ? [...picked, p.id] : picked.filter((x) => x !== p.id))
                  }
                />
                {p.email}
              </label>
            ))}
          </fieldset>
          <div className="actions">
            <button className="button" disabled={act.busy || picked.length === 0}>
              {t("team.start")}
            </button>
          </div>
        </form>
        <ErrorNote error={act.error} />
      </details>
    </Card>
  );
}

function ChatThread({ base, id, onRead }: { base: string; id: string; onRead: () => void }) {
  const i = useT();
  const { t } = i;
  const { user } = useAuth();
  const msgs = useApi<ChatMessage[]>(`${base}/staff-chat/${id}/messages`, 5_000);
  const [text, setText] = useState("");
  const send = useAction();
  const end = useRef<HTMLLIElement>(null);
  const count = msgs.data?.length ?? 0;
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
    onRead();
  }, [count, onRead]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    send.run(async () => {
      await api(`${base}/staff-chat/${id}/messages`, { method: "POST", body: JSON.stringify({ body: text }) });
      setText("");
      msgs.reload();
    });
  };
  return (
    <div className="q-thread">
      <ol aria-label={t("team.messages")}>
        {(msgs.data ?? []).map((m) => (
          <li key={m.id} className={m.author === user?.email ? "mine" : ""}>
            <div className="small muted">
              {m.author} · {i.date(m.created_at)}
            </div>
            <div>{m.body}</div>
          </li>
        ))}
        <li ref={end} aria-hidden="true" />
      </ol>
      <form onSubmit={submit} className="q-thread-form">
        <label className="sr-only" htmlFor="staff-chat-text">
          {t("team.message")}
        </label>
        <textarea id="staff-chat-text" rows={2} maxLength={4000} value={text} required onChange={(e) => setText(e.target.value)} placeholder={t("team.messagePlaceholder")} />
        <button className="button" disabled={send.busy}>
          {t("team.send")}
        </button>
      </form>
      <ErrorNote error={msgs.error ?? send.error} />
    </div>
  );
}
