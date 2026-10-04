import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, getToken, setToken, type User } from "./api";
import "./tables.css";

interface Auth {
  user: User | null;
  signOut: () => void;
}

const AuthContext = createContext<Auth>({ user: null, signOut: () => {} });
export const useAuth = () => useContext(AuthContext);

/** Shows the sign-in card until there is a valid session, then the app. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    const check = () => {
      if (!getToken()) {
        setUser(null);
        setChecking(false);
        return;
      }
      api<User>("/auth/me")
        .then(setUser)
        .catch(() => setUser(null))
        .finally(() => setChecking(false));
    };
    check();
    window.addEventListener("exa-auth", check);
    return () => window.removeEventListener("exa-auth", check);
  }, []);

  const signOut = () => {
    api("/auth/logout", { method: "POST" }).catch(() => {});
    setToken(null);
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

function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(out.token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card signin" aria-labelledby="signin-title">
      <h1 id="signin-title">Sign in</h1>
      <p className="signin-intro">Your sites, paths and routing decisions in one place.</p>
      <form onSubmit={submit} aria-describedby={error ? "signin-error" : undefined}>
        <label>
          Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p className="signin-error" id="signin-error" role="alert">
            {error}
          </p>
        )}
        <button className="button" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="signin-foot">Need an account or a new password? Ask your ExaCarib administrator.</p>
    </section>
  );
}
