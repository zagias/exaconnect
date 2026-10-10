import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, EmptyState, useAction } from "../../../ui";
import { VertoPhone, type CallState } from "./verto";
import "./dialer.css";

/* Shape from controller/exaconnect_controller/commai/api/webphone.py (ADR 0039). */
interface WebPhone {
  enabled: boolean;
  extension: string;
  reason?: string;
  url?: string;
  login?: string;
  password?: string;
  name?: string;
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"];
const STATE_WORD: Record<CallState, string> = {
  idle: "Ready",
  calling: "Calling…",
  ringing: "Ringing…",
  incoming: "Incoming call",
  active: "On a call",
  ended: "Call ended",
};

/** The browser phone for staff: their own extension, through FreeSWITCH Verto over secure WebSocket. */
export default function Dialer({ base }: { base: string }) {
  const cfg = useApi<WebPhone>(`${base}/voice/webphone`, 0);
  const phone = useRef<VertoPhone | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState("Not signed in");
  const [state, setState] = useState<CallState>("idle");
  const [detail, setDetail] = useState("");
  const [number, setNumber] = useState("");
  const [muted, setMuted] = useState(false);
  const act = useAction();

  useEffect(() => () => phone.current?.disconnect(), []);

  const signIn = () =>
    act.run(async () => {
      // Fetch the sign-in details only when the person asks, and keep them in memory only.
      const c = await api<WebPhone>(`${base}/voice/webphone?sign_in=true`);
      if (!c.enabled || !c.url || !c.login || !c.password) throw new Error(c.reason || "The browser phone is not switched on.");
      phone.current?.disconnect();
      const p = new VertoPhone(c.url, c.login, c.password, { name: c.name || "", extension: c.extension }, {
        onStatus: (ok, msg) => {
          setConnected(ok);
          setStatus(msg);
        },
        onCall: (s, d) => {
          setState(s);
          setDetail(d.reason || [d.name, d.number].filter(Boolean).join(" "));
          if (s === "ended") setMuted(false);
        },
        onRemoteStream: (stream) => {
          if (audio.current) audio.current.srcObject = stream;
        },
      });
      phone.current = p;
      p.connect();
    });
  const dial = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => phone.current?.call(number));
  };
  const key = (k: string) => {
    if (state === "active") phone.current?.dtmf(k);
    else setNumber((n) => (n + k).slice(0, 20));
  };
  const onCall = state === "calling" || state === "ringing" || state === "active";

  if (cfg.error)
    return (
      <Card title="Browser phone">
        {/extension/i.test(cfg.error) ? (
          <EmptyState title="No phone extension yet">
            The browser phone rings on your own extension. A voice admin adds you under Phone, People and numbers.
          </EmptyState>
        ) : (
          <ErrorNote error={cfg.error} />
        )}
      </Card>
    );
  if (!cfg.data) return null;
  if (!cfg.data.enabled) {
    return (
      <Card title="Browser phone">
        <p className="muted">{cfg.data.reason}</p>
        <p className="small muted">Your extension is {cfg.data.extension}. Use a desk phone or the softphone app until then.</p>
      </Card>
    );
  }
  return (
    <Card title="Browser phone" note={<span className={`pill ${connected ? "ok" : "shadow"}`}>{connected ? "Signed in" : "Signed out"}</span>}>
      <p className="small" role="status" aria-live="polite">
        {status}. {STATE_WORD[state]}
        {detail ? `: ${detail}` : ""}
      </p>
      {!connected && (
        <button className="button" disabled={act.busy} onClick={signIn}>
          Sign in to the browser phone
        </button>
      )}
      {connected && (
        <div className="voice-dialer">
          {state === "incoming" ? (
            <div className="auto-row" role="group" aria-label="Incoming call">
              <button className="button" onClick={() => act.run(async () => phone.current?.answer())}>
                Answer
              </button>
              <button className="button secondary" onClick={() => phone.current?.hangup()}>
                Decline
              </button>
            </div>
          ) : (
            <form onSubmit={dial} className="auto-row" aria-label="Place a call">
              <label htmlFor="dialer-number" className="sr-only">
                Extension or number
              </label>
              <input
                id="dialer-number"
                inputMode="tel"
                autoComplete="off"
                value={number}
                onChange={(e) => setNumber(e.target.value.replace(/[^\d+*# ]/g, "").slice(0, 20))}
                placeholder="Extension or number"
                disabled={onCall}
              />
              {/* Separate keys: React must not reuse the Hang up button as the Call (submit)
                  button mid-click, or hanging up would dial the number again. */}
              {!onCall ? (
                <button key="call" className="button" disabled={act.busy || !number.trim()}>
                  Call
                </button>
              ) : (
                <button
                  key="hangup"
                  type="button"
                  className="button danger"
                  onClick={(e) => {
                    e.preventDefault();
                    phone.current?.hangup();
                  }}
                >
                  Hang up
                </button>
              )}
              {state === "active" && (
                <button
                  type="button"
                  className="button secondary"
                  aria-pressed={muted}
                  onClick={() => {
                    phone.current?.mute(!muted);
                    setMuted(!muted);
                  }}
                >
                  {muted ? "Unmute" : "Mute"}
                </button>
              )}
            </form>
          )}
          <div className="voice-keypad" role="group" aria-label={state === "active" ? "Keypad: sends tones" : "Keypad"}>
            {KEYS.map((k) => (
              <button key={k} type="button" className="button secondary" onClick={() => key(k)} aria-label={k === "*" ? "Star" : k === "#" ? "Hash" : k}>
                {k}
              </button>
            ))}
          </div>
          <button className="button secondary small" onClick={() => phone.current?.disconnect()} style={{ marginTop: 8 }}>
            Sign out of the browser phone
          </button>
        </div>
      )}
      <audio ref={audio} autoPlay />
      <p className="small muted">Calls use your extension and your company's call rules. Calls outside the business work once ExaCarib connects the phone provider.</p>
      <ErrorNote error={act.error} />
    </Card>
  );
}
