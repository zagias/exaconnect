import { describe, expect, it } from "vitest";
import { destinations, navGroups } from "../nav";

function routes(role: string, capabilities: { ask?: boolean; integrations?: boolean }) {
  return navGroups(role, null, ["connect"], capabilities).flatMap((group) => group.items.map((item) => item.to));
}

describe("Connect navigation follows runtime readiness", () => {
  it("omits unavailable Ask and integrations while preserving working network services", () => {
    const visible = routes("customer", { ask: false, integrations: false });
    expect(visible).not.toContain("/ask");
    expect(visible).not.toContain("/integrations");
    const primary = navGroups("customer", null, ["connect"], { ask: false, integrations: false })[0];
    expect(primary.items.map((item) => item.label)).toEqual(["Overview", "Sites", "Traffic", "Activity", "Usage"]);
    expect(destinations([primary]).map((item) => item.to)).toEqual(expect.arrayContaining(["/insights", "/notices", "/locations"]));
    for (const route of ["/", "/sites", "/decisions", "/traffic", "/fabric", "/internet", "/encryption", "/order", "/metering"])
      expect(visible).toContain(route);
  });

  it("adds only the capabilities that are available", () => {
    expect(routes("customer", { ask: true, integrations: false })).toContain("/ask");
    expect(routes("customer", { ask: true, integrations: false })).not.toContain("/integrations");
    expect(routes("customer", { ask: false, integrations: true })).not.toContain("/ask");
    expect(routes("customer", { ask: false, integrations: true })).toContain("/integrations");
  });

  it("also removes unavailable integrations from the carrier menu", () => {
    expect(routes("carrier", { integrations: false })).toEqual(["/", "/notices"]);
  });
});
