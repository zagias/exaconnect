import { useEffect, useState, type FormEvent } from "react";
import { Link, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/api/automation.py (ADR 0020). */

interface ActionInfo {
  name: string;
  label: string;
  kind: string;
  sensitive: boolean;
  fields: { name: string; label: string; type: string; required: boolean }[];
}

interface Connection {
  app: string;
  status: "draft" | "authorised" | "testing" | "live" | "paused" | "broken";
  signed_in: boolean;
  auth_status: string;
  auth_method: string;
  allowed_actions: string[];
  mapping_open: string[];
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string;
  last_test_at: string | null;
  last_test_result: { ok?: boolean; results?: TestResult[] };
  settings: Record<string, unknown>;
  approved_by: string;
}

interface TestResult {
  action: string;
  ok: boolean;
  cause?: string;
  error?: string;
  result?: unknown;
}

interface App {
  app: string;
  label: string;
  description: string;
  auth: string;
  simulated: boolean;
  sign_in_ready: boolean;
  not_live_reason: string;
  token_entry: boolean;
  actions: ActionInfo[];
  mapping_objects: string[];
  connection: Connection | null;
}

interface Health {
  status: string;
  level: "healthy" | "attention" | "degraded" | "broken";
  signed_in: boolean;
  auth_status: string;
  last_success_at: string | null;
  last_failure_at: string | null;
  failures_7d: number;
  failures_since_success: number;
  cause: string;
  evidence: { run_id?: string; action?: string; error?: string; at?: string; check?: string; ok?: boolean; detail?: string }[];
  affected_workflows: { id: string; name: string; status: string }[];
  repair: { cause: string; label: string; explain: string; steps: { id: string; label: string }[] } | null;
}

interface MappingField {
  source: string;
  target: string;
  confidence: string;
  needs_person: boolean;
  by: string;
}

interface Mapping {
  objects: Record<string, { from: string; fields: MappingField[]; targets: string[] }>;
  open: string[];
  saved: Record<string, Record<string, string>>;
  still_open: string[];
}

const STATUS_WORD: Record<string, string> = {
  draft: "Not connected",
  authorised: "Signed in",
  testing: "Testing",
  live: "Live",
  paused: "Paused",
  broken: "Broken",
};
const STATUS_PILL: Record<string, string> = { live: "ok", testing: "warn", authorised: "warn", paused: "shadow", broken: "bad", draft: "shadow" };
const LEVEL: Record<Health["level"], [string, string]> = {
  healthy: ["ok", "Healthy"],
  attention: ["warn", "Needs attention"],
  degraded: ["warn", "Degraded"],
  broken: ["bad", "Broken"],
};

export default function Integrations() {
  return (
    <Routes>
      <Route index element={<Catalogue />} />
      <Route path=":app" element={<Setup />} />
    </Routes>
  );
}

function Catalogue() {
  const base = useCommaiBase();
  const { data, error } = useApi<App[]>(base && `${base}/integrations`, 30_000);
  return (
    <>
      <PageHead eyebrow="CommAI" title="Integrations">
        Connect the systems your team already uses. Each app lists exactly what CommAI may do in it, and nothing runs
        until you test it and switch it on.
      </PageHead>
      {!base && <p className="muted">Choose an organisation first.</p>}
      <ErrorNote error={error} />
      <div className="auto-grid">
        {(data ?? []).map((a) => (
          <section key={a.app} className="card auto-app" aria-labelledby={`app-${a.app}`}>
            <div className="card-head" style={{ marginBottom: 0 }}>
              <h3 id={`app-${a.app}`}>{a.label}</h3>
              {a.simulated && <span className="tag">Example app</span>}
            </div>
            <p className="muted small" style={{ margin: 0 }}>
              {a.description}
            </p>
            <span className={`pill ${STATUS_PILL[a.connection?.status ?? "draft"]}`}>{STATUS_WORD[a.connection?.status ?? "draft"]}</span>
            <ul className="auto-actions-list" aria-label={`${a.label} actions`}>
              {a.actions.map((x) => (
                <li key={x.name}>
                  <span className={`auto-kind ${x.kind === "read" ? "" : "write"} ${x.sensitive ? "sensitive" : ""}`}>
                    {x.label} · {x.kind}
                    {x.sensitive ? " · needs approval" : ""}
                  </span>
                </li>
              ))}
            </ul>
            {!a.simulated && !a.sign_in_ready && <p className="small muted" style={{ margin: 0 }}>{a.not_live_reason}</p>}
            <div>
              <Link className="button small" to={a.app}>
                {a.connection ? "Open" : "Set up"}
              </Link>
            </div>
          </section>
        ))}
      </div>
    </>
  );
}

function Setup() {
  const { app = "" } = useParams();
  const base = useCommaiBase();
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  const list = useApi<App[]>(base && `${base}/integrations`, 0);
  const info = list.data?.find((a) => a.app === app);
  const c = info?.connection ?? null;
  const health = useApi<Health>(base && c ? `${base}/integrations/${app}/health` : null, 30_000);
  const act = useAction();
  const reload = () => {
    list.reload();
    health.reload();
  };
  const call = (path: string, init: RequestInit = { method: "POST" }) =>
    act.run(async () => {
      await api(`${base}/integrations/${app}${path}`, init);
      reload();
    });

  if (!base) return <p className="muted">Choose an organisation first.</p>;
  if (!info) return <ErrorNote error={list.error} />;
  const signin = params.get("signin");

  return (
    <>
      <PageHead eyebrow="Integrations" title={info.label}>
        {info.description}
      </PageHead>
      <p className="small">
        <Link to="..">All integrations</Link>
      </p>
      {signin === "ok" && <div className="auto-banner ok">Signed in to {info.label}. Choose what CommAI may do next.</div>}
      {signin === "failed" && <div className="auto-banner bad">Sign-in did not finish: {params.get("reason")}</div>}
      <ErrorNote error={act.error} />

      {c && health.data && <HealthCard app={app} health={health.data} onDone={reload} base={base} />}

      <Card title="1. Connect and sign in" note={<span className={`pill ${STATUS_PILL[c?.status ?? "draft"]}`}>{STATUS_WORD[c?.status ?? "draft"]}</span>}>
        {!c && (
          <button className="button" disabled={act.busy} onClick={() => call("/connect")}>
            Connect {info.label}
          </button>
        )}
        {c && info.auth !== "none" && (
          <>
            <p className="muted small">
              {c.signed_in ? `Signed in (${c.auth_method === "token" ? "private-app token" : "OAuth sign-in"}).` : "Not signed in yet."} CommAI never
              asks for passwords or keys in chat.
            </p>
            {info.sign_in_ready ? (
              <SignIn base={base} app={app} label={info.label} />
            ) : (
              <p className="small">{info.not_live_reason}</p>
            )}
            {info.token_entry && <TokenEntry base={base} app={app} onDone={reload} />}
          </>
        )}
        {c && info.auth === "none" && <p className="muted small">This example app needs no sign-in.</p>}
      </Card>

      {c && <AllowedActions base={base} app={info} conn={c} onDone={reload} />}
      {c && info.mapping_objects.length > 0 && <FieldMapping base={base} app={app} onDone={reload} />}
      {c && <TestCard base={base} app={info} conn={c} onDone={reload} />}

      {c && (
        <Card title="5. Approve and switch on">
          <p className="muted small">
            A person switches the integration on after it is signed in, its actions are chosen, its fields are mapped and a test has passed.
          </p>
          <div className="auto-row">
            {c.status !== "live" && c.status !== "paused" && (
              <button className="button" disabled={act.busy} onClick={() => call("/approve")}>
                Approve and switch on
              </button>
            )}
            {c.status !== "paused" && c.status !== "draft" && (
              <button className="button secondary" disabled={act.busy} onClick={() => call("/pause")}>
                Pause
              </button>
            )}
            {c.status === "paused" && (
              <button className="button" disabled={act.busy} onClick={() => call("/resume")}>
                Resume
              </button>
            )}
            <button
              className="button danger-text"
              disabled={act.busy}
              onClick={() => {
                if (window.confirm(`Disconnect ${info.label}? Its sign-in is deleted and workflows that use it stop.`)) call("", { method: "DELETE" });
              }}
            >
              Disconnect
            </button>
          </div>
          {c.approved_by && <p className="small muted">Switched on by {c.approved_by.replace("user:", "")}.</p>}
        </Card>
      )}
    </>
  );
}

function SignIn({ base, app, label }: { base: string; app: string; label: string }) {
  const act = useAction();
  return (
    <div className="auto-row" style={{ marginBottom: 12 }}>
      <button
        className="button"
        disabled={act.busy}
        onClick={() =>
          act.run(async () => {
            const r = await api<{ url: string }>(`${base}/integrations/${app}/sign-in`, { method: "POST" });
            window.location.assign(r.url);
          })
        }
      >
        Sign in with {label}
      </button>
      <ErrorNote error={act.error} />
    </div>
  );
}

function TokenEntry({ base, app, onDone }: { base: string; app: string; onDone: () => void }) {
  const [token, setToken] = useState("");
  const act = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/integrations/${app}/token`, { method: "POST", body: JSON.stringify({ token }) });
      setToken("");
      onDone();
    });
  };
  return (
    <form className="form" onSubmit={submit} autoComplete="off">
      <label className="wide">
        Or enter a private-app token (secure entry)
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} />
      </label>
      <p className="auto-secret-note wide" style={{ margin: 0 }}>
        The token is checked once, stored encrypted and never shown again.
      </p>
      <div className="actions">
        <button className="button secondary" disabled={act.busy || token.length < 10}>
          Save token
        </button>
      </div>
      <ErrorNote error={act.error} />
    </form>
  );
}

function AllowedActions({ base, app, conn, onDone }: { base: string; app: App; conn: Connection; onDone: () => void }) {
  const [chosen, setChosen] = useState<string[]>(conn.allowed_actions);
  useEffect(() => setChosen(conn.allowed_actions), [conn.allowed_actions]);
  const act = useAction();
  const toggle = (n: string) => setChosen((c) => (c.includes(n) ? c.filter((x) => x !== n) : [...c, n]));
  const group = (read: boolean) => app.actions.filter((a) => (a.kind === "read") === read);
  return (
    <Card title="2. Allowed actions">
      <p className="muted small">Reading is separate from changes. Anything not ticked is refused, whoever asks.</p>
      <div className="form">
        {[true, false].map((read) => (
          <fieldset key={String(read)} className="wide">
            <legend>{read ? "Read" : "Create, update, cancel, refund or delete"}</legend>
            {group(read).map((a) => (
              <label key={a.name} className="check">
                <input type="checkbox" checked={chosen.includes(a.name)} onChange={() => toggle(a.name)} />
                {a.label}
                {a.sensitive && <span className="auto-kind sensitive">needs a person's approval each time</span>}
              </label>
            ))}
          </fieldset>
        ))}
        <div className="actions">
          <button
            className="button"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                await api(`${base}/integrations/${app.app}/actions`, { method: "PUT", body: JSON.stringify({ actions: chosen }) });
                onDone();
              })
            }
          >
            Save allowed actions
          </button>
        </div>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function FieldMapping({ base, app, onDone }: { base: string; app: string; onDone: () => void }) {
  const { data, error, reload } = useApi<Mapping>(`${base}/integrations/${app}/mapping`, 0);
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const act = useAction();
  if (!data) return <ErrorNote error={error} />;
  const value = (obj: string, f: MappingField) => {
    const src = f.source.split(".")[1];
    return edits[obj]?.[src] ?? data.saved[obj]?.[src] ?? (f.needs_person ? "" : f.target);
  };
  const save = () =>
    act.run(async () => {
      const mapping: Record<string, Record<string, string>> = {};
      for (const [obj, o] of Object.entries(data.objects)) {
        mapping[obj] = {};
        for (const f of o.fields) {
          const src = f.source.split(".")[1];
          const touched = edits[obj]?.[src] !== undefined || data.saved[obj]?.[src] !== undefined || !f.needs_person;
          if (touched) mapping[obj][src] = value(obj, f);
        }
      }
      await api(`${base}/integrations/${app}/mapping`, { method: "PUT", body: JSON.stringify({ mapping }) });
      setEdits({});
      reload();
      onDone();
    });
  return (
    <Card title="3. Field mapping" note={data.still_open.length ? <span className="pill warn">{data.still_open.length} to check</span> : <span className="pill ok">Done</span>}>
      <p className="muted small">
        Confident matches are filled in. Anything unclear needs a person: choose a field or "Don't send".
      </p>
      {Object.entries(data.objects).map(([obj, o]) => (
        <div className="table-wrap" key={obj}>
          <table className="paths dt stack">
            <caption className="sr-only">{obj} fields</caption>
            <thead>
              <tr>
                <th scope="col">CommAI field</th>
                <th scope="col">{app} field</th>
                <th scope="col">Suggestion</th>
              </tr>
            </thead>
            <tbody>
              {o.fields.map((f) => {
                const src = f.source.split(".")[1];
                return (
                  <tr key={f.source}>
                    <td className="mono">{f.source}</td>
                    <td data-label="Maps to">
                      <select
                        aria-label={`Map ${f.source}`}
                        value={value(obj, f)}
                        onChange={(e) => setEdits((x) => ({ ...x, [obj]: { ...(x[obj] ?? {}), [src]: e.target.value } }))}
                      >
                        <option value="">Don't send</option>
                        {o.targets.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td data-label="Suggestion">
                      {f.needs_person ? (
                        <span className="pill warn">{data.still_open.includes(f.source) ? "Needs a person" : "Checked"}</span>
                      ) : (
                        <span className="pill ok">Confident</span>
                      )}
                      {f.by === "model" && <span className="small muted"> AI suggests {f.target}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
      <div className="auto-row" style={{ marginTop: 12 }}>
        <button className="button" disabled={act.busy} onClick={save}>
          Save mapping
        </button>
      </div>
      <ErrorNote error={act.error} />
    </Card>
  );
}

function TestCard({ base, app, conn, onDone }: { base: string; app: App; conn: Connection; onDone: () => void }) {
  const act = useAction();
  const [action, setAction] = useState("");
  const [inputs, setInputs] = useState("{}");
  const [testRun, setTestRun] = useState<{ id: string; status: string } | null>(null);
  const results = conn.last_test_result?.results ?? [];
  const writes = app.actions.filter((a) => a.kind !== "read" && conn.allowed_actions.includes(a.name));
  return (
    <Card
      title="4. Test"
      note={conn.last_test_at ? <span className={`pill ${conn.last_test_result?.ok ? "ok" : "bad"}`}>{conn.last_test_result?.ok ? "Passed" : "Failed"}</span> : undefined}
    >
      <p className="muted small">Sample lookups run every allowed read action against {app.label}. Nothing is written.</p>
      <button
        className="button"
        disabled={act.busy}
        onClick={() =>
          act.run(async () => {
            await api(`${base}/integrations/${app.app}/test`, { method: "POST" });
            onDone();
          })
        }
      >
        Run sample lookups
      </button>
      {results.length > 0 && (
        <ul className="auto-evidence">
          {results.map((r) => (
            <li key={r.action}>
              <span className={`pill ${r.ok ? "ok" : "bad"}`}>{r.ok ? "OK" : "Failed"}</span> {r.action}
              {r.error ? `: ${r.error}` : ""} {conn.last_test_at && <span className="muted small">({when(conn.last_test_at)})</span>}
            </li>
          ))}
        </ul>
      )}
      {writes.length > 0 && (
        <form
          className="form"
          style={{ marginTop: 16 }}
          onSubmit={(e) => {
            e.preventDefault();
            act.run(async () => {
              const r = await api<{ id: string; status: string }>(`${base}/integrations/${app.app}/test-action`, {
                method: "POST",
                body: JSON.stringify({ action, inputs: JSON.parse(inputs || "{}") }),
              });
              setTestRun(r);
            });
          }}
        >
          <label>
            Controlled test action
            <select value={action} onChange={(e) => setAction(e.target.value)} required>
              <option value="">Choose…</option>
              {writes.map((w) => (
                <option key={w.name} value={w.name}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
          <label className="wide">
            Inputs (JSON)
            <textarea className="auto-text mono" value={inputs} onChange={(e) => setInputs(e.target.value)} />
          </label>
          <p className="small muted wide" style={{ margin: 0 }}>
            Marked as a test. Real apps only write in test mode to a test calendar or sandbox; otherwise the request is checked and shown.
          </p>
          <div className="actions">
            <button className="button secondary" disabled={act.busy || !action}>
              Run test action
            </button>
            {testRun && <span className="small">Test action {testRun.status.replace("_", " ")}. See Actions for the result.</span>}
          </div>
        </form>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}

function HealthCard({ app, health, base, onDone }: { app: string; health: Health; base: string; onDone: () => void }) {
  const act = useAction();
  const [note, setNote] = useState<string | null>(null);
  const [cls, word] = LEVEL[health.level];
  const step = (id: string) =>
    act.run(async () => {
      if (id === "sign_in") {
        try {
          const r = await api<{ url: string }>(`${base}/integrations/${app}/sign-in`, { method: "POST" });
          window.location.assign(r.url);
          return;
        } catch {
          /* fall through to the explanation */
        }
      }
      const r = await api<{ ok: boolean; detail: string }>(`${base}/integrations/${app}/repair`, { method: "POST", body: JSON.stringify({ step: id }) });
      setNote(r.detail);
      onDone();
    });
  return (
    <Card title="Health" note={<span className={`pill ${cls}`}>{word}</span>}>
      <dl className="auto-kv">
        <dt>Status</dt>
        <dd>{STATUS_WORD[health.status] ?? health.status}</dd>
        <dt>Sign-in</dt>
        <dd>{health.signed_in ? "Signed in" : health.auth_status === "expired" ? "Expired" : "Not signed in"}</dd>
        <dt>Last success</dt>
        <dd>{health.last_success_at ? when(health.last_success_at) : "Never"}</dd>
        <dt>Failures (7 days)</dt>
        <dd className="mono">{health.failures_7d}</dd>
        {health.affected_workflows.length > 0 && (
          <>
            <dt>Workflows affected</dt>
            <dd>{health.affected_workflows.map((w) => w.name).join(", ")}</dd>
          </>
        )}
      </dl>
      {health.repair && (
        <div style={{ marginTop: 12 }}>
          <p>
            <strong>{health.repair.label}.</strong> {health.repair.explain}
          </p>
          {health.evidence.length > 0 && (
            <>
              <h3 className="small" style={{ margin: "8px 0 0" }}>
                Evidence
              </h3>
              <ul className="auto-evidence">
                {health.evidence.map((e, i) => (
                  <li key={e.run_id ?? i}>
                    {e.check ? `Live check: ${e.detail}` : `${e.at ? new Date(e.at).toLocaleString("en-GB") : ""} ${e.action}: ${e.error}`}
                  </li>
                ))}
              </ul>
            </>
          )}
          <div className="auto-row" style={{ marginTop: 12 }}>
            {health.repair.steps.map((s) => (
              <button key={s.id} className="button small secondary" disabled={act.busy} onClick={() => step(s.id)}>
                {s.label}
              </button>
            ))}
          </div>
        </div>
      )}
      {note && <p className="small">{note}</p>}
      <ErrorNote error={act.error} />
    </Card>
  );
}
