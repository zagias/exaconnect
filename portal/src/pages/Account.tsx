import { useState, type FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Eyebrow } from "../components";
import ApiKeys from "./ApiKeys";

/** Your account: change your password (other sessions are signed out) and manage your API keys. */
export default function Account() {
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setDone(false);
    if (next !== again) {
      setError("The new passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      await api("/auth/password", { method: "POST", body: JSON.stringify({ current, new: next }) });
      setDone(true);
      setCurrent("");
      setNext("");
      setAgain("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <Eyebrow>Account</Eyebrow>
        <h1>Your account</h1>
        <p className="muted">
          Signed in as {user?.email}, role {user?.role}.
        </p>
      </div>
      <section className="card" style={{ maxWidth: 560 }}>
        <div className="card-head">
          <h2>Change your password</h2>
        </div>
        <form className="form" onSubmit={submit} style={{ gridTemplateColumns: "1fr" }}>
          <label>
            Current password
            <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </label>
          <label>
            New password, at least 12 characters
            <input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required />
          </label>
          <label>
            New password again
            <input type="password" autoComplete="new-password" minLength={12} value={again} onChange={(e) => setAgain(e.target.value)} required />
          </label>
          <div className="actions">
            <button className="button" disabled={busy}>
              {busy ? "Saving…" : "Change password"}
            </button>
          </div>
        </form>
        <ErrorNote error={error} />
        {done && <p className="ok-note">Password changed. Your other sessions have been signed out.</p>}
      </section>
      <ApiKeys />
    </>
  );
}
