import { useState, type FormEvent } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api, useApi } from "../../api";
import { ErrorNote } from "../../components";
import { Card, PageHead, RowActions, Tabs, useAction } from "../../ui";
import { CHANNEL_LABEL, useCommaiBase, when } from "./lib";
import Data from "./settings/Data";
import Organisation from "./settings/Organisation";
import Roles from "./settings/Roles";
import Security from "./settings/Security";
import SignIn from "./settings/SignIn";
import HelpAdmin from "../help/HelpAdmin";
import type { Member, Team } from "./types";

/** How the business runs CommAI: service targets, teams, seats, routing and developer access. */
export default function Settings() {
  const base = useCommaiBase();
  if (!base) return null;
  return (
    <>
      <PageHead eyebrow="CommAI" title="Settings">
        Who answers, how fast, and what your own systems can reach.
      </PageHead>
      <Tabs label="Settings sections">
        <NavLink to="/commai/settings" end>
          Service
        </NavLink>
        <NavLink to="/commai/settings/teams">Teams and people</NavLink>
        <NavLink to="/commai/settings/routing">Routing</NavLink>
        <NavLink to="/commai/settings/developers">Webhooks and keys</NavLink>
        <NavLink to="/commai/settings/sign-in">Sign-in</NavLink>
        <NavLink to="/commai/settings/organisation">Organisation</NavLink>
        <NavLink to="/commai/settings/roles">Roles</NavLink>
        <NavLink to="/commai/settings/security">Security</NavLink>
        <NavLink to="/commai/settings/data">Data</NavLink>
        <NavLink to="/commai/settings/help-centre">Help centre</NavLink>
      </Tabs>
      <Routes>
        <Route path="/" element={<Service base={base} />} />
        <Route path="/teams" element={<TeamsAndPeople base={base} />} />
        <Route path="/routing" element={<Routing base={base} />} />
        <Route path="/developers" element={<Developers base={base} />} />
        <Route path="/sign-in" element={<SignIn />} />
        <Route path="/organisation" element={<Organisation base={base} />} />
        <Route path="/roles" element={<Roles base={base} />} />
        <Route path="/security" element={<Security base={base} />} />
        <Route path="/data" element={<Data base={base} />} />
        <Route path="/help-centre" element={<HelpAdmin />} />
      </Routes>
    </>
  );
}

interface ServiceSettings {
  mode: "ai_first" | "human_first" | "human_only";
  timezone: string;
  first_reply_minutes: Record<string, number>;
  resolve_hours: Record<string, number>;
  ai_available: boolean;
}

const PRIORITIES = ["urgent", "high", "normal", "low"];

function Service({ base }: { base: string }) {
  const s = useApi<ServiceSettings>(`${base}/settings`, 0);
  const save = useAction();
  const [draft, setDraft] = useState<ServiceSettings | null>(null);
  const d = draft ?? s.data;
  if (!d) return <ErrorNote error={s.error} />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.run(async () => {
      await api(`${base}/settings`, {
        method: "PATCH",
        body: JSON.stringify({ mode: d.mode, timezone: d.timezone, first_reply_minutes: d.first_reply_minutes, resolve_hours: d.resolve_hours }),
      });
      setDraft(null);
      s.reload();
    });
  };
  const set = (patch: Partial<ServiceSettings>) => setDraft({ ...d, ...patch });

  return (
    <Card title="Who answers first, and service targets">
      <form className="form" onSubmit={submit}>
        <fieldset className="wide">
          <legend>New conversations go to</legend>
          {(
            [
              ["ai_first", "The AI agent first, handing over to people when needed"],
              ["human_first", "People first"],
              ["human_only", "People only (the AI never replies)"],
            ] as const
          ).map(([v, label]) => (
            <label key={v} className="check">
              <input type="radio" name="mode" checked={d.mode === v} onChange={() => set({ mode: v })} disabled={v === "ai_first" && !d.ai_available} />
              {label}
            </label>
          ))}
          {!d.ai_available && <p className="muted small">The AI agent is not set up yet.</p>}
        </fieldset>
        <label>
          Time zone
          <input value={d.timezone} onChange={(e) => set({ timezone: e.target.value })} maxLength={60} />
        </label>
        {PRIORITIES.map((p) => (
          <fieldset key={p}>
            <legend>{p[0].toUpperCase() + p.slice(1)} priority</legend>
            <label>
              First reply within (minutes)
              <input
                type="number"
                min={1}
                value={d.first_reply_minutes[p] ?? ""}
                onChange={(e) => set({ first_reply_minutes: { ...d.first_reply_minutes, [p]: Number(e.target.value) } })}
              />
            </label>
            <label>
              Resolve within (hours)
              <input
                type="number"
                min={1}
                value={d.resolve_hours[p] ?? ""}
                onChange={(e) => set({ resolve_hours: { ...d.resolve_hours, [p]: Number(e.target.value) } })}
              />
            </label>
          </fieldset>
        ))}
        <div className="actions wide">
          <button className="button" disabled={save.busy || !draft}>
            Save
          </button>
        </div>
      </form>
      <ErrorNote error={save.error} />
    </Card>
  );
}

function TeamsAndPeople({ base }: { base: string }) {
  const teams = useApi<Team[]>(`${base}/teams`, 30_000);
  const members = useApi<Member[]>(`${base}/members`, 30_000);
  const act = useAction();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const people = members.data ?? [];

  const createTeam = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      await api(`${base}/teams`, { method: "POST", body: JSON.stringify({ name, members: picked }) });
      setName("");
      setPicked([]);
      teams.reload();
    });
  };
  const setSeat = (m: Member, patch: Partial<Member>) =>
    act.run(async () => {
      await api(`${base}/members/${m.id}`, {
        method: "PUT",
        body: JSON.stringify({ seat: m.seat, skills: m.skills, languages: m.languages, available: m.available, ...patch }),
      });
      members.reload();
    });
  const removeTeam = (t: Team) => {
    if (!window.confirm(`Delete the team ${t.name}? Its conversations stay, without a team.`)) return;
    act.run(async () => {
      await api(`${base}/teams/${t.id}`, { method: "DELETE" });
      teams.reload();
    });
  };
  const emailOf = (id: string) => people.find((p) => p.id === id)?.email ?? id;

  return (
    <>
      <Card title="People">
        <p className="muted small">
          An internal seat reads conversations and writes private notes but never replies to customers. Away means new
          conversations are not assigned to them.
        </p>
        <ErrorNote error={members.error} />
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Person</th>
                <th scope="col">Seat</th>
                <th scope="col">Languages</th>
                <th scope="col">Open</th>
                <th scope="col">Available</th>
              </tr>
            </thead>
            <tbody>
              {people.map((m) => (
                <tr key={m.id}>
                  <td>{m.email}</td>
                  <td data-label="Seat">
                    <select aria-label={`Seat for ${m.email}`} value={m.seat} disabled={act.busy} onChange={(e) => setSeat(m, { seat: e.target.value as Member["seat"] })}>
                      <option value="agent">Replies to customers</option>
                      <option value="internal">Internal (notes only)</option>
                    </select>
                  </td>
                  <td data-label="Languages">
                    <input
                      aria-label={`Languages for ${m.email}`}
                      defaultValue={m.languages.join(", ")}
                      onBlur={(e) => {
                        const langs = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
                        if (langs.join(",") !== m.languages.join(",")) setSeat(m, { languages: langs });
                      }}
                    />
                  </td>
                  <td data-label="Open" className="mono">
                    {m.open_conversations}
                  </td>
                  <td data-label="Available">
                    <label className="check">
                      <input type="checkbox" checked={m.available} disabled={act.busy} onChange={(e) => setSeat(m, { available: e.target.checked })} />
                      {m.available ? "Available" : "Away"}
                    </label>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Teams">
        <ErrorNote error={teams.error} />
        {(teams.data ?? []).length === 0 && <p className="muted">No teams yet. Routing rules send conversations to a team.</p>}
        <ul className="plain-list">
          {(teams.data ?? []).map((t) => (
            <li key={t.id} className="row-between">
              <span>
                <strong>{t.name}</strong>
                <span className="muted small"> · {t.members.length ? t.members.map(emailOf).join(", ") : "no members"}</span>
              </span>
              <RowActions label={`team ${t.name}`} items={[{ label: "Delete team", danger: true, onSelect: () => removeTeam(t) }]} />
            </li>
          ))}
        </ul>
        <h3>Add a team</h3>
        <form className="form" onSubmit={createTeam}>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} placeholder="Sales" />
          </label>
          <fieldset className="wide">
            <legend>Members</legend>
            {people
              .filter((p) => p.seat === "agent")
              .map((p) => (
                <label key={p.id} className="check">
                  <input
                    type="checkbox"
                    checked={picked.includes(p.id)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, p.id] : picked.filter((x) => x !== p.id))}
                  />
                  {p.email}
                </label>
              ))}
          </fieldset>
          <div className="actions">
            <button className="button" disabled={act.busy}>
              Add team
            </button>
          </div>
        </form>
        <ErrorNote error={act.error} />
      </Card>
    </>
  );
}

interface Rule {
  id: string;
  name: string;
  position: number;
  match: { channel?: string; keywords?: string[]; language?: string };
  team_id: string | null;
  priority: string | null;
  enabled: boolean;
}

function Routing({ base }: { base: string }) {
  const rules = useApi<Rule[]>(`${base}/routing-rules`, 30_000);
  const teams = useApi<Team[]>(`${base}/teams`, 0);
  const act = useAction();
  const [f, setF] = useState({ name: "", keywords: "", channel: "", language: "", team_id: "", priority: "" });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const match: Rule["match"] = {};
    const kws = f.keywords.split(",").map((k) => k.trim()).filter(Boolean);
    if (kws.length) match.keywords = kws;
    if (f.channel) match.channel = f.channel;
    if (f.language) match.language = f.language;
    act.run(async () => {
      await api(`${base}/routing-rules`, {
        method: "POST",
        body: JSON.stringify({
          name: f.name,
          match,
          team_id: f.team_id || null,
          priority: f.priority || null,
          position: ((rules.data ?? []).length + 1) * 10,
        }),
      });
      setF({ name: "", keywords: "", channel: "", language: "", team_id: "", priority: "" });
      rules.reload();
    });
  };
  const remove = (r: Rule) =>
    act.run(async () => {
      await api(`${base}/routing-rules/${r.id}`, { method: "DELETE" });
      rules.reload();
    });
  const teamName = (id: string | null) => (teams.data ?? []).find((t) => t.id === id)?.name ?? "No team";

  return (
    <Card title="Routing rules">
      <p className="muted small">
        The first rule that matches a new conversation decides its team and priority. The available team member with the
        fewest open conversations is assigned.
      </p>
      <ErrorNote error={rules.error} />
      <ol>
        {(rules.data ?? []).map((r) => (
          <li key={r.id} className="row-between">
            <span>
              <strong>{r.name}</strong>
              <span className="muted small">
                {" "}
                · {r.match.keywords?.length ? `mentions ${r.match.keywords.join(" or ")}` : "any message"}
                {r.match.channel ? ` on ${CHANNEL_LABEL[r.match.channel] ?? r.match.channel}` : ""}
                {r.match.language ? ` in ${r.match.language}` : ""} → {teamName(r.team_id)}
                {r.priority ? `, ${r.priority} priority` : ""}
              </span>
            </span>
            <RowActions label={`rule ${r.name}`} items={[{ label: "Delete rule", danger: true, onSelect: () => remove(r) }]} />
          </li>
        ))}
      </ol>
      <h3>Add a rule</h3>
      <form className="form" onSubmit={submit}>
        <label>
          Name
          <input required value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxLength={80} placeholder="Quotes to Sales" />
        </label>
        <label>
          Words to look for (comma separated)
          <input value={f.keywords} onChange={(e) => setF({ ...f, keywords: e.target.value })} placeholder="quote, price" />
        </label>
        <label>
          Channel
          <select value={f.channel} onChange={(e) => setF({ ...f, channel: e.target.value })}>
            <option value="">Any</option>
            {Object.entries(CHANNEL_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          Language code
          <input value={f.language} onChange={(e) => setF({ ...f, language: e.target.value })} maxLength={12} placeholder="es" />
        </label>
        <label>
          Team
          <select value={f.team_id} onChange={(e) => setF({ ...f, team_id: e.target.value })}>
            <option value="">Keep default</option>
            {(teams.data ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Priority
          <select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })}>
            <option value="">Keep normal</option>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button className="button" disabled={act.busy}>
            Add rule
          </button>
        </div>
      </form>
      <ErrorNote error={act.error} />
    </Card>
  );
}

interface Endpoint {
  id: string;
  url: string;
  description: string;
  events: string[];
  active: boolean;
  failed: number;
  last_delivered_at: string | null;
}

interface Delivery {
  id: number;
  event_type: string;
  status: string;
  attempts: number;
  response_code: number | null;
  last_error: string;
  created_at: string;
}

const SCOPE_LABEL: Record<string, string> = {
  "commai:read": "Read contacts and conversations",
  "commai:write": "Write contacts and conversations, send replies",
  "commai:notes": "Private notes (staff tools only)",
  "commai:admin": "Settings, webhooks, integrations, workflows, voice",
  connect: "The network API (Connect)",
};

function Developers({ base }: { base: string }) {
  const hooks = useApi<Endpoint[]>(`${base}/webhooks`, 30_000);
  const types = useApi<string[]>(`${base}/event-types`, 0);
  const act = useAction();
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState("*");
  const [secret, setSecret] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const deliveries = useApi<Delivery[]>(open ? `${base}/webhooks/${open}/deliveries` : null, 15_000);

  const [keyName, setKeyName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["commai:read", "commai:write"]);
  const [token, setToken] = useState<string | null>(null);

  const addHook = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ secret: string }>(`${base}/webhooks`, {
        method: "POST",
        body: JSON.stringify({ url, events: events.split(",").map((x) => x.trim()).filter(Boolean) }),
      });
      setSecret(out.secret);
      setUrl("");
      hooks.reload();
    });
  };
  const addKey = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const out = await api<{ token: string }>("/auth/api-keys", { method: "POST", body: JSON.stringify({ name: keyName, scopes }) });
      setToken(out.token);
      setKeyName("");
    });
  };

  return (
    <>
      <Card title="Webhooks">
        <p className="muted small">
          CommAI posts each event to your address, signed with your secret (header X-ExaCarib-Signature, HMAC-SHA256 of
          "timestamp.body"). Failed deliveries are retried. The event id never changes, so you can ignore repeats.
        </p>
        <ErrorNote error={hooks.error} />
        <ul className="plain-list">
          {(hooks.data ?? []).map((h) => (
            <li key={h.id} className="row-between">
              <span>
                <span className="mono">{h.url}</span>
                <span className="muted small">
                  {" "}
                  · {h.events.join(", ")} · last delivered {h.last_delivered_at ? when(h.last_delivered_at) : "never"}
                  {h.failed > 0 && <span className="pill bad"> {h.failed} failed</span>}
                </span>
              </span>
              <RowActions
                label={`webhook ${h.url}`}
                items={[
                  { label: "Show deliveries", onSelect: () => setOpen(open === h.id ? null : h.id) },
                  {
                    label: "Send a test event",
                    onSelect: () =>
                      act.run(async () => {
                        await api(`${base}/webhooks/${h.id}/test`, { method: "POST" });
                        setOpen(h.id);
                      }),
                  },
                  {
                    label: "Delete webhook",
                    danger: true,
                    onSelect: () =>
                      window.confirm(`Stop sending events to ${h.url}?`) &&
                      act.run(async () => {
                        await api(`${base}/webhooks/${h.id}`, { method: "DELETE" });
                        hooks.reload();
                      }),
                  },
                ]}
              />
            </li>
          ))}
        </ul>
        {open && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <caption className="sr-only">Recent deliveries</caption>
              <thead>
                <tr>
                  <th scope="col">Event</th>
                  <th scope="col">Status</th>
                  <th scope="col">Tries</th>
                  <th scope="col">Answer</th>
                  <th scope="col">When</th>
                </tr>
              </thead>
              <tbody>
                {(deliveries.data ?? []).map((d) => (
                  <tr key={d.id}>
                    <td>{d.event_type}</td>
                    <td data-label="Status">
                      <span className={`pill ${d.status === "delivered" ? "ok" : d.status === "failed" ? "bad" : "warn"}`}>{d.status}</span>
                    </td>
                    <td data-label="Tries" className="mono">
                      {d.attempts}
                    </td>
                    <td data-label="Answer" className="mono">
                      {d.response_code ?? d.last_error ?? "–"}
                    </td>
                    <td data-label="When">{when(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <form className="form" onSubmit={addHook}>
          <label className="wide">
            Address (https)
            <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/commai-events" />
          </label>
          <label className="wide">
            Events (comma separated; * for all, message.* for a family)
            <input value={events} onChange={(e) => setEvents(e.target.value)} list="event-types" />
            <datalist id="event-types">
              {(types.data ?? []).map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </label>
          <div className="actions">
            <button className="button" disabled={act.busy}>
              Add webhook
            </button>
          </div>
        </form>
        {secret && (
          <div className="secret" role="status">
            <p className="callout warn small">
              <strong>Copy the signing secret now.</strong> It won't be shown again.
            </p>
            <code>{secret}</code>
            <div className="secret-actions">
              <button type="button" className="button small" onClick={() => setSecret(null)}>
                Done
              </button>
            </div>
          </div>
        )}
      </Card>
      <Card title="API keys for CommAI">
        <p className="muted small">
          A key with scopes can only do what you tick. A key for a customer-facing app should never have private notes.
          Writes accept an Idempotency-Key header, so a retried request never sends twice.
        </p>
        <form className="form" onSubmit={addKey}>
          <label>
            Name
            <input required value={keyName} onChange={(e) => setKeyName(e.target.value)} maxLength={60} placeholder="Website app" />
          </label>
          <fieldset className="wide">
            <legend>It may</legend>
            {Object.entries(SCOPE_LABEL).map(([s, label]) => (
              <label key={s} className="check">
                <input type="checkbox" checked={scopes.includes(s)} onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))} />
                {label}
              </label>
            ))}
          </fieldset>
          <div className="actions">
            <button className="button" disabled={act.busy || scopes.length === 0}>
              Create key
            </button>
          </div>
        </form>
        {token && (
          <div className="secret" role="status">
            <p className="callout warn small">
              <strong>Copy it now.</strong> This key won't be shown again. Manage and revoke keys on your Account page.
            </p>
            <code>{token}</code>
            <div className="secret-actions">
              <button type="button" className="button small" onClick={() => setToken(null)}>
                Done
              </button>
            </div>
          </div>
        )}
        <ErrorNote error={act.error} />
      </Card>
    </>
  );
}
