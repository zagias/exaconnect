import { useState, type FormEvent } from "react";
import { api, createPasskey, passkeysSupported, useApi, type TwoStepStatus } from "../api";
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";
import { PageHead, useAction } from "../ui";
import ApiKeys from "./ApiKeys";
import "./identity.css";

/** Your account: password, two-step sign-in, passkeys and your API keys. */
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
      <PageHead eyebrow="You" title="Profile and sign-in">
        Change your password, turn on two-step sign-in and manage the API keys your own code uses.
      </PageHead>
      {user?.role === "customer" && (
        <section className="card" style={{ maxWidth: 880, marginBottom: 24 }} aria-labelledby="orgs-title">
          <div className="card-head">
            <h2 id="orgs-title">Your organisations</h2>
            <Link to="/account/people">People</Link>
          </div>
          {(user.memberships ?? []).length === 0 ? (
            <p className="muted small">You are not a member of an organisation. Ask an owner to invite you.</p>
          ) : (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {(user.memberships ?? []).map((m) => (
                <li key={m.customer_id}>
                  <strong>{m.name}</strong>: {m.role}
                  {m.customer_id === user.customer_id && <span className="muted"> (acting for now)</span>}
                  {m.managed_by && <span className="muted"> · managed by your directory</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      <section className="card" style={{ maxWidth: 880 }} aria-labelledby="password-title">
        <div className="card-head">
          <h2 id="password-title">Change your password</h2>
        </div>
        <form className="form" onSubmit={submit} style={{ gridTemplateColumns: "repeat(auto-fill, minmax(min(220px, 100%), 1fr))" }}>
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
          <div className="actions wide">
            <button className="button" disabled={busy}>
              {busy ? "Saving…" : "Change password"}
            </button>
          </div>
        </form>
        <ErrorNote error={error} />
        {done && (
          <p className="ok-note" role="status">
            Password changed. Your other sessions have been signed out.
          </p>
        )}
      </section>
      <TwoStep />
      <ApiKeys />
    </>
  );
}

function day(iso: string | null): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Recovery codes, shown once, with copy and save. */
function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const text = `ExaCarib recovery codes\n${codes.join("\n")}\n`;
  const save = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "exacarib-recovery-codes.txt";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="secret" role="region" aria-label="Recovery codes">
      <p className="callout" style={{ margin: 0 }}>
        <strong>Save these recovery codes now.</strong> Each one signs you in once if you lose your phone or passkey.
        They are not shown again.
      </p>
      <ul className="identity-codes">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className="secret-actions">
        <button type="button" className="button secondary" onClick={() => navigator.clipboard?.writeText(text)}>
          Copy
        </button>
        <button type="button" className="button secondary" onClick={save}>
          Save as a file
        </button>
        <button type="button" className="button" onClick={onDone}>
          I've saved them
        </button>
      </div>
    </div>
  );
}

/** Two-step sign-in: an authenticator app (TOTP) and passkeys, with recovery codes. */
function TwoStep() {
  const { data, error, reload } = useApi<TwoStepStatus>("/auth/two-step", 0);
  const act = useAction();
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [mode, setMode] = useState<"off" | "codes" | null>(null);
  const [keyName, setKeyName] = useState("");

  const start = () =>
    act.run(async () => {
      setSetup(await api<{ secret: string; otpauth_uri: string }>("/auth/two-step/start", { method: "POST" }));
      setCode("");
    });

  const confirm = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ recovery_codes: string[] }>("/auth/two-step/confirm", {
        method: "POST",
        body: JSON.stringify({ code: code.trim() }),
      });
      setSetup(null);
      setCode("");
      setCodes(out.recovery_codes);
      reload();
    });
  };

  const withCode = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      if (mode === "off") {
        await api("/auth/two-step/disable", { method: "POST", body: JSON.stringify({ code: code.trim() }) });
      } else {
        const out = await api<{ recovery_codes: string[] }>("/auth/two-step/recovery-codes", {
          method: "POST",
          body: JSON.stringify({ code: code.trim() }),
        });
        setCodes(out.recovery_codes);
      }
      setMode(null);
      setCode("");
      reload();
    });
  };

  const addPasskey = () =>
    act.run(async () => {
      const opts = await api<{ ticket: string; options: Record<string, unknown> }>("/auth/passkeys/options", {
        method: "POST",
      });
      const credential = await createPasskey(opts.options);
      const out = await api<{ recovery_codes: string[] | null }>("/auth/passkeys", {
        method: "POST",
        body: JSON.stringify({ ticket: opts.ticket, credential, name: keyName.trim() || "Passkey" }),
      });
      setKeyName("");
      if (out.recovery_codes) setCodes(out.recovery_codes);
      reload();
    });

  const removePasskey = (id: number, name: string) => {
    if (!window.confirm(`Remove the passkey "${name}"?`)) return;
    act.run(async () => {
      await api(`/auth/passkeys/${id}`, { method: "DELETE" });
      reload();
    });
  };

  const secretGroups = setup?.secret.match(/.{1,4}/g)?.join(" ") ?? "";

  return (
    <section className="card" style={{ maxWidth: 880, marginTop: 24 }} aria-labelledby="two-step-title">
      <div className="card-head">
        <h2 id="two-step-title">Two-step sign-in</h2>
        {data && (
          <span className={`identity-status ${data.enabled || data.passkeys.length ? "on" : "off"}`}>
            {data.enabled || data.passkeys.length ? "✓ On" : "Off"}
          </span>
        )}
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        After your password, ExaCarib asks for a code from an authenticator app or for your passkey. If your
        organisation signs you in with its own single sign-on, its two-step rules apply instead.
      </p>
      <ErrorNote error={error} />
      {codes && <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />}

      <h3 style={{ margin: "16px 0 8px" }}>Authenticator app</h3>
      {data && !data.enabled && !setup && (
        <button type="button" className="button" onClick={start} disabled={act.busy}>
          Set up an authenticator app
        </button>
      )}
      {setup && (
        <div className="secret">
          <p style={{ margin: "0 0 8px" }}>
            In your authenticator app, add an account with this key (time-based), or open the link on your phone.
          </p>
          <p style={{ margin: "0 0 8px" }}>
            Key: <code aria-label="Setup key">{secretGroups}</code>
          </p>
          <p className="small" style={{ margin: "0 0 8px" }}>
            <a href={setup.otpauth_uri}>Open in an authenticator app</a>
            <span className="muted"> · Link: </span>
            <code className="small">{setup.otpauth_uri}</code>
          </p>
          <form className="form" onSubmit={confirm}>
            <label>
              6-digit code from the app
              <input
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9 ]{6,7}"
                maxLength={7}
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            <div className="actions">
              <button className="button" disabled={act.busy}>
                {act.busy ? "Checking…" : "Turn on"}
              </button>
              <button type="button" className="button secondary" onClick={() => setSetup(null)}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}
      {data?.enabled && (
        <>
          <p className="small" style={{ margin: "0 0 8px" }}>
            On since {day(data.enabled_at)}. {data.recovery_codes_left} recovery codes left.
          </p>
          {!mode && (
            <div className="identity-row">
              <button type="button" className="button secondary" onClick={() => setMode("codes")}>
                New recovery codes
              </button>
              <button type="button" className="button secondary" onClick={() => setMode("off")}>
                Turn off
              </button>
            </div>
          )}
          {mode && (
            <form className="form" onSubmit={withCode}>
              <label>
                {mode === "off" ? "A code, to turn it off" : "A code, for new recovery codes"}
                <input
                  autoComplete="one-time-code"
                  maxLength={20}
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </label>
              <div className="actions">
                <button className="button" disabled={act.busy}>
                  {mode === "off" ? "Turn off" : "Make new codes"}
                </button>
                <button type="button" className="button secondary" onClick={() => setMode(null)}>
                  Cancel
                </button>
              </div>
            </form>
          )}
        </>
      )}

      <h3 style={{ margin: "24px 0 8px" }}>Passkeys</h3>
      {data && !data.passkeys_available && <p className="muted small">Passkeys are not available on this controller yet.</p>}
      {data?.passkeys_available && (
        <>
          {data.passkeys.length > 0 && (
            <div className="table-wrap">
              <table className="paths dt stack">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Added</th>
                    <th scope="col">Last used</th>
                    <th scope="col" className="actions">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.passkeys.map((p) => (
                    <tr key={p.id}>
                      <td>
                        <strong>{p.name}</strong>
                      </td>
                      <td data-label="Added" className="mono">
                        {day(p.created_at)}
                      </td>
                      <td data-label="Last used" className="mono">
                        {day(p.last_used_at)}
                      </td>
                      <td className="actions">
                        <button type="button" className="button secondary" onClick={() => removePasskey(p.id, p.name)}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {passkeysSupported() ? (
            <div className="form" style={{ marginTop: 8 }}>
              <label>
                Name for the new passkey
                <input value={keyName} maxLength={60} placeholder="Work laptop" onChange={(e) => setKeyName(e.target.value)} />
              </label>
              <div className="actions">
                <button type="button" className="button" onClick={addPasskey} disabled={act.busy}>
                  Add a passkey
                </button>
              </div>
            </div>
          ) : (
            <p className="muted small">This browser can't create passkeys.</p>
          )}
        </>
      )}
      <ErrorNote error={act.error} />
    </section>
  );
}
