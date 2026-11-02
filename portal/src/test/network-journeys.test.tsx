// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { download } from "../api";
import Decisions from "../pages/Decisions";
import Notices from "../pages/Notices";
import Metering from "../pages/Metering";
import { SiteList } from "../pages/Sites";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "admin", email: "staff@example.org" } }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "chosen-org", name: "Selected organisation" } }), useStormToggle: () => ({}) }));
vi.mock("../pages/Overview", () => ({ NodeState: () => <span>Online</span> }));
vi.mock("../api", async (importOriginal) => ({ ...await importOriginal<typeof import("../api")>(), download: vi.fn().mockResolvedValue(undefined) }));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function responses() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const data = path.includes("/classes?") ? [{ name: "voice" }, { name: "payments" }]
      : path.includes("/overview") ? { sites: [{ id: "site-a", name: "Head office", kind: "site", node_id: "box-a", applied_version: 2, desired_version: 2, online: true, last_seen: new Date().toISOString(), paths: [{ path: "a", label: "Fibre", sent: 30, health: "ok" }], steering: [] }] }
      : path.includes("/metering/links") ? { start: "2026-11-01T00:00:00Z", end: "2026-12-01T00:00:00Z", links: [] }
      : path.includes("/metering/usage") ? { totals: { gb: 0, over_commit_gb: 0, satellite_gb: 0 }, paths: [] }
      : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}

describe("selected organisation network journeys", () => {
  it("loads the selected organisation's locations and exposes the installation journey", async () => {
    const fetch = responses();
    render(<MemoryRouter><SiteList /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Head office")).toBeTruthy());
    expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/overview?customer_id=chosen-org")).toBe(true);
    expect(screen.getByRole("link", { name: "Add or connect a location" }).getAttribute("href")).toBe("/locations");
    expect(screen.getByRole("cell", { name: "Within SLA" })).toBeTruthy();
  });

  it("offers custom classes, keeps site and organisation filters, and never infers health from no decisions", async () => {
    const fetch = responses();
    render(<MemoryRouter initialEntries={["/decisions?site_id=site-a"]}><Decisions /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("option", { name: "payments" })).toBeTruthy());
    fireEvent.change(screen.getByRole("combobox", { name: "Class" }), { target: { value: "payments" } });
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => {
      const url = new URL(String(path), "http://localhost");
      return url.pathname === "/api/v1/decisions" && url.searchParams.get("customer_id") === "chosen-org"
        && url.searchParams.get("site_id") === "site-a" && url.searchParams.get("class_name") === "payments";
    })).toBe(true));
    expect(screen.getByText("No routing decisions match these filters.")).toBeTruthy();
    expect(screen.queryByText(/Every path is within SLA/)).toBeNull();
  });

  it("keeps carrier notices and connection names inside the selected organisation", async () => {
    const fetch = responses();
    render(<MemoryRouter><Notices /></MemoryRouter>);
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/notices?customer_id=chosen-org")).toBe(true));
    expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/links?customer_id=chosen-org")).toBe(true);
    expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/carrier/notices")).toBe(false);
  });

  it("defaults usage to this month, scopes CSVs, and distinguishes estimates from invoices", async () => {
    const fetch = responses();
    render(<MemoryRouter><Metering /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("No links to show.")).toBeTruthy());
    expect(fetch.mock.calls.some(([path]) => String(path) === "/api/v1/metering/links?customer_id=chosen-org")).toBe(true);
    expect(screen.getByRole("button", { name: "This month" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(/fixed commit charge is included in full/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "open Billing" }).getAttribute("href")).toBe("/billing");
    fireEvent.click(screen.getByRole("button", { name: "Last 6 hours" }));
    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    expect(download).toHaveBeenCalledWith("/metering/settlement.csv?hours=6&customer_id=chosen-org", "xakore-connect-usage.csv");
  });
});
