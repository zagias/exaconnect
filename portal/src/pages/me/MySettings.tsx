import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "../commai/lib";
import VoiceMine from "../commai/voice/MySettings";
import { QrCode } from "../commai/voice/QrCode";
import "./me.css";

type Kind = "assignment" | "mention" | "sla_warning";
type Availability = "online" | "away" | "offline";

interface MySettingsData {
  profile: {
    email: string;
    display_name: string;
    given_name: string;
    family_name: string;
    managed_by_directory: boolean;
    seat: string;
    two_step: boolean;
  };
  language: string;
  languages_spoken: string[];
  language_choices: { code: string; name: string }[];
  availability: Availability;
  notify: Record<Kind, { in_app: boolean; email: boolean }>;
  quiet_hours: { start: string; end: string };
  timezone: string;
}

interface Notice {
  kind: Kind;
  at: string;
  conversation_id: string;
  text: string;
}

interface SessionRow {
  id: string;
  via: string;
  started: string;
  expires: string;
  current: boolean;
}

const KIND_LABEL: Record<Kind, string> = {
  assignment: "A conversation is assigned to me",
  mention: "Someone mentions me in a note",
  sla_warning: "One of my conversations nears its service target",
};

const AVAILABILITY: { value: Availability; label: string; help: string }[] = [
  { value: "online", label: "Online", help: "You get new conversations." },
  { value: "away", label: "Away", help: "New conversations go to others. Yours stay with you." },
  { value: "offline", label: "Offline", help: "New conversations go to others." },
];

const SECTIONS = [
  ["profile", "Profile and language"],
  ["notifications", "Notifications"],
  ["availability", "Availability"],
  ["calls", "My calls"],
  ["devices", "Devices and sessions"],
  ["keys", "API keys"],
] as const;

/** My settings: only the signed-in person's own settings (ADR 0031). */
export default function MySettings() {
  const base = useCommaiBase();
  const s = useApi<MySettingsData>(base ? `${base}/me/settings` : null, 0);
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  return (
    <div className="me">
      <PageHead title="My settings">
        Your own profile, notifications, availability, phone and sign-in. Nobody else's settings change here.
      </PageHead>
      <nav className="me-toc" aria-label="On this page">
        <ul>
          {SECTIONS.map(([id, label]) => (
            <li key={id}>
              <a href={`#me-${id}`}>{label}</a>
            </li>
          ))}
        </ul>
      </nav>
      <ErrorNote error={s.error} />
      {s.data && (
        <>
          <Profile base={base} data={s.data} onSaved={s.reload} />
          <Notifications base={base} data={s.data} onSaved={s.reload} />
          <AvailabilityCard base={base} data={s.data} onSaved={s.reload} />
        </>
      )}
      <Section id="calls" title="My calls">
        <p className="muted small">Forwarding, do not disturb, voicemail and your call history. Recordings follow your company's policy.</p>
        <VoiceMine base={base} />
        <Softphone base={base} />
      </Section>
      <Sessions base={base} twoStep={s.data?.profile.two_step ?? false} />
      <Section id="keys" title="API keys">
        <p>
          Keys for your own scripts and apps act as you. Make and revoke them on <Link to="/account">your account</Link>.
        </p>
      </Section>
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="card me-section" id={`me-${id}`} aria-labelledby={`me-${id}-title`}>
      <h2 id={`me-${id}-title`}>{title}</h2>
      {children}
    </section>
  );
}

function Saved({ show }: { show: boolean }) {
  return (
    <span role="status" aria-live="polite" className="ok-note">
      {show ? "✓ Saved" : ""}
    </span>
  );
}

function Profile({ base, data, onSaved }: { base: string; data: MySettingsData; onSaved: () => void }) {
  const [d, setD] = useState({
    display_name: data.profile.display_name,
    language: data.language,
    spoken: data.languages_spoken,
    timezone: data.timezone,
  });
  const [saved, setSaved] = useState(false);
  const act = useAction();
  const dir = data.profile.managed_by_directory;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      await api(`${base}/me/settings`, {
        method: "PATCH",
        body: JSON.stringify({
          ...(dir ? {} : { display_name: d.display_name }),
          language: d.language,
          languages_spoken: d.spoken,
          timezone: d.timezone,
        }),
      });
      setSaved(true);
      onSaved();
    });
  };
  const toggleSpoken = (code: string) =>
    setD({ ...d, spoken: d.spoken.includes(code) ? d.spoken.filter((c) => c !== code) : [...d.spoken, code] });
  return (
    <Section id="profile" title="Profile and language">
      <p className="muted small">
        Signed in as <strong>{data.profile.email}</strong>
        {data.profile.seat === "internal" ? " (internal seat: notes only)" : ""}.
      </p>
      <form className="form" onSubmit={submit}>
        <label>
          Name shown to your team
          <input
            value={d.display_name}
            maxLength={200}
            disabled={dir}
            aria-describedby={dir ? "me-dir-note" : undefined}
            onChange={(e) => setD({ ...d, display_name: e.target.value })}
          />
        </label>
        <label>
          Portal language
          <select value={d.language} onChange={(e) => setD({ ...d, language: e.target.value })}>
            {data.language_choices.map((l) => (
              <option key={l.code} value={l.code}>
                {l.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          My time zone
          <input value={d.timezone} maxLength={60} placeholder="America/Port_of_Spain" onChange={(e) => setD({ ...d, timezone: e.target.value })} />
        </label>
        <fieldset className="wide">
          <legend>Languages I answer customers in</legend>
          {data.language_choices.map((l) => (
            <label key={l.code} className="check">
              <input type="checkbox" checked={d.spoken.includes(l.code)} onChange={() => toggleSpoken(l.code)} />
              {l.name}
            </label>
          ))}
        </fieldset>
        {dir && (
          <p id="me-dir-note" className="muted small wide">
            Your name comes from your company directory. Change it there.
          </p>
        )}
        <div className="actions wide">
          <button className="button" disabled={act.busy}>
            Save
          </button>
          <Saved show={saved} />
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Section>
  );
}

function Notifications({ base, data, onSaved }: { base: string; data: MySettingsData; onSaved: () => void }) {
  const [notify, setNotify] = useState(data.notify);
  const [quiet, setQuiet] = useState(data.quiet_hours);
  const [saved, setSaved] = useState(false);
  const act = useAction();
  const feed = useApi<Notice[]>(`${base}/me/notifications`, 60_000);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSaved(false);
    act.run(async () => {
      await api(`${base}/me/settings`, { method: "PATCH", body: JSON.stringify({ notify, quiet_hours: quiet }) });
      setSaved(true);
      onSaved();
      feed.reload();
    });
  };
  const set = (k: Kind, way: "in_app" | "email", v: boolean) => setNotify({ ...notify, [k]: { ...notify[k], [way]: v } });
  return (
    <Section id="notifications" title="Notifications">
      <form onSubmit={submit}>
        <div className="table-wrap">
          <table className="me-table">
            <caption className="sr-only">Which notifications you get, and how</caption>
            <thead>
              <tr>
                <th scope="col">Tell me when</th>
                <th scope="col">In the portal</th>
                <th scope="col">By email</th>
              </tr>
            </thead>
            <tbody>
              {(Object.keys(KIND_LABEL) as Kind[]).map((k) => (
                <tr key={k}>
                  <th scope="row">{KIND_LABEL[k]}</th>
                  {(["in_app", "email"] as const).map((way) => (
                    <td key={way}>
                      <input
                        type="checkbox"
                        aria-label={`${KIND_LABEL[k]}: ${way === "in_app" ? "in the portal" : "by email"}`}
                        checked={notify[k][way]}
                        onChange={(e) => set(k, way, e.target.checked)}
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <fieldset className="me-quiet">
          <legend>Quiet hours (no emails; portal notices wait for you)</legend>
          <label>
            From
            <input type="time" value={quiet.start} onChange={(e) => setQuiet({ ...quiet, start: e.target.value })} />
          </label>
          <label>
            Until
            <input type="time" value={quiet.end} onChange={(e) => setQuiet({ ...quiet, end: e.target.value })} />
          </label>
          <button type="button" className="button secondary small" onClick={() => setQuiet({ start: "", end: "" })}>
            No quiet hours
          </button>
        </fieldset>
        <div className="me-row">
          <button className="button" disabled={act.busy}>
            Save
          </button>
          <Saved show={saved} />
        </div>
        <p className="muted small">Times are in {data.timezone}.</p>
      </form>
      <ErrorNote error={act.error} />
      <h3 className="me-sub">Recent notices</h3>
      {feed.data && feed.data.length === 0 && <p className="muted">Nothing for you just now.</p>}
      <ul className="me-list">
        {(feed.data ?? []).slice(0, 10).map((n, i) => (
          <li key={i}>
            <Link to={`/commai/c/${n.conversation_id}`}>{n.text}</Link> <span className="muted small">{when(n.at)}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function AvailabilityCard({ base, data, onSaved }: { base: string; data: MySettingsData; onSaved: () => void }) {
  const act = useAction();
  const [value, setValue] = useState<Availability>(data.availability);
  const choose = (v: Availability) =>
    act.run(async () => {
      await api(`${base}/me/availability`, { method: "PUT", body: JSON.stringify({ availability: v }) });
      setValue(v);
      onSaved();
    });
  return (
    <Section id="availability" title="Availability">
      <fieldset className="me-avail" disabled={act.busy}>
        <legend className="sr-only">My availability</legend>
        {AVAILABILITY.map((a) => (
          <label key={a.value} className={value === a.value ? "chosen" : ""}>
            <input type="radio" name="me-availability" checked={value === a.value} onChange={() => choose(a.value)} />
            <span>
              <strong>{a.label}</strong>
              <span className="muted small"> {a.help}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <ErrorNote error={act.error} />
    </Section>
  );
}

function Softphone({ base }: { base: string }) {
  const act = useAction();
  const [link, setLink] = useState<string | null>(null);
  return (
    <div className="card-inset me-softphone">
      <h3>Softphone sign-in</h3>
      <p className="muted small">
        A new sign-in link for your own softphone app. Open it on your phone, or scan the QR code with it. The previous link
        stops working.
      </p>
      <button
        type="button"
        className="button secondary"
        disabled={act.busy}
        onClick={() =>
          act.run(async () => {
            const r = await api<{ join_link: string }>(`${base}/me/softphone-link`, { method: "POST" });
            setLink(r.join_link);
          })
        }
      >
        Get a sign-in link
      </button>
      {link && (
        <div className="secret">
          <p>
            Shown once: <code>{link}</code>
          </p>
          <QrCode text={link} />
        </div>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function Sessions({ base, twoStep }: { base: string; twoStep: boolean }) {
  const rows = useApi<SessionRow[]>(`${base}/me/sessions`, 0);
  const act = useAction();
  const [ended, setEnded] = useState<number | null>(null);
  const others = (rows.data ?? []).filter((r) => !r.current).length;
  return (
    <Section id="devices" title="Devices and sessions">
      <p>
        Two-step sign-in is <strong>{twoStep ? "on" : "off"}</strong>.{" "}
        <Link to="/account">{twoStep ? "Manage it, and your passkeys, on your account" : "Set it up on your account"}</Link>.
      </p>
      <ErrorNote error={rows.error} />
      <ul className="me-list">
        {(rows.data ?? []).map((r) => (
          <li key={r.id}>
            <strong>{r.current ? "This browser" : "Another sign-in"}</strong>{" "}
            <span className="muted small">
              by {r.via}, started {new Date(r.started).toLocaleString("en-GB")}
            </span>
            {!r.current && (
              <>
                {" "}
                <button
                  type="button"
                  className="link"
                  disabled={act.busy}
                  onClick={() =>
                    act.run(async () => {
                      await api(`${base}/me/sessions/${r.id}`, { method: "DELETE" });
                      rows.reload();
                    })
                  }
                >
                  Sign out<span className="sr-only"> of this session</span>
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      <div className="me-row">
        <button
          type="button"
          className="button danger-text"
          disabled={act.busy || others === 0}
          onClick={() =>
            act.run(async () => {
              const r = await api<{ ended: number }>(`${base}/me/sessions/sign-out-others`, { method: "POST" });
              setEnded(r.ended);
              rows.reload();
            })
          }
        >
          Sign out everywhere else
        </button>
        <span role="status" aria-live="polite" className="ok-note">
          {ended !== null ? `✓ Ended ${ended} other session${ended === 1 ? "" : "s"}` : ""}
        </span>
      </div>
      <ErrorNote error={act.error} />
    </Section>
  );
}
