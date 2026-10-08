import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  useApi,
  type DirectoryGroup,
  type DomainClaim,
  type ScimToken,
  type SsoConnection,
  type SsoConnections,
} from "../../../api";
import { useAuth } from "../../../auth";
import { ErrorNote } from "../../../components";
import { useCustomer } from "../../../customer";
import { Card, useAction } from "../../../ui";
import "../../identity.css";
import DirectoryPicker from "./Directory";

const STATUS: Record<SsoConnection["status"], { word: string; cls: string }> = {
  draft: { word: "Draft: not tested", cls: "off" },
  tested: { word: "Tested: not switched on", cls: "wait" },
  enabled: { word: "✓ On", cls: "on" },
  disabled: { word: "Off", cls: "off" },
};

const DOMAIN: Record<string, { word: string; cls: string }> = {
  pending: { word: "waiting for ExaCarib approval", cls: "wait" },
  approved: { word: "✓ approved", cls: "on" },
  rejected: { word: "✕ not approved", cls: "bad" },
};

function day(iso: string | null | undefined): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** A result handed back in the address after a test sign-in. */
function testResultFromAddress(): { ok: boolean; message: string } | null {
  try {
    const q = new URLSearchParams(window.location.search);
    const r = q.get("sso_test");
    if (!r) return null;
    const out = { ok: r === "ok", message: (q.get("message") ?? "").slice(0, 300) };
    q.delete("sso_test");
    q.delete("message");
    const rest = q.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
    return out;
  } catch {
    return null;
  }
}

/** Sign-in settings for a business: single sign-on, directory sync (SCIM) and group mappings (ADR 0017). */
export default function SignIn() {
  const { current } = useCustomer();
  const { user } = useAuth();
  const [result] = useState(testResultFromAddress);
  if (!current) return <p className="muted">Choose an organisation first.</p>;
  const base = `/customers/${current.id}`;
  return (
    <>
      {result && (
        <p className={result.ok ? "ok-note" : "signin-error"} role="status">
          {result.ok ? "✓ " : "✕ "}
          {result.message || (result.ok ? "Test sign-in worked." : "Test sign-in failed.")}
        </p>
      )}
      <DirectoryPicker customerId={current.id} />
      <Connections base={base} />
      <Directory base={base} customerId={current.id} />
      {user?.role === "admin" && <DomainApprovals />}
    </>
  );
}

function Connections({ base }: { base: string }) {
  const { data, error, reload } = useApi<SsoConnections>(`${base}/sso-connections`, 0);
  const act = useAction();
  const [adding, setAdding] = useState(false);

  const call = (path: string, init: RequestInit = { method: "POST" }) =>
    act.run(async () => {
      await api(`${base}/sso-connections/${path}`, init);
      reload();
    });

  const test = (c: SsoConnection) =>
    act.run(async () => {
      const out = await api<{ start_url: string }>(`${base}/sso-connections/${c.id}/test`, {
        method: "POST",
        body: JSON.stringify({ next: window.location.pathname }),
      });
      window.location.assign(out.start_url);
    });

  const remove = (c: SsoConnection) => {
    if (!window.confirm(`Delete the single sign-on connection "${c.display_name}"? People who use it can't sign in.`)) return;
    call(c.id, { method: "DELETE" });
  };

  const gateway = data?.gateway;
  return (
    <Card title="Single sign-on">
      <p className="muted small" style={{ marginTop: 0 }}>
        Let your people sign in with your own identity provider (Microsoft Entra ID, Okta, Google Workspace, ADFS and
        others) over SAML 2.0 or OpenID Connect. Add it, run a test sign-in, then switch it on. Each email domain is
        checked by ExaCarib before it sends anyone to your provider.
      </p>
      {gateway && !gateway.configured && (
        <p className="callout small">The sign-in gateway is not set up on this controller yet, so test sign-ins can't run.</p>
      )}
      {gateway?.simulated && (
        <p className="callout small">
          Connections are kept by a simulated gateway until ExaCarib connects the live one. Nothing is sent to your
          identity provider yet.
        </p>
      )}
      <ErrorNote error={error} />
      {data && data.items.length === 0 && !adding && <p className="muted">No single sign-on connection yet.</p>}
      {data?.items.map((c) => (
        <div className="identity-conn" key={c.id}>
          <div className="identity-row" style={{ justifyContent: "space-between" }}>
            <h3>
              {c.display_name} <span className="muted small">{c.protocol === "saml" ? "SAML 2.0" : "OpenID Connect"}</span>
            </h3>
            <span className={`identity-status ${STATUS[c.status].cls}`}>{STATUS[c.status].word}</span>
          </div>
          <ul className="identity-domains" aria-label="Email domains">
            {c.domains.map((d) => (
              <li key={d.id} className="small">
                <span className="mono">{d.domain}</span>{" "}
                <span className={`identity-status ${DOMAIN[d.status].cls}`}>{DOMAIN[d.status].word}</span>
              </li>
            ))}
            {c.domains.length === 0 && <li className="muted small">No email domains yet.</li>}
          </ul>
          {c.last_test && (
            <p className="muted small">
              Last test {day(c.last_test.at)}: {c.last_test.ok ? "worked" : "failed"}
              {c.last_test.email ? ` for ${c.last_test.email}` : ""}. {c.last_test.message}
            </p>
          )}
          {c.provider_setup?.redirect_uri && (
            <details className="small">
              <summary>Details for your identity provider</summary>
              <p className="muted small">
                {c.protocol === "saml" ? "Service provider entity ID" : "Redirect URI"}:{" "}
                <code className="wrap">{c.protocol === "saml" ? c.provider_setup.sp_entity_id : c.provider_setup.redirect_uri}</code>
              </p>
              {c.protocol === "saml" && (
                <>
                  <p className="muted small">
                    Reply (ACS) URL: <code className="wrap">{c.provider_setup.redirect_uri}</code>
                  </p>
                  <p className="muted small">
                    Service provider metadata: <code className="wrap">{c.provider_setup.sp_metadata_url}</code>
                  </p>
                </>
              )}
            </details>
          )}
          <label className="check small" style={{ display: "inline-flex", gap: 8, margin: "8px 0" }}>
            <input
              type="checkbox"
              checked={c.require_sso}
              disabled={act.busy}
              onChange={(e) =>
                call(c.id, { method: "PATCH", body: JSON.stringify({ require_sso: e.target.checked }) })
              }
            />
            Require single sign-on: turn off passwords for these domains
          </label>
          <div className="identity-row">
            <button type="button" className="button secondary" onClick={() => test(c)} disabled={act.busy || !gateway?.configured}>
              Test sign-in
            </button>
            {c.status !== "enabled" ? (
              <button
                type="button"
                className="button"
                onClick={() => call(`${c.id}/enable`)}
                disabled={act.busy || !c.last_test?.ok || c.status === "draft"}
              >
                Switch on
              </button>
            ) : (
              <button type="button" className="button secondary" onClick={() => call(`${c.id}/disable`)} disabled={act.busy}>
                Switch off
              </button>
            )}
            <button type="button" className="button secondary" onClick={() => remove(c)} disabled={act.busy}>
              Delete
            </button>
          </div>
        </div>
      ))}
      <ErrorNote error={act.error} />
      {adding ? (
        <AddConnection base={base} onDone={() => (setAdding(false), reload())} />
      ) : (
        <button type="button" className="button" onClick={() => setAdding(true)} style={{ marginTop: 8 }}>
          Add a connection
        </button>
      )}
    </Card>
  );
}

function AddConnection({ base, onDone }: { base: string; onDone: () => void }) {
  const act = useAction();
  const [protocol, setProtocol] = useState<"saml" | "oidc">("saml");
  const [name, setName] = useState("");
  const [domains, setDomains] = useState("");
  const [xml, setXml] = useState("");
  const [url, setUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");

  const readFile = (f: File | undefined) => {
    if (!f) return;
    if (f.size > 512_000) {
      act.setError("The metadata file is too large.");
      return;
    }
    f.text().then(setXml);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/sso-connections`, {
        method: "POST",
        body: JSON.stringify({
          protocol,
          display_name: name.trim(),
          domains: domains
            .split(/[\s,]+/)
            .map((d) => d.trim())
            .filter(Boolean),
          metadata_xml: protocol === "saml" ? xml : "",
          metadata_url: url.trim(),
          client_id: protocol === "oidc" ? clientId.trim() : "",
          client_secret: protocol === "oidc" ? secret : "",
        }),
      });
      setSecret("");
      onDone();
    });
  };

  return (
    <form className="form" onSubmit={submit} style={{ marginTop: 16 }}>
      <label>
        Protocol
        <select value={protocol} onChange={(e) => setProtocol(e.target.value as "saml" | "oidc")}>
          <option value="saml">SAML 2.0</option>
          <option value="oidc">OpenID Connect</option>
        </select>
      </label>
      <label>
        Name people see
        <input value={name} maxLength={80} required placeholder="Example Bank staff" onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="wide">
        Email domains, separated by commas
        <input value={domains} placeholder="examplebank.com, examplebank.co.tt" onChange={(e) => setDomains(e.target.value)} />
      </label>
      {protocol === "saml" ? (
        <>
          <label className="wide">
            Identity provider metadata (XML file)
            <input type="file" accept=".xml,text/xml,application/xml" onChange={(e) => readFile(e.target.files?.[0])} />
          </label>
          {xml && <p className="muted small wide">Metadata loaded ({Math.round(xml.length / 1024)} KB).</p>}
          <label className="wide">
            Or its metadata URL
            <input type="url" value={url} placeholder="https://…/FederationMetadata.xml" onChange={(e) => setUrl(e.target.value)} />
          </label>
        </>
      ) : (
        <>
          <label className="wide">
            Discovery URL
            <input
              type="url"
              value={url}
              required
              placeholder="https://login.example.com/.well-known/openid-configuration"
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <label>
            Client ID
            <input value={clientId} required onChange={(e) => setClientId(e.target.value)} />
          </label>
          <label>
            Client secret
            <input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </label>
          <p className="muted small wide">The secret goes to the sign-in gateway only. ExaCarib does not keep it.</p>
        </>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          {act.busy ? "Saving…" : "Save as draft"}
        </button>
        <button type="button" className="button secondary" onClick={onDone}>
          Cancel
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

function Directory({ base, customerId }: { base: string; customerId: string }) {
  const tokens = useApi<{ scim_url: string; items: ScimToken[] }>(`${base}/scim-tokens`, 0);
  const groups = useApi<DirectoryGroup[]>(`${base}/directory-groups`, 30_000);
  const teams = useApi<{ id: string; name: string }[]>(`/commai/customers/${customerId}/teams`, 0);
  const act = useAction();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ScimToken | null>(null);
  useEffect(() => setCreated(null), [customerId]);

  const create = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      setCreated(await api<ScimToken>(`${base}/scim-tokens`, { method: "POST", body: JSON.stringify({ name: name.trim() }) }));
      setName("");
      tokens.reload();
    });
  };

  const revoke = (t: ScimToken) => {
    if (!window.confirm(`Revoke the SCIM token "${t.name}"? Your directory stops syncing until it has a new one.`)) return;
    act.run(async () => {
      await api(`${base}/scim-tokens/${t.id}`, { method: "DELETE" });
      tokens.reload();
    });
  };

  const map = (g: DirectoryGroup, change: Partial<{ seat: string; team_id: string | null; business_admin: boolean }>) =>
    act.run(async () => {
      await api(`${base}/directory-groups/${g.id}`, {
        method: "PUT",
        body: JSON.stringify({
          seat: g.seat,
          team_id: g.team_id,
          business_admin: g.business_admin !== "none",
          ...change,
        }),
      });
      groups.reload();
    });

  const approve = (g: DirectoryGroup) =>
    act.run(async () => {
      await api(`${base}/directory-groups/${g.id}/approve-admin`, { method: "POST" });
      groups.reload();
    });

  return (
    <Card title="Directory sync (SCIM)">
      <p className="muted small" style={{ marginTop: 0 }}>
        Your directory adds, changes and removes people and groups here automatically. Someone removed or switched off
        there is signed out of ExaCarib at once and their API keys stop working. New people can use Jibsy; they get
        admin rights only through a group mapping that a second admin approves.
      </p>
      <ErrorNote error={tokens.error} />
      {tokens.data && (
        <p className="small">
          SCIM address: <code className="wrap">{tokens.data.scim_url}</code>
        </p>
      )}
      {created?.token && (
        <div className="secret" role="region" aria-label="New SCIM token">
          <p className="callout" style={{ margin: 0 }}>
            <strong>Copy this token into your directory now.</strong> It is not shown again.
          </p>
          <code>{created.token}</code>
          <div className="secret-actions">
            <button type="button" className="button secondary" onClick={() => navigator.clipboard?.writeText(created.token ?? "")}>
              Copy
            </button>
            <button type="button" className="button" onClick={() => setCreated(null)}>
              Done
            </button>
          </div>
        </div>
      )}
      {tokens.data && tokens.data.items.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Token</th>
                <th scope="col">Created</th>
                <th scope="col">Last used</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.data.items.map((t) => (
                <tr key={t.id}>
                  <td>
                    <strong>{t.name}</strong> <span className="mono sub">{t.prefix}…</span>
                  </td>
                  <td data-label="Created" className="mono">
                    {day(t.created_at)}
                  </td>
                  <td data-label="Last used" className="mono">
                    {day(t.last_used_at)}
                  </td>
                  <td className="actions">
                    <button type="button" className="button secondary" onClick={() => revoke(t)} disabled={act.busy}>
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="form" onSubmit={create} style={{ marginTop: 8 }}>
        <label>
          Token name
          <input value={name} maxLength={60} required placeholder="Entra ID" onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Create SCIM token
          </button>
        </div>
      </form>

      <h3 style={{ margin: "24px 0 8px" }}>Groups from your directory</h3>
      <ErrorNote error={groups.error} />
      {groups.data && groups.data.length === 0 && <p className="muted small">No groups have arrived from your directory yet.</p>}
      {groups.data && groups.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Group</th>
                <th scope="col">Team</th>
                <th scope="col">Seat</th>
                <th scope="col">Business admin</th>
              </tr>
            </thead>
            <tbody>
              {groups.data.map((g) => (
                <tr key={g.id}>
                  <td>
                    <strong>{g.display_name}</strong>
                    <span className="sub">{g.members} people</span>
                  </td>
                  <td data-label="Team">
                    <select
                      aria-label={`Team for ${g.display_name}`}
                      value={g.team_id ?? ""}
                      disabled={act.busy}
                      onChange={(e) => map(g, { team_id: e.target.value || null })}
                    >
                      <option value="">No team</option>
                      {(teams.data ?? []).map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td data-label="Seat">
                    <select
                      aria-label={`Seat for ${g.display_name}`}
                      value={g.seat}
                      disabled={act.busy}
                      onChange={(e) => map(g, { seat: e.target.value })}
                    >
                      <option value="agent">Replies to customers</option>
                      <option value="internal">Internal: notes only</option>
                    </select>
                  </td>
                  <td data-label="Business admin">
                    {g.business_admin === "none" && (
                      <button type="button" className="button secondary" onClick={() => map(g, { business_admin: true })} disabled={act.busy}>
                        Ask for admin rights
                      </button>
                    )}
                    {g.business_admin === "pending" && (
                      <div className="identity-row">
                        <span className="identity-status wait">Waiting for a second admin</span>
                        <button type="button" className="button" onClick={() => approve(g)} disabled={act.busy}>
                          Approve
                        </button>
                        <button type="button" className="button secondary" onClick={() => map(g, { business_admin: false })} disabled={act.busy}>
                          Withdraw
                        </button>
                      </div>
                    )}
                    {g.business_admin === "approved" && (
                      <div className="identity-row">
                        <span className="identity-status on">✓ Admin</span>
                        <button type="button" className="button secondary" onClick={() => map(g, { business_admin: false })} disabled={act.busy}>
                          Remove
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">
        Admin rights give the whole organisation account: Connect, Jibsy settings, sign-in settings and API keys. The
        person who asks can't approve their own request.
      </p>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function DomainApprovals() {
  const { data, error, reload } = useApi<DomainClaim[]>("/sso-domains?state=pending", 30_000);
  const act = useAction();
  const decide = (d: DomainClaim, decision: "approve" | "reject") =>
    act.run(async () => {
      await api(`/sso-domains/${d.id}/${decision}`, { method: "POST" });
      reload();
    });
  return (
    <Card title="Email domains waiting for approval">
      <p className="muted small" style={{ marginTop: 0 }}>
        For ExaCarib admins. Approve a domain only after the organisation has shown it owns it, for example with a DNS
        TXT record. An approved domain sends everyone with that email to the organisation's own sign-in.
      </p>
      <ErrorNote error={error} />
      {data && data.length === 0 && <p className="muted small">Nothing waiting.</p>}
      {data && data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Domain</th>
                <th scope="col">Organisation</th>
                <th scope="col">Asked by</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.domain}</td>
                  <td data-label="Organisation">
                    {d.customer} <span className="sub">{d.connection}</span>
                  </td>
                  <td data-label="Asked by">{d.requested_by.replace(/^user:/, "")}</td>
                  <td className="actions">
                    <div className="identity-row">
                      <button type="button" className="button" onClick={() => decide(d, "approve")} disabled={act.busy}>
                        Approve
                      </button>
                      <button type="button" className="button secondary" onClick={() => decide(d, "reject")} disabled={act.busy}>
                        Reject
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}
