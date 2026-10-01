import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, getToken, setToken, type User } from "./api";
import { Eyebrow } from "./components";

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

  if (checking) return <p className="muted">Checking your session…</p>;
  if (!user) return <SignIn />;
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
    <section className="card signin">
      <Eyebrow>Sign in</Eyebrow>
      <h1>ExaConnect portal</h1>
      <form onSubmit={submit}>
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
          <p className="pill bad" role="alert">
            {error}
          </p>
        )}
        <button className="button" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </section>
  );
}
