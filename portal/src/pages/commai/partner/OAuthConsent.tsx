import { useLocation } from "react-router-dom";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { useAction } from "../../../ui";
import { SCOPE_WORDS } from ".";
import "./partner.css";

interface Info {
  client: { client_id: string; name: string; partner_name: string };
  redirect_uri: string;
  requested: string[];
  granted: string[];
  not_granted: string[];
  customer_name: string;
}

const PARAMS = ["client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method", "response_type"] as const;

/** The OAuth consent page (ADR 0031): /commai/oauth/authorize?client_id=...&code_challenge=... */
export default function OAuthConsent() {
  const q = new URLSearchParams(useLocation().search);
  const params = Object.fromEntries(PARAMS.map((p) => [p, q.get(p) ?? ""])) as Record<(typeof PARAMS)[number], string>;
  if (!params.response_type) params.response_type = "code";
  const info = useApi<Info>(`/commai/oauth/authorize?${new URLSearchParams(params).toString()}`, 0);
  const act = useAction();
  const answer = (approve: boolean) =>
    act.run(async () => {
      const out = await api<{ redirect: string }>("/commai/oauth/authorize", { method: "POST", body: JSON.stringify({ ...params, approve }) });
      window.location.assign(out.redirect);
    });
  return (
    <section className="card consent" aria-labelledby="consent-title">
      <div className="eyebrow">Connect an app</div>
      {!info.data && <ErrorNote error={info.error} />}
      {info.data && (
        <>
          <h1 id="consent-title" style={{ fontSize: 26, lineHeight: "32px" }}>
            {info.data.client.name} wants to reach {info.data.customer_name}
          </h1>
          <p className="muted">Made by {info.data.client.partner_name}. If you allow it, the app can:</p>
          <ul>
            {info.data.granted.map((s) => (
              <li key={s}>{SCOPE_WORDS[s] ?? s}</li>
            ))}
          </ul>
          {info.data.not_granted.length > 0 && (
            <p className="small">
              It also asked for things your account can't give: {info.data.not_granted.map((s) => SCOPE_WORDS[s] ?? s).join("; ")}. It won't get them.
            </p>
          )}
          <p className="small muted">
            You can revoke access at any time in CommAI, Partners and apps. After you answer you go to <span className="mono">{new URL(info.data.redirect_uri).host}</span>.
          </p>
          <div className="actions">
            <button className="button" disabled={act.busy || info.data.granted.length === 0} onClick={() => answer(true)}>
              Allow
            </button>
            <button className="button secondary" disabled={act.busy} onClick={() => answer(false)}>
              Don't allow
            </button>
          </div>
          <ErrorNote error={act.error} />
        </>
      )}
    </section>
  );
}
