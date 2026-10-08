import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api } from "../api";
import "./identity.css";

/** Which reset screen a signed-out path asks for, if any. */
export function resetRoute(
  path: string,
): { kind: "forgot" } | { kind: "reset"; token: string } | null {
  if (path === "/forgot") return { kind: "forgot" };
  const m = /^\/reset\/([^/?#]+)/.exec(path);
  return m ? { kind: "reset", token: decodeURIComponent(m[1]) } : null;
}

function Frame({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main className="signin-page" id="main">
      <div className="signin-box">
        <div className="signin-brand">
          <img
            className="wm-light"
            src="/brand/exacarib-wordmark.png"
            alt="ExaCarib"
            width={168}
            height={29}
          />
          <img
            className="wm-dark"
            src="/brand/exacarib-wordmark-reversed.png"
            alt="ExaCarib"
            width={168}
            height={29}
          />
          <span className="signin-product">Connect</span>
        </div>
        <section className="card signin" aria-labelledby="reset-title">
          <h1 id="reset-title">{title}</h1>
          {children}
          <p className="signin-foot">
            <a className="link" href="/">
              Back to sign in
            </a>
          </p>
        </section>
      </div>
    </main>
  );
}

/** Ask for a reset link. The answer never says whether the account exists. */
export function ForgotPage() {
  const [email, setEmail] = useState(
    () => new URLSearchParams(window.location.search).get("email") ?? "",
  );
  const [sent, setSent] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ emailed: boolean }>("/auth/password-reset", {
        method: "POST",
        body: JSON.stringify({ email: email.trim() }),
      });
      setSent(out.emailed);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame title="Reset your password">
      {error && (
        <p className="signin-error" role="alert">
          {error}
        </p>
      )}
      {sent === null ? (
        <form onSubmit={submit}>
          <p className="signin-intro">
            Enter the email address you sign in with, and we'll send a link to
            choose a new password.
          </p>
          <label>
            Email
            <input
              type="email"
              autoComplete="username"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <button className="button" type="submit" disabled={busy}>
            {busy ? "Sending…" : "Send reset link"}
          </button>
        </form>
      ) : sent ? (
        <p className="signin-intro" role="status">
          If an account uses <strong>{email.trim()}</strong>, a link is on its
          way. It works once, for one hour. If your organisation signs in with
          its own single sign-on, reset your password there instead.
        </p>
      ) : (
        <p className="signin-intro" role="status">
          Email isn't switched on for this service yet, so no link can be sent.
          Ask your administrator to reset your password.
        </p>
      )}
    </Frame>
  );
}

/** Choose a new password from an emailed link. */
export function ResetPage({ token }: { token: string }) {
  const [email, setEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const path = `/auth/password-reset/${encodeURIComponent(token)}`;

  useEffect(() => {
    api<{ email: string }>(path)
      .then((d) => setEmail(d.email))
      .catch((e) => setError((e as Error).message));
  }, [path]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== again) {
      setError("The passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api(path, { method: "POST", body: JSON.stringify({ password }) });
      setDone(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame title={done ? "Password changed" : "Choose a new password"}>
      {error && (
        <p className="signin-error" role="alert">
          {error}
        </p>
      )}
      {!email && !error && (
        <p className="signin-checking" role="status">
          Checking your link…
        </p>
      )}
      {done && (
        <p className="signin-intro" role="status">
          Your password has changed and you've been signed out everywhere. Sign
          in with the new one.
        </p>
      )}
      {email && !done && (
        <form onSubmit={submit}>
          <p className="signin-intro">
            For <strong>{email}</strong>.
          </p>
          <input
            type="email"
            autoComplete="username"
            value={email}
            readOnly
            hidden
          />
          <label>
            New password, at least 12 characters
            <input
              type="password"
              autoComplete="new-password"
              minLength={12}
              required
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label>
            New password again
            <input
              type="password"
              autoComplete="new-password"
              minLength={12}
              required
              value={again}
              onChange={(e) => setAgain(e.target.value)}
            />
          </label>
          <button className="button" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save new password"}
          </button>
        </form>
      )}
    </Frame>
  );
}
