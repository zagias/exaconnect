// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import Traffic from "../pages/Traffic";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "selected-org", name: "Coral", sites: [], auto_prioritise: false }, reload: vi.fn() }),
  who: (actor: string) => actor,
}));

const configured = Array.from({ length: 20 }, (_, i) => ({ id: `site-${i}`, name: `Branch ${i}`, kind: "site", node_id: null, last_seen: null, online: null }));
const applied = { id: 1, site: "Branch 1", label: "Teams", current_class: "business", suggested_class: "voice", confidence: 0.9,
  status: "applied", reason: "Recognised calls", first_seen: new Date().toISOString(), last_seen: new Date().toISOString() };
function responses(options: { sites?: unknown[]; failedList?: boolean; failedOverview?: boolean; pending?: boolean } = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.includes("/applications") && options.pending) return new Promise<Response>(() => {});
    if (path.includes("/applications") && options.failedList)
      return new Response(JSON.stringify({ detail: "Application service unavailable" }), { status: 503 });
    if (path.includes("/overview") && options.failedOverview)
      return new Response(JSON.stringify({ detail: "Overview unavailable" }), { status: 503 });
    const data = path.includes("/overview") ? { sites: options.sites ?? configured }
      : path.includes("include_closed=true") ? [applied] : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}
function show() {
  return render(<MemoryRouter initialEntries={["/traffic/applications"]}><Routes><Route path="/traffic/*" element={<Traffic />} /></Routes></MemoryRouter>);
}

beforeEach(() => {
  auth.user = { email: "operator@example.org", role: "customer", customer_id: "selected-org", org_role: "member" };
  HTMLElement.prototype.scrollTo = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("application suggestions with missing data", () => {
  it("does not turn a pending request into an empty or healthy state", () => {
    const fetch = responses({ pending: true });
    show();
    expect(screen.getByText("Loading application suggestions…")).toBeTruthy();
    expect(screen.queryByText("No suggestions to review")).toBeNull();
    expect(screen.queryByText("No box reports yet")).toBeNull();
    expect(fetch.mock.calls.some(([path]) => String(path).includes("/overview"))).toBe(false);
  });

  it("shows the application error without claiming the network has nothing to suggest", async () => {
    const fetch = responses({ failedList: true });
    show();
    await screen.findByText("Application service unavailable");
    expect(screen.queryByText("No suggestions to review")).toBeNull();
    expect(screen.queryByText("No box reports yet")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(fetch.mock.calls.some(([path]) => String(path).includes("/overview"))).toBe(false);
  });

  it("identifies configured sites that have never reported and offers the site connection path", async () => {
    const fetch = responses();
    show();
    await screen.findByText("No box reports yet");
    expect(screen.getByText(/20 sites are configured/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Check site connections" }).getAttribute("href")).toBe("/sites");
    expect(screen.getByRole("link", { name: "Set a traffic rule" }).getAttribute("href")).toBe("/traffic/rules");
    expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/overview?customer_id=selected-org")).toBe(true);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST" || init?.method === "PATCH")).toBe(false);
  });

  it("keeps box heartbeat evidence distinct from application traffic evidence", async () => {
    responses({ sites: [{ ...configured[0], node_id: "box-1", last_seen: new Date().toISOString(), online: true }] });
    show();
    await screen.findByText("1 of 1 site reporting to Connect.");
    expect(screen.getByText("No suggestions to review")).toBeTruthy();
    expect(screen.getByText(/does not confirm that application traffic has been reported/)).toBeTruthy();
    expect(screen.queryByText("No box reports yet")).toBeNull();
    expect(screen.queryByText(/Everything is prioritised|Nothing to suggest|Sites report what they send every minute/)).toBeNull();
  });

  it("points an organisation without sites to the actual connection flow", async () => {
    responses({ sites: [] });
    show();
    await screen.findByText("Connect a site to get started");
    expect(screen.getByRole("link", { name: "Connect a site" }).getAttribute("href")).toBe("/locations");
  });

  it("distinguishes stopped box reports from a first report that never arrived", async () => {
    responses({ sites: [{ ...configured[0], node_id: "box-1", last_seen: "2020-01-01T00:00:00Z", online: false }] });
    show();
    await screen.findByText("Box reports are out of date");
    expect(screen.getByText(/Last box report/)).toBeTruthy();
    expect(screen.queryByText("No box reports yet")).toBeNull();
  });

  it("does not invent site reporting when the evidence request fails", async () => {
    responses({ failedOverview: true });
    show();
    await screen.findByText(/Site reporting could not be checked/);
    expect(screen.queryByText("No box reports yet")).toBeNull();
    expect(screen.queryByText("Connect a site to get started")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("preserves read-only access on the compact controls and empty-state actions", async () => {
    auth.user = { ...auth.user!, org_role: "viewer" };
    responses();
    show();
    await screen.findByText("No box reports yet");
    expect(screen.getByText("Automatic application priorities: Off")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Check now" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Set a traffic rule" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /Prioritise known/ })).toBeNull();
  });

  it("reveals actual applied records using the existing history filter", async () => {
    const fetch = responses();
    show();
    await screen.findByText("No box reports yet");
    fireEvent.click(screen.getByRole("checkbox", { name: "Include applied and dismissed" }));
    await screen.findByText(/Teams: business/);
    expect(screen.getByText("Applied")).toBeTruthy();
    expect(screen.queryByText("No box reports yet")).toBeNull();
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/customers/selected-org/applications?include_closed=true")).toBe(true));
  });
});
