// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import QuotesAndOrders from "../pages/Commercial";
import Billing from "../pages/Billing";
import Traffic from "../pages/Traffic";
import { NetworkSites } from "../NetworkSites";
import type { OverviewSite } from "../api";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "customer", org_role: "owner", customer_id: "org-a" } }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Coral" } }), who: (value: string) => value }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const quote = (id: string) => ({ id, number: `Q-${id}`, customer_id: "org-a", customer: "Coral", title: `Saved ${id}`, currency: "USD", term_months: 12,
  valid_until: "2026-12-31", notes: `Notes for ${id}`, status: "draft", lines: [], example: false });
function quotes() {
  return <MemoryRouter initialEntries={["/quotes/a/edit"]}><Link to="/quotes/b/edit">Next quote</Link><Routes><Route path="/quotes/*" element={<QuotesAndOrders />} /></Routes></MemoryRouter>;
}

describe("quote recovery without replacing unseen data", () => {
  it("keeps editing unavailable until a failed initial read is retried successfully", async () => {
    let unavailable = true;
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/quotes/a")
      ? unavailable ? response({ detail: "Temporary interruption" }, 503) : response(quote("a")) : response({ items: [] }));
    render(quotes());
    await screen.findByText("Temporary interruption");
    expect(screen.queryByLabelText("Title")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(screen.getByRole("link", { name: "Back to quotes" }).getAttribute("href")).toBe("/quotes");
    expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect((await screen.findByLabelText("Title") as HTMLInputElement).value).toBe("Saved a");
  });

  it("does not carry an edited quote into another quote's form", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => /\/quotes\/[ab]$/.test(String(input))
      ? response(quote(String(input).split("/").pop()!)) : response({ items: [] }));
    render(quotes());
    const title = await screen.findByLabelText("Title");
    fireEvent.change(title, { target: { value: "Unsaved a" } });
    fireEvent.click(screen.getByRole("link", { name: "Next quote" }));
    await waitFor(() => expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Saved b"));
  });
});

describe("stale section links", () => {
  it.each([["/billing/old/section", Billing, "Back to billing", "/billing"], ["/traffic/old/section", Traffic, "Open traffic priorities", "/traffic/applications"]] as const)("offers a useful destination for %s", (path, Component, label, href) => {
    render(<MemoryRouter initialEntries={[path]}><Routes><Route path={path.startsWith("/billing") ? "/billing/*" : "/traffic/*"} element={<Component />} /></Routes></MemoryRouter>);
    expect(screen.getByRole("link", { name: label }).getAttribute("href")).toBe(href);
    expect(screen.getByText("Section not found")).toBeTruthy();
  });
});

describe("site inventory continuity", () => {
  it("retains search, sort and paging when a site is opened and the visitor returns", () => {
    const sites = Array.from({ length: 30 }, (_, index) => ({ id: `site-${index + 1}`, name: `Office ${index + 1}`, location: "Kingston", kind: "site", paths: [], steering: [] } as unknown as OverviewSite));
    function Detail() { const location = useLocation(); return <Link to={location.state.siteList}>Back to filtered sites</Link>; }
    render(<MemoryRouter initialEntries={["/sites?site_search=Office&site_sort=name&site_page=1"]}><Routes><Route path="/sites" element={<NetworkSites sites={sites} />} /><Route path="/sites/:id" element={<Detail />} /></Routes></MemoryRouter>);
    expect(screen.getByRole("status").textContent).toBe("11–20 of 30 sites");
    fireEvent.click(screen.getByRole("link", { name: "Office 11" }));
    fireEvent.click(screen.getByRole("link", { name: "Back to filtered sites" }));
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("Office");
    expect((screen.getByRole("combobox", { name: "Sort by" }) as HTMLSelectElement).value).toBe("name");
    expect(screen.getByRole("status").textContent).toBe("11–20 of 30 sites");
  });
});
