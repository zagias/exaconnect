import type { User } from "./api";

/** Operational controls mirror the controller: members may operate; viewers cannot. */
export function canOperateConnect(user: User | null): boolean {
  if (!user || user.role === "carrier") return false;
  if (user.scopes && !user.scopes.includes("connect")) return false;
  return user.role === "admin" || (!!user.customer_id && user.org_role !== "viewer");
}
