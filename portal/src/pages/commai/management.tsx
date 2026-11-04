import type { ReactNode } from "react";
import { useApi } from "../../api";
import { useAuth } from "../../auth";

interface OwnSettings { profile: { seat: string } }
interface OwnPermissions { has_roles: boolean; permissions: string[] }

/** Presentation only; the API remains authoritative for every change. */
export function useJibsyManagement(base: string | null, permission: "manage_integrations" | "manage_teams" = "manage_integrations") {
  const { user } = useAuth();
  const ownerOrAdmin = user?.role === "admin" || (user?.role === "customer" && (user.org_role === "owner" || user.org_role === "admin"));
  const hasScope = !!user && (!user.scopes || user.scopes.includes("commai:admin"));
  const check = !!base && ownerOrAdmin && hasScope && user?.role !== "admin";
  const own = useApi<OwnSettings>(check ? `${base}/me/settings` : null, 60_000);
  const roles = useApi<OwnPermissions>(check ? `${base}/roles/me` : null, 60_000);
  const error = check ? own.error ?? roles.error : null;
  const checking = check && !error && (!own.data || !roles.data);
  const internal = own.data?.profile.seat === "internal";
  const roleAllowed = roles.data && (!roles.data.has_roles || roles.data.permissions.includes(permission));
  const canManage = !!base && ownerOrAdmin && hasScope && (user?.role === "admin" || (!checking && !error && !!own.data && !internal && !!roleAllowed));
  const message = !base || canManage ? null
    : !ownerOrAdmin ? "You can view these settings. An organisation owner or admin can change them."
    : !hasScope ? "Your account can view these settings, but does not have settings access. Ask an organisation administrator."
    : error ? "Your settings access could not be checked. Try again."
    : checking ? "Checking your settings access…"
    : internal ? "Your Jibsy seat can view these settings, but cannot change them. Ask an administrator about your seat."
    : "Your assigned role can view these settings, but cannot change them. Ask an organisation administrator.";
  return { canManage, message, error, reload: () => { own.reload(); roles.reload(); } };
}

export function ManagementNote({ access }: { access: ReturnType<typeof useJibsyManagement> }) {
  if (!access.message) return null;
  return <p className="muted small" role="status">
    {access.message}{" "}
    {access.error && <button type="button" className="linklike" onClick={access.reload}>Try again</button>}
  </p>;
}

/** Keeps the settings visible while disabling their native form controls. */
export function ManagementControls({ canManage, children }: { canManage: boolean; children: ReactNode }) {
  return <fieldset disabled={!canManage} style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }}>
    <legend className="sr-only">Settings</legend>
    {children}
  </fieldset>;
}
