// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import LocationsPage from "../pages/org/Locations";
import type { OrgLocation } from "../pages/org/common";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Coral" } }) }));

function location(number: number): OrgLocation {
  return { id: `location-${number}`, name: `Location ${number}`, address_line1: `${number} Main Road`, address_line2: "",
    city: number % 2 ? "Kingston" : "Port of Spain", island: number % 2 ? "Jamaica" : "Trinidad", country: number % 2 ? "JM" : "TT",
    postcode: "", timezone: "America/Port_of_Spain", latitude: null, longitude: null, phone: null, hours: null,
    connect: number % 4 === 1 ? null : { id: `site-${number}`, name: `Network ${number}`,
      node_id: number % 4 === 2 ? null : `box-${number}`, last_seen: new Date().toISOString(), online: number % 4 === 3, links: 2 } };
}
function show() {
  const locations = Array.from({ length: 20 }, (_, index) => location(20 - index));
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
    const data = init?.method === "PUT" ? {} : { locations, products: ["connect"], hub_ready: true };
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  const view = render(<MemoryRouter><LocationsPage /></MemoryRouter>);
  return { ...view, fetch };
}
function cards() { return screen.getAllByRole("region").filter((region) => region.classList.contains("loc")); }
function names() { return cards().map((card) => within(card).getByRole("heading", { level: 2 }).textContent); }

beforeEach(() => { auth.user = { email: "owner@example.org", role: "customer", customer_id: "org-a", org_role: "owner" }; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("twenty-location onboarding inventory", () => {
  it("uses natural name order and two bounded pages without losing a location", async () => {
    show();
    await screen.findByRole("region", { name: "Location 1" });
    expect(cards()).toHaveLength(10);
    expect(names()).toEqual(Array.from({ length: 10 }, (_, index) => `Location ${index + 1}`));
    expect(screen.getByRole("status").textContent).toBe("1–10 of 20 locations");
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(names()).toEqual(Array.from({ length: 10 }, (_, index) => `Location ${index + 11}`));
    expect(screen.getByRole("status").textContent).toBe("11–20 of 20 locations");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("resets paging for search, combines location and installation filters, and handles no results", async () => {
    show();
    await screen.findByRole("region", { name: "Location 1" });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search locations" }), { target: { value: "  PORT OF SPAIN  " } });
    expect(names()).toEqual(Array.from({ length: 10 }, (_, index) => `Location ${(index + 1) * 2}`));
    expect(screen.getByRole("status").textContent).toBe("1–10 of 10 locations");
    fireEvent.change(screen.getByRole("combobox", { name: "Connection status" }), { target: { value: "waiting" } });
    expect(names()).toEqual(["Location 2", "Location 6", "Location 10", "Location 14", "Location 18"]);
    expect(cards().every((card) => within(card).getByText("Waiting for the box to be installed"))).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Connection status" }), { target: { value: "connected" } });
    expect(names()).toEqual(["Location 4", "Location 8", "Location 12", "Location 16", "Location 20"]);
    expect(cards().every((card) => within(card).getByText("Box offline"))).toBe(true);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search locations" }), { target: { value: "No such place" } });
    expect(screen.queryAllByRole("region").filter((region) => region.classList.contains("loc"))).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toBe("No locations match these filters.");
  });

  it("separates not connected from installed boxes even when a box is offline", async () => {
    show();
    await screen.findByRole("region", { name: "Location 1" });
    fireEvent.change(screen.getByRole("combobox", { name: "Connection status" }), { target: { value: "not_connected" } });
    expect(names()).toEqual(["Location 1", "Location 5", "Location 9", "Location 13", "Location 17"]);
    expect(screen.getAllByRole("button", { name: "Connect this location" })).toHaveLength(5);
    fireEvent.change(screen.getByRole("combobox", { name: "Connection status" }), { target: { value: "connected" } });
    expect(cards()).toHaveLength(10);
    expect(screen.getAllByText("Box offline")).toHaveLength(5);
    expect(screen.getAllByText("Box online")).toHaveLength(5);
    expect(screen.queryByRole("button", { name: "Connect this location" })).toBeNull();
  });
});

describe("location address management and Connect permissions", () => {
  it("lets a connect:read owner update an address without offering provisioning or install-code writes", async () => {
    auth.user = { ...auth.user!, scopes: ["connect:read"] };
    const { fetch } = show();
    const first = await screen.findByRole("region", { name: "Location 1" });
    expect(within(first).getByRole("button", { name: "Edit" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Connect this location" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Get a new install code" })).toBeNull();
    fireEvent.click(within(first).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Street address" }), { target: { value: "27 Updated Street" } });
    fireEvent.click(screen.getByRole("button", { name: "Save location" }));
    await waitFor(() => expect(fetch.mock.calls.some(([url, init]) => String(url) === "/api/v1/orgs/org-a/locations/location-1"
      && init?.method === "PUT" && JSON.parse(String(init.body)).address_line1 === "27 Updated Street")).toBe(true));
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("retains Connect and install-code actions for an owner with Connect write access", async () => {
    show();
    const first = await screen.findByRole("region", { name: "Location 1" });
    expect(within(first).getByRole("button", { name: "Connect this location" })).toBeTruthy();
    const waiting = screen.getByRole("region", { name: "Location 2" });
    expect(within(waiting).getByRole("button", { name: "Get a new install code" })).toBeTruthy();
  });

  it("gives a viewer location and connection facts without address or provisioning controls", async () => {
    auth.user = { ...auth.user!, org_role: "viewer" };
    show();
    await screen.findByRole("region", { name: "Location 1" });
    expect(cards()).toHaveLength(10);
    expect(screen.getAllByText("Box offline").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /^(Edit|Delete|Add a location|Connect this location|Get a new install code)$/ })).toBeNull();
  });
});
