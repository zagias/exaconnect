import { useState, type FormEvent } from "react";
import {
  api,
  useApi,
  type OrgInvite,
  type OrgInviteCreated,
  type OrgMember,
  type OrgMembers,
  type OrgRole,
} from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";
import { useCustomer } from "../customer";
import { PageHead, RowActions, useAction, type RowAction } from "../ui";
import "./identity.css";

const ROLE_LABEL: Record<OrgRole, string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
};
const ROLE_HELP: Record<OrgRole, string> = {
  owner: "Everything an admin can do, and hands ownership on.",
  admin: "Manages people and settings.",
  member: "Makes changes.",
  viewer: "Read only.",
};
const INVITE_ROLES: OrgRole[] = ["member", "viewer", "admin"];

function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Account > People: who is in this organisation, their roles, and invitations (ADR 0023). */
export default function People() {
  const { user } = useAuth();
  const { current } = useCustomer();
  // ExaCarib admins manage the organisation picked in the header.
  const cid =
    (user?.role === "admin" ? current?.id : user?.customer_id) ?? null;
  const members = useApi<OrgMembers>(
    cid ? `/orgs/${cid}/members` : null,
    30_000,
  );
  const manage = members.data?.can_manage ?? false;
  const invites = useApi<OrgInvite[]>(
    cid && manage ? `/orgs/${cid}/invites` : null,
    30_000,
  );
  const act = useAction();

  if (!cid)
    return (
      <>
        <PageHead eyebrow="Account" title="People" />
        <div className="empty">
          <p>
            You are not a member of an organisation. Ask an owner to invite you.
          </p>
        </div>
      </>
    );

  const yourRole = members.data?.your_role ?? null;
  const owner = user?.role === "admin" || yourRole === "owner";

  const reloadAll = () => {
    members.reload();
    invites.reload();
  };

  const setRole = (m: OrgMember, role: OrgRole) =>
    act.run(async () => {
      await api(`/orgs/${cid}/members/${m.user_id}`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
      });
      reloadAll();
    });

  const remove = (m: OrgMember) => {
    if (
      !window.confirm(
        `Remove ${m.email} from ${members.data?.organisation.name}? Their API keys made here stop working.`,
      )
    )
      return;
    act.run(async () => {
      await api(`/orgs/${cid}/members/${m.user_id}`, { method: "DELETE" });
      reloadAll();
    });
  };

  const transfer = (m: OrgMember) => {
    if (!window.confirm(`Make ${m.email} the owner? You become an admin.`))
      return;
    act.run(async () => {
      await api(`/orgs/${cid}/transfer-ownership`, {
        method: "POST",
        body: JSON.stringify({ user_id: m.user_id }),
      });
      window.location.reload();
    });
  };

  const leave = () => {
    if (
      !window.confirm(
        `Leave ${members.data?.organisation.name}? You lose access to it straight away.`,
      )
    )
      return;
    act.run(async () => {
      await api(`/orgs/${cid}/leave`, { method: "POST" });
      window.location.assign("/");
    });
  };

  const actionsFor = (m: OrgMember): RowAction[] => {
    if (!manage || m.managed_by) return [];
    const items: RowAction[] = [];
    const roles: OrgRole[] = owner
      ? ["owner", "admin", "member", "viewer"]
      : ["admin", "member", "viewer"];
    if (m.role === "owner" && !owner) return [];
    for (const r of roles)
      if (r !== m.role && r !== "owner")
        items.push({
          label: `Make ${ROLE_LABEL[r].toLowerCase()}`,
          onSelect: () => setRole(m, r),
        });
    if (owner && m.role !== "owner" && !m.you)
      items.push({
        label: "Hand ownership to them",
        onSelect: () => transfer(m),
      });
    if (owner && m.role !== "owner")
      items.push({
        label: "Make owner too",
        onSelect: () => setRole(m, "owner"),
      });
    if (!m.you)
      items.push({
        label: "Remove from organisation",
        danger: true,
        onSelect: () => remove(m),
      });
    return items;
  };

  const people = members.data?.members ?? [];

  return (
    <>
      <PageHead eyebrow="Account" title="People">
        Everyone in {members.data?.organisation.name ?? "your organisation"},
        their roles and pending invitations.
      </PageHead>

      <section
        className="card"
        style={{ maxWidth: 960 }}
        aria-labelledby="people-title"
      >
        <div className="card-head">
          <h2 id="people-title">Members</h2>
          {yourRole && (
            <span className="muted small">
              Your role: {ROLE_LABEL[yourRole]}
            </span>
          )}
        </div>
        <ErrorNote error={members.error} />
        {people.length > 0 && (
          <div className="table-wrap">
            <table className="paths dt stack">
              <thead>
                <tr>
                  <th scope="col">Person</th>
                  <th scope="col">Role</th>
                  <th scope="col">Joined</th>
                  <th scope="col" className="actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {people.map((m) => {
                  const items = actionsFor(m);
                  return (
                    <tr key={m.user_id}>
                      <td className="cell-wrap">
                        <strong>{m.name || m.email}</strong>
                        {m.you && <span className="muted small"> (you)</span>}
                        {m.name && <span className="sub">{m.email}</span>}
                        {m.disabled && (
                          <span className="sub">Switched off</span>
                        )}
                      </td>
                      <td data-label="Role">
                        {ROLE_LABEL[m.role]}
                        {m.managed_by && (
                          <span className="sub">
                            {["sandbox", "partner"].includes(m.managed_by)
                              ? `Managed by ExaCarib (${m.managed_by})`
                              : `Managed by your directory (${m.managed_by.toUpperCase()})`}
                          </span>
                        )}
                      </td>
                      <td data-label="Joined" className="mono">
                        {day(m.created_at)}
                      </td>
                      <td className="actions">
                        {items.length > 0 && (
                          <RowActions
                            label={m.email}
                            disabled={act.busy}
                            items={items}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <ErrorNote error={act.error} />
        <ul
          className="muted small"
          style={{ margin: "12px 0 0", paddingLeft: 18 }}
        >
          {(Object.keys(ROLE_HELP) as OrgRole[]).map((r) => (
            <li key={r}>
              <strong>{ROLE_LABEL[r]}</strong>: {ROLE_HELP[r]}
            </li>
          ))}
        </ul>
        {user?.role === "customer" && (
          <div className="actions" style={{ marginTop: 16 }}>
            <button
              type="button"
              className="button secondary"
              onClick={leave}
              disabled={act.busy}
            >
              Leave this organisation
            </button>
          </div>
        )}
      </section>

      {manage && (
        <Invitations
          cid={cid}
          invites={invites.data ?? []}
          error={invites.error}
          reload={reloadAll}
        />
      )}
    </>
  );
}

function Invitations({
  cid,
  invites,
  error,
  reload,
}: {
  cid: string;
  invites: OrgInvite[];
  error: string | null;
  reload: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<OrgRole>("member");
  const [created, setCreated] = useState<OrgInviteCreated | null>(null);
  const create = useAction();
  const revoke = useAction();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.run(async () => {
      const out = await api<OrgInviteCreated>(`/orgs/${cid}/invites`, {
        method: "POST",
        body: JSON.stringify({ email: email.trim(), role }),
      });
      setCreated(out);
      setEmail("");
      reload();
    });
  };

  const onRevoke = (i: OrgInvite) => {
    if (
      !window.confirm(
        `Cancel the invitation for ${i.email}? Its link stops working.`,
      )
    )
      return;
    revoke.run(async () => {
      await api(`/orgs/${cid}/invites/${i.id}`, { method: "DELETE" });
      reload();
    });
  };

  const link = created
    ? new URL(created.path, window.location.origin).toString()
    : "";

  return (
    <section
      className="card"
      style={{ maxWidth: 960, marginTop: 24 }}
      aria-labelledby="invites-title"
    >
      <div className="card-head">
        <h2 id="invites-title">Invitations</h2>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        We don't send invitation emails yet. You get a link to pass on yourself;
        it is shown once, works once and expires in 7 days.
      </p>
      <ErrorNote error={error} />
      {invites.length > 0 && (
        <div className="table-wrap">
          <table className="paths dt stack">
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col">Role</th>
                <th scope="col">Invited by</th>
                <th scope="col">Expires</th>
                <th scope="col" className="actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {invites.map((i) => (
                <tr key={i.id}>
                  <td className="cell-wrap">
                    <strong>{i.email}</strong>
                  </td>
                  <td data-label="Role">{ROLE_LABEL[i.role]}</td>
                  <td data-label="Invited by">
                    {i.invited_by.replace(/^user:/, "")}
                  </td>
                  <td data-label="Expires" className="mono">
                    {i.expired ? "Expired" : day(i.expires_at)}
                  </td>
                  <td className="actions">
                    <RowActions
                      label={`invitation for ${i.email}`}
                      disabled={revoke.busy}
                      items={[
                        {
                          label: "Cancel invitation",
                          danger: true,
                          onSelect: () => onRevoke(i),
                        },
                      ]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorNote error={revoke.error} />

      <h3 style={{ margin: "24px 0 8px" }}>Invite someone</h3>
      <form className="form" onSubmit={submit}>
        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={255}
            required
          />
        </label>
        <label>
          Role
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as OrgRole)}
          >
            {INVITE_ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}: {ROLE_HELP[r]}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button className="button" disabled={create.busy}>
            {create.busy ? "Inviting…" : "Create invitation"}
          </button>
        </div>
      </form>
      <ErrorNote error={create.error} />
      {created && (
        <div className="secret" role="region" aria-label="Invitation link">
          <p className="callout" style={{ margin: 0 }}>
            <strong>Copy this link for {created.email} now.</strong> It is not
            shown again.
          </p>
          <p>
            <code className="small" style={{ wordBreak: "break-all" }}>
              {link}
            </code>
          </p>
          <div className="secret-actions">
            <button
              type="button"
              className="button secondary"
              onClick={() => navigator.clipboard?.writeText(link)}
            >
              Copy
            </button>
            <button
              type="button"
              className="button"
              onClick={() => setCreated(null)}
            >
              Done
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
