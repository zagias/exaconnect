import { describe, expect, it } from "vitest";
import type { User } from "../api";
import { areaFor, myApps } from "../apps";
import { accountGroups, destinations, navGroups } from "../nav";

const person = (extra: Partial<User>): User => ({ email: "p@example.com", role: "customer", customer_id: "c", ...extra });

describe("apps (ADR 0040)", () => {
  it("puts each screen in one app or the shared account pages", () => {
    expect(areaFor("/")).toBe("connect");
    expect(areaFor("/sites/1")).toBe("connect");
    expect(areaFor("/commai")).toBe("commai");
    expect(areaFor("/commai/voice/me")).toBe("commai");
    expect(areaFor("/account/people")).toBe("account");
    expect(areaFor("/billing/invoices")).toBe("account");
  });

  it("follows the person's apps, then the plan", () => {
    expect(myApps(person({ products: ["connect", "commai"], apps: ["commai"] }))).toEqual(["commai"]);
    expect(myApps(person({ products: ["connect"] }))).toEqual(["connect"]);
    expect(myApps(person({ products: null, apps: null }))).toEqual(["connect", "commai"]);
    expect(myApps({ ...person({}), role: "carrier" })).toEqual(["connect"]);
    expect(myApps(null)).toEqual([]);
  });

  it("keeps each app's menu apart and leaves out apps the person lacks", () => {
    const jibsyOnly = navGroups("customer", null, ["commai"]);
    expect(jibsyOnly.every((g) => g.app === "commai")).toBe(true);
    const both = navGroups("customer", null, ["connect", "commai"]);
    expect(new Set(both.map((g) => g.app))).toEqual(new Set(["connect", "commai"]));
    const where = destinations(both).find((d) => d.to === "/commai/contacts")?.where;
    expect(where).toBe("Jibsy · Conversations");
  });

  it("offers My settings on the account pages only with Jibsy", () => {
    const flat = (j: boolean) => accountGroups({ carrier: false, jibsy: j }).flatMap((g) => g.items.map((i) => i.to));
    expect(flat(true)).toContain("/commai/me");
    expect(flat(false)).not.toContain("/commai/me");
    expect(flat(false)).toContain("/account/apps");
  });
});
