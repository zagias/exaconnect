import type { User } from "../../api";
import { parsePlanningBrief, type PlanningBrief } from "./PlanningBrief";

/** Commercial writes follow the controller's account scope as well as the organisation role. */
export function canBuildPlanningQuote(user: User | null): boolean {
  if (!user || (user.scopes != null && !user.scopes.includes("connect"))) return false;
  return user.role === "admin" || (user.role === "customer" && ["owner", "admin"].includes(user.org_role ?? ""));
}

export function planningQuoteActor(user: User | null, customerId: string | undefined): string {
  return JSON.stringify([user?.email, user?.role, user?.org_role, user?.customer_id, user?.scopes, customerId]);
}

let handoff: { actor: string; customerId: string; brief: PlanningBrief } | null = null;

export function clearPlanningQuoteHandoff(): void { handoff = null; }

export function queuePlanningQuote(user: User | null, customerId: string, brief: PlanningBrief): void {
  if (!canBuildPlanningQuote(user)) throw new Error("Your account cannot create a quote for this organisation.");
  handoff = { actor: planningQuoteActor(user, customerId), customerId, brief: parsePlanningBrief(JSON.stringify(brief)) };
}

/** Kept in this module only, never in history state, a URL or browser storage. */
export function readPlanningQuote(user: User | null, customerId: string | undefined): PlanningBrief | null {
  if (!handoff) return null;
  if (!canBuildPlanningQuote(user) || handoff.actor !== planningQuoteActor(user, customerId) || handoff.customerId !== customerId) {
    clearPlanningQuoteHandoff();
    return null;
  }
  return parsePlanningBrief(JSON.stringify(handoff.brief));
}

if (typeof window !== "undefined") {
  window.addEventListener("exa-signed-out", clearPlanningQuoteHandoff);
  window.addEventListener("pagehide", clearPlanningQuoteHandoff);
}
