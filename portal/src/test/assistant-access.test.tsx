// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import Ask from "../pages/Ask";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Coral" } }) }));

const draft = { id: 1, status: "draft", question: "Give voice priority", summary: ["Place voice traffic in its priority class"],
  problems: [], results: [], created_at: "2026-11-01T10:00:00Z", applied_by: null, undone_by: null };
const applied = { ...draft, id: 2, status: "applied", question: "What changed yesterday?",
  results: [{ action: 0, message: "Voice routing rule was applied", order_id: 9 }], applied_by: "user:operator@example.org" };

function responses() {
  let plans = [draft, applied];
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    let data: unknown = [];
    if (url.endsWith("/ai/status")) data = { ask_enabled: true, model: "Test assistant" };
    else if (url.includes("/assistant/plans")) data = plans;
    else if (url.endsWith("/ai/ask")) data = { answer: "Voice stayed on the measured fibre connection.", plan: draft };
    else if (url.endsWith("/ai/plans/1/apply") && init?.method === "POST") {
      plans = [{ ...draft, status: "applied", results: [] }, applied];
      data = plans[0];
    }
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}
function show() { return render(<MemoryRouter><Ask /></MemoryRouter>); }

beforeEach(() => { auth.user = { email: "person@example.org", role: "customer", customer_id: "org-a", org_role: "member" }; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each([
  ["viewer", { org_role: "viewer" as const }],
  ["read-scoped owner", { org_role: "owner" as const, scopes: ["connect:read"] }],
])("%s assistant access", (_label, access) => {
  it("supports questions and proposal history without commands or plan mutation controls", async () => {
    auth.user = { ...auth.user!, ...access };
    const fetch = responses();
    show();
    await screen.findByText("Place voice traffic in its priority class");
    expect(screen.getByRole("heading", { level: 1, name: "Ask about your network" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Why did voice move today?" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Are we heading for burst charges this month?" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Put Zoom|Fix it|Apply changes|Don't apply|Undo/ })).toBeNull();
    expect(screen.getByText("Voice routing rule was applied")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Review the order" }).getAttribute("href")).toBe("/order");
    fireEvent.change(screen.getByRole("textbox", { name: "Your question" }), { target: { value: "Where did voice run today?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Voice stayed on the measured fibre connection.");
    expect(screen.queryByRole("button", { name: /Apply changes|Don't apply|Undo/ })).toBeNull();
    expect(fetch.mock.calls.some(([url, init]) => String(url).endsWith("/ai/ask") && init?.method === "POST"
      && JSON.parse(String(init.body)).question === "Where did voice run today?")).toBe(true);
    expect(fetch.mock.calls.some(([url, init]) => /\/ai\/plans\/\d+\//.test(String(url)) && init?.method === "POST")).toBe(false);
  });
});

describe("member assistant access", () => {
  it("retains command suggestions and authorised plan actions", async () => {
    const fetch = responses();
    show();
    await screen.findByRole("button", { name: "Apply changes" });
    expect(screen.getByRole("button", { name: "Put Zoom, Teams and Webex in voice at every site" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Don't apply" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(fetch.mock.calls.some(([url, init]) => String(url).endsWith("/ai/plans/1/apply") && init?.method === "POST")).toBe(true));
  });
});
