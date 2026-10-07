import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, useApi, type Loaded } from "../../api";
import { ErrorNote, ExampleTag } from "../../components";
import { Card, PageHead, Tabs, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./channels.css";
import { EmailLimits } from "./ChannelExtras";

/* Shapes from controller/exaconnect_controller/commai/api/channels.py (ADR 0018). */

interface WidgetSettings {
  title: string;
  greeting: string;
  colour: string;
  position: "left" | "right";
  offline_message: string;
  hours: Partial<Record<Day, [string, string] | null>>;
  callbacks: boolean;
  attachments: boolean;
  ai_calls?: boolean;
  ask_contact: "before" | "after_first" | "never";
}

interface Install {
  origin: string;
  page: string;
  allowed: boolean;
  hits: number;
  first_seen_at: string;
  last_seen_at: string;
}

interface WidgetKey {
  id: string;
  public_key: string;
  name: string;
  allowed_origins: string[];
  settings: WidgetSettings;
  active: boolean;
  secret_hint: string;
  secret?: string;
  installs?: Install[];
}

interface Account {
  id: string;
  channel: "whatsapp" | "sms" | "email";
  provider: string;
  provider_label: string;
  simulated: boolean;
  name: string;
  address: string;
  status: "setup" | "live" | "paused" | "broken";
  settings: Record<string, unknown>;
  missing_env: string[];
  webhook_url: string;
  secret_hint: string;
  secret?: string;
  last_inbound_at: string | null;
  last_sent_at: string | null;
  last_error: string;
  last_error_at: string | null;
}

interface Template {
  id: string;
  name: string;
  language: string;
  category: string;
  body: string;
  status: "pending" | "approved" | "rejected";
  status_reason: string;
  status_by: string;
  provider_template_id: string;
  placeholders: number;
}

interface OutboxRow {
  id: number;
  channel: string;
  to_address: string;
  body: string;
  template: string;
  created_at: string;
}

interface Finding {
  area: string;
  status: "ok" | "problem" | "unknown";
  summary: string;
  evidence: string[];
}

interface CommaiSettings {
  mode: "ai_first" | "human_first" | "human_only";
  timezone: string;
}

type Day = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";
const DAYS: [Day, string][] = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
];

const STATUS_WORD: Record<Account["status"], [string, "ok" | "warn" | "bad"]> = {
  live: ["Live", "ok"],
  setup: ["Being set up", "warn"],
  paused: ["Paused", "warn"],
  broken: ["Broken", "bad"],
};

const TEMPLATE_WORD: Record<Template["status"], [string, "ok" | "warn" | "bad"]> = {
  approved: ["Approved", "ok"],
  pending: ["Waiting for approval", "warn"],
  rejected: ["Rejected", "bad"],
};

/** Channels: website chat, WhatsApp, SMS and email (ADR 0018). */
export default function Channels() {
  const base = useCommaiBase();
  if (!base) return <p className="muted">Choose a business first.</p>;
  return (
    <>
      <PageHead eyebrow="CommAI" title="Channels">
        Where your customers reach you. Every channel lands in the same inbox.
      </PageHead>
      <Tabs label="Channels">
        <NavLink to="/commai/channels/web">Website chat</NavLink>
        <NavLink to="/commai/channels/whatsapp">WhatsApp</NavLink>
        <NavLink to="/commai/channels/sms">SMS</NavLink>
        <NavLink to="/commai/channels/email">Email</NavLink>
      </Tabs>
      <Routes>
        <Route index element={<Navigate to="web" replace />} />
        <Route path="web" element={<WebChat base={base} />} />
        <Route path="whatsapp" element={<WhatsApp base={base} />} />
        <Route path="sms" element={<Sms base={base} />} />
        <Route path="email" element={<Email base={base} />} />
      </Routes>
    </>
  );
}

// ---- small pieces ---------------------------------------------------------------------------

function Pill({ word, health }: { word: string; health: "ok" | "warn" | "bad" }) {
  return <span className={`pill ${health}`}>{word}</span>;
}

function SimulatedTag() {
  return (
    <span className="tag" title="Nothing reaches real phones or inboxes">
      Simulated
    </span>
  );
}

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="button secondary small"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 2000);
        });
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

function ShownOnce({ title, secret, onDone, children }: { title: string; secret: string; onDone: () => void; children?: ReactNode }) {
  return (
    <div className="secret" role="status">
      <p className="callout warn">
        <strong>{title}</strong> It is shown once. Store it in your server's secret settings, never in web pages.
      </p>
      <code>{secret}</code>
      {children}
      <div className="secret-actions">
        <Copy text={secret} label="Copy secret" />
        <button type="button" className="button small" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

function Diagnostics({ base, area }: { base: string; area: string }) {
  const { data, error } = useApi<Finding[]>(`${base}/channels/diagnostics`, 60_000);
  const mine = (data ?? []).filter((f) => f.area === area);
  return (
    <Card title="Checks">
      <ErrorNote error={error} />
      {data && mine.length === 0 && <p className="muted">Nothing to report.</p>}
      <ul className="ch-findings">
        {mine.map((f, i) => (
          <li key={i}>
            <Pill word={f.status === "ok" ? "Fine" : f.status === "problem" ? "Needs attention" : "Unknown"} health={f.status === "ok" ? "ok" : f.status === "problem" ? "bad" : "warn"} />{" "}
            {f.summary}
            {f.evidence.length > 0 && (
              <ul className="muted small">
                {f.evidence.map((e, j) => (
                  <li key={j}>{e}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---- website chat -----------------------------------------------------------------------------

function WebChat({ base }: { base: string }) {
  const keys = useApi<WidgetKey[]>(`${base}/widget-keys`, 30_000);
  const create = useAction();
  const [origin, setOrigin] = useState("");
  const [fresh, setFresh] = useState<WidgetKey | null>(null);

  const onCreate = (e: FormEvent) => {
    e.preventDefault();
    create.run(async () => {
      const k = await api<WidgetKey>(`${base}/widget-keys`, {
        method: "POST",
        body: JSON.stringify({ name: "Website", allowed_origins: origin.trim() ? [origin.trim()] : [] }),
      });
      setFresh(k);
      setOrigin("");
      keys.reload();
    });
  };

  const list = (keys.data ?? []).filter((k) => k.active);
  return (
    <>
      <ErrorNote error={keys.error} />
      {fresh?.secret && (
        <ShownOnce title="Your widget secret." secret={fresh.secret} onDone={() => setFresh(null)}>
          <p className="small">Your website uses it to sign the token that tells the chat a customer is signed in.</p>
        </ShownOnce>
      )}
      {keys.data && list.length === 0 && (
        <Card title="Add website chat">
          <p className="muted">One script tag on your website. Visitors chat with your team (or your AI agent) and every chat lands in the inbox.</p>
          <form className="form" onSubmit={onCreate}>
            <label className="wide">
              Your website's address
              <input value={origin} onChange={(e) => setOrigin(e.target.value)} placeholder="https://www.example.com" required />
            </label>
            <div className="actions">
              <button className="button" disabled={create.busy}>
                {create.busy ? "Creating…" : "Create website chat"}
              </button>
            </div>
          </form>
          <ErrorNote error={create.error} />
        </Card>
      )}
      {list.map((k) => (
        <WidgetSetup key={k.id} base={base} k={k} reload={keys.reload} onSecret={setFresh} />
      ))}
      <Diagnostics base={base} area="website_chat" />
    </>
  );
}

function WidgetSetup({ base, k, reload, onSecret }: { base: string; k: WidgetKey; reload: () => void; onSecret: (k: WidgetKey) => void }) {
  const snippet = `<script src="${window.location.origin}/api/v1/commai/widget/v1.js" data-key="${k.public_key}" async></script>`;
  const signedIn = `<script src="${window.location.origin}/api/v1/commai/widget/v1.js" data-key="${k.public_key}"\n        data-user-token="SIGNED_BY_YOUR_SERVER" async></script>`;
  const rotate = useAction();
  const test = useAction();
  const [testConv, setTestConv] = useState<string | null>(null);

  return (
    <>
      <Card title="Install">
        <p className="muted">Paste this just before the closing &lt;/body&gt; tag on every page where you want chat.</p>
        <pre className="ch-code">
          <code>{snippet}</code>
        </pre>
        <div className="form-actions">
          <Copy text={snippet} label="Copy snippet" />
        </div>
        <h3 className="ch-h3">Signed-in customers</h3>
        <p className="small">
          A visitor who types an email address never sees past conversations. To show customers their history, your server signs a short token for the customer who is signed in to
          your site (HS256 JWT with <code>sub</code>, <code>email</code>, <code>name</code> and an <code>exp</code> within 24 hours) using the widget secret, and passes it to the
          script:
        </p>
        <pre className="ch-code">
          <code>{signedIn}</code>
        </pre>
        <p className="small muted">
          Widget secret ends {k.secret_hint}.{" "}
          <button
            type="button"
            className="button secondary small"
            disabled={rotate.busy}
            onClick={() => {
              if (!window.confirm("Make a new secret? Signed-in tokens made with the old one and open chats stop working.")) return;
              rotate.run(async () => {
                onSecret(await api<WidgetKey>(`${base}/widget-keys/${k.id}/rotate`, { method: "POST" }));
                reload();
              });
            }}
          >
            Make a new secret
          </button>
        </p>
        <ErrorNote error={rotate.error} />
      </Card>

      <div className="grid">
        <div className="span-6">
          <Origins base={base} k={k} reload={reload} />
          <Mode base={base} />
        </div>
        <div className="span-6">
          <Card title="Preview" note={<ExampleTag />}>
            <Preview settings={k.settings} />
          </Card>
        </div>
      </div>

      <Appearance base={base} k={k} reload={reload} />
      <Hours base={base} k={k} reload={reload} />

      <Card title="Installation check">
        <p className="muted small">The widget reports the page it runs on each time it loads. We never fetch your website.</p>
        {(k.installs ?? []).length === 0 ? (
          <p>Not seen yet. Install the snippet and open your website.</p>
        ) : (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Website</th>
                  <th scope="col">Allowed</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">Page</th>
                </tr>
              </thead>
              <tbody>
                {(k.installs ?? []).map((i) => (
                  <tr key={i.origin}>
                    <td className="cell-wrap">{i.origin}</td>
                    <td data-label="Allowed">{i.allowed ? <Pill word="Allowed" health="ok" /> : <Pill word="Not on the list" health="bad" />}</td>
                    <td data-label="Last seen" className="mono">
                      {when(i.last_seen_at)}
                    </td>
                    <td data-label="Page" className="cell-wrap small">
                      {i.page}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <h3 className="ch-h3">Send a test conversation</h3>
        <p className="small">Puts a conversation from a test visitor into the inbox, routed like a real one.</p>
        <div className="form-actions">
          <button
            type="button"
            className="button secondary"
            disabled={test.busy}
            onClick={() =>
              test.run(async () => {
                const out = await api<{ conversation_id: string }>(`${base}/widget-keys/${k.id}/test-conversation`, { method: "POST" });
                setTestConv(out.conversation_id);
              })
            }
          >
            {test.busy ? "Sending…" : "Send a test conversation"}
          </button>
          {testConv && (
            <span className="ok-note" role="status">
              Sent. <Link to={`/commai/c/${testConv}`}>Open it in the inbox</Link>
            </span>
          )}
        </div>
        <ErrorNote error={test.error} />
      </Card>
    </>
  );
}

function Origins({ base, k, reload }: { base: string; k: WidgetKey; reload: () => void }) {
  const [text, setText] = useState(k.allowed_origins.join("\n"));
  const save = useAction();
  return (
    <Card title="Allowed websites">
      <p className="muted small">The chat only works on these. One per line; https://*.example.com allows every subdomain.</p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          save.run(async () => {
            await api(`${base}/widget-keys/${k.id}`, {
              method: "PATCH",
              body: JSON.stringify({ allowed_origins: text.split(/\s+/).filter(Boolean) }),
            });
            reload();
          });
        }}
      >
        <label className="wide">
          Websites
          <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        </label>
        <div className="actions">
          <button className="button" disabled={save.busy}>
            Save
          </button>
        </div>
      </form>
      <ErrorNote error={save.error} />
    </Card>
  );
}

function Mode({ base }: { base: string }) {
  const s = useApi<CommaiSettings>(`${base}/settings`, 0);
  const save = useAction();
  const modes: [CommaiSettings["mode"], string, string][] = [
    ["ai_first", "AI first", "Your AI agent answers and hands over to a person when it should. Needs an AI agent set up."],
    ["human_first", "Human first", "A person answers. The AI can suggest replies but doesn't send them."],
    ["human_only", "Human only", "Only people answer. Out of hours, visitors leave a message."],
  ];
  return (
    <Card title="Who answers">
      <ErrorNote error={s.error} />
      <fieldset className="ch-modes" disabled={!s.data || save.busy}>
        <legend className="sr-only">Who answers</legend>
        {modes.map(([id, label, help]) => (
          <label key={id} className="ch-mode">
            <input
              type="radio"
              name="mode"
              checked={s.data?.mode === id}
              onChange={() =>
                save.run(async () => {
                  await api(`${base}/settings`, { method: "PATCH", body: JSON.stringify({ mode: id }) });
                  s.reload();
                })
              }
            />
            <span>
              <strong>{label}</strong>
              <span className="muted small"> {help}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <ErrorNote error={save.error} />
    </Card>
  );
}

declare global {
  interface Window {
    ExaCaribChat?: { preview: (el: HTMLElement, cfg: Partial<WidgetSettings>) => () => void };
  }
}

function Preview({ settings }: { settings: WidgetSettings }) {
  const box = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let undo: (() => void) | undefined;
    const show = () => {
      if (box.current && window.ExaCaribChat) {
        box.current.textContent = "";
        undo = window.ExaCaribChat.preview(box.current, settings);
      }
    };
    if (window.ExaCaribChat) show();
    else {
      let s = document.querySelector<HTMLScriptElement>("script[data-exacarib-preview]");
      if (!s) {
        s = document.createElement("script");
        s.src = "/api/v1/commai/widget/v1.js";
        s.async = true;
        s.dataset.exacaribPreview = "1";
        document.head.append(s);
      }
      s.addEventListener("load", show);
      s.addEventListener("error", () => setFailed(true));
    }
    return () => undo?.();
  }, [settings]);
  return (
    <>
      <p className="muted small">How the chat looks on your website, with example messages.</p>
      {failed && <p className="pill bad">The preview couldn't load.</p>}
      <div ref={box} className="ch-preview" aria-label="Chat preview" />
    </>
  );
}

function Appearance({ base, k, reload }: { base: string; k: WidgetKey; reload: () => void }) {
  const [s, setS] = useState<WidgetSettings>(k.settings);
  const save = useAction();
  const set = <K extends keyof WidgetSettings>(key: K, v: WidgetSettings[K]) => setS({ ...s, [key]: v });
  return (
    <Card title="Look and options">
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          save.run(async () => {
            const { hours: _hours, ...rest } = s;
            void _hours;
            await api(`${base}/widget-keys/${k.id}`, { method: "PATCH", body: JSON.stringify({ settings: rest }) });
            reload();
          });
        }}
      >
        <label>
          Title
          <input value={s.title} maxLength={300} onChange={(e) => set("title", e.target.value)} />
        </label>
        <label>
          Colour
          <input type="color" value={s.colour} onChange={(e) => set("colour", e.target.value.toUpperCase())} />
        </label>
        <label>
          Corner
          <select value={s.position} onChange={(e) => set("position", e.target.value as "left" | "right")}>
            <option value="right">Bottom right</option>
            <option value="left">Bottom left</option>
          </select>
        </label>
        <label>
          Ask for name and email
          <select value={s.ask_contact} onChange={(e) => set("ask_contact", e.target.value as WidgetSettings["ask_contact"])}>
            <option value="after_first">After the first message</option>
            <option value="never">Never</option>
          </select>
        </label>
        <label className="wide">
          Greeting
          <input value={s.greeting} maxLength={300} onChange={(e) => set("greeting", e.target.value)} />
        </label>
        <label className="wide">
          Out-of-hours message
          <input value={s.offline_message} maxLength={300} onChange={(e) => set("offline_message", e.target.value)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={s.callbacks} onChange={(e) => set("callbacks", e.target.checked)} /> Visitors can ask for a call back
        </label>
        <label className="check">
          <input type="checkbox" checked={s.attachments} onChange={(e) => set("attachments", e.target.checked)} /> Visitors can attach images, PDFs and text files (up to 2 MB)
        </label>
        <label className="check">
          <input type="checkbox" checked={!!s.ai_calls} onChange={(e) => set("ai_calls", e.target.checked)} /> Visitors can talk to the AI assistant (needs the AI agent on)
        </label>
        <div className="actions wide">
          <button className="button" disabled={save.busy}>
            Save
          </button>
        </div>
      </form>
      <ErrorNote error={save.error} />
    </Card>
  );
}

function Hours({ base, k, reload }: { base: string; k: WidgetKey; reload: () => void }) {
  const tz = useApi<CommaiSettings>(`${base}/settings`, 0);
  const initial = k.settings.hours ?? {};
  const always = Object.keys(initial).length === 0;
  const [open247, setOpen247] = useState(always);
  const [hours, setHours] = useState<Record<Day, [string, string] | null>>(() => {
    const out = {} as Record<Day, [string, string] | null>;
    for (const [d] of DAYS) out[d] = always ? (d === "sat" || d === "sun" ? null : ["08:00", "17:00"]) : initial[d] ?? null;
    return out;
  });
  const save = useAction();
  return (
    <Card title="Opening hours">
      <p className="muted small">Outside these hours visitors see the out-of-hours message and can leave a message or ask for a call back. Times are in {tz.data?.timezone ?? "your business's time zone"}.</p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          save.run(async () => {
            await api(`${base}/widget-keys/${k.id}`, { method: "PATCH", body: JSON.stringify({ settings: { hours: open247 ? {} : hours } }) });
            reload();
          });
        }}
      >
        <label className="check wide">
          <input type="checkbox" checked={open247} onChange={(e) => setOpen247(e.target.checked)} /> Always open
        </label>
        {!open247 &&
          DAYS.map(([d, label]) => (
            <fieldset key={d} className="ch-day">
              <legend>{label}</legend>
              <label className="check">
                <input type="checkbox" checked={!!hours[d]} onChange={(e) => setHours({ ...hours, [d]: e.target.checked ? ["08:00", "17:00"] : null })} /> Open
              </label>
              {hours[d] && (
                <span className="ch-times">
                  <input type="time" aria-label={`${label} opens`} value={hours[d]![0]} onChange={(e) => setHours({ ...hours, [d]: [e.target.value, hours[d]![1]] })} />
                  <span aria-hidden="true">to</span>
                  <input type="time" aria-label={`${label} closes`} value={hours[d]![1]} onChange={(e) => setHours({ ...hours, [d]: [hours[d]![0], e.target.value] })} />
                </span>
              )}
            </fieldset>
          ))}
        <div className="actions wide">
          <button className="button" disabled={save.busy}>
            Save hours
          </button>
        </div>
      </form>
      <ErrorNote error={save.error} />
    </Card>
  );
}

// ---- accounts (WhatsApp, SMS, email) ------------------------------------------------------------------

const PROVIDERS: Record<Account["channel"], [string, string][]> = {
  whatsapp: [
    ["simulated", "Simulated (no account needed)"],
    ["twilio", "Twilio"],
    ["360dialog", "360dialog"],
  ],
  sms: [
    ["simulated", "Simulated (no account needed)"],
    ["twilio", "Twilio"],
  ],
  email: [
    ["simulated", "Simulated (no mail server needed)"],
    ["smtp", "SMTP"],
  ],
};

function useAccounts(base: string, channel: Account["channel"]) {
  const all = useApi<Account[]>(`${base}/channel-accounts`, 30_000);
  return { ...all, list: (all.data ?? []).filter((a) => a.channel === channel) };
}

function Accounts({ base, channel, title, placeholder, extra }: { base: string; channel: Account["channel"]; title: string; placeholder: string; extra?: (a: Account, reload: () => void) => ReactNode }) {
  const acc = useAccounts(base, channel);
  const add = useAction();
  const act = useAction();
  const [provider, setProvider] = useState("simulated");
  const [address, setAddress] = useState("");
  const [fresh, setFresh] = useState<Account | null>(null);

  const setStatus = (a: Account, status: Account["status"]) =>
    act.run(async () => {
      await api(`${base}/channel-accounts/${a.id}`, { method: "PATCH", body: JSON.stringify({ status }) });
      acc.reload();
    });

  return (
    <Card title={title}>
      <ErrorNote error={acc.error} />
      {fresh?.secret && (
        <ShownOnce title={channel === "email" ? "The shared secret for inbound email." : "The webhook signing secret."} secret={fresh.secret} onDone={() => setFresh(null)} />
      )}
      {acc.data && acc.list.length === 0 && <p className="muted">None yet.</p>}
      {acc.list.map((a) => (
        <div key={a.id} className="card-inset ch-account">
          <div className="ch-account-head">
            <h3>
              {a.address} {a.simulated && <SimulatedTag />}
            </h3>
            <Pill word={STATUS_WORD[a.status][0]} health={STATUS_WORD[a.status][1]} />
          </div>
          <dl className="ch-dl">
            <dt>Provider</dt>
            <dd>{a.provider_label}</dd>
            <dt>Inbound webhook</dt>
            <dd className="mono wrap small">{a.webhook_url}</dd>
            <dt>Last message in</dt>
            <dd>{a.last_inbound_at ? when(a.last_inbound_at) : "None yet"}</dd>
            <dt>Last sent</dt>
            <dd>{a.last_sent_at ? when(a.last_sent_at) : "None yet"}</dd>
          </dl>
          {a.missing_env.length > 0 && (
            <p className="callout warn small">
              <strong>Not live yet.</strong> {a.provider_label} needs {a.missing_env.join(", ")} set on the server by ExaCarib before it can send or receive.
            </p>
          )}
          {a.last_error && (
            <p className="pill bad small" role="note">
              Last error {a.last_error_at ? when(a.last_error_at) : ""}: {a.last_error}
            </p>
          )}
          <div className="form-actions">
            {a.status !== "live" && (
              <button type="button" className="button small" disabled={act.busy} onClick={() => setStatus(a, "live")}>
                Set live
              </button>
            )}
            {a.status === "live" && (
              <button type="button" className="button secondary small" disabled={act.busy} onClick={() => setStatus(a, "paused")}>
                Pause sending
              </button>
            )}
            <button
              type="button"
              className="button danger-text small"
              disabled={act.busy}
              onClick={() => {
                if (!window.confirm(`Remove ${a.address}? Replies to its conversations stop going out.`)) return;
                act.run(async () => {
                  await api(`${base}/channel-accounts/${a.id}`, { method: "DELETE" });
                  acc.reload();
                });
              }}
            >
              Remove
            </button>
          </div>
          {extra?.(a, acc.reload)}
          {(a.simulated || channel === "email") && <SimulateInbound base={base} a={a} onDone={acc.reload} />}
        </div>
      ))}
      <ErrorNote error={act.error} />
      <h3 className="ch-h3">Add</h3>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          add.run(async () => {
            const a = await api<Account>(`${base}/channel-accounts`, { method: "POST", body: JSON.stringify({ channel, provider, address }) });
            setFresh(a);
            setAddress("");
            acc.reload();
          });
        }}
      >
        <label>
          Provider
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            {PROVIDERS[channel].map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          {channel === "email" ? "Email address" : "Number"}
          <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder={placeholder} required />
        </label>
        <div className="actions">
          <button className="button" disabled={add.busy}>
            Add
          </button>
        </div>
      </form>
      <ErrorNote error={add.error} />
    </Card>
  );
}

function SimulateInbound({ base, a, onDone }: { base: string; a: Account; onDone: () => void }) {
  const [from, setFrom] = useState(a.channel === "email" ? "ana@example.org" : "+18685550101");
  const [body, setBody] = useState("Hello, I'd like some help please.");
  const run = useAction();
  const [ok, setOk] = useState(false);
  return (
    <details className="ch-sim">
      <summary>Send a test message in{a.simulated ? " (simulated)" : ""}</summary>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          setOk(false);
          run.run(async () => {
            await api(`${base}/channel-accounts/${a.id}/simulate-inbound`, {
              method: "POST",
              body: JSON.stringify({ from, body, name: "Test customer", subject: "Test message" }),
            });
            setOk(true);
            onDone();
          });
        }}
      >
        <label>
          From
          <input value={from} onChange={(e) => setFrom(e.target.value)} required />
        </label>
        <label className="wide">
          Message
          <input value={body} onChange={(e) => setBody(e.target.value)} required />
        </label>
        <div className="actions">
          <button className="button secondary small" disabled={run.busy}>
            Send in
          </button>
          {ok && (
            <span className="ok-note" role="status">
              In the inbox. <Link to="/commai">Open the inbox</Link>
            </span>
          )}
        </div>
      </form>
      <ErrorNote error={run.error} />
    </details>
  );
}

function Outbox({ base, channel }: { base: string; channel: string }) {
  const { data } = useApi<OutboxRow[]>(`${base}/channels/outbox?limit=50`, 15_000);
  const rows = (data ?? []).filter((r) => r.channel === channel).slice(0, 10);
  if (rows.length === 0) return null;
  return (
    <Card title="Sent by the simulator" note={<SimulatedTag />}>
      <p className="muted small">What would have gone out. Nothing reached a real phone or inbox.</p>
      <div className="table-wrap">
        <table className="paths dt stack">
          <thead>
            <tr>
              <th scope="col">To</th>
              <th scope="col">Message</th>
              <th scope="col">When</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.to_address}</td>
                <td data-label="Message" className="cell-wrap">
                  {r.template && <span className="tag">Template {r.template}</span>} {r.body}
                </td>
                <td data-label="When" className="mono">
                  {when(r.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ---- WhatsApp ----------------------------------------------------------------------------------------

function WhatsApp({ base }: { base: string }) {
  const acc = useAccounts(base, "whatsapp");
  const tpls = useApi<Template[]>(`${base}/whatsapp-templates`, 30_000);
  const real = acc.list.filter((a) => !a.simulated);
  const liveReal = real.some((a) => a.status === "live");
  const approved = (tpls.data ?? []).some((t) => t.status === "approved");
  const steps: [string, string, "done" | "todo" | "outside"][] = [
    ["Meta Business account", "Your business's own account at business.facebook.com. You create it; we can't check it from here.", "outside"],
    ["Business verification", "Meta checks your business documents. It can take a few days. We can't check it from here.", "outside"],
    [
      "Connect your number through the provider",
      "Through the provider's Embedded Signup (Twilio or 360dialog). ExaCarib hasn't chosen the provider yet, so this step isn't open.",
      real.length ? "done" : "todo",
    ],
    ["Number live in CommAI", "The provider's credentials are set on the server and the number is set live.", liveReal ? "done" : "todo"],
    ["At least one approved template", "Needed to message a customer who hasn't written in the last 24 hours.", approved ? "done" : "todo"],
  ];
  return (
    <>
      <Card title="What it takes">
        <p className="muted">
          CommAI uses the official WhatsApp Business Platform through an approved provider. You can reply freely within 24 hours of a customer's last message; after that, only an
          approved template can be sent. WhatsApp calling is a separate feature and isn't included.
        </p>
        <ol className="ch-steps">
          {steps.map(([title, help, state]) => (
            <li key={title}>
              <Pill word={state === "done" ? "Done" : state === "outside" ? "Outside CommAI" : "To do"} health={state === "done" ? "ok" : "warn"} /> <strong>{title}</strong>
              <span className="muted small"> {help}</span>
            </li>
          ))}
        </ol>
        {real.length === 0 && (
          <p className="callout small">
            Until then you can try everything with a <strong>simulated</strong> number: messages go in and out of the inbox, but nothing reaches a real phone.
          </p>
        )}
      </Card>
      <Accounts base={base} channel="whatsapp" title="Numbers" placeholder="+1 868 555 0100" />
      <Templates base={base} tpls={tpls} />
      <Outbox base={base} channel="whatsapp" />
      <Diagnostics base={base} area="whatsapp" />
    </>
  );
}

function Templates({ base, tpls }: { base: string; tpls: Loaded<Template[]> }) {
  const add = useAction();
  const act = useAction();
  const [name, setName] = useState("");
  const [lang, setLang] = useState("en");
  const [category, setCategory] = useState("utility");
  const [body, setBody] = useState("Hello {{1}}, your appointment is on {{2}}. Reply here if you need to change it.");
  const review = (t: Template, status: Template["status"]) =>
    act.run(async () => {
      await api(`${base}/whatsapp-templates/${t.id}/review`, { method: "POST", body: JSON.stringify({ status }) });
      tpls.reload();
    });
  return (
    <Card title="Templates">
      <p className="muted small">Meta reviews every template before it can be sent. Write values to fill in as {"{{1}}"}, {"{{2}}"}…</p>
      <ErrorNote error={tpls.error} />
      {(tpls.data ?? []).length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Text</th>
                <th scope="col">Status</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(tpls.data ?? []).map((t) => (
                <tr key={t.id}>
                  <td className="cell-wrap">
                    <strong className="mono">{t.name}</strong>
                    <span className="sub">
                      {t.language} · {t.category}
                    </span>
                  </td>
                  <td data-label="Text" className="cell-wrap small">
                    {t.body}
                  </td>
                  <td data-label="Status">
                    <Pill word={TEMPLATE_WORD[t.status][0]} health={TEMPLATE_WORD[t.status][1]} />
                    {t.status_by && <span className="sub">by {t.status_by}</span>}
                  </td>
                  <td className="actions">
                    {t.status !== "approved" && (
                      <button type="button" className="button secondary small" disabled={act.busy} onClick={() => review(t, "approved")}>
                        Approve
                      </button>
                    )}{" "}
                    {t.status === "pending" && (
                      <button type="button" className="button danger-text small" disabled={act.busy} onClick={() => review(t, "rejected")}>
                        Reject
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small muted">With a simulated number, Approve stands in for Meta's review. With a real provider, approval comes from Meta and ExaCarib records it here.</p>
      <ErrorNote error={act.error} />
      <h3 className="ch-h3">New template</h3>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          add.run(async () => {
            await api(`${base}/whatsapp-templates`, { method: "POST", body: JSON.stringify({ name, language: lang, category, body }) });
            setName("");
            tpls.reload();
          });
        }}
      >
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} pattern="[a-z0-9_]{1,60}" title="Lower case letters, digits and underscores" placeholder="appointment_reminder" required />
        </label>
        <label>
          Language
          <input value={lang} onChange={(e) => setLang(e.target.value)} pattern="[a-z]{2}(_[A-Z]{2})?" required />
        </label>
        <label>
          Category
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="utility">Utility</option>
            <option value="marketing">Marketing</option>
            <option value="authentication">Authentication</option>
          </select>
        </label>
        <label className="wide">
          Text
          <textarea rows={3} value={body} maxLength={1024} onChange={(e) => setBody(e.target.value)} required />
        </label>
        <div className="actions">
          <button className="button" disabled={add.busy}>
            Submit for approval
          </button>
        </div>
      </form>
      <ErrorNote error={add.error} />
    </Card>
  );
}

// ---- SMS ---------------------------------------------------------------------------------------------------

function Limits({ base, a, reload }: { base: string; a: Account; reload: () => void }) {
  const current = (a.settings.daily_limits as Record<string, number> | undefined) ?? {};
  const [text, setText] = useState(
    Object.entries(current)
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const save = useAction();
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        save.run(async () => {
          const limits: Record<string, number> = {};
          for (const line of text.split("\n")) {
            const [k, v] = line.split("=").map((x) => x.trim());
            if (!k) continue;
            if (!/^\d+$/.test(v ?? "")) throw new Error(`Write each limit as COUNTRY=number, like TT=500 (line "${line}").`);
            limits[k.toUpperCase() === "*" ? "*" : k.toUpperCase()] = Number(v);
          }
          await api(`${base}/channel-accounts/${a.id}`, { method: "PATCH", body: JSON.stringify({ settings: { daily_limits: limits } }) });
          reload();
        });
      }}
    >
      <label className="wide">
        Daily limits per country (one per line, like TT=500; * for everywhere else; 1,000 when not set)
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="actions">
        <button className="button secondary small" disabled={save.busy}>
          Save limits
        </button>
      </div>
      <ErrorNote error={save.error} />
    </form>
  );
}

function Sms({ base }: { base: string }) {
  return (
    <>
      <Card title="How SMS works here">
        <p className="muted">
          Replies go out through the same provider as WhatsApp. When someone texts STOP, STOPALL, UNSUBSCRIBE, CANCEL, END or QUIT, they're never texted again until they send
          START or UNSTOP. Each number has a daily sending limit per country.
        </p>
      </Card>
      <Accounts base={base} channel="sms" title="Numbers" placeholder="+1 868 555 0100" extra={(a, reload) => <Limits base={base} a={a} reload={reload} />} />
      <Outbox base={base} channel="sms" />
      <Diagnostics base={base} area="sms" />
    </>
  );
}

// ---- Email -----------------------------------------------------------------------------------------------------

function Email({ base }: { base: string }) {
  return (
    <>
      <Card title="How email works here">
        <p className="muted">
          Your mail provider forwards incoming email to the inbound webhook below, with the shared secret in an <code>X-Exa-Email-Secret</code> header (or <code>?secret=</code> on
          the URL). It accepts a simple JSON format and Mailgun's forwarding format. Replies go out by SMTP and thread with the customer's email. Anyone who unsubscribes only gets
          replies to their own emails from the last 24 hours.
        </p>
        <pre className="ch-code">
          <code>{`{"from": "Ana <ana@example.org>", "subject": "...", "text": "...",
 "message_id": "<...>", "in_reply_to": "<...>", "references": "<...> <...>"}`}</code>
        </pre>
      </Card>
      <Accounts base={base} channel="email" title="Addresses" placeholder="help@example.com" extra={(a, reload) => <EmailLimits base={base} a={a} reload={reload} />} />
      <Outbox base={base} channel="email" />
      <Diagnostics base={base} area="email" />
    </>
  );
}
