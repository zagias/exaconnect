// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import type { PlanningBrief } from "../pages/assistant/PlanningBrief";
import { canBuildPlanningQuote, clearPlanningQuoteHandoff, queuePlanningQuote, readPlanningQuote } from "../pages/assistant/planningQuoteHandoff";

const owner: User = { email: "owner@example.invalid", role: "customer", customer_id: "org-a", org_role: "owner" };
const brief: PlanningBrief = { format: "xakore-planning-brief", version: 1, offer: "Connect",
  facts: [{ label: "Estate", value: "20 existing shops; new sites unconfirmed" }], gaps: ["Confirm backup capacity."] };

afterEach(() => { clearPlanningQuoteHandoff(); vi.restoreAllMocks(); });

describe("planning-to-quote handoff", () => {
  it("uses commercial manager access and respects scoped accounts", () => {
    expect(canBuildPlanningQuote(owner)).toBe(true);
    expect(canBuildPlanningQuote({ ...owner, role: "admin" })).toBe(true);
    for (const user of [null, { ...owner, org_role: "member" as const },
      { ...owner, org_role: "viewer" as const }, { ...owner, role: "carrier" as const },
      { ...owner, scopes: ["connect:read"] }, { ...owner, scopes: ["commai:read"] }]) {
      expect(canBuildPlanningQuote(user)).toBe(false);
    }
  });

  it("copies all reviewed requirements in memory without storage or navigation", () => {
    const local = vi.spyOn(Storage.prototype, "setItem");
    const push = vi.spyOn(window.history, "pushState");
    const network = vi.spyOn(globalThis, "fetch");
    const full = { ...brief, facts: Array.from({ length: 24 }, (_, i) => ({ label: `Need ${i}`, value: "x".repeat(400) })) };
    queuePlanningQuote(owner, "org-a", full);
    full.facts[0].value = "Changed outside the handoff";
    const received = readPlanningQuote(owner, "org-a");
    expect(received?.facts).toHaveLength(24);
    expect(received?.facts[0].value).toBe("x".repeat(400));
    expect(received?.gaps).toEqual(brief.gaps);
    expect(local).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(network).not.toHaveBeenCalled();
  });

  it.each([
    [{ ...owner }, "org-b"], [{ ...owner, email: "someone@example.invalid" }, "org-a"],
    [{ ...owner, scopes: ["connect:read"] }, "org-a"], [{ ...owner, org_role: "member" }, "org-a"],
  ] as [User, string][])("discards the handoff after organisation, actor or access changes", (user, organisation) => {
    queuePlanningQuote(owner, "org-a", brief);
    expect(readPlanningQuote(user, organisation)).toBeNull();
    expect(readPlanningQuote(owner, "org-a")).toBeNull();
  });

  it.each(["exa-signed-out", "pagehide"])("clears on %s", (event) => {
    queuePlanningQuote(owner, "org-a", brief);
    window.dispatchEvent(new Event(event));
    expect(readPlanningQuote(owner, "org-a")).toBeNull();
  });
});
