// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import Ask from "../pages/Ask";
import { assistantAddress, type AssistantContext, type Plan } from "../pages/assistant/types";
import { accountGroups, navGroups } from "../nav";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Coral" } }) }));

const revision = "a".repeat(64);
const draft: Plan = {
  id: 1, status: "draft", customer_id: "org-a", question: "Give voice priority", summary: ["Place voice traffic in its priority class"],
  actions: [{ action: "add_rule", name: "Voice priority", class_name: "voice" }], problems: [], results: [],
  created_at: "2026-11-01T10:00:00Z", applied_by: null, undone_by: null,
  revision, approval: null, can_execute: false, expires_at: new Date(Date.now() + 1_800_000).toISOString(),
};
const applied: Plan = { ...draft, id: 2, status: "applied", question: "What changed yesterday?",
  results: [{ action: 0, message: "Voice routing rule was applied", order_id: 9 }], applied_by: "user:operator@example.org" };

function context(): AssistantContext {
  const limited = auth.user?.org_role === "viewer" || !!auth.user?.scopes && !auth.user.scopes.includes("connect");
  return {
    account: { customer_id: "org-a", organisation: "Coral", role: auth.user?.org_role ?? "member", products: ["connect"], accessible_products: ["connect"], subscriptions: [] },
    tools: { connect_read: true, connect_changes: !limited, jibsy_changes: false, subscription_changes: false, jibsy_diagnostics_path: null },
    upgrade_guidance: { path: "/account/apps", quote_path: "/quotes", can_request: false, policy: "Separate review required" },
    generated_at: new Date().toISOString(), sources: ["organisation_access", "active_product_catalogue"], ask_enabled: true, model: "Test assistant",
  };
}

function responses(options: { context?: AssistantContext; plan?: Plan; failExecutionOnce?: boolean; failedContext?: boolean } = {}) {
  let plan = options.plan ?? draft;
  let attempts = 0;
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    let data: unknown = [];
    if (url.includes("/assistant/context")) {
      if (options.failedContext) return new Response(JSON.stringify({ detail: "Account context unavailable" }), { status: 503 });
      data = options.context ?? context();
    } else if (url.endsWith("/assistant/ask") && !url.includes("/commai/")) data = { answer: "Voice stayed on the measured fibre connection.", plan, based_on: { sites: 2, decisions: 3 } };
    else if (url.endsWith("/assistant/plans/1/approve")) {
      plan = { ...plan, approval: { revision: plan.revision!, approved_by: "user:person@example.org", approved_at: new Date().toISOString() }, can_execute: true };
      data = plan;
    } else if (url.endsWith("/assistant/plans/1/execute")) {
      attempts += 1;
      if (options.failExecutionOnce && attempts === 1) return new Response(JSON.stringify({ detail: "Connection interrupted. Retry safely." }), { status: 503 });
      plan = { ...plan, status: "applied", results: [{ action: 0, message: "Voice priority applied" }], can_execute: false };
      data = plan;
    } else if (url.endsWith("/assistant/plans/1/cancel")) { plan = { ...plan, status: "cancelled" }; data = plan; }
    else if (/\/assistant\/plans\?/.test(url)) data = [plan, applied];
    else if (url.endsWith("/assistant/plans/1")) data = plan;
    else if (url.endsWith("/commai/customers/org-a/assistant/ask")) data = {
      id: "answer-1", question: "Why are bookings failing?", answer: "The bookings workflow is paused.", confidence: "confirmed", source: "checks",
      findings: [{ area: "workflow", status: "problem", confidence: "confirmed", summary: "Bookings is paused", evidence: ["Workflow has been disabled"], fix: { id: "resume", label: "Resume bookings" } }],
      fixes: [{ id: "fix-1", fix_id: "resume", label: "Resume bookings", status: "proposed", appliable: true }],
    };
    else if (url.endsWith("/assistant/fixes/fix-1/apply")) data = { id: "fix-1", fix_id: "resume", label: "Resume bookings", status: "applied", result: { resolved: true }, message: "The check now passes." };
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}
function show(path = "/assistant?scope=connect") { return render(<MemoryRouter initialEntries={[path]}><Ask /></MemoryRouter>); }
function posts(fetch: ReturnType<typeof responses>, suffix: string) {
  return fetch.mock.calls.filter(([url, init]) => String(url).endsWith(suffix) && init?.method === "POST");
}

beforeEach(() => { auth.user = { email: "person@example.org", role: "customer", customer_id: "org-a", org_role: "member" }; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each([
  ["viewer", { org_role: "viewer" as const }],
  ["read-scoped owner", { org_role: "owner" as const, scopes: ["connect:read"] }],
])("%s assistant access", (_label, access) => {
  it("supports questions and proposal history without mutation controls", async () => {
    auth.user = { ...auth.user!, ...access };
    const fetch = responses();
    show();
    await screen.findByText("Place voice traffic in its priority class");
    expect(screen.getByRole("heading", { level: 1, name: "XaKore assistant" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Why did voice move today?" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Are we heading for burst charges this month?" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Put Zoom|Approve this revision|Apply approved|Don't apply|Undo/ })).toBeNull();
    expect(screen.getByText("Voice routing rule was applied")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Review the order" }).getAttribute("href")).toBe("/order");
    fireEvent.change(screen.getByRole("textbox", { name: "Your question" }), { target: { value: "Where did voice run today?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Voice stayed on the measured fibre connection.");
    expect(JSON.parse(String(posts(fetch, "/assistant/ask")[0][1]?.body))).toMatchObject({ scope: "connect", customer_id: "org-a", question: "Where did voice run today?" });
    expect(fetch.mock.calls.some(([url, init]) => /\/plans\/\d+\//.test(String(url)) && init?.method === "POST")).toBe(false);
  });
});

describe("reviewed proposal execution", () => {
  it("requires separate approval and application of exactly the displayed revision", async () => {
    const fetch = responses();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Approve this revision" }));
    const apply = await screen.findByRole("button", { name: "Apply approved changes" });
    expect(posts(fetch, "/assistant/plans/1/execute")).toHaveLength(0);
    expect(JSON.parse(String(posts(fetch, "/assistant/plans/1/approve")[0][1]?.body))).toEqual({ revision });
    fireEvent.click(apply);
    await screen.findByText("Voice priority applied");
    expect(JSON.parse(String(posts(fetch, "/assistant/plans/1/execute")[0][1]?.body))).toMatchObject({ revision, idempotency_key: expect.any(String) });
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/ai/plans/1/apply"))).toBe(false);
  });

  it("retains the same execution key when a response fails and the person retries", async () => {
    const fetch = responses({ failExecutionOnce: true });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Approve this revision" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply approved changes" }));
    await screen.findByText("Connection interrupted. Retry safely.");
    fireEvent.click(screen.getByRole("button", { name: "Apply approved changes" }));
    await screen.findByText("Voice priority applied");
    const calls = posts(fetch, "/assistant/plans/1/execute");
    expect(calls).toHaveLength(2);
    expect(calls[0][1]?.body).toEqual(calls[1][1]?.body);
  });

  it("does not enable an expired proposal or treat a different revision as approved", async () => {
    const fetch = responses({ plan: { ...draft, expires_at: "2020-01-01T00:00:00Z", approval: { revision: "different", approved_by: "user:person@example.org", approved_at: "2020-01-01T00:00:00Z" } } });
    show();
    expect((await screen.findByRole("button", { name: "Approve this revision" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Apply approved changes" })).toBeNull();
    expect(posts(fetch, "/assistant/plans/1/approve")).toHaveLength(0);
  });

  it("prefills a linked question without sending or approving it", async () => {
    const fetch = responses();
    show("/assistant?scope=connect&q=yes%2C%20apply%20it");
    expect((await screen.findByRole("textbox", { name: "Your question or request" }) as HTMLTextAreaElement).value).toBe("yes, apply it");
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
});

describe("shared account and Jibsy guidance", () => {
  it("lets a Jibsy-only member ask account questions without calling network or administrator tools", async () => {
    auth.user = { ...auth.user!, products: ["commai"], apps: ["commai"] };
    const c = context();
    c.account.products = ["commai"];
    c.account.accessible_products = ["commai"];
    c.tools.connect_read = false;
    c.tools.connect_changes = false;
    const fetch = responses({ context: c });
    show("/assistant");
    await screen.findByRole("textbox", { name: "Your account question" });
    expect(screen.queryByRole("button", { name: "Connect network" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Jibsy checks" })).toBeNull();
    expect(screen.getByText("Not on your plan")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "What is included in our plan?" }));
    await screen.findByText("Voice stayed on the measured fibre connection.");
    expect(JSON.parse(String(posts(fetch, "/assistant/ask")[0][1]?.body)).scope).toBe("account");
    expect(fetch.mock.calls.some(([url]) => /\/ai\/|\/assistant\/plans|\/commai\//.test(String(url)))).toBe(false);
  });

  it.each(["connect", "jibsy"])("blocks a direct %s tool bookmark when context does not permit it", async (scope) => {
    const c = context(); c.tools.connect_read = false; c.tools.connect_changes = false;
    const fetch = responses({ context: c });
    show(`/assistant?scope=${scope}`);
    await screen.findByRole("button", { name: "Ask about this account" });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(fetch.mock.calls.some(([url]) => /\/assistant\/plans|\/commai\//.test(String(url)))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Ask about this account" }));
    expect(await screen.findByRole("textbox", { name: "Your account question" })).toBeTruthy();
  });

  it("embeds existing Jibsy evidence and repairs only after administrator tools are selected", async () => {
    const c = context(); c.tools.jibsy_diagnostics_path = "/assistant?scope=jibsy";
    const fetch = responses({ context: c });
    show("/assistant");
    fireEvent.click(await screen.findByRole("button", { name: "Jibsy checks" }));
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    fireEvent.click(await screen.findByRole("button", { name: "Why are bookings failing?" }));
    await screen.findByText("The bookings workflow is paused.");
    expect(screen.getByText("Workflow has been disabled")).toBeTruthy();
    expect(posts(fetch, "/assistant/fixes/fix-1/apply")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Approve and apply: Resume bookings" }));
    await screen.findByText("The check now passes.");
    expect(posts(fetch, "/assistant/fixes/fix-1/apply")).toHaveLength(1);
  });

  it("fails closed when account context cannot be checked", async () => {
    const fetch = responses({ failedContext: true });
    show();
    await screen.findByText("Account context unavailable");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(fetch.mock.calls.some(([url]) => /\/assistant\/plans|\/commai\//.test(String(url)))).toBe(false);
  });

  it("shows an unavailable reason without enabling requests", async () => {
    const c = context(); c.ask_enabled = false; c.ask_unavailable_reason = "The AI service is not configured.";
    const fetch = responses({ context: c });
    show("/assistant");
    await screen.findByText("The AI service is not configured.");
    expect((screen.getByRole("textbox", { name: "Your account question" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
});

describe("one assistant entry", () => {
  it("preserves existing bookmarked questions and selected tool", () => {
    expect(assistantAddress("?q=What%20changed%3F", "connect")).toBe("/assistant?q=What+changed%3F&scope=connect");
    expect(assistantAddress("?q=Why%3F", "jibsy")).toBe("/assistant?q=Why%3F&scope=jibsy");
  });
  it("uses the same destination in both app menus and account pages", () => {
    for (const app of ["connect", "commai"]) {
      const items = navGroups("customer", null, [app], { ask: false }).flatMap((g) => g.items);
      expect(items.filter((i) => i.to === "/assistant")).toHaveLength(1);
      expect(items.some((i) => i.to === "/ask" || i.to === "/commai/assistant")).toBe(false);
    }
    expect(accountGroups({ carrier: false, jibsy: false }).flatMap((g) => g.items).some((i) => i.to === "/assistant")).toBe(true);
    expect(navGroups("carrier").flatMap((g) => g.items).some((i) => i.to === "/assistant")).toBe(false);
  });
});
