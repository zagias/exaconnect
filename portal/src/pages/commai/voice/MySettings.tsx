import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, useAction } from "../../../ui";
import { when } from "../lib";
import { PriceLines } from "./ChangeBox";
import type { Me, PriceImpact } from "./types";

interface Proposal {
  understood: boolean;
  message?: string;
  id?: string;
  summary?: string;
  scope?: "self" | "admin";
  price_impact?: PriceImpact;
}

interface MyCall {
  call_id: string;
  direction: string;
  from_number: string;
  to_number: string;
  ended_at: string;
  seconds: number;
  status: string;
  has_recording: boolean;
}

function localInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Each person's own forwarding, do not disturb, voicemail, calls and recordings. */
export default function MySettings({ base, onChange }: { base: string; onChange?: () => void }) {
  const me = useApi<Me>(`${base}/voice/me`, 30_000);
  if (me.error && !me.data) {
    return (
      <Card title="My phone">
        <p className="muted">{me.error}</p>
      </Card>
    );
  }
  if (!me.data) return null;
  const reload = () => {
    me.reload();
    onChange?.();
  };
  return (
    <>
      <Say base={base} onApplied={reload} />
      <Forwarding base={base} me={me.data} onSaved={reload} />
      <Voicemail base={base} me={me.data} onSaved={reload} />
      <History base={base} me={me.data} />
    </>
  );
}

function Say({ base, onApplied }: { base: string; onApplied: () => void }) {
  const [text, setText] = useState("");
  const [p, setP] = useState<Proposal | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();
  const ask = (e: FormEvent) => {
    e.preventDefault();
    setDone(null);
    act.run(async () => setP(await api<Proposal>(`${base}/voice/say`, { method: "POST", body: JSON.stringify({ text }) })));
  };
  const confirm = () =>
    act.run(async () => {
      if (!p?.id) return;
      const body = p.price_impact?.changes_bill ? { accepted_price: { monthly_delta: p.price_impact.monthly_delta, one_time: p.price_impact.one_time } } : {};
      await api(`${base}/voice/say/${p.id}/confirm`, { method: "POST", body: JSON.stringify(body) });
      setDone(p.summary ?? "Done.");
      setP(null);
      setText("");
      onApplied();
    });
  const cancel = () =>
    act.run(async () => {
      if (p?.id) await api(`${base}/voice/say/${p.id}/cancel`, { method: "POST" });
      setP(null);
    });
  return (
    <Card title="Say what you want">
      <form className="voice-say" onSubmit={ask}>
        <label htmlFor="voice-say-input" className="sr-only">
          What do you want to change?
        </label>
        <input
          id="voice-say-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Forward my calls to my mobile until 5"
          maxLength={500}
          required
          minLength={2}
        />
        <button className="button" disabled={act.busy}>
          Show me the change
        </button>
      </form>
      <p className="muted small">Nothing changes until you confirm. Try: “do not disturb until 3pm”, “send my voicemail to email”, “stop forwarding”.</p>
      <div aria-live="polite">
        {p && !p.understood && <p className="pill warn">{p.message}</p>}
        {p?.understood && (
          <div className="voice-panel">
            <p>
              <strong>{p.summary}</strong>
            </p>
            {p.price_impact && <PriceLines price={p.price_impact} />}
            <button className="button" onClick={confirm} disabled={act.busy}>
              Confirm
            </button>{" "}
            <button className="button secondary" onClick={cancel} disabled={act.busy}>
              Cancel
            </button>
          </div>
        )}
        {done && <p className="pill ok">Done: {done}</p>}
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Forwarding({ base, me, onSaved }: { base: string; me: Me; onSaved: () => void }) {
  const [to, setTo] = useState(me.forward_to);
  const [until, setUntil] = useState(localInput(me.forward_until));
  const [dndUntil, setDndUntil] = useState(localInput(me.dnd_until));
  const act = useAction();
  const patch = (body: Record<string, unknown>) =>
    act.run(async () => {
      await api(`${base}/voice/me`, { method: "PATCH", body: JSON.stringify(body) });
      onSaved();
    });
  return (
    <Card title={`My phone: extension ${me.extension}`}>
      <p className="small">
        {me.dnd ? (
          <span className="pill warn">Do not disturb{me.dnd_until ? ` until ${new Date(me.dnd_until).toLocaleString("en-GB")}` : ""}</span>
        ) : me.forward_to ? (
          <span className="pill warn">
            Forwarded to {me.forward_to}
            {me.forward_until ? ` until ${new Date(me.forward_until).toLocaleString("en-GB")}` : ""}
          </span>
        ) : (
          <span className="pill ok">Ringing your phones</span>
        )}
        {me.site && <span className="muted small"> Site: {me.site}</span>}
      </p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          patch({ forward_to: to, forward_until: to && until ? new Date(until).toISOString() : null });
        }}
      >
        <label>
          Forward my calls to
          <input value={to} onChange={(e) => setTo(e.target.value)} placeholder={me.mobile || "+1 868 … or an extension"} maxLength={40} />
        </label>
        <label>
          Until (optional)
          <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
        </label>
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            Save forwarding
          </button>{" "}
          {me.forward_to && (
            <button type="button" className="button secondary" disabled={act.busy} onClick={() => (setTo(""), patch({ forward_to: "" }))}>
              Stop forwarding
            </button>
          )}
        </div>
      </form>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          patch(me.dnd ? { dnd: false } : { dnd: true, dnd_until: dndUntil ? new Date(dndUntil).toISOString() : null });
        }}
      >
        {!me.dnd && (
          <label>
            Do not disturb until (optional)
            <input type="datetime-local" value={dndUntil} onChange={(e) => setDndUntil(e.target.value)} />
          </label>
        )}
        <div className="actions wide">
          <button className="button secondary" disabled={act.busy}>
            {me.dnd ? "Turn off do not disturb" : "Turn on do not disturb"}
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Voicemail({ base, me, onSaved }: { base: string; me: Me; onSaved: () => void }) {
  const [greeting, setGreeting] = useState(me.voicemail_greeting);
  const [toEmail, setToEmail] = useState(me.voicemail_to_email);
  const act = useAction();
  return (
    <Card title="Voicemail">
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          act.run(async () => {
            await api(`${base}/voice/me`, { method: "PATCH", body: JSON.stringify({ voicemail_greeting: greeting, voicemail_to_email: toEmail }) });
            onSaved();
          });
        }}
      >
        <label className="wide">
          Greeting (read out when you can't answer)
          <textarea rows={2} value={greeting} onChange={(e) => setGreeting(e.target.value)} maxLength={500} placeholder={`You've reached ${me.name}. Please leave a message.`} />
        </label>
        <label className="check wide">
          <input type="checkbox" checked={toEmail} onChange={(e) => setToEmail(e.target.checked)} disabled={!me.email} /> Send my voicemail to{" "}
          {me.email || "my email (ask your voice admin to add your address)"}
        </label>
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            Save voicemail
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function History({ base, me }: { base: string; me: Me }) {
  const calls = useApi<MyCall[]>(`${base}/voice/me/calls`, 30_000);
  const rec = useApi<unknown[]>(me.recording_access === "none" ? null : `${base}/voice/me/recordings`, 0);
  const recError = rec.error;
  return (
    <Card title="My calls">
      <ErrorNote error={calls.error} />
      {calls.data && calls.data.length === 0 && <p className="muted">No calls yet.</p>}
      <ul className="plain-list">
        {(calls.data ?? []).map((c) => (
          <li key={c.call_id} className="row-between">
            <span>
              {c.direction === "inbound" ? "From" : "To"} <span className="mono">{c.direction === "inbound" ? c.from_number : c.to_number}</span>{" "}
              <span className="muted small">
                {when(c.ended_at)}, {c.seconds} s
              </span>
              {c.status === "blocked" && <span className="pill small bad">Blocked</span>}
            </span>
            {c.has_recording && me.recording_access !== "none" && <span className="tag">Recorded</span>}
          </li>
        ))}
      </ul>
      <p className="muted small">
        {me.recording_access === "none"
          ? "Your company's policy doesn't let anyone play call recordings."
          : me.recording_access === "admins"
            ? "Only voice admins can play recordings."
            : "You can play recordings of your own calls."}
        {recError && !recError.includes("policy") ? ` ${recError}` : ""}
      </p>
    </Card>
  );
}
