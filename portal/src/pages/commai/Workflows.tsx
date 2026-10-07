import { useEffect, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, EmptyState, PageHead, useAction } from "../../ui";
import { useCommaiBase, when } from "./lib";
import "./automation.css";

/* Shapes from controller/exaconnect_controller/commai/api/automation.py (ADR 0020). */

type Cond = { field: string; op: string; value: unknown };
type Step = { id?: string; type: string; [k: string]: unknown };
type Schedule = { every_minutes?: number; at?: string; days?: string[] };
interface Definition {
  name: string;
  description?: string;
  /** No type: an event starts it. "schedule": a time. "manual": a person or the API (ADR 0033). */
  trigger: { type?: "schedule" | "manual"; event: string; conditions: Cond[]; schedule?: Schedule };
  steps: Step[];
  notify_on_failure?: string[];
}
interface Draft {
  definition: Definition;
  notes: string[];
  unrecognised: string[];
  source: "rules" | "model";
  problems: string[];
  warnings: string[];
  preview: string[];
}
interface Summary {
  id: string;
  name: string;
  description: string;
  status: "draft" | "live" | "paused";
  live_version: number | null;
  latest_version: number;
  draft_pending: boolean;
  runs: number;
  failed: number;
  active: number;
  last_run_at: string | null;
  pack: string;
}
interface Detail extends Summary {
  definition: Definition;
  versions: { version: number; source: string; created_by: string; created_at: string }[];
  preview: string[];
  problems: string[];
  warnings: string[];
  tools_needed: string[];
  schedule?: { next_at: string; last_at: string | null } | null;
}
interface Pack {
  id: string;
  label: string;
  description: string;
  workflows: { name: string; description: string }[];
}
interface Run {
  id: string;
  version: number;
  test: boolean;
  status: string;
  error: string;
  started_at: string;
  approval_prompt: string | null;
}
interface RunDetail {
  steps: { step_index: number; step_id: string; type: string; status: string; summary: string; at: string }[];
}
interface DryRun {
  trigger_matches: boolean;
  event: { id: string; type: string };
  steps: { step: string; type?: string; status: string; summary: string; issues?: string[] }[];
}
interface AppInfo {
  app: string;
  label: string;
  actions: { name: string; label: string; fields: { name: string; label: string; required: boolean }[] }[];
}

const STATUS: Record<string, [string, string]> = { live: ["ok", "Live"], paused: ["shadow", "Paused"], draft: ["warn", "Draft"] };
const RUN_STATUS: Record<string, string> = {
  done: "ok",
  skipped: "shadow",
  waiting: "warn",
  awaiting_approval: "warn",
  running: "warn",
  held: "shadow",
  failed: "bad",
  cancelled: "shadow",
};
const STEP_TYPES: [string, string][] = [
  ["collect", "Ask the customer"],
  ["action", "Action in an app"],
  ["send_message", "Send a message"],
  ["assign", "Assign"],
  ["add_note", "Add a private note"],
  ["condition", "Condition"],
  ["wait", "Wait"],
  ["approval", "Ask a person to approve"],
  ["remind", "Remind if nobody replies"],
  ["escalate", "Escalate if nobody replies"],
];
const EVENTS = [
  "message.received",
  "conversation.created",
  "conversation.state_changed",
  "conversation.handed_over",
  "booking.confirmed",
  "action.failed",
  "action.succeeded",
  "contact.created",
];
const OPS = ["contains", "eq", "ne", "not_contains", "in", "exists", "gt", "lt"];
const DAYS: [string, string][] = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];
const EXAMPLE =
  "When a new customer asks for a quote, collect their requirements, create a lead, assign it to Sales and remind the owner if nobody responds";

export default function Workflows() {
  return (
    <Routes>
      <Route index element={<List />} />
      <Route path=":id" element={<Editor />} />
    </Routes>
  );
}

function List() {
  const base = useCommaiBase();
  const list = useApi<Summary[]>(base && `${base}/workflows`, 20_000);
  const packs = useApi<Pack[]>(base && `${base}/workflows/packs`, 0);
  const [text, setText] = useState(EXAMPLE);
  const [draft, setDraft] = useState<Draft | null>(null);
  const act = useAction();
  const nav = useNavigate();
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  return (
    <>
      <PageHead title="Workflows">
        Describe what should happen in plain English. CommAI turns it into steps you can edit, test and switch on.
      </PageHead>
      <Card title="Describe a workflow">
        <label className="sr-only" htmlFor="wf-text">
          What should happen
        </label>
        <textarea id="wf-text" className="auto-text" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} />
        <div className="auto-row" style={{ marginTop: 8 }}>
          <button
            className="button"
            disabled={act.busy || text.trim().length < 3}
            onClick={() =>
              act.run(async () => {
                setDraft(await api<Draft>(`${base}/workflows/draft`, { method: "POST", body: JSON.stringify({ text }) }));
              })
            }
          >
            Draft the steps
          </button>
        </div>
        <ErrorNote error={act.error} />
        {draft && (
          <div style={{ marginTop: 16 }}>
            <p className="small muted">
              {draft.source === "model" ? "Drafted by the AI." : "Drafted by CommAI's rules."} Nothing runs until you publish it.
            </p>
            <ul className="auto-preview">
              {draft.preview.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
            {draft.unrecognised.length > 0 && (
              <p className="small">
                <strong>Not understood:</strong> {draft.unrecognised.join("; ")}. Add these steps by hand.
              </p>
            )}
            <Notes items={draft.notes} cls="" />
            <Notes items={draft.problems} cls="auto-problems" />
            <Notes items={draft.warnings} cls="auto-warnings" />
            <button
              className="button"
              disabled={act.busy}
              onClick={() =>
                act.run(async () => {
                  const r = await api<{ workflow: { id: string } }>(`${base}/workflows`, {
                    method: "POST",
                    body: JSON.stringify({ definition: draft.definition }),
                  });
                  nav(r.workflow.id);
                })
              }
            >
              Save as a draft and edit
            </button>
          </div>
        )}
      </Card>

      <Card title="Your workflows">
        <ErrorNote error={list.error} />
        {list.data && list.data.length === 0 && <EmptyState title="No workflows yet">Describe one above in plain English, or start from a starter pack below.</EmptyState>}
        {list.data && list.data.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Status</th>
                  <th scope="col">Version</th>
                  <th scope="col">Runs</th>
                  <th scope="col">Failed</th>
                  <th scope="col">Last run</th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((w) => (
                  <tr key={w.id}>
                    <td className="cell-wrap">
                      <Link to={w.id}>
                        <strong>{w.name}</strong>
                      </Link>
                      {w.draft_pending && <span className="sub">Unpublished changes</span>}
                    </td>
                    <td data-label="Status">
                      <span className={`pill ${STATUS[w.status][0]}`}>{STATUS[w.status][1]}</span>
                    </td>
                    <td data-label="Version" className="mono">
                      {w.live_version ?? "–"} / {w.latest_version}
                    </td>
                    <td data-label="Runs" className="mono">
                      {w.runs}
                    </td>
                    <td data-label="Failed" className="mono">
                      {w.failed}
                    </td>
                    <td data-label="Last run">{w.last_run_at ? when(w.last_run_at) : "Never"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Starter packs">
        <p className="muted small">Templates for common industries. They are added as drafts for you to check and publish.</p>
        <div className="auto-grid">
          {(packs.data ?? []).map((p) => (
            <section key={p.id} className="auto-draft" aria-label={p.label}>
              <strong>{p.label}</strong>
              <span className="small muted">{p.description}</span>
              <ul className="auto-evidence">
                {p.workflows.map((w) => (
                  <li key={w.name}>
                    {w.name}: {w.description}
                  </li>
                ))}
              </ul>
              <div>
                <button
                  className="button small secondary"
                  disabled={act.busy}
                  onClick={() =>
                    act.run(async () => {
                      await api(`${base}/workflows/packs/${p.id}`, { method: "POST", body: "{}" });
                      list.reload();
                    })
                  }
                >
                  Add as drafts
                </button>
              </div>
            </section>
          ))}
        </div>
      </Card>
    </>
  );
}

function Notes({ items, cls }: { items: string[]; cls: string }) {
  if (!items.length) return null;
  return (
    <ul className={cls || "auto-evidence"}>
      {items.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

function Editor() {
  const { id = "" } = useParams();
  const base = useCommaiBase();
  const wf = useApi<Detail>(base && `${base}/workflows/${id}`, 0);
  const apps = useApi<AppInfo[]>(base && `${base}/integrations`, 0);
  const [def, setDef] = useState<Definition | null>(null);
  const [check, setCheck] = useState<{ problems: string[]; warnings: string[]; preview: string[] } | null>(null);
  const [grant, setGrant] = useState(false);
  const act = useAction();
  useEffect(() => {
    if (wf.data) setDef(wf.data.definition);
  }, [wf.data]);
  if (!base) return <p className="muted">Choose an organisation first.</p>;
  if (!wf.data || !def) return <ErrorNote error={wf.error} />;
  const w = wf.data;
  const preview = check?.preview ?? w.preview;
  const problems = check?.problems ?? w.problems;
  const warnings = check?.warnings ?? w.warnings;
  const setStep = (i: number, s: Step) => setDef({ ...def, steps: def.steps.map((x, k) => (k === i ? s : x)) });
  const move = (i: number, d: number) => {
    const steps = [...def.steps];
    const [s] = steps.splice(i, 1);
    steps.splice(i + d, 0, s);
    setDef({ ...def, steps });
  };
  const post = (path: string, body: unknown = {}) =>
    act.run(async () => {
      await api(`${base}/workflows/${id}${path}`, { method: "POST", body: JSON.stringify(body) });
      wf.reload();
    });

  return (
    <>
      <PageHead eyebrow="Workflows" title={w.name}>
        {w.description}
      </PageHead>
      <p className="small">
        <Link to="..">All workflows</Link>
      </p>
      <div className="auto-row" style={{ marginBottom: 16 }}>
        <span className={`pill ${STATUS[w.status][0]}`}>{STATUS[w.status][1]}</span>
        <span className="small muted">
          Live version {w.live_version ?? "none"} · latest {w.latest_version}
        </span>
        {w.status === "live" && (
          <button className="button small secondary" disabled={act.busy} onClick={() => post("/pause")}>
            Pause
          </button>
        )}
        {w.status === "paused" && (
          <button className="button small" disabled={act.busy} onClick={() => post("/resume")}>
            Resume
          </button>
        )}
      </div>
      <ErrorNote error={act.error} />

      <Card title="Trigger">
        <div className="auto-fields">
          <label>
            Name
            <input value={def.name} onChange={(e) => setDef({ ...def, name: e.target.value })} maxLength={120} />
          </label>
          <label>
            Starts
            <select
              value={def.trigger.type ?? "event"}
              onChange={(e) => {
                const t = e.target.value;
                if (t === "event") setDef({ ...def, trigger: { event: EVENTS[0], conditions: def.trigger.conditions } });
                else
                  setDef({
                    ...def,
                    trigger: { type: t as "schedule" | "manual", event: "", conditions: [], schedule: t === "schedule" ? { every_minutes: 60 } : undefined },
                  });
              }}
            >
              <option value="event">When something happens</option>
              <option value="schedule">On a schedule</option>
              <option value="manual">When someone runs it</option>
            </select>
          </label>
          {!def.trigger.type && (
            <>
              <label>
                What happens
                <select value={def.trigger.event} onChange={(e) => setDef({ ...def, trigger: { ...def.trigger, event: e.target.value } })}>
                  {[...new Set([def.trigger.event, ...EVENTS])].map((ev) => (
                    <option key={ev}>{ev}</option>
                  ))}
                </select>
              </label>
              <div className="wide">
                <Conditions conds={def.trigger.conditions} onChange={(c) => setDef({ ...def, trigger: { ...def.trigger, conditions: c } })} />
              </div>
            </>
          )}
          {def.trigger.type === "schedule" && (
            <ScheduleFields value={def.trigger.schedule ?? {}} onChange={(sc) => setDef({ ...def, trigger: { ...def.trigger, schedule: sc } })} />
          )}
          {def.trigger.type === "manual" && <p className="small muted wide">People start it with Run now below, or a system starts it through the API.</p>}
        </div>
        {w.schedule?.next_at && (
          <p className="small" role="status">
            Next run {new Date(w.schedule.next_at).toLocaleString("en-GB")}
            {w.schedule.last_at ? ` · last ran ${when(w.schedule.last_at)}` : ""} (business time zone)
          </p>
        )}
      </Card>
      {w.status === "live" && def.trigger.type === "manual" && <RunNow base={base} id={id} done={wf.reload} />}

      <Card title="Steps">
        <ol className="auto-steps" aria-label="Steps">
          {def.steps.map((s, i) => (
            <li key={i} className="auto-step">
              <div className="auto-step-head">
                <h3 className="small" style={{ margin: 0 }}>
                  {i + 1}. {STEP_TYPES.find((t) => t[0] === s.type)?.[1] ?? s.type}
                  {s.id ? <span className="muted"> ({String(s.id)})</span> : null}
                </h3>
                <div className="auto-row">
                  <button className="button small secondary" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move step ${i + 1} up`}>
                    Up
                  </button>
                  <button
                    className="button small secondary"
                    disabled={i === def.steps.length - 1}
                    onClick={() => move(i, 1)}
                    aria-label={`Move step ${i + 1} down`}
                  >
                    Down
                  </button>
                  <button
                    className="button small danger-text"
                    onClick={() => setDef({ ...def, steps: def.steps.filter((_, k) => k !== i) })}
                    aria-label={`Remove step ${i + 1}`}
                  >
                    Remove
                  </button>
                </div>
              </div>
              <StepFields step={s} apps={apps.data ?? []} onChange={(x) => setStep(i, x)} />
              <FailureFields index={i} step={s} later={def.steps.slice(i + 1)} onChange={(x) => setStep(i, x)} />
            </li>
          ))}
        </ol>
        <div className="auto-row" style={{ marginTop: 12 }}>
          <label className="sr-only" htmlFor="add-step">
            Add a step
          </label>
          <select id="add-step" value="" onChange={(e) => e.target.value && setDef({ ...def, steps: [...def.steps, { type: e.target.value }] })}>
            <option value="">Add a step…</option>
            {STEP_TYPES.map(([t, l]) => (
              <option key={t} value={t}>
                {l}
              </option>
            ))}
          </select>
          <button
            className="button secondary"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                setCheck(await api(`${base}/workflows/validate`, { method: "POST", body: JSON.stringify({ definition: def }) }));
              })
            }
          >
            Check and preview
          </button>
          <button
            className="button"
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                await api(`${base}/workflows/${id}`, { method: "PUT", body: JSON.stringify({ definition: def }) });
                setCheck(null);
                wf.reload();
              })
            }
          >
            Save as version {w.latest_version + 1}
          </button>
        </div>
      </Card>

      <Card title="If a run fails">
        <div className="auto-fields">
          <label className="wide">
            Tell these people (emails of people in this business, separated by commas)
            <input
              value={(def.notify_on_failure ?? []).join(", ")}
              onChange={(e) =>
                setDef({
                  ...def,
                  notify_on_failure: e.target.value
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
                })
              }
              placeholder="manager@example.com"
              type="text"
              inputMode="email"
            />
          </label>
        </div>
        <p className="small muted">They get a mention on the conversation and the failure event goes to your webhooks. To handle a failure, give a step an id and choose it under “If it fails” on an earlier step.</p>
      </Card>

      <Card title="Preview">
        <ul className="auto-preview" aria-live="polite">
          {preview.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
        <Notes items={problems} cls="auto-problems" />
        <Notes items={warnings} cls="auto-warnings" />
      </Card>

      <Card title="Publish">
        <p className="muted small">Publishing makes the latest saved version live. The live version keeps running until then.</p>
        {w.tools_needed.length > 0 && (
          <label className="check" style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <input type="checkbox" checked={grant} onChange={(e) => setGrant(e.target.checked)} />
            Allow this workflow to use {w.tools_needed.join(", ")}
          </label>
        )}
        <button className="button" disabled={act.busy} onClick={() => post("/publish", { grant_tools: grant })}>
          Publish version {w.latest_version}
        </button>
      </Card>

      <TestRun base={base} id={id} />
      <Runs base={base} id={id} />

      <Card title="Versions">
        <ul className="auto-evidence">
          {w.versions.map((v) => (
            <li key={v.version}>
              Version {v.version} · {v.source} · {v.created_by.replace("user:", "")} · {when(v.created_at)}
              {v.version === w.live_version && <strong> (live)</strong>}
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function ScheduleFields({ value, onChange }: { value: Schedule; onChange: (s: Schedule) => void }) {
  const kind = value.at ? "at" : "every";
  return (
    <>
      <label>
        How often
        <select value={kind} onChange={(e) => onChange(e.target.value === "at" ? { at: "09:00", days: ["mon", "tue", "wed", "thu", "fri"] } : { every_minutes: 60 })}>
          <option value="every">Every few minutes or hours</option>
          <option value="at">At a time on chosen days</option>
        </select>
      </label>
      {kind === "every" ? (
        <label>
          Every (minutes, at least 5)
          <input type="number" min={5} step={5} value={value.every_minutes ?? 60} onChange={(e) => onChange({ every_minutes: Number(e.target.value) })} />
        </label>
      ) : (
        <>
          <label>
            At
            <input type="time" value={value.at ?? "09:00"} onChange={(e) => onChange({ ...value, at: e.target.value })} />
          </label>
          <fieldset className="wide">
            <legend>On</legend>
            <div className="auto-row">
              {DAYS.map(([d, l]) => (
                <label key={d} className="check">
                  <input
                    type="checkbox"
                    checked={(value.days ?? []).includes(d)}
                    onChange={(e) =>
                      onChange({ ...value, days: e.target.checked ? [...(value.days ?? []), d] : (value.days ?? []).filter((x) => x !== d) })
                    }
                  />{" "}
                  {l}
                </label>
              ))}
            </div>
          </fieldset>
        </>
      )}
    </>
  );
}

function FailureFields({ index, step, later, onChange }: { index: number; step: Step; later: Step[]; onChange: (s: Step) => void }) {
  const targets = later.filter((s) => s.id);
  const dflt = step.type === "action" ? "stop" : "continue";
  return (
    <details className="small" style={{ marginTop: 8 }}>
      <summary>If this step fails{step.only_on_failure ? " (runs only after a failure)" : ""}</summary>
      <div className="auto-fields" style={{ marginTop: 8 }}>
        <label>
          Step id (for jumps)
          <input value={String(step.id ?? "")} onChange={(e) => onChange({ ...step, id: e.target.value.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) || undefined })} placeholder={`step${index + 1}`} />
        </label>
        <label>
          If it fails
          <select value={String(step.on_failure ?? dflt)} onChange={(e) => onChange({ ...step, on_failure: e.target.value })}>
            <option value="stop">Stop the workflow</option>
            <option value="continue">Carry on</option>
            {targets.map((t) => (
              <option key={String(t.id)} value={String(t.id)}>
                Go to {String(t.id)}
              </option>
            ))}
          </select>
        </label>
        {index > 0 && (
          <label className="check">
            <span>
              <input type="checkbox" checked={!!step.only_on_failure} onChange={(e) => onChange({ ...step, only_on_failure: e.target.checked || undefined })} /> Only run this step after a failure
            </span>
          </label>
        )}
      </div>
    </details>
  );
}

function RunNow({ base, id, done }: { base: string; id: string; done: () => void }) {
  const [conv, setConv] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const run = () =>
    act.run(async () => {
      await api(`${base}/workflows/${id}/trigger`, {
        method: "POST",
        body: JSON.stringify(conv.trim() ? { conversation_id: conv.trim() } : {}),
      });
      setMsg("Started. It shows in the run log below.");
      done();
    });
  return (
    <Card title="Run now">
      <div className="auto-row">
        <label className="small">
          Conversation id (optional){" "}
          <input value={conv} onChange={(e) => setConv(e.target.value)} maxLength={40} />
        </label>
        <button className="button" disabled={act.busy} onClick={run}>
          Run now
        </button>
      </div>
      {msg && (
        <p className="small" role="status">
          {msg}
        </p>
      )}
      <ErrorNote error={act.error} />
    </Card>
  );
}

function Conditions({ conds, onChange }: { conds: Cond[]; onChange: (c: Cond[]) => void }) {
  const set = (i: number, c: Cond) => onChange(conds.map((x, k) => (k === i ? c : x)));
  return (
    <fieldset style={{ border: "1px solid var(--line)", borderRadius: 8, padding: 8 }}>
      <legend className="small">Conditions (all must hold)</legend>
      {conds.map((c, i) => (
        <div className="auto-row" key={i} style={{ marginBottom: 6 }}>
          <input aria-label="Field" value={c.field} onChange={(e) => set(i, { ...c, field: e.target.value })} placeholder="text" />
          <select aria-label="Comparison" value={c.op} onChange={(e) => set(i, { ...c, op: e.target.value })}>
            {OPS.map((o) => (
              <option key={o}>{o}</option>
            ))}
          </select>
          <input
            aria-label="Value"
            value={Array.isArray(c.value) ? c.value.join(", ") : String(c.value ?? "")}
            onChange={(e) => {
              const v = e.target.value;
              set(i, { ...c, value: v === "true" ? true : v === "false" ? false : v.includes(",") ? v.split(",").map((x) => x.trim()) : v });
            }}
          />
          <button className="button small danger-text" onClick={() => onChange(conds.filter((_, k) => k !== i))}>
            Remove
          </button>
        </div>
      ))}
      <button className="button small secondary" onClick={() => onChange([...conds, { field: "text", op: "contains", value: "" }])}>
        Add condition
      </button>
    </fieldset>
  );
}

function Duration({ label, value, onChange }: { label: string; value: unknown; onChange: (n: number) => void }) {
  const n = Number(value ?? 0);
  const unit = n && n % 86400 === 0 ? 86400 : n && n % 3600 === 0 ? 3600 : 60;
  return (
    <label>
      {label}
      <span className="auto-row">
        <input type="number" min={1} style={{ width: 90 }} value={n ? n / unit : ""} onChange={(e) => onChange(Number(e.target.value) * unit)} />
        <select aria-label={`${label} unit`} value={unit} onChange={(e) => onChange((n ? n / unit : 1) * Number(e.target.value))}>
          <option value={60}>minutes</option>
          <option value={3600}>hours</option>
          <option value={86400}>days</option>
        </select>
      </span>
    </label>
  );
}

function StepFields({ step, apps, onChange }: { step: Step; apps: AppInfo[]; onChange: (s: Step) => void }) {
  const set = (k: string, v: unknown) => onChange({ ...step, [k]: v });
  const text = (k: string, label: string, wide = false) => (
    <label className={wide ? "wide" : ""}>
      {label}
      {wide ? (
        <textarea value={String(step[k] ?? "")} onChange={(e) => set(k, e.target.value)} rows={2} />
      ) : (
        <input value={String(step[k] ?? "")} onChange={(e) => set(k, e.target.value)} />
      )}
    </label>
  );
  const t = step.type;
  if (t === "action") {
    const app = apps.find((a) => a.app === step.app);
    const spec = app?.actions.find((a) => a.name === step.action);
    const inputs = (step.inputs as Record<string, string>) ?? {};
    return (
      <div className="auto-fields">
        <label>
          App
          <select value={String(step.app ?? "")} onChange={(e) => onChange({ ...step, app: e.target.value, action: "" })}>
            <option value="">Choose…</option>
            {apps.map((a) => (
              <option key={a.app} value={a.app}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Action
          <select value={String(step.action ?? "")} onChange={(e) => set("action", e.target.value)}>
            <option value="">Choose…</option>
            {(app?.actions ?? []).map((a) => (
              <option key={a.name} value={a.name}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        {(spec?.fields ?? []).map((f) => (
          <label key={f.name}>
            {f.label}
            {f.required ? "" : " (optional)"}
            <input value={inputs[f.name] ?? ""} onChange={(e) => set("inputs", { ...inputs, [f.name]: e.target.value })} placeholder="{{contact.name}}" />
          </label>
        ))}
      </div>
    );
  }
  if (t === "condition") {
    return (
      <div className="auto-fields">
        <div className="wide">
          <Conditions conds={(step.if as Cond[]) ?? []} onChange={(c) => set("if", c)} />
        </div>
        {text("else", "Otherwise (end, continue or a later step id)")}
      </div>
    );
  }
  if (t === "send_message") {
    return (
      <div className="auto-fields">
        {text("body", "Message", true)}
        <label>
          Send to
          <select value={String(step.audience ?? "conversation")} onChange={(e) => set("audience", e.target.value)}>
            <option value="conversation">This conversation</option>
            <option value="matching">Every matching conversation (needs an approval step first)</option>
          </select>
        </label>
      </div>
    );
  }
  if (t === "assign") return <div className="auto-fields">{text("team", "Team")}{text("user", "Or a person (email)")}</div>;
  if (t === "add_note") return <div className="auto-fields">{text("body", "Note", true)}</div>;
  if (t === "wait")
    return (
      <div className="auto-fields">
        <Duration label="Wait for" value={step.duration_s} onChange={(n) => set("duration_s", n)} />
        {text("until_event", "Or until this event")}
        <Duration label="Give up after" value={step.timeout_s} onChange={(n) => set("timeout_s", n)} />
      </div>
    );
  if (t === "approval")
    return (
      <div className="auto-fields">
        {text("prompt", "What the person approves", true)}
        <Duration label="Give up after" value={step.timeout_s} onChange={(n) => set("timeout_s", n)} />
      </div>
    );
  if (t === "remind" || t === "escalate")
    return (
      <div className="auto-fields">
        <Duration label="If nobody replies within" value={step.after_s} onChange={(n) => set("after_s", n)} />
        {t === "remind" ? text("to", "Remind (assignee or an email)") : text("team", "Move to team")}
        {t === "escalate" && (
          <label>
            Priority
            <select value={String(step.priority ?? "high")} onChange={(e) => set("priority", e.target.value)}>
              {["normal", "high", "urgent"].map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
        )}
        {text("body", "Note (optional)", true)}
      </div>
    );
  if (t === "collect")
    return (
      <div className="auto-fields">
        {text("question", "Question to ask", true)}
        {text("save_as", "Save the answer as")}
        <Duration label="Wait for the answer up to" value={step.timeout_s} onChange={(n) => set("timeout_s", n)} />
      </div>
    );
  return null;
}

function TestRun({ base, id }: { base: string; id: string }) {
  const events = useApi<{ id: string; type: string; at: string }[]>(`${base}/workflows/${id}/sample-events`, 0);
  const [eventId, setEventId] = useState("");
  const [res, setRes] = useState<DryRun | null>(null);
  const act = useAction();
  return (
    <Card title="Test">
      <p className="muted small">A dry run: it shows what each step would do. Nothing is sent, noted or changed.</p>
      <div className="auto-row">
        <label>
          <span className="sr-only">Event to test against</span>
          <select value={eventId} onChange={(e) => setEventId(e.target.value)}>
            <option value="">The most recent matching event</option>
            {(events.data ?? []).map((e) => (
              <option key={e.id} value={e.id}>
                {e.type} · {when(e.at)}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button secondary"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              setRes(await api<DryRun>(`${base}/workflows/${id}/test`, { method: "POST", body: JSON.stringify(eventId ? { event_id: eventId } : {}) }));
            })
          }
        >
          Run a test
        </button>
      </div>
      <ErrorNote error={act.error} />
      {res && (
        <ul className="auto-log" style={{ marginTop: 12 }}>
          {res.steps.map((s, i) => (
            <li key={i}>
              <span className="auto-status">{s.status.replace("_", " ")}</span>
              <span>
                {s.summary}
                {s.issues && s.issues.length > 0 && <span className="auto-problems"> {s.issues.join(" ")}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Runs({ base, id }: { base: string; id: string }) {
  const runs = useApi<Run[]>(`${base}/workflows/${id}/runs`, 15_000);
  const [open, setOpen] = useState<string | null>(null);
  const detail = useApi<RunDetail>(open ? `${base}/workflow-runs/${open}` : null, 0);
  const act = useAction();
  const decide = (runId: string, verb: "approve" | "reject") =>
    act.run(async () => {
      await api(`${base}/workflow-runs/${runId}/${verb}`, { method: "POST", body: "{}" });
      runs.reload();
    });
  return (
    <Card title="Run log">
      <ErrorNote error={runs.error || act.error} />
      {runs.data && runs.data.length === 0 && <EmptyState title="No runs yet">Each time this workflow runs, its steps and results appear here.</EmptyState>}
      {runs.data && runs.data.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <caption className="sr-only">Runs of this workflow, newest first</caption>
            <thead>
              <tr>
                <th scope="col">Started</th>
                <th scope="col">Status</th>
                <th scope="col">Version</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <button
                      className="button small secondary"
                      onClick={() => setOpen(open === r.id ? null : r.id)}
                      aria-expanded={open === r.id}
                      aria-label={`Steps of the run started ${when(r.started_at)}`}
                    >
                      {when(r.started_at)}
                    </button>
                    {r.test && <span className="tag">Test</span>}
                  </td>
                  <td data-label="Status">
                    <span className={`pill ${RUN_STATUS[r.status] ?? "shadow"}`}>{r.status.replace("_", " ")}</span>
                  </td>
                  <td data-label="Version" className="mono">
                    {r.version}
                  </td>
                  <td data-label="Detail" className="cell-wrap">
                    {r.error}
                    {r.status === "awaiting_approval" && (
                      <span className="auto-row">
                        {r.approval_prompt}
                        <button className="button small" disabled={act.busy} onClick={() => decide(r.id, "approve")}>
                          Approve
                        </button>
                        <button className="button small danger-text" disabled={act.busy} onClick={() => decide(r.id, "reject")}>
                          Reject
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && detail.data && (
        <ul className="auto-log" style={{ marginTop: 12 }} aria-label="Steps of this run">
          {detail.data.steps.map((s, i) => (
            <li key={i}>
              <span className="auto-status">
                {s.type} · {s.status}
              </span>
              <span>{s.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
