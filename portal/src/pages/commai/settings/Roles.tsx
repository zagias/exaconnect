import { useState, type FormEvent } from "react";
import { api, useApi } from "../../../api";
import { ErrorNote } from "../../../components";
import "../../identity.css";
import { Card, useAction } from "../../../ui";

interface Role {
  id: string;
  key: string;
  name: string;
  description: string;
  permissions: string[];
  builtin: boolean;
}
interface Person {
  id: string;
  email: string;
  seat: string;
  has_roles: boolean;
  builtin: string;
  permissions: string[];
  team_permissions: Record<string, string[]>;
}
interface Assignment {
  id: string;
  user_id: string;
  email: string;
  role_id: string;
  role_name: string;
  team_id: string | null;
  team_name: string | null;
}
interface RolesData {
  permissions: { key: string; label: string }[];
  roles: Role[];
  assignments: Assignment[];
  people: Person[];
}

/** Custom roles from a fixed permission list, given per person and optionally per team (ADR 0024). */
export default function Roles({ base }: { base: string }) {
  const r = useApi<RolesData>(`${base}/roles`, 0);
  const teams = useApi<{ id: string; name: string }[]>(`${base}/teams`, 0);
  const act = useAction();
  const [draft, setDraft] = useState<{ id?: string; name: string; description: string; permissions: string[] } | null>(null);
  const [assign, setAssign] = useState({ user_id: "", role_id: "", team_id: "" });
  if (!r.data) return <ErrorNote error={r.error} />;
  const d = r.data;
  const label = (k: string) => d.permissions.find((p) => p.key === k)?.label ?? k;

  const saveRole = (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    act.run(async () => {
      const body = JSON.stringify({ name: draft.name, description: draft.description, permissions: draft.permissions });
      await api(draft.id ? `${base}/roles/${draft.id}` : `${base}/roles`, { method: draft.id ? "PUT" : "POST", body });
      setDraft(null);
      r.reload();
    });
  };
  const call = (path: string, init: RequestInit) =>
    act.run(async () => {
      await api(`${base}/roles${path}`, init);
      r.reload();
    });

  return (
    <>
      <p className="muted small">
        A role narrows what a person may do. People without a role keep the rights of their account and seat, shown here
        as the built-in roles. A role given for one team applies only to that team&apos;s conversations.
      </p>
      <ErrorNote error={act.error} />
      <Card title="Roles">
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Permissions</th>
                <th scope="col" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {d.roles.map((role) => (
                <tr key={role.id}>
                  <td>
                    {role.name} {role.builtin && <span className="muted small">Built in</span>}
                    {role.description && <div className="muted small">{role.description}</div>}
                  </td>
                  <td data-label="Permissions" className="small">
                    {role.permissions.map(label).join(", ") || "None"}
                  </td>
                  <td>
                    {!role.builtin && (
                      <>
                        <button className="link" onClick={() => setDraft({ id: role.id, name: role.name, description: role.description, permissions: role.permissions })}>
                          Edit
                        </button>{" "}
                        <button
                          className="link"
                          disabled={act.busy}
                          onClick={() => window.confirm(`Delete the role ${role.name}? People lose it.`) && call(`/${role.id}`, { method: "DELETE" })}
                        >
                          Delete
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!draft && (
          <div className="actions">
            <button className="button" onClick={() => setDraft({ name: "", description: "", permissions: ["read_inbox"] })}>
              New role
            </button>
          </div>
        )}
        {draft && (
          <form className="form" onSubmit={saveRole}>
            <label>
              Name
              <input value={draft.name} required maxLength={80} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </label>
            <label>
              Description
              <input value={draft.description} maxLength={300} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
            </label>
            <fieldset className="wide">
              <legend>Permissions</legend>
              {d.permissions.map((p) => (
                <label key={p.key} className="check">
                  <input
                    type="checkbox"
                    checked={draft.permissions.includes(p.key)}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        permissions: e.target.checked ? [...draft.permissions, p.key] : draft.permissions.filter((x) => x !== p.key),
                      })
                    }
                  />
                  {p.label}
                </label>
              ))}
            </fieldset>
            <div className="actions wide">
              <button className="button" disabled={act.busy || !draft.name}>
                Save role
              </button>
              <button type="button" className="button secondary" onClick={() => setDraft(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </Card>
      <Card title="Who has which role">
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Person</th>
                <th scope="col">Roles</th>
                <th scope="col">Can</th>
              </tr>
            </thead>
            <tbody>
              {d.people.map((p) => {
                const mine = d.assignments.filter((a) => a.user_id === p.id);
                return (
                  <tr key={p.id}>
                    <td>{p.email}</td>
                    <td data-label="Roles">
                      {mine.length === 0 && <span className="muted small">{p.builtin === "internal" ? "Internal (seat)" : "Business admin (no role set)"}</span>}
                      {mine.map((a) => (
                        <div key={a.id} className="small">
                          {a.role_name}
                          {a.team_name ? ` for ${a.team_name}` : ""}{" "}
                          <button className="link" disabled={act.busy} onClick={() => call(`/assignments/${a.id}`, { method: "DELETE" })}>
                            Remove
                          </button>
                        </div>
                      ))}
                    </td>
                    <td data-label="Can" className="small">
                      {p.permissions.map(label).join(", ") || "Nothing across the business"}
                      {Object.entries(p.team_permissions).map(([t, perms]) => (
                        <div key={t} className="muted">
                          For {teams.data?.find((x) => x.id === t)?.name ?? "a team"}: {perms.map(label).join(", ")}
                        </div>
                      ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            call("/assignments", { method: "POST", body: JSON.stringify({ ...assign, team_id: assign.team_id || null }) });
          }}
        >
          <label>
            Person
            <select value={assign.user_id} required onChange={(e) => setAssign({ ...assign, user_id: e.target.value })}>
              <option value="">Choose…</option>
              {d.people.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.email}
                </option>
              ))}
            </select>
          </label>
          <label>
            Role
            <select value={assign.role_id} required onChange={(e) => setAssign({ ...assign, role_id: e.target.value })}>
              <option value="">Choose…</option>
              {d.roles.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Where
            <select value={assign.team_id} onChange={(e) => setAssign({ ...assign, team_id: e.target.value })}>
              <option value="">Across the business</option>
              {(teams.data ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  Only for {t.name}
                </option>
              ))}
            </select>
          </label>
          <div className="actions wide">
            <button className="button" disabled={act.busy || !assign.user_id || !assign.role_id}>
              Give role
            </button>
          </div>
        </form>
      </Card>
    </>
  );
}
