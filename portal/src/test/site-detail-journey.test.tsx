// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SiteDetail, User } from "../api";
import { SitePage } from "../pages/Sites";

const auth = vi.hoisted(() => ({ user: null as User | null, storm: vi.fn() }));
const current = { id: "org-a", name: "Coral", sites: [{ id: "site-a", name: "Head office", storm_mode: false }] };
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current }),
  useStormToggle: () => ({ current, busy: false, error: null, toggle: auth.storm }),
  who: (actor: string | null) => actor ?? "unknown",
}));
vi.mock("../components", async (original) => ({
  ...await original<typeof import("../components")>(),
  LineChart: ({ title }: { title: string }) => <figure aria-label={title}><figcaption>{title}</figcaption></figure>,
}));

function site(overrides: Partial<SiteDetail> = {}): SiteDetail {
  const now = new Date().toISOString();
  return { id: "site-a", name: "Head office", kind: "site", location: "Port of Spain", customer_id: "org-a",
    node_id: "box-a", node_name: "Office box", last_seen: now, online: true, asn: 65001, lan_prefixes: ["192.168.10.0/24"],
    applied_version: 4, desired_version: 4, apply_ok: true, apply_error: null, agent_version: "1.2.0",
    paths: [{ path: "carrier-a", label: "Office fibre", carrier: "Carrier A", underlay_type: "fibre", commit_mbps: 100,
      rtt_avg_ms: 20, jitter_ms: 2, loss_pct: 0, sent: 30, received: 30, at: now, health: "ok" }],
    slas: [{ class_name: "voice", max_latency_ms: 100, max_jitter_ms: 20, max_loss_pct: 1, allow_satellite: false }],
    steering: [{ class_name: "voice", intended: "carrier-a", intended_label: "Office fibre", since: now,
      actual: "carrier-a", actual_label: "Office fibre", paused: false, failover: false, reported_at: now, last_reason: "Kept the measured connection" }],
    tunnels: [{ tunnel: "wg-office", path: "carrier-a", handshake_age_s: 20, bfd: "up", updated_at: now }],
    box: { console: true, admin_set: true, url: "http://192.168.10.1", ports: [{ name: "eth0" }, { name: "eth1" }],
      wan: [{ iface: "eth0", path: "carrier-a", mode: "dhcp" }], lan: { iface: "eth1", mode: "static", address: "192.168.10.1/24" } },
    box_managed: { mode: "cloud", at: null, by: null }, ...overrides };
}
function show(overrides: Partial<SiteDetail> = {}) {
  const data = site(overrides);
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    const body = url.includes("/metrics?") ? { points: [] }
      : url.includes("/events?") ? [{ time: new Date().toISOString(), kind: "config_applied", detail: null, node: "box-a" }]
      : data;
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  const view = render(<MemoryRouter initialEntries={["/sites/site-a"]}><Routes><Route path="/sites/:id" element={<SitePage />} /></Routes></MemoryRouter>);
  return { ...view, fetch };
}
function disclosure(title: string): HTMLDetailsElement {
  return screen.getByText(title, { selector: "summary" }).closest("details")!;
}

beforeEach(() => { auth.user = { email: "owner@example.org", role: "customer", customer_id: "org-a", org_role: "owner" }; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("site performance journey", () => {
  it("puts performance, current traffic and activity before collapsed box and technical details", async () => {
    const { fetch } = show();
    const title = await screen.findByRole("heading", { level: 1, name: "Head office" });
    const header = title.closest(".page-head")!;
    expect(within(header as HTMLElement).getByText("Port of Spain")).toBeTruthy();
    expect(within(header as HTMLElement).getByText("Within SLA")).toBeTruthy();
    expect(within(header as HTMLElement).getByText(/Last report/)).toBeTruthy();
    expect(header.textContent).not.toMatch(/65001|192\.168\.10|configuration version|Managed from/i);
    expect(screen.getAllByRole("heading", { level: 2 }).filter((heading) => !heading.closest("details")).map((heading) => heading.textContent)).toEqual(["Connections", "Current traffic", "Recent activity"]);
    expect(screen.getByText("Kept the measured connection")).toBeTruthy();
    expect(screen.getByText("Config applied")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Routing history at this site" }).getAttribute("href")).toBe("/decisions?site_id=site-a");
    expect(disclosure("Box settings").open).toBe(false);
    expect(disclosure("Technical details").open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "1 hour" }));
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).includes("/sites/site-a/metrics?minutes=60"))).toBe(true));
    fireEvent.click(disclosure("Technical details").querySelector("summary")!);
    expect(disclosure("Technical details").open).toBe(true);
    expect(screen.getByRole("heading", { name: "Tunnel diagnostics" })).toBeTruthy();
    expect(screen.getByText("65001")).toBeTruthy();
    expect(screen.getByText("192.168.10.0/24")).toBeTruthy();
  });

  it("preserves owner box management and port editing inside the secondary disclosure", async () => {
    show();
    await screen.findByRole("heading", { name: "Head office" });
    fireEvent.click(disclosure("Box settings").querySelector("summary")!);
    const box = disclosure("Box settings");
    expect(within(box).getByRole("group", { name: "Managed from" })).toBeTruthy();
    fireEvent.click(within(box).getByRole("button", { name: "Change the ports" }));
    expect(within(box).getByRole("button", { name: "Send to the box" })).toBeTruthy();
    expect(within(box).getAllByRole("combobox").length).toBeGreaterThan(0);
  });

  it.each([
    ["viewer", { org_role: "viewer" as const }],
    ["read-scoped owner", { scopes: ["connect:read"] }],
  ])("keeps %s diagnostics readable without operational controls", async (_label, access) => {
    auth.user = { ...auth.user!, ...access };
    show();
    await screen.findByRole("heading", { name: "Head office" });
    expect(screen.getByRole("region", { name: "Connections" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Switch Storm Mode/ })).toBeNull();
    fireEvent.click(disclosure("Box settings").querySelector("summary")!);
    const box = disclosure("Box settings");
    expect(within(box).getByRole("heading", { name: "Ports on this box" })).toBeTruthy();
    expect(within(box).queryByRole("button", { name: "Change the ports" })).toBeNull();
    expect(within(box).getAllByRole("button").every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
  });

  it("labels a reporting hub plainly without inventing missing connection measurements", async () => {
    show({ kind: "pop", name: "Caribbean hub", paths: [], steering: [] });
    const title = await screen.findByRole("heading", { level: 1, name: "Caribbean hub" });
    const header = title.closest(".page-head")!;
    expect(within(header as HTMLElement).getByText("Network hub")).toBeTruthy();
    expect(within(header as HTMLElement).getByText("Online")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Connections" })).toBeNull();
    expect(screen.getByRole("region", { name: "Recent activity" })).toBeTruthy();
  });
});
