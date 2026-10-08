import { useEffect, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { clientId, helpApi, useHelp, type HelpHome, type PublicMessage } from "./api";
import "./help.css";

/**
 * The help centre a business offers its own customers (ADR 0037), at
 * /help/<slug>, outside the staff portal: no staff sign-in, the business's
 * own name and colour.
 */
export default function HelpCentre() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const home = useHelp<HelpHome>(slug, "");
  const [signinNote, setSigninNote] = useState<string | null>(null);

  // Arriving from an emailed link (?signin=...) or the business's own site (#user_token=...).
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const link = q.get("signin");
    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const userToken = hash.get("user_token");
    if (!link && !userToken) return;
    window.history.replaceState(null, "", window.location.pathname);
    const call = link
      ? helpApi(slug, "/signin/link", { method: "POST", body: JSON.stringify({ token: link }) })
      : helpApi(slug, "/signin/token", { method: "POST", body: JSON.stringify({ user_token: userToken }) });
    call
      .then(() => {
        home.reload();
        navigate(`/help/${slug}/me`);
      })
      .catch((e: Error) => setSigninNote(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  useEffect(() => {
    if (home.data) document.title = `${home.data.title} · ${home.data.business}`;
  }, [home.data]);

  if (home.error) {
    return (
      <main className="help" id="main">
        <div className="help-wrap">
          <h1>Help centre not found</h1>
          <p>There is no help centre at this address. Check the link you were given.</p>
        </div>
      </main>
    );
  }
  if (!home.data) return <p className="help-loading">Loading…</p>;
  const h = home.data;
  return (
    <div className="help" style={{ "--help-brand": h.colour } as CSSProperties}>
      <a className="help-skip" href="#main">
        Skip to content
      </a>
      <header className="help-head">
        <div className="help-wrap help-head-row">
          <Link to={`/help/${slug}`} className="help-brand">
            {h.logo_url && <img src={h.logo_url} alt="" className="help-logo" height={32} />}
            {h.business}
            <span className="help-brand-sub">{h.title}</span>
          </Link>
          {h.show.signin && (
            <nav aria-label="Your account">
              <Link to={`/help/${slug}/me`} className="help-head-link">
                {h.signed_in ? "My account" : "Sign in"}
              </Link>
            </nav>
          )}
        </div>
      </header>
      <main id="main" className="help-wrap">
        {signinNote && (
          <p className="help-alert" role="alert">
            {signinNote}
          </p>
        )}
        <Routes>
          <Route path="/" element={<Home slug={slug} home={h} />} />
          <Route path="/a/:id" element={<Article slug={slug} />} />
          <Route path="/me" element={<Account slug={slug} home={h} onChange={home.reload} />} />
          <Route path="/c/:id" element={<Conversation slug={slug} />} />
        </Routes>
      </main>
      <footer className="help-foot help-wrap">
        <p>
          {h.business} help centre. Runs on {h.platform || "ExaCarib"}.
        </p>
      </footer>
    </div>
  );
}

function Panel({ title, id, children }: { title: string; id: string; children: ReactNode }) {
  return (
    <section className="help-panel" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  );
}

function useBusy() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

function Problem({ error }: { error: string | null }) {
  return error ? (
    <p className="help-alert" role="alert">
      {error}
    </p>
  ) : null;
}

/** The public front page. With `preview`, nothing is sent (the admin's preview). */
export function Home({ slug, home, preview = false }: { slug: string; home: HelpHome; preview?: boolean }) {
  return (
    <>
      <div className="help-hero">
        <h1>{home.title}</h1>
        {home.intro && <p>{home.intro}</p>}
        {home.show.search && home.show.articles && <Search slug={slug} preview={preview} />}
      </div>
      <div className="help-grid">
        <div>
          {home.show.ask && <Ask slug={slug} preview={preview} signedIn={!!home.signed_in} />}
          {home.show.articles && (
            <Panel title="Help articles" id="help-articles">
              {(home.categories ?? []).length === 0 && <p className="muted">No articles yet.</p>}
              {(home.categories ?? []).map((c) => (
                <div key={c.name} className="help-cat">
                  <h3>{c.name}</h3>
                  <ul>
                    {c.articles.map((a) => (
                      <li key={a.id}>{preview ? <span>{a.title}</span> : <Link to={`/help/${slug}/a/${a.id}`}>{a.title}</Link>}</li>
                    ))}
                  </ul>
                </div>
              ))}
            </Panel>
          )}
          {home.show.contact && <Contact slug={slug} preview={preview} signedIn={!!home.signed_in} />}
        </div>
        <aside>
          {home.show.hours && home.hours && (
            <Panel title="Opening hours" id="help-hours">
              <p className={home.hours.open_now ? "help-open" : "help-closed"}>
                {home.hours.open_now ? "● Open now" : "■ Closed now"}
              </p>
              <p className="small">{home.hours.text}</p>
              <p className="muted small">Times in {home.hours.timezone.replace("_", " ")}.</p>
            </Panel>
          )}
          {home.show.channels && (home.channels ?? []).length > 0 && (
            <Panel title="Other ways to reach us" id="help-channels">
              <ul className="help-channels">
                {(home.channels ?? []).map((c) => (
                  <li key={c.kind}>
                    <a href={c.href} target={c.kind === "whatsapp" ? "_blank" : undefined} rel="noreferrer">
                      <strong>{c.label}</strong> <span>{c.value}</span>
                      {c.kind === "whatsapp" && <span className="sr-only"> (opens WhatsApp)</span>}
                    </a>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
          {home.show.signin && !home.signed_in && <SignIn slug={slug} home={home} preview={preview} />}
        </aside>
      </div>
    </>
  );
}

interface Hit {
  id: string;
  title: string;
  category: string;
  snippet: string;
}

function Search({ slug, preview }: { slug: string; preview: boolean }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const b = useBusy();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (preview || !q.trim()) return;
    b.run(async () => setHits(await helpApi<Hit[]>(slug, `/search?q=${encodeURIComponent(q)}`)));
  };
  return (
    <form role="search" className="help-search" onSubmit={submit}>
      <label htmlFor="help-q" className="sr-only">
        Search help articles
      </label>
      <input id="help-q" type="search" value={q} placeholder="Search help articles" onChange={(e) => setQ(e.target.value)} />
      <button className="help-button" disabled={b.busy || preview}>
        Search
      </button>
      <Problem error={b.error} />
      <div aria-live="polite">
        {hits && hits.length === 0 && <p>No articles match. Try other words, or ask below.</p>}
        {hits && hits.length > 0 && (
          <ul className="help-hits">
            {hits.map((h) => (
              <li key={h.id}>
                <Link to={`/help/${slug}/a/${h.id}`}>{h.title}</Link>
                <p className="small">{h.snippet}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </form>
  );
}

function Ask({ slug, preview, signedIn }: { slug: string; preview: boolean; signedIn: boolean }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<{ answered: boolean; replies: PublicMessage[]; message?: string } | null>(null);
  const b = useBusy();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (preview) return;
    b.run(async () => {
      setAnswer(
        await helpApi(slug, "/ask", { method: "POST", body: JSON.stringify({ question: q, client_id: clientId() }) }),
      );
    });
  };
  return (
    <Panel title="Ask a question" id="help-ask">
      <form onSubmit={submit} className="help-form">
        <label>
          Your question
          <textarea value={q} rows={2} maxLength={1000} required onChange={(e) => setQ(e.target.value)} />
        </label>
        <p className="muted small">
          Our AI assistant answers from this business's own approved information. If it isn't sure, a person helps
          {signedIn ? " and replies in your conversations" : ""}.
        </p>
        <button className="help-button" disabled={b.busy || preview}>
          {b.busy ? "Asking…" : "Ask"}
        </button>
      </form>
      <Problem error={b.error} />
      <div aria-live="polite">
        {answer && (
          <div className="help-answer">
            {answer.replies.map((m) => (
              <p key={m.id}>{m.body}</p>
            ))}
            {answer.message && <p>{answer.message}</p>}
          </div>
        )}
      </div>
    </Panel>
  );
}

function Contact({ slug, preview, signedIn }: { slug: string; preview: boolean; signedIn: boolean }) {
  const [f, setF] = useState({ name: "", email: "", message: "" });
  const [done, setDone] = useState<string | null>(null);
  const b = useBusy();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (preview) return;
    b.run(async () => {
      const r = await helpApi<{ message: string }>(slug, "/contact", {
        method: "POST",
        body: JSON.stringify({ ...f, client_id: clientId() }),
      });
      setDone(r.message);
      setF({ name: "", email: "", message: "" });
    });
  };
  return (
    <Panel title="Contact us" id="help-contact">
      <form onSubmit={submit} className="help-form">
        {!signedIn && (
          <>
            <label>
              Your name
              <input value={f.name} maxLength={200} autoComplete="name" onChange={(e) => setF({ ...f, name: e.target.value })} />
            </label>
            <label>
              Your email (so we can reply)
              <input type="email" required value={f.email} maxLength={255} autoComplete="email" onChange={(e) => setF({ ...f, email: e.target.value })} />
            </label>
          </>
        )}
        <label>
          Message
          <textarea required rows={4} maxLength={4000} value={f.message} onChange={(e) => setF({ ...f, message: e.target.value })} />
        </label>
        <button className="help-button" disabled={b.busy || preview}>
          Send
        </button>
      </form>
      <Problem error={b.error} />
      <p role="status" aria-live="polite" className="help-ok">
        {done ?? ""}
      </p>
    </Panel>
  );
}

function SignIn({ slug, home, preview }: { slug: string; home: HelpHome; preview: boolean }) {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const b = useBusy();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (preview) return;
    b.run(async () => {
      const r = await helpApi<{ message: string }>(slug, "/signin", { method: "POST", body: JSON.stringify({ email }) });
      setSent(r.message);
    });
  };
  return (
    <Panel title="Sign in" id="help-signin">
      <p className="small">See your conversations and bookings, and choose how we contact you.</p>
      <form onSubmit={submit} className="help-form">
        <label>
          Email address
          <input type="email" required value={email} autoComplete="email" onChange={(e) => setEmail(e.target.value)} />
        </label>
        <button className="help-button" disabled={b.busy || preview}>
          Email me a sign-in link
        </button>
      </form>
      <Problem error={b.error} />
      <p role="status" aria-live="polite" className="help-ok">
        {sent ?? ""}
      </p>
      {home.signin_with_business && (
        <p className="muted small">Have an account on {home.business}'s website? Sign in there and choose Help.</p>
      )}
    </Panel>
  );
}

interface ArticleData {
  id: string;
  title: string;
  body: string;
  category: string;
  updated_at: string;
}

function Article({ slug }: { slug: string }) {
  const { id = "" } = useParams();
  const a = useHelp<ArticleData>(slug, `/articles/${encodeURIComponent(id)}`);
  const h1 = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (a.data) h1.current?.focus();
  }, [a.data]);
  if (a.error) return <p role="alert">That article isn't available.</p>;
  if (!a.data) return <p>Loading…</p>;
  return (
    <article className="help-article">
      <p>
        <Link to={`/help/${slug}`}>← All help</Link>
      </p>
      <p className="help-eyebrow">{a.data.category}</p>
      <h1 ref={h1} tabIndex={-1}>
        {a.data.title}
      </h1>
      {a.data.body.split(/\n\s*\n/).map((p, i) => (
        <p key={i}>{p}</p>
      ))}
      <p className="muted small">Updated {new Date(a.data.updated_at).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}</p>
    </article>
  );
}

// ---- signed in ----------------------------------------------------------------------------

interface Conv {
  id: string;
  channel: string;
  subject: string;
  state: string;
  last_message_at: string | null;
  preview: string | null;
}

interface Booking {
  id: string;
  start: string;
  reason: string;
  reference: string;
  state: "confirmed" | "pending" | "failed" | "cancelled" | "cancelling";
  note: string;
  can_change: boolean;
}

interface Prefs {
  language: string;
  channels: { id: string; channel: string; address: string; receive: boolean }[];
}

interface DataReq {
  id: string;
  kind: "download" | "delete";
  status: "open" | "done" | "refused";
  created_at: string;
}

const LANGS: [string, string][] = [
  ["", "No preference"],
  ["en", "English"],
  ["es", "Spanish"],
  ["fr", "French"],
  ["pt", "Portuguese"],
  ["nl", "Dutch"],
  ["ht", "Haitian Creole"],
  ["pap", "Papiamento"],
];
const CHANNEL: Record<string, string> = { whatsapp: "WhatsApp", sms: "Text messages (SMS)", email: "Email", web: "Website chat" };
const STATE: Record<string, string> = {
  open: "Open",
  reopened: "Open",
  awaiting_customer: "Waiting for you",
  awaiting_internal: "With the team",
  snoozed: "With the team",
  resolved: "Closed",
};
const BOOKING: Record<Booking["state"], string> = {
  confirmed: "✓ Confirmed",
  pending: "Waiting for the calendar",
  failed: "✕ Not booked",
  cancelled: "Cancelled",
  cancelling: "Cancellation requested",
};

function Account({ slug, home, onChange }: { slug: string; home: HelpHome; onChange: () => void }) {
  const me = useHelp<{ name: string; email: string }>(slug, "/me");
  const b = useBusy();
  if (me.error?.status === 401) {
    return (
      <>
        <h1>Sign in</h1>
        <SignIn slug={slug} home={home} preview={false} />
      </>
    );
  }
  if (me.error) return <Problem error={me.error.message} />;
  if (!me.data) return <p>Loading…</p>;
  return (
    <>
      <div className="help-account-head">
        <h1>My account</h1>
        <button
          type="button"
          className="help-button secondary"
          disabled={b.busy}
          onClick={() =>
            b.run(async () => {
              await helpApi(slug, "/signout", { method: "POST" });
              onChange();
              me.reload();
            })
          }
        >
          Sign out
        </button>
      </div>
      <p className="muted">Signed in{me.data.name ? ` as ${me.data.name}` : ""}{me.data.email ? ` (${me.data.email})` : ""}.</p>
      <Conversations slug={slug} />
      {home.show.bookings && <Bookings slug={slug} />}
      <Preferences slug={slug} />
      {home.show.data_requests && <DataRequests slug={slug} />}
    </>
  );
}

function Conversations({ slug }: { slug: string }) {
  const c = useHelp<Conv[]>(slug, "/me/conversations", 30_000);
  return (
    <Panel title="My conversations" id="help-convs">
      {c.data && c.data.length === 0 && <p>No conversations yet.</p>}
      <ul className="help-list">
        {(c.data ?? []).map((x) => (
          <li key={x.id}>
            <Link to={`/help/${slug}/c/${x.id}`}>{x.subject || CHANNEL[x.channel] || "Conversation"}</Link>{" "}
            <span className="help-tag">{STATE[x.state] ?? x.state}</span>
            {x.preview && <p className="small muted help-clip">{x.preview}</p>}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function Conversation({ slug }: { slug: string }) {
  const { id = "" } = useParams();
  const c = useHelp<{ id: string; subject: string; state: string; can_reply: boolean; channel: string; messages: PublicMessage[] }>(
    slug,
    `/me/conversations/${encodeURIComponent(id)}`,
    15_000,
  );
  const [body, setBody] = useState("");
  const b = useBusy();
  if (c.error?.status === 401) return <p>Your session has ended. <Link to={`/help/${slug}/me`}>Sign in again</Link>.</p>;
  if (c.error) return <p role="alert">That conversation isn't available.</p>;
  if (!c.data) return <p>Loading…</p>;
  const send = (e: FormEvent) => {
    e.preventDefault();
    b.run(async () => {
      await helpApi(slug, `/me/conversations/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        body: JSON.stringify({ body, client_id: clientId() }),
      });
      setBody("");
      c.reload();
    });
  };
  const who = { you: "You", team: "Team", assistant: "Assistant", system: "Update" };
  return (
    <>
      <p>
        <Link to={`/help/${slug}/me`}>← My account</Link>
      </p>
      <h1>{c.data.subject || "Conversation"}</h1>
      <p className="muted small">
        {CHANNEL[c.data.channel] ?? c.data.channel} · {STATE[c.data.state] ?? c.data.state}
      </p>
      <ol className="help-thread" aria-label="Messages">
        {c.data.messages.map((m) => (
          <li key={m.id} className={m.from === "you" ? "mine" : ""}>
            <span className="help-who">{who[m.from]}</span>
            <p>{m.body}</p>
            <time className="muted small" dateTime={m.at}>
              {new Date(m.at).toLocaleString("en-GB")}
            </time>
          </li>
        ))}
      </ol>
      {c.data.can_reply ? (
        <form onSubmit={send} className="help-form">
          <label>
            Reply
            <textarea rows={3} required maxLength={4000} value={body} onChange={(e) => setBody(e.target.value)} />
          </label>
          <button className="help-button" disabled={b.busy}>
            Send
          </button>
        </form>
      ) : (
        <p>This conversation is closed. Use the contact form to start a new one.</p>
      )}
      <Problem error={b.error} />
    </>
  );
}

function localValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Bookings({ slug }: { slug: string }) {
  const list = useHelp<Booking[]>(slug, "/me/bookings", 20_000);
  const [moving, setMoving] = useState<string | null>(null);
  const [when, setWhen] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const b = useBusy();
  const act = (fn: () => Promise<{ message: string }>) =>
    b.run(async () => {
      const r = await fn();
      setNote(r.message);
      setMoving(null);
      list.reload();
    });
  return (
    <Panel title="My bookings" id="help-bookings">
      {list.data && list.data.length === 0 && <p>No bookings.</p>}
      <ul className="help-list">
        {(list.data ?? []).map((x) => (
          <li key={x.id}>
            <strong>{new Date(x.start).toLocaleString("en-GB", { dateStyle: "full", timeStyle: "short" })}</strong>{" "}
            <span className="help-tag">{BOOKING[x.state]}</span>
            {x.reason && <span className="small"> · {x.reason}</span>}
            {x.reference && <span className="muted small"> · Ref. {x.reference.slice(0, 8)}</span>}
            {x.note && <p className="small">{x.note}</p>}
            {x.can_change && moving !== x.id && (
              <div className="help-row">
                <button type="button" className="help-button secondary" onClick={() => { setMoving(x.id); setWhen(localValue(x.start)); }}>
                  Change time
                </button>
                <button type="button" className="help-button secondary" disabled={b.busy}
                  onClick={() => act(() => helpApi(slug, `/me/bookings/${x.id}/cancel`, { method: "POST" }))}>
                  Cancel booking
                </button>
              </div>
            )}
            {moving === x.id && (
              <form
                className="help-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  act(() =>
                    helpApi(slug, `/me/bookings/${x.id}/reschedule`, {
                      method: "POST",
                      body: JSON.stringify({ start: new Date(when).toISOString() }),
                    }),
                  );
                }}
              >
                <label>
                  New date and time (on the hour or half hour)
                  <input type="datetime-local" step={1800} required value={when} onChange={(e) => setWhen(e.target.value)} />
                </label>
                <div className="help-row">
                  <button className="help-button" disabled={b.busy}>Ask for this time</button>
                  <button type="button" className="help-button secondary" onClick={() => setMoving(null)}>Keep my booking</button>
                </div>
              </form>
            )}
          </li>
        ))}
      </ul>
      <Problem error={b.error} />
      <p role="status" aria-live="polite" className="help-ok">{note ?? ""}</p>
    </Panel>
  );
}

function Preferences({ slug }: { slug: string }) {
  const p = useHelp<Prefs>(slug, "/me/preferences");
  const b = useBusy();
  if (!p.data) return null;
  return (
    <Panel title="How we contact you" id="help-prefs">
      <fieldset className="help-fieldset" disabled={b.busy}>
        <legend>Send me messages on</legend>
        {p.data.channels.length === 0 && <p className="small">We have no phone number or email address for you.</p>}
        {p.data.channels.map((c) => (
          <label key={c.id} className="help-check">
            <input
              type="checkbox"
              checked={c.receive}
              onChange={(e) =>
                b.run(async () => {
                  await helpApi(slug, `/me/preferences/${c.id}`, { method: "PUT", body: JSON.stringify({ receive: e.target.checked }) });
                  p.reload();
                })
              }
            />
            {CHANNEL[c.channel] ?? c.channel}: {c.address}
          </label>
        ))}
      </fieldset>
      <label className="help-field">
        Preferred language
        <select
          value={p.data.language}
          disabled={b.busy}
          onChange={(e) =>
            b.run(async () => {
              await helpApi(slug, "/me/language", { method: "PUT", body: JSON.stringify({ language: e.target.value }) });
              p.reload();
            })
          }
        >
          {LANGS.map(([code, name]) => (
            <option key={code} value={code}>{name}</option>
          ))}
        </select>
      </label>
      <p className="muted small">Replies to messages you send us within the last day may still reach you.</p>
      <Problem error={b.error} />
    </Panel>
  );
}

function DataRequests({ slug }: { slug: string }) {
  const r = useHelp<DataReq[]>(slug, "/me/data-requests");
  const b = useBusy();
  const ask = (kind: "download" | "delete") =>
    b.run(async () => {
      await helpApi(slug, "/me/data-requests", { method: "POST", body: JSON.stringify({ kind }) });
      r.reload();
    });
  const word = { open: "Waiting for the business", done: "✓ Done", refused: "✕ Refused" };
  return (
    <Panel title="Your data" id="help-data">
      <p className="small">Ask for a copy of what we hold about you, or for it to be deleted. The business handles each request.</p>
      <div className="help-row">
        <button type="button" className="help-button secondary" disabled={b.busy} onClick={() => ask("download")}>
          Request a copy of my data
        </button>
        <button type="button" className="help-button secondary" disabled={b.busy} onClick={() => ask("delete")}>
          Ask to delete my data
        </button>
      </div>
      <Problem error={b.error} />
      <ul className="help-list" aria-live="polite">
        {(r.data ?? []).map((x) => (
          <li key={x.id}>
            {x.kind === "download" ? "Copy of my data" : "Delete my data"}: <span className="help-tag">{word[x.status]}</span>{" "}
            <span className="muted small">asked {new Date(x.created_at).toLocaleDateString("en-GB")}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
