// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Ask from "../pages/Ask";
import { AuthGate } from "../auth";
import type { AssistantContext } from "../pages/assistant/types";

const organisation = vi.hoisted(() => ({ current: { id: "org-a", name: "Coral" } }));
vi.mock("../customer", () => ({ useCustomer: () => organisation }));
const brief = { format: "xakore-planning-brief", version: 1, offer: "Connect", facts: [{ label: "Sites", value: "Twenty shops" }], gaps: ["Independent backup?"] };

function context(): AssistantContext {
  return { account: { customer_id: organisation.current.id, organisation: organisation.current.name, role: "owner", products: ["connect"], accessible_products: ["connect"], subscriptions: [] },
    tools: { connect_read: true, connect_changes: true, jibsy_changes: false, subscription_changes: false, jibsy_diagnostics_path: null },
    upgrade_guidance: { path: "/account/apps", quote_path: "/quotes", can_request: false, policy: "Review" },
    generated_at: new Date().toISOString(), sources: ["organisation_access"], ask_enabled: true, model: "Fixture" };
}
function response(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }); }
function mockSession() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return response({ email: "owner@example.invalid", role: "customer", customer_id: organisation.current.id, org_role: "owner" });
    if (url.includes("/assistant/context")) return response(context());
    if (url.endsWith("/assistant/ask")) return response({ answer: "Review the independent backup requirement.", model: "fixture", plan: null });
    return response([]);
  });
}
function app() { return <MemoryRouter initialEntries={["/assistant?from=website"]}><AuthGate><Ask /></AuthGate></MemoryRouter>; }

beforeEach(() => { organisation.current = { id: "org-a", name: "Coral" }; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("website to signed-in Hub", () => {
  it("does not send notes until the visitor reviews the organisation and submits a question", async () => {
    const fetch = mockSession(); render(app());
    fireEvent.change(await screen.findByLabelText("Paste your planning brief"), { target: { value: JSON.stringify(brief) } });
    fireEvent.click(screen.getByRole("button", { name: "Review notes" }));
    fireEvent.click(screen.getByRole("button", { name: "Use these notes for Coral" }));
    expect(fetch.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(0);
    expect(screen.getByText("Planning brief attached")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Review the independent backup requirement.");
    const posts = fetch.mock.calls.filter(([, i]) => i?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ customer_id: "org-a", scope: "connect", brief });
    expect(String(posts[0][0])).toBe("/api/v1/assistant/ask");
    expect(window.location.href).not.toContain("Twenty");
  });

  it("clears imported notes and the prepared question when the organisation changes", async () => {
    const fetch = mockSession(); const view = render(app());
    fireEvent.change(await screen.findByLabelText("Paste your planning brief"), { target: { value: JSON.stringify(brief) } });
    fireEvent.click(screen.getByRole("button", { name: "Review notes" }));
    fireEvent.click(screen.getByRole("button", { name: "Use these notes for Coral" }));
    organisation.current = { id: "org-b", name: "Seagrape" };
    view.rerender(app());
    await screen.findByText(/Review them for Seagrape/);
    expect(screen.queryByText("Planning brief attached")).toBeNull();
    expect((screen.getByRole("textbox", { name: "Your question or request" }) as HTMLTextAreaElement).value).toBe("");
    expect(fetch.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(0);
  });
});

describe("shared Hub entry", () => {
  it("uses shared Hub branding when signed out", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/auth/me") ? response({}, 401) : response({ providers: [] }));
    render(<AuthGate><div>Account content</div></AuthGate>);
    expect(await screen.findByText("Sign in to XaKore Hub")).toBeTruthy();
    expect(screen.queryByText("Your Connect account")).toBeNull();
  });

  it("offers session retry for a server error rather than asking for credentials again", async () => {
    let down = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => down ? response({ detail: "Temporary interruption" }, 503) : response({ email: "member@example.invalid", role: "customer" }));
    render(<AuthGate><div>Account content</div></AuthGate>);
    expect(await screen.findByText(/couldn't check your session/)).toBeTruthy();
    expect(screen.queryByLabelText("Work email")).toBeNull();
    down = false; fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("Account content")).toBeTruthy());
  });
});

