import { useEffect, useState, type FormEvent } from "react";
import { api, type InviteInfo, type User } from "../api";
import "../tables.css";
import "./identity.css";

export const PENDING_INVITE = "exa.invite";

const ROLE_LABEL: Record<string, string> = { owner: "its owner", admin: "an admin", member: "a member", viewer: "a viewer" };

/** The token in /invite/:token. */
export function inviteToken(path: string): string | null {
  const m = /^\/invite\/([^/?#]+)/.exec(path);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Accept an invitation (ADR 0023). Works signed out: someone new chooses a
 * password; someone with an account signs in first and comes back here.
 */
export default function InvitePage({ token }: { token: string }) {
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [me, setMe] = useState<User | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [joined, setJoined] = useState<string | null>(null);
  // Owners and admins start at the set-up checklist (ADR 0043); everyone else at their first app.
  const [start, setStart] = useState("/");

  useEffect(() => {
    api<InviteInfo>(`/invites/${encodeURIComponent(token)}`)
      .then(setInfo)
      .catch((e) => setError((e as Error).message));
    api<User>("/auth/me")
      .then(setMe)
      .catch(() => setMe(null));
  }, [token]);

  const accept = async (body: Record<string, string>) => {
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ organisation: string; role?: string }>(`/invites/${encodeURIComponent(token)}/accept`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      try {
        sessionStorage.removeItem(PENDING_INVITE);
      } catch {
        /* storage unavailable */
      }
      setStart(out.role === "owner" || out.role === "admin" ? "/org/setup" : "/");
      setJoined(out.organisation);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const signUp = (e: FormEvent) => {
    e.preventDefault();
    if (password !== again) {
      setError("The passwords don't match.");
      return;
    }
    accept({ password, name: name.trim() });
  };

  const signInFirst = () => {
    try {
      sessionStorage.setItem(PENDING_INVITE, token);
    } catch {
      /* storage unavailable: they open the link again after signing in */
    }
    window.location.assign("/");
  };

  const wrongAccount = me && info && me.email.toLowerCase() !== info.email;

  return (
    <main className="signin-page" id="main">
      <div className="signin-box">
        <div className="signin-brand">
          <img className="wm-light" src="/brand/exacarib-wordmark.png" alt="ExaCarib" width={168} height={29} />
          <img className="wm-dark" src="/brand/exacarib-wordmark-reversed.png" alt="ExaCarib" width={168} height={29} />
          <span className="signin-product">Connect</span>
        </div>
        <section className="card signin" aria-labelledby="invite-title">
          <h1 id="invite-title">{joined ? "You're in" : "Join an organisation"}</h1>
          {error && (
            <p className="signin-error" role="alert">
              {error}
            </p>
          )}
          {!info && !error && <p className="signin-checking" role="status">Checking your invitation…</p>}

          {joined && (
            <>
              <p className="signin-intro">
                You are now a member of <strong>{joined}</strong>.
              </p>
              <button className="button" type="button" onClick={() => window.location.assign(start)}>
                {start === "/" ? "Open ExaCarib" : "Start setting up"}
              </button>
            </>
          )}

          {info && !joined && (
            <>
              <p className="signin-intro">
                You're invited to join <strong>{info.organisation}</strong> as {ROLE_LABEL[info.role] ?? info.role}. The
                invitation is for <strong>{info.email}</strong> and expires on{" "}
                {new Date(info.expires_at).toLocaleDateString("en-GB", { day: "numeric", month: "long" })}.
              </p>

              {me === undefined && <p className="signin-checking">Checking your session…</p>}

              {me && !wrongAccount && (
                <button className="button" type="button" disabled={busy} onClick={() => accept({})}>
                  {busy ? "Joining…" : `Join ${info.organisation}`}
                </button>
              )}

              {me && wrongAccount && (
                <p className="signin-intro">
                  You're signed in as <strong>{me.email}</strong>. Sign out, then open this link again and sign in as{" "}
                  {info.email}.
                </p>
              )}

              {me === null && (info.has_account || info.sso_required) && (
                <>
                  <p className="signin-intro">
                    {info.sso_required
                      ? "Your organisation signs in with its own single sign-on. Sign in that way, and you'll come back here to join."
                      : "You already have an account. Sign in, and you'll come back here to join."}
                  </p>
                  <button className="button" type="button" onClick={signInFirst}>
                    Sign in
                  </button>
                </>
              )}

              {me === null && !info.has_account && !info.sso_required && (
                <form onSubmit={signUp}>
                  <input type="email" autoComplete="username" value={info.email} readOnly hidden />
                  <label>
                    Your name
                    <input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoComplete="name" />
                  </label>
                  <label>
                    Choose a password, at least 12 characters
                    <input
                      type="password"
                      autoComplete="new-password"
                      minLength={12}
                      required
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  </label>
                  <label>
                    Password again
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
                    {busy ? "Joining…" : "Create account and join"}
                  </button>
                </form>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}
