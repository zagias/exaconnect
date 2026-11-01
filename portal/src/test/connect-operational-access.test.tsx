// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Insight, User } from "../api";
import { Classes } from "../pages/Classes";
import Insights, { InsightList, InsightSummary } from "../pages/Insights";
import Traffic from "../pages/Traffic";

const auth = vi.hoisted(() => ({ user: null as User | null, storm: vi.fn() }));
const organisation = {
  id: "org-a", name: "Coral", auto_prioritise: false,
  sites: [{ id: "site-a", name: "Head office", storm_mode: false }],
};

vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: organisation, reload: vi.fn() }),
  useStormToggle: () => ({ current: organisation, busy: false, error: null, toggle: auth.storm }),
  who: (actor: string) => actor.replace(/^user:/, ""),
}));

const watch: Insight = {
  id: 10, customer_id: "org-a", kind: "storm_warning", severity: "warning", title: "Weather warning",
  detail: "Check the backup connection at Head office.", site: "Head office", example: false,
  first_seen: "2026-11-01T10:00:00Z", last_seen: "2026-11-01T10:00:00Z",
  resolved_at: null, acknowledged_by: null, acknowledged_at: null, path: null, carrier: null,
  data: { sites: [{ name: "Head office", suggest_storm_mode: true }] },
};

function responses() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const data = init?.method === "POST" ? {}
      : path.includes("/applications/catalogue") ? []
      : path.includes("/applications") ? [{ id: 1, site: "Head office", label: "Teams", current_class: "normal", suggested_class: "voice", confidence: 0.95, status: "suggested", reason: "Recognised call traffic", first_seen: watch.first_seen, last_seen: watch.last_seen }]
      : path.includes("/rules") ? [{ id: 2, name: "ERP rule", class_name: "payments", site_ids: [], apps: [], ports: "tcp:443", dst_subnets: [], src_subnets: [], vlans: [], domains: [], dscp: [], enabled: true, source: "customer", created_by: "user:ops@example.org" }]
      : path.includes("/classes") ? [{ name: "payments", description: "Payment services", priority: "interactive", preferred_path: null, dscp: [], ports: "", subnets: [], max_latency_ms: 100, max_jitter_ms: null, max_loss_pct: 1, allow_satellite: true, builtin: false }]
      : path.includes("/insights") ? [watch] : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}

function traffic(section: string) {
  return render(<MemoryRouter initialEntries={[`/traffic/${section}`]}><Routes><Route path="/traffic/*" element={<Traffic />} /></Routes></MemoryRouter>);
}

beforeEach(() => {
  auth.user = { email: "person@example.org", role: "customer", customer_id: "org-a", org_role: "member" };
  auth.storm.mockReset();
  HTMLElement.prototype.scrollTo = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each([
  ["viewer", { org_role: "viewer" as const }],
  ["read-scoped member", { scopes: ["connect:read"] }],
])("%s network access", (_label, access) => {
  beforeEach(() => { auth.user = { ...auth.user!, ...access }; responses(); });

  it("keeps application suggestions and automatic-priority state readable without write controls", async () => {
    traffic("applications");
    await waitFor(() => expect(screen.getByText(/Teams: normal/)).toBeTruthy());
    expect(screen.getByText("Automatic application priorities: Off")).toBeTruthy();
    expect(screen.getByText("Confidence 95%")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Check now|Put it in|Dismiss/ })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /Prioritise known/ })).toBeNull();
  });

  it("shows traffic rules and classes without add, edit or delete actions", async () => {
    traffic("rules");
    await waitFor(() => expect(screen.getByText("ERP rule")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Add a rule|Edit|actions/i })).toBeNull();
    cleanup();
    render(<Classes />);
    await waitFor(() => expect(screen.getByText("Payment services")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Add a class|Edit|actions/i })).toBeNull();
    expect(screen.getByText("100 ms")).toBeTruthy();
  });

  it("keeps alert details accessible while hiding acknowledge and Storm Mode actions", async () => {
    render(<MemoryRouter><ul><InsightSummary items={[watch]} reload={vi.fn()} /></ul></MemoryRouter>);
    expect(screen.queryByRole("button", { name: /Acknowledge|Storm Mode/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.getByText(watch.detail)).toBeTruthy();
    cleanup();
    render(<MemoryRouter><InsightList items={[{ ...watch, acknowledged_by: "user:ops@example.org" }]} reload={vi.fn()} /></MemoryRouter>);
    expect(screen.queryByRole("button", { name: /Acknowledge|Storm Mode/ })).toBeNull();
    expect(screen.getByText("Acknowledged by ops@example.org")).toBeTruthy();
  });
});

describe("member operational access", () => {
  it("preserves application actions and traffic-rule editing", async () => {
    responses();
    traffic("applications");
    await waitFor(() => expect(screen.getByRole("button", { name: "Put it in voice" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "Check now" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: /Prioritise known/ })).toBeTruthy();
    cleanup();
    traffic("rules");
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit ERP rule" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "Add a rule" })).toBeTruthy();
    cleanup();
    render(<Classes />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit payments" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "Add a class" })).toBeTruthy();
  });

  it("permits real alert actions and honours an explicit read-only summary", async () => {
    const fetch = responses();
    render(<MemoryRouter><InsightList items={[watch]} reload={vi.fn()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Switch Storm Mode on for Head office" }));
    expect(auth.storm).toHaveBeenCalledWith(organisation.sites[0], true);
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(fetch.mock.calls.some(([path, init]) => String(path).endsWith("/insights/10/acknowledge") && init?.method === "POST")).toBe(true));
    cleanup();
    render(<MemoryRouter><ul><InsightSummary items={[watch]} reload={vi.fn()} readOnly /></ul></MemoryRouter>);
    expect(screen.queryByRole("button", { name: /Acknowledge|Storm Mode/ })).toBeNull();
  });

  it("does not expose example-alert generators even to staff", async () => {
    auth.user = { email: "staff@example.org", role: "admin", customer_id: null };
    responses();
    render(<MemoryRouter><Insights /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(watch.title)).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Show example alerts|Clear example alerts/ })).toBeNull();
  });
});
