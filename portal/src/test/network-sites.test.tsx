// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OverviewSite, PathRow } from "../api";
import { NetworkSites, siteHealth } from "../NetworkSites";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "customer", org_role: "member", customer_id: "org-a" } }) }));
afterEach(cleanup);

function path(overrides: Partial<PathRow> = {}): PathRow {
  return { path: "carrier-a", label: "Carrier A", carrier: "Carrier A", underlay_type: "fibre", commit_mbps: 100,
    rtt_avg_ms: 25, jitter_ms: 2, loss_pct: 0, sent: 30, received: 30,
    at: new Date().toISOString(), health: "ok", ...overrides };
}
function site(number: number, overrides: Partial<OverviewSite> = {}): OverviewSite {
  return { id: `site-${number}`, name: `Office ${number}`, kind: "site", location: number % 2 ? "Kingston" : "Port of Spain",
    node_id: `box-${number}`, last_seen: new Date().toISOString(), online: true, apply_ok: true,
    applied_version: 2, desired_version: 2, storm_mode: false, steering: [],
    paths: [path(), path({ path: "carrier-b", label: "Carrier B", carrier: "Carrier B" })], ...overrides };
}
function inventory(count: number): OverviewSite[] {
  const sites = Array.from({ length: count }, (_, index) => site(index + 1));
  sites[count - 1] = site(count, { online: false });
  sites[count - 2] = site(count - 1, { paths: [path({ health: "bad", received: 0, loss_pct: 100 })] });
  sites[count - 3] = site(count - 2, { apply_ok: false });
  sites[count - 4] = site(count - 3, { paths: [path({ sent: 0, received: 0, health: "ok" })] });
  sites[count - 5] = site(count - 4, { node_id: null, last_seen: null, online: null });
  return sites;
}
function show(sites: OverviewSite[]) {
  return render(<MemoryRouter><NetworkSites sites={sites} /></MemoryRouter>);
}
function rows() { return within(screen.getByRole("table")).getAllByRole("row").slice(1); }
function names() { return rows().map((row) => within(row).getByRole("link").textContent); }

describe("network inventory at 20 sites", () => {
  it("puts outages and attention first and bounds the view to ten locations", () => {
    show(inventory(20));
    expect(rows()).toHaveLength(10);
    expect(names()).toEqual(["Office 20", "Office 18", "Office 19", "Office 17", "Office 16", "Office 1", "Office 2", "Office 3", "Office 4", "Office 5"]);
    expect(screen.getByRole("status").textContent).toBe("1–10 of 20 sites");
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(rows()).toHaveLength(10);
    expect(screen.getByRole("status").textContent).toBe("11–20 of 20 sites");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Rows per page" }), { target: { value: "25" } });
    expect(rows()).toHaveLength(20);
    expect(screen.getByRole("status").textContent).toBe("1–20 of 20 sites");
  });

  it("resets paging for status filters and keeps missing measurements out of healthy results", () => {
    show(inventory(20));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Site status" }), { target: { value: "offline" } });
    expect(names()).toEqual(["Office 20"]);
    expect(screen.getByRole("status").textContent).toContain("1–1 of 1");
    fireEvent.change(screen.getByRole("combobox", { name: "Site status" }), { target: { value: "unmeasured" } });
    expect(names()).toEqual(["Office 17"]);
    expect(within(rows()[0]).getByText("Waiting for measurements")).toBeTruthy();
    expect(within(rows()[0]).queryByText("Within SLA")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Site status" }), { target: { value: "healthy" } });
    expect(screen.getByRole("status").textContent).toBe("1–10 of 15 sites");
    expect(names().some((name) => ["Office 16", "Office 17", "Office 18", "Office 19", "Office 20"].includes(name!))).toBe(false);
  });
});

describe("network inventory at 50 sites", () => {
  it("shows every site once across five bounded pages", () => {
    show(inventory(50));
    const seen: string[] = [];
    for (let page = 0; page < 5; page++) {
      expect(rows()).toHaveLength(10);
      seen.push(...names() as string[]);
      expect(screen.getByRole("status").textContent).toBe(`${page * 10 + 1}–${page * 10 + 10} of 50 sites`);
      if (page < 4) fireEvent.click(screen.getByRole("button", { name: "Next" }));
    }
    expect(new Set(seen).size).toBe(50);
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Rows per page" }), { target: { value: "25" } });
    expect(rows()).toHaveLength(25);
    expect(screen.getByRole("status").textContent).toBe("1–25 of 50 sites");
  });

  it("searches names and locations, combines search with status and sorts names naturally", () => {
    show(inventory(50));
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "name" } });
    expect(names().slice(0, 3)).toEqual(["Office 1", "Office 2", "Office 3"]);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sites" }), { target: { value: "  KINGSTON  " } });
    expect(screen.getByRole("status").textContent).toBe("1–10 of 25 sites");
    expect(rows().every((row) => within(row).getByText("Kingston"))).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Site status" }), { target: { value: "attention" } });
    expect(names()).toEqual(["Office 47", "Office 49"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Site status" }), { target: { value: "all" } });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sites" }), { target: { value: "Office 31" } });
    expect(names()).toEqual(["Office 31"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sites" }), { target: { value: "Missing location" } });
    expect(rows()).toHaveLength(0);
    expect(screen.getByText("No sites match these filters.")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("0 sites");
  });
});

describe("unknown health never becomes healthy", () => {
  it.each([
    ["offline", { online: false }],
    ["uninstalled", { node_id: null, last_seen: null }],
    ["first report", { last_seen: null }],
    ["no paths", { paths: [] }],
    ["zero probes", { paths: [path({ sent: 0, received: 0 })] }],
    ["partial measurements", { paths: [path(), path({ path: "carrier-b", sent: null, received: null })] }],
  ] as [string, Partial<OverviewSite>][]) ("does not label %s as within SLA", (_label, overrides) => {
    const candidate = site(1, overrides);
    expect(siteHealth(candidate).key).not.toBe("healthy");
    expect(siteHealth(candidate).tone).not.toBe("ok");
    show([candidate]);
    expect(within(rows()[0]).queryByText("Within SLA")).toBeNull();
  });
});
