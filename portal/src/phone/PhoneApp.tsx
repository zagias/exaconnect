import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, useApi } from "../api";
import { AuthGate, useAuth } from "../auth";
import { CustomerProvider, useCustomer } from "../customer";
import { plainReason, VertoPhone, type CallState } from "../pages/commai/voice/verto";
import type { Me } from "../pages/commai/voice/types";
import { Ringer, RINGTONES, type Ringtone } from "./ringer";

/*
 * Jibsy Phone: the browser phone as an app of its own (installable on Mac, Windows,
 * iOS and Android). Same sign-in, same extension and same Verto connection as the
 * portal's Browser phone tab; the person never needs the portal.
 */

interface WebPhone {
  enabled: boolean;
  extension: string | null;
  reason?: string;
  url?: string;
  login?: string;
  password?: string;
  name?: string;
}

interface Contacts {
  me: { name: string; extension: string };
  people: { name: string; extension: string }[];
  lines: { name: string; extension: string; kind: "ring_group" | "queue" | "menu" }[];
}

interface Recent {
  number: string;
  name: string;
  dir: "in" | "out" | "missed";
  at: number;
  secs: number;
}

export interface Settings {
  ringtone: Ringtone;
  volume: number;
  micId: string;
  speakerId: string;
  notify: boolean;
  autoConnect: boolean;
  theme: "system" | "light" | "dark";
}

const DEFAULTS: Settings = {
  ringtone: "classic",
  volume: 0.7,
  micId: "",
  speakerId: "",
  notify: false,
  autoConnect: true,
  theme: "system",
};
const SETTINGS_KEY = "jibsy.phone.settings";
const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"];
const LETTERS: Record<string, string> = {
  "2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ", "0": "+",
};
const TABS = ["keypad", "contacts", "recents", "settings"] as const;
type Tab = (typeof TABS)[number];
const KIND_WORD = { ring_group: "Ring group", queue: "Queue", menu: "Menu" } as const;

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private window: settings last until the app closes */
  }
}

function loadList<T>(key: string): T[] {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

/** Every ExaCarib address serves the phone connection at /verto, so the app uses the one it
 * was opened on: the page may only connect to its own address. */
export function sameHost(url: string): string {
  try {
    const u = new URL(url);
    if (u.pathname === "/verto" && u.host !== window.location.host) return `wss://${window.location.host}/verto`;
  } catch {
    /* not a URL: VertoPhone says so */
  }
  return url;
}

function clock(secs: number): string {
  const m = Math.floor(secs / 60);
  return `${m}:${String(secs % 60).padStart(2, "0")}`;
}

function ago(at: number): string {
  const mins = Math.round((Date.now() - at) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return new Date(at).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export default function PhoneApp() {
  return (
    <AuthGate product="Phone" intro="Calls on your company extension, from any device.">
      <CustomerProvider>
        <Phone />
      </CustomerProvider>
    </AuthGate>
  );
}

function Phone() {
  const { current } = useCustomer();
  const base = current ? `/commai/customers/${current.id}` : null;
  const [settings, setSettingsState] = useState<Settings>(() => load(SETTINGS_KEY, DEFAULTS));
  const setSettings = (s: Partial<Settings>) =>
    setSettingsState((prev) => {
      const next = { ...prev, ...s };
      save(SETTINGS_KEY, next);
      return next;
    });
  const [tab, setTab] = useState<Tab>(() => {
    const h = window.location.hash.slice(1) as Tab;
    return TABS.includes(h) ? h : "keypad";
  });
  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash.slice(1) as Tab;
      if (TABS.includes(h)) setTab(h);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (t: Tab) => {
    setTab(t);
    window.history.replaceState(null, "", `#${t}`);
  };

  useEffect(() => {
    const root = document.documentElement;
    if (settings.theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", settings.theme);
  }, [settings.theme]);

  const cfg = useApi<WebPhone>(base ? `${base}/voice/webphone` : null, 0);
  const phone = usePhone(base, cfg.data, settings);
  const [number, setNumber] = useState("");
  const dial = (to: string, name = "") => {
    if (!to.trim()) return;
    setNumber(to);
    phone.call(to, name);
  };

  if (!base || (!cfg.data && !cfg.error)) {
    return (
      <AppFrame>
        <p className="ph-empty" role="status">
          Loading your phone…
        </p>
      </AppFrame>
    );
  }
  if (cfg.error || !cfg.data?.enabled) {
    const noExt = /extension/i.test(cfg.error || "") || cfg.data?.extension === null;
    return (
      <AppFrame>
        <div className="ph-empty">
          <h1>{noExt ? "No phone extension yet" : "The phone isn't switched on yet"}</h1>
          <p>
            {noExt
              ? "Jibsy Phone rings on your own extension. Ask your company's voice admin to add you to the phone system, then open this app again."
              : cfg.data?.reason || cfg.error}
          </p>
          <SignOutButton />
        </div>
      </AppFrame>
    );
  }

  return (
    <AppFrame
      status={
        <span className={`ph-status ${phone.connected ? "on" : "off"}`} role="status" aria-live="polite">
          <span className="ph-dot" aria-hidden="true" />
          {phone.connected ? `Ext ${cfg.data.extension}` : phone.connecting ? "Connecting…" : "Offline"}
        </span>
      }
    >
      {!phone.connected && !phone.connecting && (
        <div className="ph-banner" role="alert">
          <span>{phone.status || "Not connected, so calls can't ring here."}</span>
          <button className="button small" onClick={phone.connect}>
            Connect
          </button>
        </div>
      )}
      <main className="ph-main" id="main">
        {tab === "keypad" && (
          <Keypad number={number} setNumber={setNumber} onCall={dial} disabled={!phone.connected} />
        )}
        {tab === "contacts" && <ContactsTab base={base} onCall={dial} disabled={!phone.connected} />}
        {tab === "recents" && <RecentsTab recents={phone.recents} onCall={dial} onClear={phone.clearRecents} />}
        {tab === "settings" && (
          <SettingsTab base={base} settings={settings} setSettings={setSettings} ringer={phone.ringer} ext={cfg.data.extension} />
        )}
      </main>
      <nav className="ph-tabs" aria-label="Phone">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? "active" : ""} aria-current={tab === t ? "page" : undefined} onClick={() => go(t)}>
            <TabIcon tab={t} />
            <span>{t[0].toUpperCase() + t.slice(1)}</span>
          </button>
        ))}
      </nav>
      {phone.state === "incoming" && (
        <div className="ph-overlay" role="dialog" aria-modal="true" aria-label="Incoming call">
          <p className="ph-overlay-label">Incoming call</p>
          <p className="ph-overlay-name">{phone.peer.name || phone.peer.number}</p>
          {phone.peer.name && <p className="ph-overlay-number">{phone.peer.number}</p>}
          <div className="ph-overlay-actions">
            <button className="ph-round decline" onClick={phone.hangup} aria-label="Decline">
              <PhoneGlyph down />
            </button>
            <button className="ph-round answer" onClick={phone.answer} aria-label="Answer">
              <PhoneGlyph />
            </button>
          </div>
        </div>
      )}
      {(phone.state === "calling" || phone.state === "ringing" || phone.state === "active") && (
        <InCall phone={phone} />
      )}
      {phone.state !== "incoming" && phone.state !== "calling" && phone.state !== "ringing" && phone.state !== "active" && phone.endReason && (
        <p className="ph-toast" role="status">
          {phone.endReason}
        </p>
      )}
      <audio ref={phone.audioRef} autoPlay />
    </AppFrame>
  );
}

function AppFrame({ status, children }: { status?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="ph-app">
      <header className="ph-head">
        <span className="ph-logo" aria-hidden="true">
          <PhoneGlyph />
        </span>
        <span className="ph-title">Jibsy Phone</span>
        {status}
      </header>
      {children}
    </div>
  );
}

function SignOutButton() {
  const { signOut } = useAuth();
  return (
    <button className="button secondary" onClick={signOut}>
      Sign out
    </button>
  );
}

/* ---- The connection, calls, ringing and the call list ---------------------------- */

type PhoneHook = ReturnType<typeof usePhone>;

function usePhone(base: string | null, cfg: WebPhone | null, settings: Settings) {
  const phoneRef = useRef<VertoPhone | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const ringer = useMemo(() => new Ringer(), []);
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [status, setStatus] = useState("");
  const [state, setState] = useState<CallState>("idle");
  const [peer, setPeer] = useState<{ number: string; name: string }>({ number: "", name: "" });
  const [endReason, setEndReason] = useState("");
  // A short message after a call ends or can't start; it stays long enough to read.
  const say = useCallback((why: string) => {
    setEndReason(why);
    if (why) setTimeout(() => setEndReason((r) => (r === why ? "" : r)), 7000);
  }, []);
  const [muted, setMuted] = useState(false);
  const [since, setSince] = useState<number | null>(null);
  const recentsKey = cfg?.extension ? `jibsy.phone.recents.${cfg.extension}` : "";
  const [recents, setRecents] = useState<Recent[]>([]);
  const wanted = useRef(false);
  const retry = useRef(0);
  const call = useRef<{ dir: "in" | "out"; number: string; name: string; answeredAt: number | null } | null>(null);
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  // The contact's name for the call being placed (from Contacts or Recents).
  const pendingName = useRef("");
  const setPeerName = (n: string) => (pendingName.current = n);

  useEffect(() => setRecents(recentsKey ? loadList<Recent>(recentsKey) : []), [recentsKey]);
  // Browsers play sound only after a tap: the first tap anywhere readies the ringtone.
  useEffect(() => {
    const unlock = () => ringer.unlock();
    window.addEventListener("pointerdown", unlock, { once: true });
    window.addEventListener("keydown", unlock, { once: true });
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
    };
  }, [ringer]);

  const remember = useCallback(
    (r: Recent) =>
      setRecents((prev) => {
        const next = [r, ...prev].slice(0, 100);
        if (recentsKey) save(recentsKey, next);
        return next;
      }),
    [recentsKey],
  );

  const connect = useCallback(async () => {
    if (!base || phoneRef.current) return;
    wanted.current = true;
    setConnecting(true);
    setStatus("");
    try {
      // The sign-in details are fetched on each connect and kept in memory only.
      const c = await api<WebPhone>(`${base}/voice/webphone?sign_in=true`);
      if (!c.enabled || !c.url || !c.login || !c.password) throw new Error(c.reason || "The phone is not switched on.");
      const p = new VertoPhone(sameHost(c.url), c.login, c.password, { name: c.name || "", extension: c.extension || "" }, {
        onStatus: (ok, msg) => {
          setConnected(ok);
          setConnecting(false);
          setStatus(ok ? "" : msg);
          if (ok) retry.current = 0;
          if (!ok && phoneRef.current === p) {
            phoneRef.current = null;
            // Lost the connection (sleep, network change): try again, more slowly each time.
            if (wanted.current) {
              const wait = Math.min(30_000, 1000 * 2 ** retry.current++);
              setTimeout(() => wanted.current && !phoneRef.current && void connectRef.current(), wait);
            }
          }
        },
        onCall: (s, d) => {
          setState(s);
          if (s === "incoming") {
            setPeer({ number: d.number || "", name: d.name || "" });
            call.current = { dir: "in", number: d.number || "", name: d.name || "", answeredAt: null };
            ringer.start(settingsRef.current.ringtone, settingsRef.current.volume);
            notify(d.name || d.number || "Unknown caller");
          } else ringer.stop();
          if (s === "calling") {
            setPeer({ number: d.number || "", name: pendingName.current });
            pendingName.current = "";
            setEndReason("");
          }
          if (s === "active") {
            setSince(Date.now());
            if (call.current) call.current.answeredAt = Date.now();
          }
          if (s === "ended") {
            const c = call.current;
            if (c) {
              const secs = c.answeredAt ? Math.round((Date.now() - c.answeredAt) / 1000) : 0;
              remember({ number: c.number, name: c.name, dir: c.dir === "in" && !c.answeredAt ? "missed" : c.dir, at: Date.now(), secs });
            }
            call.current = null;
            setSince(null);
            setMuted(false);
            const why = d.reason && d.reason !== "You hung up." ? plainReason(d.reason) || d.reason : "";
            say(why);
            setTimeout(() => setState((st) => (st === "ended" ? "idle" : st)), 300);
          }
        },
        onRemoteStream: (stream) => {
          const el = audioRef.current;
          if (!el) return;
          el.srcObject = stream;
          const sink = settingsRef.current.speakerId;
          const withSink = el as HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };
          if (sink && withSink.setSinkId) void withSink.setSinkId(sink).catch(() => undefined);
        },
      });
      p.micId = settingsRef.current.micId;
      phoneRef.current = p;
      p.connect();
    } catch (e) {
      setConnecting(false);
      setStatus((e as Error).message);
    }
  }, [base, remember, ringer, say]);
  const connectRef = useRef(connect);
  connectRef.current = connect;

  // Connect when the app opens, and again when the device comes back online or awake.
  useEffect(() => {
    if (!cfg?.enabled || !settings.autoConnect) return;
    void connectRef.current();
    const again = () => {
      if (wanted.current && !phoneRef.current && document.visibilityState === "visible") void connectRef.current();
    };
    // The browser knows the network is gone before the phone's connection times out: say so now.
    const gone = () => {
      const p = phoneRef.current;
      phoneRef.current = null;
      p?.disconnect();
      setConnected(false);
      setConnecting(false);
      setStatus("You're offline. Calls will ring again when you're back online.");
    };
    window.addEventListener("online", again);
    window.addEventListener("offline", gone);
    document.addEventListener("visibilitychange", again);
    return () => {
      window.removeEventListener("online", again);
      window.removeEventListener("offline", gone);
      document.removeEventListener("visibilitychange", again);
    };
  }, [cfg?.enabled, settings.autoConnect]);

  useEffect(
    () => () => {
      wanted.current = false;
      ringer.stop();
      phoneRef.current?.disconnect();
    },
    [ringer],
  );

  useEffect(() => {
    if (phoneRef.current) phoneRef.current.micId = settings.micId;
  }, [settings.micId]);

  const notify = (who: string) => {
    if (!settingsRef.current.notify || document.visibilityState === "visible") return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    navigator.serviceWorker?.getRegistration("/phone").then((reg) => {
      const opts = { body: who, tag: "jibsy-call", icon: "/phone/icon-192.png", requireInteraction: true } as NotificationOptions;
      if (reg) void reg.showNotification("Incoming call", opts);
      else new Notification("Incoming call", opts);
    });
  };

  return {
    audioRef,
    ringer,
    connected,
    connecting,
    status,
    state,
    peer,
    endReason,
    muted,
    since,
    recents,
    connect: () => void connect(),
    clearRecents: () => {
      setRecents([]);
      if (recentsKey) save(recentsKey, []);
    },
    call: (to: string, name = "") => {
      ringer.unlock();
      if (name) setPeerName(name);
      call.current = { dir: "out", number: to.replace(/[^\d+*#]/g, ""), name, answeredAt: null };
      phoneRef.current?.call(to).catch((e: Error) => say(e.message));
    },
    answer: () => {
      ringer.stop();
      phoneRef.current?.answer().catch((e: Error) => say(e.message));
    },
    hangup: () => {
      ringer.stop();
      phoneRef.current?.hangup();
    },
    mute: () => {
      phoneRef.current?.mute(!muted);
      setMuted(!muted);
    },
    dtmf: (k: string) => phoneRef.current?.dtmf(k),
  };
}

/* ---- Screens --------------------------------------------------------------------- */

function Keypad({
  number,
  setNumber,
  onCall,
  disabled,
}: {
  number: string;
  setNumber: (n: string) => void;
  onCall: (n: string) => void;
  disabled: boolean;
}) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onCall(number);
  };
  // Typing on a computer keyboard works too.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT") return;
      if (/^[0-9*#+]$/.test(e.key)) setNumber((number + e.key).slice(0, 20));
      else if (e.key === "Backspace") setNumber(number.slice(0, -1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [number, setNumber]);
  return (
    <form className="ph-keypad" onSubmit={submit} aria-label="Place a call">
      <label htmlFor="ph-number" className="sr-only">
        Extension or number
      </label>
      <div className="ph-number">
        <input
          id="ph-number"
          inputMode="tel"
          autoComplete="off"
          placeholder="Extension or number"
          value={number}
          onChange={(e) => setNumber(e.target.value.replace(/[^\d+*# ]/g, "").slice(0, 20))}
        />
        {number && (
          <button type="button" className="ph-back" onClick={() => setNumber(number.slice(0, -1))} aria-label="Delete last digit">
            ⌫
          </button>
        )}
      </div>
      <div className="ph-keys" role="group" aria-label="Keypad">
        {KEYS.map((k) => (
          <button key={k} type="button" onClick={() => setNumber((number + k).slice(0, 20))} aria-label={k === "*" ? "Star" : k === "#" ? "Hash" : k}>
            <span className="ph-digit">{k}</span>
            <span className="ph-letters">{LETTERS[k] ?? " "}</span>
          </button>
        ))}
      </div>
      <button className="ph-round answer ph-call" type="submit" disabled={disabled || !number.trim()} aria-label="Call">
        <PhoneGlyph />
      </button>
    </form>
  );
}

function ContactsTab({ base, onCall, disabled }: { base: string; onCall: (n: string, name?: string) => void; disabled: boolean }) {
  const c = useApi<Contacts>(`${base}/voice/webphone/contacts`, 60_000);
  const [q, setQ] = useState("");
  if (c.error) return <p className="ph-empty">{c.error}</p>;
  if (!c.data) return <p className="ph-empty">Loading…</p>;
  const match = (s: { name: string; extension: string }) =>
    !q || s.name.toLowerCase().includes(q.toLowerCase()) || s.extension.includes(q);
  const people = c.data.people.filter(match);
  const lines = c.data.lines.filter(match);
  return (
    <div className="ph-list">
      <label className="sr-only" htmlFor="ph-search">
        Search contacts
      </label>
      <input id="ph-search" className="ph-search" type="search" placeholder="Search names or extensions" value={q} onChange={(e) => setQ(e.target.value)} />
      {people.length === 0 && lines.length === 0 && <p className="ph-empty">No one matches.</p>}
      {people.length > 0 && <h2>People</h2>}
      <ul>
        {people.map((p) => (
          <li key={p.extension}>
            <span className="ph-avatar" aria-hidden="true">
              {p.name.slice(0, 1).toUpperCase()}
            </span>
            <span className="ph-who">
              <strong>{p.name}</strong>
              <span>Ext {p.extension}</span>
            </span>
            <button className="ph-mini" disabled={disabled} onClick={() => onCall(p.extension, p.name)} aria-label={`Call ${p.name}`}>
              <PhoneGlyph />
            </button>
          </li>
        ))}
      </ul>
      {lines.length > 0 && <h2>Shared lines</h2>}
      <ul>
        {lines.map((l) => (
          <li key={l.extension}>
            <span className="ph-avatar line" aria-hidden="true">
              #
            </span>
            <span className="ph-who">
              <strong>{l.name}</strong>
              <span>
                {KIND_WORD[l.kind]}, ext {l.extension}
              </span>
            </span>
            <button className="ph-mini" disabled={disabled} onClick={() => onCall(l.extension, l.name)} aria-label={`Call ${l.name}`}>
              <PhoneGlyph />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RecentsTab({ recents, onCall, onClear }: { recents: Recent[]; onCall: (n: string, name?: string) => void; onClear: () => void }) {
  if (recents.length === 0) return <p className="ph-empty">Calls you make and receive on this device show here.</p>;
  const word = { in: "Incoming", out: "Outgoing", missed: "Missed" };
  return (
    <div className="ph-list">
      <ul>
        {recents.map((r) => (
          <li key={`${r.at}-${r.number}`} className={r.dir === "missed" ? "missed" : ""}>
            <span className="ph-who">
              <strong>{r.name || r.number}</strong>
              <span>
                {word[r.dir]}
                {r.secs ? `, ${clock(r.secs)}` : ""} · {ago(r.at)}
              </span>
            </span>
            <button className="ph-mini" onClick={() => onCall(r.number, r.name)} aria-label={`Call ${r.name || r.number} back`}>
              <PhoneGlyph />
            </button>
          </li>
        ))}
      </ul>
      <button className="button secondary small" onClick={onClear}>
        Clear the list
      </button>
    </div>
  );
}

function InCall({ phone }: { phone: PhoneHook }) {
  const [now, setNow] = useState(Date.now());
  const [pad, setPad] = useState(false);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const label =
    phone.state === "active" && phone.since ? clock(Math.round((now - phone.since) / 1000)) : phone.state === "ringing" ? "Ringing…" : "Calling…";
  return (
    <div className="ph-overlay incall" role="dialog" aria-modal="true" aria-label="On a call">
      <p className="ph-overlay-label" role="status">
        {phone.state === "active" ? "On a call" : label}
      </p>
      <p className="ph-overlay-name">{phone.peer.name || phone.peer.number}</p>
      {phone.state === "active" && <p className="ph-overlay-number ph-timer">{label}</p>}
      {pad && phone.state === "active" && (
        <div className="ph-keys small" role="group" aria-label="Keypad: sends tones">
          {KEYS.map((k) => (
            <button key={k} type="button" onClick={() => phone.dtmf(k)} aria-label={k === "*" ? "Star" : k === "#" ? "Hash" : k}>
              <span className="ph-digit">{k}</span>
            </button>
          ))}
        </div>
      )}
      <div className="ph-overlay-tools">
        <button className={`ph-tool ${phone.muted ? "on" : ""}`} onClick={phone.mute} aria-pressed={phone.muted} disabled={phone.state !== "active"}>
          {phone.muted ? "Unmute" : "Mute"}
        </button>
        <button className={`ph-tool ${pad ? "on" : ""}`} onClick={() => setPad(!pad)} aria-pressed={pad} disabled={phone.state !== "active"}>
          Keypad
        </button>
      </div>
      <button className="ph-round decline" onClick={phone.hangup} aria-label="Hang up">
        <PhoneGlyph down />
      </button>
    </div>
  );
}

/* ---- Settings: the person's own phone (server) and this device (local) ------------- */

function SettingsTab({
  base,
  settings,
  setSettings,
  ringer,
  ext,
}: {
  base: string;
  settings: Settings;
  setSettings: (s: Partial<Settings>) => void;
  ringer: Ringer;
  ext: string | null;
}) {
  const { user, signOut } = useAuth();
  const { customers, current, select } = useCustomer();
  const me = useApi<Me>(`${base}/voice/me`, 0);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const listDevices = () =>
    navigator.mediaDevices
      ?.enumerateDevices()
      .then(setDevices)
      .catch(() => setDevices([]));
  useEffect(() => {
    void listDevices();
  }, []);
  const mics = devices.filter((d) => d.kind === "audioinput");
  const speakers = devices.filter((d) => d.kind === "audiooutput");
  const named = mics.some((d) => d.label);
  const canPickSpeaker = typeof (HTMLMediaElement.prototype as { setSinkId?: unknown }).setSinkId === "function";
  const notifyOk = "Notification" in window;

  return (
    <div className="ph-settings">
      <section>
        <h2>My phone</h2>
        <p className="ph-note">
          {me.data?.name || user?.email}, extension {ext}
          {me.data?.site ? `, ${me.data.site}` : ""}. These follow you on every device and desk phone.
        </p>
        {me.data && <MyPhone base={base} me={me.data} onSaved={me.reload} />}
        {me.error && <p className="ph-error">{me.error}</p>}
      </section>

      <section>
        <h2>Ringing</h2>
        <label>
          Ringtone
          <select value={settings.ringtone} onChange={(e) => setSettings({ ringtone: e.target.value as Ringtone })}>
            {RINGTONES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Ring volume
          <input type="range" min={0} max={1} step={0.05} value={settings.volume} onChange={(e) => setSettings({ volume: Number(e.target.value) })} />
        </label>
        <button className="button secondary small" type="button" onClick={() => ringer.preview(settings.ringtone, settings.volume)}>
          Play the ringtone
        </button>
        {notifyOk && (
          <label className="ph-check">
            <input
              type="checkbox"
              checked={settings.notify && Notification.permission === "granted"}
              onChange={async (e) => {
                if (!e.target.checked) return setSettings({ notify: false });
                const p = await Notification.requestPermission();
                setSettings({ notify: p === "granted" });
              }}
            />
            Show a notification for incoming calls while the app is in the background
          </label>
        )}
        <p className="ph-note">
          Calls ring here while Jibsy Phone is open. On iPhone and iPad, keep it open on screen to take calls; on a computer
          or Android, it can sit in the background.
        </p>
      </section>

      <section>
        <h2>Sound</h2>
        {!named && (
          <button className="button secondary small" type="button" onClick={() => navigator.mediaDevices.getUserMedia({ audio: true }).then((s) => (s.getTracks().forEach((t) => t.stop()), listDevices()))}>
            Show my microphones and speakers
          </button>
        )}
        <label>
          Microphone
          <select value={settings.micId} onChange={(e) => setSettings({ micId: e.target.value })}>
            <option value="">The device's default</option>
            {mics
              .filter((d) => d.deviceId && d.deviceId !== "default")
              .map((d, i) => (
                <option key={d.deviceId} value={d.deviceId}>
                  {d.label || `Microphone ${i + 1}`}
                </option>
              ))}
          </select>
        </label>
        {canPickSpeaker && (
          <label>
            Speaker
            <select value={settings.speakerId} onChange={(e) => setSettings({ speakerId: e.target.value })}>
              <option value="">The device's default</option>
              {speakers
                .filter((d) => d.deviceId && d.deviceId !== "default")
                .map((d, i) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label || `Speaker ${i + 1}`}
                  </option>
                ))}
            </select>
          </label>
        )}
      </section>

      <section>
        <h2>This app</h2>
        <label className="ph-check">
          <input type="checkbox" checked={settings.autoConnect} onChange={(e) => setSettings({ autoConnect: e.target.checked })} />
          Connect when the app opens
        </label>
        <label>
          Appearance
          <select value={settings.theme} onChange={(e) => setSettings({ theme: e.target.value as Settings["theme"] })}>
            <option value="system">Match the device</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
        {customers.length > 1 && (
          <label>
            Business
            <select value={current?.id ?? ""} onChange={(e) => select(e.target.value)}>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name || c.id}
                </option>
              ))}
            </select>
          </label>
        )}
        <Install />
        <button className="button secondary" onClick={signOut}>
          Sign out
        </button>
      </section>
    </div>
  );
}

function MyPhone({ base, me, onSaved }: { base: string; me: Me; onSaved: () => void }) {
  const [to, setTo] = useState(me.forward_to);
  const [greeting, setGreeting] = useState(me.voicemail_greeting);
  // Switches move at once and go back if the save fails.
  const [dnd, setDnd] = useState(me.dnd);
  const [vmEmail, setVmEmail] = useState(me.voicemail_to_email);
  useEffect(() => setDnd(me.dnd), [me.dnd]);
  useEffect(() => setVmEmail(me.voicemail_to_email), [me.voicemail_to_email]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const patch = async (body: Record<string, unknown>, done: string, undo?: () => void) => {
    setBusy(true);
    setMsg(null);
    try {
      await api(`${base}/voice/me`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg({ ok: true, text: done });
      onSaved();
    } catch (e) {
      undo?.();
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <label className="ph-check">
        <input
          type="checkbox"
          checked={dnd}
          disabled={busy}
          onChange={(e) => {
            const on = e.target.checked;
            setDnd(on);
            void patch({ dnd: on }, on ? "Do not disturb is on: calls go to voicemail." : "Do not disturb is off.", () => setDnd(!on));
          }}
        />
        Do not disturb
      </label>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void patch({ forward_to: to.trim() }, to.trim() ? `Calls forward to ${to.trim()}.` : "Forwarding is off.");
        }}
      >
        <label>
          Forward my calls to
          <input value={to} onChange={(e) => setTo(e.target.value)} inputMode="tel" placeholder="An extension or a number; blank for off" />
        </label>
        <button className="button small" disabled={busy}>
          Save forwarding
        </button>
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void patch({ voicemail_greeting: greeting }, "Voicemail greeting saved.");
        }}
      >
        <label>
          Voicemail greeting
          <textarea rows={2} value={greeting} onChange={(e) => setGreeting(e.target.value)} placeholder="Read out to callers when you can't answer" maxLength={1000} />
        </label>
        <button className="button small" disabled={busy}>
          Save greeting
        </button>
      </form>
      <label className="ph-check">
        <input
          type="checkbox"
          checked={vmEmail}
          disabled={busy}
          onChange={(e) => {
            const on = e.target.checked;
            setVmEmail(on);
            void patch({ voicemail_to_email: on }, on ? "Voicemail will be emailed to you." : "Voicemail won't be emailed.", () => setVmEmail(!on));
          }}
        />
        Email me my voicemail
      </label>
      {msg && (
        <p className={msg.ok ? "ph-ok" : "ph-error"} role="status">
          {msg.text}
        </p>
      )}
    </>
  );
}

interface InstallEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: string }>;
}

let deferred: InstallEvent | null = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferred = e as InstallEvent;
});

/** How to put the app on this device: one button where the browser offers it, else the steps. */
function Install() {
  const [, redraw] = useState(0);
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;
  if (standalone) return <p className="ph-note">Jibsy Phone is installed on this device.</p>;
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (ua.includes("Mac") && navigator.maxTouchPoints > 1);
  const safariMac = !ios && /Macintosh/.test(ua) && /Safari/.test(ua) && !/Chrome|Edg|Firefox/.test(ua);
  const firefox = /Firefox/.test(ua);
  return (
    <div className="ph-install">
      <h3>Put Jibsy Phone on this device</h3>
      {deferred ? (
        <button
          className="button"
          onClick={async () => {
            await deferred?.prompt();
            deferred = null;
            redraw((n) => n + 1);
          }}
        >
          Install the app
        </button>
      ) : ios ? (
        <p className="ph-note">In Safari, tap Share, then Add to Home Screen.</p>
      ) : safariMac ? (
        <p className="ph-note">In Safari, choose File, then Add to Dock.</p>
      ) : firefox ? (
        <p className="ph-note">Firefox can't install apps: open this address in Chrome or Edge to install it, or keep using it here.</p>
      ) : (
        <p className="ph-note">In Chrome or Edge, choose Install from the address bar or the browser menu. On Android, choose Add to Home screen.</p>
      )}
    </div>
  );
}

/* ---- Icons ------------------------------------------------------------------------- */

function PhoneGlyph({ down = false }: { down?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" style={down ? { transform: "rotate(135deg)" } : undefined}>
      <path
        fill="currentColor"
        d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1C10.6 21 3 13.4 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z"
      />
    </svg>
  );
}

function TabIcon({ tab }: { tab: Tab }) {
  const d = {
    keypad:
      "M6 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM6 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm-6 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
    contacts: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-3.3 0-8 1.7-8 5v1h16v-1c0-3.3-4.7-5-8-5z",
    recents: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm1 9.4 3.3 2-.8 1.3L11 13V7h2v5.4z",
    settings:
      "M19.4 13a7.5 7.5 0 0 0 0-2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-1.7-1L15 3.5h-4l-.4 2.5a7 7 0 0 0-1.7 1l-2.4-1-2 3.4L6.6 11a7.5 7.5 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.4 2.5h4l.4-2.5a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2.1-1.6zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z",
  }[tab];
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      <path fill="currentColor" d={d} />
    </svg>
  );
}
