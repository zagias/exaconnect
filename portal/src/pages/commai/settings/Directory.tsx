import { useEffect, useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import { Card, useAction } from "../../../ui";
import "../../identity.css";
import "./directory.css";

/* Directory templates and connectors (ADR 0036). Provider names are text labels: no third-party logos. */

interface ProviderRow {
  provider: string;
  name: string;
  summary: string;
  available: boolean;
  status: "off" | "connected";
  started: boolean;
  mode: string | null;
}

interface Check {
  check: string;
  ok: boolean;
  detail: string;
  warn?: boolean;
}

interface TestOut {
  ok: boolean;
  at: string;
  results: Check[];
  signin_test_path?: string;
}

interface Claims {
  email: string;
  name: string;
  given_name: string;
  family_name: string;
  groups: string;
  note: string;
}

interface Guide {
  provider: string;
  name: string;
  console: string;
  summary: string;
  protocols: string[];
  modes: string[];
  pull: string | null;
  protocol: string;
  mode: string;
  available: boolean;
  gateway: { configured: boolean; simulated: boolean };
  plain_ldap_allowed: boolean;
  inputs: { key: string; label: string; example: string; help: string; needed_for: string[]; value: string }[];
  values: { key: string; label: string; value: string; missing: string }[];
  provider_metadata: { kind: string; url: string; needs: string[]; upload: boolean };
  saml: { name_id_format: string; name_id_value: string; signing: string; claims: Claims } | null;
  oidc: { scopes: string; signing: string; claims: Claims } | null;
  scim: { supported: boolean; mappings: { ours: string; theirs: string }[]; quirks: string[]; note: string };
  steps: string[];
  presets: { id: string; group: string; seat: string; business_admin: boolean; note: string }[];
  setup: null | {
    status: "off" | "connected";
    protocol: string;
    mode: string;
    inputs: Record<string, string>;
    settings: Record<string, string | number | boolean>;
    has_password: boolean;
    sync_interval_min: number;
    accepted_presets: string[];
    presets_by: string | null;
    last_test: TestOut | null;
    last_sync: null | { ok: boolean; at: string; message?: string; added?: number; updated?: number; disabled?: number; people?: number; groups?: number; skipped?: string[] };
    connected_by: string | null;
    domains: { id: number; domain: string; status: string }[];
    connection: null | { id: string; status: string; last_test: { ok: boolean; message?: string } | null; has_metadata_xml: boolean; metadata_url: string; client_id: string };
    scim_token: null | { prefix: string; last_used_at: string | null };
  };
  scim_token_value?: string;
}

interface PreviewRow {
  group: string;
  in_exacarib: boolean;
  team: string;
  seat: string;
  preset: string | null;
  preset_accepted: boolean;
  business_admin: string;
}

const MODE_WORDS: Record<string, string> = {
  scim: "SCIM: your directory sends changes",
  pull: "Pull: ExaCarib reads your directory on a schedule",
  ldap: "LDAPS sync on a schedule",
  none: "No sync: accounts are made at first sign-in",
};

const PROTOCOL_WORDS: Record<string, string> = { saml: "SAML 2.0", oidc: "OpenID Connect", "": "No single sign-on" };

/** Settings > Sign-in: "Connect your directory". */
export default function DirectoryPicker({ customerId }: { customerId: string }) {
  const base = `/customers/${customerId}/directory`;
  const { data, error, reload } = useApi<ProviderRow[]>(base, 0);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => setOpen(null), [customerId]);
  return (
    <Card title="Connect your directory">
      <p className="muted small" style={{ marginTop: 0 }}>
        Pick your identity provider for a guided set-up: the exact values to paste into its admin console, the steps,
        a connection test and the group mappings. Each provider stays off for your organisation until you connect it,
        and ExaCarib switches a provider on only after testing it against a real tenant.
      </p>
      <ErrorNote error={error} />
      <div className="dir-picker" role="list">
        {data?.map((p) => (
          <button
            key={p.provider}
            type="button"
            role="listitem"
            className={`dir-tile${open === p.provider ? " active" : ""}`}
            onClick={() => setOpen(open === p.provider ? null : p.provider)}
            aria-expanded={open === p.provider}
          >
            <span className="dir-name">{p.name}</span>
            <span className="dir-summary">{p.summary}</span>
            <span className={`identity-status ${p.status === "connected" ? "on" : p.started ? "wait" : "off"}`}>
              {p.status === "connected" ? "✓ Connected" : p.started ? "Started: not connected" : "Off"}
            </span>
            {!p.available && <span className="dir-note">Not yet switched on by ExaCarib</span>}
          </button>
        ))}
      </div>
      {open && <Setup key={open} base={`${base}/${open}`} onChange={reload} />}
    </Card>
  );
}

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="button secondary dir-copy"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
    >
      {done ? "✓ Copied" : "Copy"}
    </button>
  );
}

function Setup({ base, onChange }: { base: string; onChange: () => void }) {
  const guide = useApi<Guide>(base, 0);
  const act = useAction();
  const [token, setToken] = useState<string | null>(null);
  const [test, setTest] = useState<TestOut | null>(null);
  const g = guide.data;
  useEffect(() => setTest(g?.setup?.last_test ?? null), [g?.setup?.last_test]);
  if (!g) return <ErrorNote error={guide.error} />;
  const s = g.setup;

  const post = (path: string, after?: (out: Guide) => void) =>
    act.run(async () => {
      const out = await api<Guide>(`${base}${path}`, { method: "POST" });
      if (out?.scim_token_value) setToken(out.scim_token_value);
      after?.(out);
      guide.reload();
      onChange();
    });

  const runTest = () =>
    act.run(async () => {
      setTest(await api<TestOut>(`${base}/test`, { method: "POST" }));
      guide.reload();
    });

  const testSignIn = () =>
    act.run(async () => {
      const out = await api<{ start_url: string }>(`${test?.signin_test_path ?? ""}`, {
        method: "POST",
        body: JSON.stringify({ next: window.location.pathname }),
      });
      window.location.assign(out.start_url);
    });

  const syncNow = () =>
    act.run(async () => {
      await api(`${base}/sync`, { method: "POST" });
      setTimeout(guide.reload, 3000);
    });

  return (
    <div className="dir-setup">
      <h3>{g.name}</h3>
      <p className="muted small">{g.summary}</p>
      {!g.available && (
        <p className="callout small">
          ExaCarib has not switched {g.name} on yet. You can prepare and test everything now; Connect works once it is
          on.
        </p>
      )}
      {g.gateway.simulated && g.protocols.length > 0 && (
        <p className="callout small">Sign-in connections are kept by a simulated gateway until ExaCarib connects the live one.</p>
      )}

      <Details g={g} base={base} onSaved={() => (guide.reload(), onChange())} />

      <h4>2. Paste these into the {g.console}</h4>
      {g.values.length === 0 && <p className="muted small">Nothing to paste for this choice.</p>}
      <dl className="dir-values">
        {g.values.map((v) => (
          <div key={v.key}>
            <dt>{v.label}</dt>
            <dd>
              {v.value ? (
                <>
                  <code className="wrap">{v.value}</code>
                  {v.key !== "scim_token" && <Copy text={v.value} />}
                </>
              ) : (
                <span className="muted small">{v.missing}</span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      {token && (
        <div className="secret" role="region" aria-label="New SCIM token">
          <p className="callout" style={{ margin: 0 }}>
            <strong>Copy this token into {g.name} now.</strong> It is not shown again.
          </p>
          <code>{token}</code>
          <div className="secret-actions">
            <Copy text={token} />
            <button type="button" className="button" onClick={() => setToken(null)}>
              Done
            </button>
          </div>
        </div>
      )}

      <h4>3. Steps</h4>
      <ol className="dir-steps">
        {g.steps.map((step, i) => (
          <li key={i}>{step}</li>
        ))}
      </ol>
      <ProviderFacts g={g} />

      <h4>4. Test</h4>
      <div className="identity-row">
        <button type="button" className="button secondary" onClick={runTest} disabled={act.busy || !s}>
          Run the connection test
        </button>
        {test?.signin_test_path && (
          <button type="button" className="button secondary" onClick={testSignIn} disabled={act.busy || !g.gateway.configured}>
            Test sign-in
          </button>
        )}
      </div>
      {!s && <p className="muted small">Save your details first.</p>}
      {test && (
        <ul className="dir-results" aria-label="Test results">
          {test.results.map((r, i) => (
            <li key={i} className={r.ok ? (r.warn ? "wait" : "on") : "bad"}>
              <strong>{r.ok ? (r.warn ? "! " : "✓ ") : "✕ "}{r.check}</strong> {r.detail}
            </li>
          ))}
        </ul>
      )}

      <h4>5. Group mappings</h4>
      <Presets g={g} base={base} onSaved={guide.reload} />

      <h4>6. Connect</h4>
      {s?.status === "connected" ? (
        <>
          <p className="identity-status on">✓ Connected{s.connected_by ? ` by ${s.connected_by.replace(/^user:/, "")}` : ""}</p>
          {s.last_sync && (
            <p className={`small ${s.last_sync.ok ? "" : "signin-error"}`}>
              Last sync {new Date(s.last_sync.at).toLocaleString("en-GB")}:{" "}
              {s.last_sync.ok
                ? `${s.last_sync.people ?? 0} people, ${s.last_sync.groups ?? 0} groups; ${s.last_sync.added ?? 0} added, ${s.last_sync.updated ?? 0} updated, ${s.last_sync.disabled ?? 0} switched off.`
                : s.last_sync.message}
            </p>
          )}
          <div className="identity-row">
            {(s.mode === "pull" || s.mode === "ldap") && (
              <button type="button" className="button secondary" onClick={syncNow} disabled={act.busy}>
                Sync now
              </button>
            )}
            <button
              type="button"
              className="button secondary"
              disabled={act.busy}
              onClick={() => window.confirm(`Disconnect ${g.name}? Sign-in through it stops and its SCIM token is revoked.`) && post("/disconnect")}
            >
              Disconnect
            </button>
          </div>
        </>
      ) : (
        <button type="button" className="button" onClick={() => post("/connect")} disabled={act.busy || !g.available || !s}>
          Connect {g.name}
        </button>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function Details({ g, base, onSaved }: { g: Guide; base: string; onSaved: () => void }) {
  const act = useAction();
  const s = g.setup;
  const [protocol, setProtocol] = useState(s?.protocol ?? g.protocol);
  const [mode, setMode] = useState(s?.mode ?? g.mode);
  const [inputs, setInputs] = useState<Record<string, string>>(s?.inputs ?? {});
  const [domains, setDomains] = useState((s?.domains ?? []).map((d) => d.domain).join(", "));
  const [xml, setXml] = useState("");
  const [metadataUrl, setMetadataUrl] = useState("");
  const [clientId, setClientId] = useState(s?.connection?.client_id ?? "");
  const [secret, setSecret] = useState("");
  const [prefix, setPrefix] = useState(String(s?.settings.group_prefix ?? "ExaCarib"));
  const [ldap, setLdap] = useState<Record<string, string>>({
    host: String(s?.settings.host ?? ""),
    port: String(s?.settings.port ?? "636"),
    base_dn: String(s?.settings.base_dn ?? ""),
    bind_dn: String(s?.settings.bind_dn ?? ""),
    user_filter: String(s?.settings.user_filter ?? ""),
    group_filter: String(s?.settings.group_filter ?? ""),
  });
  const [password, setPassword] = useState("");
  const [caCert, setCaCert] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = { protocol, mode, inputs, group_prefix: prefix };
    if (protocol) body.domains = domains.split(/[\s,]+/).map((d) => d.trim()).filter(Boolean);
    if (xml) body.metadata_xml = xml;
    if (metadataUrl.trim()) body.metadata_url = metadataUrl.trim();
    if (protocol === "oidc") body.client_id = clientId.trim();
    if (secret) body.client_secret = secret;
    if (mode === "ldap") {
      Object.assign(body, { ...ldap, port: Number(ldap.port) || 636 });
      if (password) body.bind_password = password;
      if (caCert.trim()) body.ca_cert_pem = caCert;
    }
    act.run(async () => {
      await api(base, { method: "PUT", body: JSON.stringify(body) });
      setSecret("");
      setPassword("");
      onSaved();
    });
  };

  const readFile = (f: File | undefined) => {
    if (!f) return;
    if (f.size > 512_000) return act.setError("The metadata file is too large.");
    f.text().then(setXml);
  };

  const shown = g.inputs.filter((i) => i.needed_for.some((n) => n === protocol || n === mode));
  return (
    <form className="form" onSubmit={submit}>
      <h4 className="wide">1. Tell us about your {g.name}</h4>
      {g.protocols.length > 0 && (
        <label>
          Sign-in
          <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
            {g.protocols.map((p) => (
              <option key={p} value={p}>
                {PROTOCOL_WORDS[p]}
              </option>
            ))}
            <option value="">{PROTOCOL_WORDS[""]}</option>
          </select>
        </label>
      )}
      <label>
        People and groups
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          {g.modes.map((m) => (
            <option key={m} value={m}>
              {MODE_WORDS[m]}
            </option>
          ))}
        </select>
      </label>
      {shown.map((i) => (
        <label key={i.key} title={i.help}>
          {i.label}
          <input
            value={inputs[i.key] ?? ""}
            placeholder={i.example}
            onChange={(e) => setInputs({ ...inputs, [i.key]: e.target.value })}
          />
        </label>
      ))}
      {protocol && (
        <label className="wide">
          Email domains, separated by commas
          <input value={domains} placeholder="examplebank.com" onChange={(e) => setDomains(e.target.value)} />
        </label>
      )}
      {protocol === "saml" && (
        <>
          <label className="wide">
            Identity provider metadata (XML file){g.provider_metadata.upload ? "" : ", or leave empty to use the address below"}
            <input type="file" accept=".xml,text/xml,application/xml" onChange={(e) => readFile(e.target.files?.[0])} />
          </label>
          {xml && <p className="muted small wide">Metadata loaded ({Math.round(xml.length / 1024)} KB).</p>}
        </>
      )}
      {protocol && (
        <label className="wide">
          {protocol === "saml" ? "Metadata URL" : "Discovery URL"} (filled in from the template)
          <input
            type="url"
            value={metadataUrl}
            placeholder={g.provider_metadata.url || "https://…"}
            onChange={(e) => setMetadataUrl(e.target.value)}
          />
        </label>
      )}
      {protocol === "oidc" && (
        <>
          <label>
            Client ID
            <input value={clientId} onChange={(e) => setClientId(e.target.value)} />
          </label>
          <label>
            Client secret
            <input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </label>
          <p className="muted small wide">The secret goes to the sign-in gateway only. ExaCarib Connect does not keep it.</p>
        </>
      )}
      {(mode === "pull" || mode === "ldap") && (
        <label>
          Group prefix
          <input value={prefix} maxLength={60} onChange={(e) => setPrefix(e.target.value)} />
        </label>
      )}
      {mode === "ldap" && (
        <>
          {(["host", "port", "base_dn", "bind_dn"] as const).map((k) => (
            <label key={k}>
              {{ host: "LDAPS host", port: "Port", base_dn: "Base DN", bind_dn: "Bind DN" }[k]}
              <input
                value={ldap[k]}
                placeholder={{ host: "dc1.example.com", port: "636", base_dn: "dc=example,dc=com", bind_dn: "cn=svc-exacarib,ou=Service,dc=example,dc=com" }[k]}
                onChange={(e) => setLdap({ ...ldap, [k]: e.target.value })}
              />
            </label>
          ))}
          <label>
            Bind password {s?.has_password && <span className="muted small">(saved; leave empty to keep)</span>}
            <input type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          <label className="wide">
            User filter (optional)
            <input value={ldap.user_filter} placeholder="(&(objectClass=user)(mail=*))" onChange={(e) => setLdap({ ...ldap, user_filter: e.target.value })} />
          </label>
          <label className="wide">
            Group filter (optional)
            <input value={ldap.group_filter} placeholder="(objectClass=group)" onChange={(e) => setLdap({ ...ldap, group_filter: e.target.value })} />
          </label>
          <label className="wide">
            Internal CA certificate (PEM, optional){s?.settings.has_ca_cert ? " — one is saved" : ""}
            <textarea rows={3} value={caCert} onChange={(e) => setCaCert(e.target.value)} />
          </label>
          <p className="muted small wide">
            LDAPS only. The password is kept in ExaCarib's encrypted store and never shown again.
          </p>
        </>
      )}
      <div className="actions wide">
        <button className="button" disabled={act.busy}>
          {act.busy ? "Saving…" : "Save"}
        </button>
      </div>
      <div className="wide">
        <ErrorNote error={act.error} />
      </div>
    </form>
  );
}

function ProviderFacts({ g }: { g: Guide }) {
  const spec = g.protocol === "saml" ? g.saml : g.protocol === "oidc" ? g.oidc : null;
  return (
    <details className="small dir-facts">
      <summary>Claims, attributes and SCIM details for {g.name}</summary>
      {spec && (
        <table className="paths dt">
          <tbody>
            {"name_id_format" in spec && (
              <tr>
                <th scope="row">NameID</th>
                <td>
                  <code className="wrap">{spec.name_id_format}</code> from {spec.name_id_value}
                </td>
              </tr>
            )}
            <tr>
              <th scope="row">Signing</th>
              <td>{spec.signing}</td>
            </tr>
            {(["email", "name", "given_name", "family_name", "groups"] as const).map((k) => (
              <tr key={k}>
                <th scope="row">{{ email: "Email", name: "Name", given_name: "Given name", family_name: "Family name", groups: "Groups" }[k]}</th>
                <td>
                  <code className="wrap">{spec.claims[k]}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {spec?.claims.note && <p className="muted">{spec.claims.note}</p>}
      {g.provider_metadata.url && (
        <p>
          {g.provider_metadata.kind === "saml" ? "Metadata URL" : "Discovery URL"}: <code className="wrap">{g.provider_metadata.url}</code>
        </p>
      )}
      {g.scim.supported ? (
        <>
          <table className="paths dt">
            <thead>
              <tr>
                <th scope="col">ExaCarib (SCIM)</th>
                <th scope="col">{g.name} sends</th>
              </tr>
            </thead>
            <tbody>
              {g.scim.mappings.map((m) => (
                <tr key={m.ours}>
                  <td className="mono">{m.ours}</td>
                  <td className="mono">{m.theirs}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>Known behaviour:</p>
          <ul>
            {g.scim.quirks.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </>
      ) : (
        <p className="muted">{g.scim.note}</p>
      )}
    </details>
  );
}

function Presets({ g, base, onSaved }: { g: Guide; base: string; onSaved: () => void }) {
  const preview = useApi<{ note: string; groups: PreviewRow[] }>(`${base}/preview`, 0);
  const act = useAction();
  const accepted = new Set(g.setup?.accepted_presets ?? []);

  const save = (ids: string[]) =>
    act.run(async () => {
      await api(`${base}/presets`, { method: "PUT", body: JSON.stringify({ accepted: ids }) });
      onSaved();
      preview.reload();
    });

  const apply = () =>
    act.run(async () => {
      await api(`${base}/presets/apply`, { method: "POST" });
      preview.reload();
    });

  return (
    <>
      <p className="muted small">
        Suggested mappings for groups with these names. Accept the ones you want. A group mapped to admin rights still
        waits for a second admin to approve it under "Groups from your directory".
      </p>
      <ul className="dir-presets">
        {g.presets.map((p) => (
          <li key={p.id}>
            <label className="check small">
              <input
                type="checkbox"
                checked={accepted.has(p.id)}
                disabled={act.busy || !g.setup}
                onChange={(e) => {
                  const next = new Set(accepted);
                  if (e.target.checked) next.add(p.id);
                  else next.delete(p.id);
                  save([...next]);
                }}
              />
              <strong>{p.group}</strong> → {p.seat === "agent" ? "replies to customers" : "internal: notes only"}
              {p.business_admin && <span className="identity-status wait"> + admin rights (needs approval)</span>}
            </label>
          </li>
        ))}
      </ul>
      {preview.data && preview.data.groups.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack" aria-label="Group mapping preview">
            <thead>
              <tr>
                <th scope="col">Group</th>
                <th scope="col">Team</th>
                <th scope="col">Seat</th>
                <th scope="col">Admin rights</th>
              </tr>
            </thead>
            <tbody>
              {preview.data.groups.map((r) => (
                <tr key={r.group}>
                  <td>
                    <strong>{r.group}</strong>
                    <span className="sub">{r.in_exacarib ? "In ExaCarib" : "Not arrived yet"}</span>
                  </td>
                  <td data-label="Team">{r.team}</td>
                  <td data-label="Seat">{r.seat === "agent" ? "Replies to customers" : "Internal"}</td>
                  <td data-label="Admin rights">
                    <span className={`identity-status ${r.business_admin === "approved" ? "on" : r.business_admin === "needs approval" ? "wait" : "off"}`}>
                      {r.business_admin === "none" ? "No" : r.business_admin}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">{preview.data?.note}</p>
      <button type="button" className="button secondary" onClick={apply} disabled={act.busy || !g.setup?.accepted_presets.length}>
        Apply accepted mappings to groups already here
      </button>
      <ErrorNote error={preview.error ?? act.error} />
    </>
  );
}
