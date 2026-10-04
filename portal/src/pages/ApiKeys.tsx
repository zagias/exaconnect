import { useRef, useState, type FormEvent } from "react";
import { apiKeysPath, createApiKey, revokeApiKey, useApi, type ApiKey, type ApiKeyCreated } from "../api";
import { ErrorNote } from "../components";
import { RowActions, useAction } from "../ui";

const EXPIRY: { label: string; days: number | null }[] = [
  { label: "Never", days: null },
  { label: "30 days", days: 30 },
  { label: "90 days", days: 90 },
  { label: "1 year", days: 365 },
];

function day(iso: string | null): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function dayTime(iso: string | null): string {
  if (!iso) return "Never";
  return `${day(iso)}, ${new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

/** API keys for your own code, the Python SDK and Terraform (ADR 0013). */
export default function ApiKeys() {
  const { data, error, reload } = useApi<ApiKey[]>(apiKeysPath, 60_000);
  const create = useAction();
  const revoke = useAction();
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [revoking, setRevoking] = useState<number | null>(null);
  // The new token lives only here, until Done or leaving the page.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    if (n.length < 1 || n.length > 60) {
      create.setError("Give the key a name of 1 to 60 characters.");
      return;
    }
    create.run(async () => {
      const body: { name: string; days?: number } = { name: n };
      if (days) body.days = Number(days);
      setCreated(await createApiKey(body));
      setName("");
      setDays("");
      reload();
    });
  };

  const onRevoke = (k: ApiKey) => {
    if (!window.confirm(`Revoke the key "${k.name}" (${k.prefix}…)? Anything using it stops working straight away.`)) return;
    setRevoking(k.id);
    revoke
      .run(async () => {
        await revokeApiKey(k.id);
        if (created?.id === k.id) setCreated(null);
        reload();
      })
      .finally(() => setRevoking(null));
  };

  const keys = data ?? [];

  return (
    <section className="card" style={{ maxWidth: 880, marginTop: 24 }} aria-labelledby="api-keys-title">
      <div className="card-head">
        <h2 id="api-keys-title">API keys</h2>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        A key lets your own code, the ExaConnect Python SDK or Terraform act as you, with your access. It is shown once, when
        you create it.
      </p>

      <ErrorNote error={error} />
      {data && keys.length === 0 && (
        <div className="empty">
          <p>You have no API keys yet. Create one below when your code or Terraform needs access.</p>
        </div>
      )}
      {keys.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Key</th>
                <th scope="col">Created</th>
                <th scope="col">Last used</th>
                <th scope="col">Expires</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td className="cell-wrap">
                    <strong>{k.name}</strong>
                    {revoking === k.id && <span className="sub">Revoking…</span>}
                  </td>
                  <td data-label="Key" className="mono">
                    {k.prefix}…
                  </td>
                  <td data-label="Created" className="mono">
                    {day(k.created_at)}
                  </td>
                  <td data-label="Last used" className="mono">
                    {dayTime(k.last_used_at)}
                  </td>
                  <td data-label="Expires" className="mono">
                    {day(k.expires_at)}
                  </td>
                  <td className="actions">
                    <RowActions
                      label={`key ${k.name}`}
                      disabled={revoke.busy}
                      items={[{ label: "Revoke key", danger: true, onSelect: () => onRevoke(k) }]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorNote error={revoke.error} />

      <h3 style={{ margin: "24px 0 8px" }}>Create a key</h3>
      <form className="form" onSubmit={submit}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} required placeholder="Terraform, prod" />
        </label>
        <label>
          Expires
          <select value={days} onChange={(e) => setDays(e.target.value)}>
            {EXPIRY.map((x) => (
              <option key={x.label} value={x.days ?? ""}>
                {x.label}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button className="button" disabled={create.busy}>
            {create.busy ? "Creating…" : "Create key"}
          </button>
        </div>
      </form>
      <ErrorNote error={create.error} />
      {created && <NewKey created={created} onDone={() => setCreated(null)} />}

      <h3 style={{ margin: "24px 0 8px" }}>Use it</h3>
      <div className="snippets">
        <div>
          <h3>Python SDK</h3>
          <pre className="code">
            {`export EXACONNECT_API_KEY=exa_...

from exaconnect import ExaConnect
exa = ExaConnect("https://connect.exacarib.com")`}
          </pre>
        </div>
        <div>
          <h3>Terraform</h3>
          <pre className="code">
            {`provider "exaconnect" {
  url     = "https://connect.exacarib.com"   # or EXACONNECT_URL
  api_key = var.exaconnect_api_key           # or EXACONNECT_API_KEY; sensitive
}`}
          </pre>
        </div>
      </div>
    </section>
  );
}

/** The new token, shown once with Copy and Done. */
function NewKey({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const codeRef = useRef<HTMLElement>(null);
  const [copied, setCopied] = useState<"copied" | "selected" | null>(null);

  const select = () => {
    const el = codeRef.current;
    const sel = window.getSelection();
    if (!el || !sel) return;
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    setCopied("selected");
  };

  const copy = () => {
    if (!navigator.clipboard?.writeText) {
      select();
      return;
    }
    navigator.clipboard.writeText(created.token).then(() => setCopied("copied"), select);
  };

  return (
    <div className="secret" role="status">
      <p className="callout warn small">
        <strong>⚠ Copy it now.</strong> This key won't be shown again. If you lose it, revoke it and create another.
      </p>
      <div className="small muted">
        New key "{created.name}", {created.expires_at ? `expires ${day(created.expires_at)}` : "no expiry"}
      </div>
      <code ref={codeRef}>{created.token}</code>
      <div className="secret-actions">
        <button type="button" className="button secondary small" onClick={copy}>
          {copied === "copied" ? "Copied" : "Copy"}
        </button>
        <button type="button" className="button small" onClick={onDone}>
          Done
        </button>
        {copied === "selected" && <span className="small muted">Selected. Press Ctrl+C or ⌘C to copy.</span>}
      </div>
    </div>
  );
}
