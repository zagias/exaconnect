import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  api,
  getPasskey,
  passkeysSupported,
  signedIn,
  type Discovery,
  type LoginResult,
  type SignInProvider,
  type User,
} from "./api";
import "./tables.css";
import "./pages/identity.css";

interface Auth {
  user: User | null;
  signOut: () => void;
}

const AuthContext = createContext<Auth>({ user: null, signOut: () => {} });
export const useAuth = () => useContext(AuthContext);

/** Shows the sign-in card until there is a valid session, then the app.
 * The session is an HttpOnly cookie; who is signed in comes from /auth/me. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const check = () => {
      api<User>("/auth/me")
        .then((u) => !cancelled && setUser(u))
        .catch(() => !cancelled && setUser(null))
        .finally(() => !cancelled && setChecking(false));
    };
    const out = () => setUser(null);
    check();
    window.addEventListener("exa-auth", check);
    window.addEventListener("exa-signed-out", out);
    return () => {
      cancelled = true;
      window.removeEventListener("exa-auth", check);
      window.removeEventListener("exa-signed-out", out);
    };
  }, []);

  const signOut = () => {
    api("/auth/logout", { method: "POST" })
      .catch(() => {})
      .finally(() => setUser(null));
  };

  if (checking || !user)
    return (
      <main className="signin-page" id="main">
        <div className="signin-box">
          <div className="signin-brand">
            <img className="wm-light" src="/brand/exacarib-wordmark.png" alt="ExaCarib" width={168} height={29} />
            <img className="wm-dark" src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={168} height={29} />
            <span className="signin-product">Connect</span>
          </div>
          {checking ? (
            <p className="signin-checking" role="status">
              Checking your session…
            </p>
          ) : (
            <SignIn />
          )}
        </div>
      </main>
    );
  return <AuthContext.Provider value={{ user, signOut }}>{children}</AuthContext.Provider>;
}

/** A sign-in error handed back in the address after a single sign-on redirect. */
function errorFromAddress(): string | null {
  try {
    const q = new URLSearchParams(window.location.search);
    const msg = q.get("signin_error");
    if (msg) {
      q.delete("signin_error");
      const rest = q.toString();
      window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
    }
    return msg ? msg.slice(0, 300) : null;
  } catch {
    return null;
  }
}

type Step = "email" | "password" | "code";

function SignIn() {
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [sso, setSso] = useState<Discovery | null>(null);
  const [challenge, setChallenge] = useState<LoginResult | null>(null);
  const [providers, setProviders] = useState<SignInProvider[]>([]);
  const [error, setError] = useState<string | null>(errorFromAddress);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ providers: SignInProvider[] }>("/auth/providers")
      .then((p) => setProviders(p.providers))
      .catch(() => setProviders([]));
  }, []);

  const attempt = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const discover = (e: FormEvent) => {
    e.preventDefault();
    attempt(async () => {
      const d = await api<Discovery>("/auth/sso/discover", { method: "POST", body: JSON.stringify({ email }) });
      if (d.method === "sso" && d.start_url && !d.password_allowed) {
        window.location.assign(d.start_url);
        return;
      }
      setSso(d.method === "sso" ? d : null);
      setStep("password");
    });
  };

  const signIn = (e: FormEvent) => {
    e.preventDefault();
    attempt(async () => {
      const out = await api<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      setPassword("");
      if (out.mfa_required) {
        setChallenge(out);
        setStep("code");
        return;
      }
      signedIn();
    });
  };

  const sendCode = (e: FormEvent) => {
    e.preventDefault();
    attempt(async () => {
      await api("/auth/login/mfa", {
        method: "POST",
        body: JSON.stringify({ challenge: challenge?.challenge, code: code.trim() }),
      });
      signedIn();
    });
  };

  const withPasskey = () =>
    attempt(async () => {
      const opts = await api<{ ticket: string; options: Record<string, unknown> }>("/auth/login/passkey/options", {
        method: "POST",
        body: JSON.stringify({ challenge: challenge?.challenge }),
      });
      const credential = await getPasskey(opts.options);
      await api("/auth/login/passkey", {
        method: "POST",
        body: JSON.stringify({ challenge: challenge?.challenge, ticket: opts.ticket, credential }),
      });
      signedIn();
    });

  const restart = () => {
    setStep("email");
    setPassword("");
    setCode("");
    setChallenge(null);
    setSso(null);
    setError(null);
  };

  const errorNote = error && (
    <p className="signin-error" id="signin-error" role="alert">
      {error}
    </p>
  );
  const describedBy = error ? "signin-error" : undefined;
  const methods = challenge?.methods ?? [];

  return (
    <section className="card signin" aria-labelledby="signin-title">
      <h1 id="signin-title">{step === "code" ? "Two-step sign-in" : "Sign in"}</h1>
      {step === "email" && (
        <>
          <p className="signin-intro">Your sites, paths and routing decisions in one place.</p>
          <form onSubmit={discover} aria-describedby={describedBy}>
            <label>
              Work email
              <input
                type="email"
                autoComplete="username"
                required
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            {errorNote}
            <button className="button" type="submit" disabled={busy}>
              {busy ? "Checking…" : "Continue"}
            </button>
          </form>
          {providers.length > 0 && (
            <div className="signin-providers" aria-label="Other ways to sign in">
              <p className="signin-or">
                <span>or</span>
              </p>
              {providers.map((p) => (
                <a key={p.id} className="button secondary" href={p.start_url}>
                  {p.label}
                </a>
              ))}
            </div>
          )}
        </>
      )}

      {step === "password" && (
        <>
          <p className="signin-intro">
            Signing in as <strong>{email}</strong>.{" "}
            <button type="button" className="link" onClick={restart}>
              Change
            </button>
          </p>
          {sso?.start_url && (
            <div className="signin-sso">
              <a className="button" href={sso.start_url}>
                Continue with {sso.name ?? "your organisation"}
              </a>
              <p className="signin-or">
                <span>or use your password</span>
              </p>
            </div>
          )}
          <form onSubmit={signIn} aria-describedby={describedBy}>
            <input type="email" autoComplete="username" value={email} readOnly hidden />
            <label>
              Password
              <input
                type="password"
                autoComplete="current-password"
                required
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {errorNote}
            <button className={sso?.start_url ? "button secondary" : "button"} type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
          <p className="signin-foot">
            <a className="link" href={`/forgot?email=${encodeURIComponent(email)}`}>
              Forgotten your password?
            </a>
          </p>
        </>
      )}

      {step === "code" && (
        <>
          <p className="signin-intro">
            {methods.includes("totp")
              ? "Enter the 6-digit code from your authenticator app, or one of your recovery codes."
              : "Use your passkey, or enter one of your recovery codes."}
          </p>
          {methods.includes("passkey") && passkeysSupported() && (
            <div className="signin-sso">
              <button className="button" type="button" onClick={withPasskey} disabled={busy}>
                Use a passkey
              </button>
            </div>
          )}
          <form onSubmit={sendCode} aria-describedby={describedBy}>
            <label>
              {methods.includes("totp") ? "Code" : "Recovery code"}
              <input
                inputMode={methods.includes("totp") ? "numeric" : "text"}
                autoComplete="one-time-code"
                required
                autoFocus={!methods.includes("passkey")}
                maxLength={20}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            {errorNote}
            <button className={methods.includes("passkey") ? "button secondary" : "button"} type="submit" disabled={busy}>
              {busy ? "Checking…" : "Verify"}
            </button>
          </form>
          <p className="signin-foot">
            <button type="button" className="link" onClick={restart}>
              Start again
            </button>
          </p>
        </>
      )}
      <p className="signin-foot">Need an account? Ask your organisation&apos;s administrator.</p>
    </section>
  );
}
