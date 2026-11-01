// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Overview as OverviewData, OverviewSite, PathRow } from "../api";
import Overview from "../pages/Overview";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "customer", org_role: "viewer", customer_id: "org-a" } }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "org-a", name: "Coral", sites: [] } }),
  useStormToggle: () => ({ current: { sites: [] }, busy: false, error: null, toggle: vi.fn() }),
  who: (actor: string | null) => actor ?? "unknown",
}));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function path(overrides: Partial<PathRow> = {}): PathRow {
  return { path: "fibre", label: "Fibre", carrier: "Carrier A", underlay_type: "fibre", commit_mbps: 100,
    rtt_avg_ms: 20, jitter_ms: 1, loss_pct: 0, sent: 30, received: 30,
    at: new Date().toISOString(), health: "ok", ...overrides };
}
function site(number: number, overrides: Partial<OverviewSite> = {}): OverviewSite {
  return { id: `site-${number}`, name: `Office ${number}`, kind: "site", location: "Port of Spain",
    node_id: `box-${number}`, last_seen: new Date().toISOString(), online: true, apply_ok: true,
    applied_version: 2, desired_version: 2, storm_mode: false, steering: [], paths: [path()], ...overrides };
}
function network(sites: OverviewSite[]): OverviewData {
  return { sites, attention: [], sla_24h: [], sla_target_pct: 99.5, moves_24h: 0, recent_decisions: [] };
}
function show(sites: OverviewSite[], responses: { insights?: () => Promise<Response>; overview?: () => Promise<Response> } = {}) {
  const overview = network(sites);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/insights") && responses.insights) return responses.insights();
    if (url.endsWith("/overview") && responses.overview) return responses.overview();
    const data = url.endsWith("/overview") ? overview : url.endsWith("/version") ? { version: "test" } : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  return render(<MemoryRouter><Overview /></MemoryRouter>);
}

describe("overview distinguishes absent measurements from healthy service", () => {
  it.each([
    ["offline box", { online: false }, "Your sites are not reporting"],
    ["zero paths", { paths: [] }, "1 issue needs attention"],
    ["zero probes", { paths: [path({ sent: 0, received: 0, health: "bad" })] }, "1 issue needs attention"],
    ["zero probes with a stale healthy flag", { paths: [path({ sent: 0, received: 0 })] }, "1 issue needs attention"],
    ["partial measurements", { paths: [path(), path({ path: "backup", sent: null, received: null })] }, "1 issue needs attention"],
  ] as [string, Partial<OverviewSite>, string][])("does not give %s a healthy headline or a green all-clear", async (_label, overrides, heading) => {
    show([site(1, overrides)]);
    await screen.findByRole("heading", { level: 1, name: heading });
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(screen.queryByRole("heading", { level: 1, name: "All sites within SLA" })).toBeNull();
    expect(within(attention).queryByText("Nothing open")).toBeNull();
    expect(attention.querySelector(".pill.ok")).toBeNull();
    expect(within(attention).queryByText(/All measured connections are within SLA/)).toBeNull();
  });

  it("only shows a healthy all-clear when the enrolled site is reporting measured paths", async () => {
    show([site(1)]);
    await screen.findByRole("heading", { level: 1, name: "All sites within SLA" });
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("Nothing open").classList.contains("ok")).toBe(true);
    expect(within(attention).getByText(/All measured connections are within SLA/)).toBeTruthy();
  });

  it("does not let one measured site mask another reporting site with no paths", async () => {
    show([site(1), site(2, { paths: [] })]);
    await screen.findByRole("heading", { level: 1, name: "1 issue needs attention" });
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(screen.queryByRole("heading", { level: 1, name: "All sites within SLA" })).toBeNull();
    expect(within(attention).queryByText("Nothing open")).toBeNull();
    expect(attention.querySelector(".pill.ok")).toBeNull();
    const siteLink = within(attention).getByRole("link", { name: "Open site" });
    expect(siteLink.getAttribute("href")).toBe("/sites/site-2");
  });
});

describe("overview service availability", () => {
  it("waits for alerts before issuing an all-clear, then updates once the check completes", async () => {
    let completeAlerts!: (response: Response) => void;
    const alerts = new Promise<Response>((resolve) => { completeAlerts = resolve; });
    show([site(1)], { insights: () => alerts });
    await screen.findByRole("link", { name: "Office 1" });
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(screen.queryByRole("heading", { level: 1, name: "All sites within SLA" })).toBeNull();
    expect(within(attention).queryByText("Nothing open")).toBeNull();
    expect(attention.querySelector(".pill.ok")).toBeNull();
    expect(screen.queryByText(/No open alerts\.|no open risk or cost alerts/i)).toBeNull();
    expect(screen.getAllByText(/checking.*alerts/i).length).toBeGreaterThan(0);
    await act(async () => completeAlerts(new Response("[]", { status: 200 })));
    await screen.findByRole("heading", { level: 1, name: "All sites within SLA" });
    expect(within(attention).getByText("Nothing open").classList.contains("ok")).toBe(true);
  });

  it("surfaces a failed alert check and withholds the no-alerts claim even while measured sites are healthy", async () => {
    show([site(1)], { insights: async () => new Response(JSON.stringify({ detail: "Alert checks failed for this test" }), { status: 503 }) });
    await screen.findByText("Alert checks failed for this test");
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(screen.queryByRole("heading", { level: 1, name: "All sites within SLA" })).toBeNull();
    expect(within(attention).queryByText("Nothing open")).toBeNull();
    expect(attention.querySelector(".pill.ok")).toBeNull();
    expect(screen.queryByText(/No open alerts\.|no open risk or cost alerts/i)).toBeNull();
    expect(screen.getByRole("link", { name: "Office 1" })).toBeTruthy();
  });

  it("surfaces a failed network request without showing a healthy dashboard", async () => {
    show([], { overview: async () => new Response(JSON.stringify({ detail: "Network request failed for this test" }), { status: 503 }) });
    await screen.findByText("Network request failed for this test");
    expect(screen.queryByRole("heading", { level: 1, name: "All sites within SLA" })).toBeNull();
    expect(screen.queryByText("Nothing open")).toBeNull();
    expect(screen.queryByRole("region", { name: "Key figures" })).toBeNull();
  });

  it("retains last known readings after a refresh fails, withdraws the all-clear and recovers after a successful refresh", async () => {
    const refreshes: (() => void)[] = [];
    vi.spyOn(globalThis, "setInterval").mockImplementation(((callback: TimerHandler, delay?: number) => {
      if (delay === 10_000 && typeof callback === "function") refreshes.push(callback as () => void);
      return 1;
    }) as typeof setInterval);
    const sites = [site(1)];
    let failing = false;
    show(sites, { overview: async () => failing
      ? new Response(JSON.stringify({ detail: "Network refresh failed for this test" }), { status: 503 })
      : new Response(JSON.stringify(network(sites)), { status: 200 }) });
    await screen.findByRole("heading", { level: 1, name: "All sites within SLA" });

    failing = true;
    await act(async () => { refreshes.forEach((refresh) => refresh()); });
    await screen.findByRole("heading", { level: 1, name: "Current network status unavailable" });
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(within(attention).queryByText("Nothing open")).toBeNull();
    expect(attention.querySelector(".pill.ok")).toBeNull();
    expect(screen.queryByText(/All measured connections are within SLA/)).toBeNull();
    expect(screen.getByText(/Readings below are from the last successful load/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Office 1" })).toBeTruthy();
    expect(screen.getByText("Paths within SLA, last known")).toBeTruthy();
    expect(screen.queryByText("Paths within SLA now")).toBeNull();

    failing = false;
    await act(async () => { refreshes.forEach((refresh) => refresh()); });
    await screen.findByRole("heading", { level: 1, name: "All sites within SLA" });
    expect(within(attention).getByText("Nothing open").classList.contains("ok")).toBe(true);
    expect(screen.queryByText(/Readings below are from the last successful load/)).toBeNull();
  });
});

describe("overview at fifty network issues", () => {
  it("bounds the written summary and attention list while retaining the complete count and site journey", async () => {
    const view = show(Array.from({ length: 50 }, (_, index) => site(index + 1, { apply_ok: false })));
    await screen.findByRole("heading", { level: 1, name: "50 issues need attention" });
    const summary = view.container.querySelector(".ov-summary")!;
    expect(summary.textContent).toContain("Office 1, config failed at Office 2, config failed at Office 3 and 47 more");
    expect(summary.textContent).not.toContain("Office 4");
    expect(summary.textContent!.length).toBeLessThan(220);
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(attention.querySelectorAll(".attn-row")).toHaveLength(5);
    expect(within(attention).getAllByRole("link", { name: "Open site" })).toHaveLength(5);
    expect(within(attention).getByRole("link", { name: "Review all sites (50 network issues)" }).getAttribute("href")).toBe("#network-sites");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("1–10 of 50 sites"));
  });
});
