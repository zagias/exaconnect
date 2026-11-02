// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import Fabric from "../pages/Fabric";
import Internet from "../pages/Internet";
import OrderPage from "../pages/Order";
import Encryption from "../pages/Encryption";
import { BoxSoftware } from "../pages/BoxSoftware";
import { BoxTemplates } from "../pages/BoxTemplates";

const auth = vi.hoisted(() => ({ user: null as User | null, example: false }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "org-a", name: "Coral", example: auth.example, sites: [{ id: "site-a", name: "Head office" }] }, reload: vi.fn() }),
  who: (actor: string | null) => actor?.replace(/^user:/, "") ?? "unknown",
}));
vi.mock("../components", async (original) => ({
  ...await original<typeof import("../components")>(),
  LineChart: ({ title }: { title: string }) => <p>{title}</p>,
}));

function responses() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const data = path.includes("/box-software/versions") ? { versions: ["1.0.0"], latest: "1.0.0" }
      : path.includes("/box-software") ? { boxes: [{ site_id: "site-a", site: "Head office", managed: "cloud", ab: true, version: "1.0.0", slot: "a", trial: false, installing: false, asked: null, state: null, detail: null }] }
      : path.includes("/box-templates") ? { templates: [{ id: "template-a", name: "Office ports", network: { wan: [] }, updated_by: "ops@example.org", updated_at: "2026-11-01T10:00:00Z" }], boxes: [{ site_id: "site-a", site: "Head office", managed: "cloud", template_id: "template-a", ports: ["eth0"], state: "kept" }] }
      : path.endsWith("/encryption") ? { summary: { encrypted: 0, total: 0 }, paths: [], circuits: [], layer2: [], control: { protocol: "HTTPS" }, internet: "Internet traffic follows your configured route." }
      : path.endsWith("/internet") ? { public_address: "203.0.113.1", inbound_dropped: 0, sites: [{ id: "site-a", name: "Head office", mode: "pop", via_label: "ExaCarib hub", uplinks: [] }], rules: [{ id: 1, position: 1, site_id: null, site: null, action: "allow", src: [], dst: [], protocol: "tcp", ports: "443", description: "Business access", enabled: true, packets: 10, bytes: 1000 }], forwards: [{ id: 1, description: "Customer portal", protocol: "tcp", port: 8443, to_site_id: "site-a", to_site: "Head office", to_address: "192.168.1.10", to_port: 443, allow_from: [], enabled: true, packets: 1, bytes: 100 }] }
      : path.endsWith("/orders") ? [{ id: 1, status: "draft", engine: "form", text: null, actions: [{ action: "cloud_circuit", provider: "aws" }], summary: ["Requested VPN connection"], needs: [], problems: [], monthly_estimate: null, results: [], created_by: "user:ops@example.org", created_at: "2026-11-01T10:00:00Z" }]
      : path.endsWith("/partners") ? [false, true].map((example) => ({ id: example ? 2 : 1, name: example ? "Example provider" : "Cloud provider", slug: example ? "example" : "cloud", kind: "cloud", category: "cloud", example, regions: [], description: "Private VPN connection", price_per_mbps_month: 1, website: null }))
      : path.endsWith("/settings") ? { cloud_to_cloud: true }
      : path.includes("/circuits/providers") ? {}
      : path.includes("/charges") ? { segments: [], total: 5, price_per_mbps_month: 1 }
      : path.includes("/metrics") ? []
      : path.endsWith("/circuits") ? [{ id: 1, kind: "site", name: "Office link", status: "up", enabled: true, bandwidth_mbps: 20, a_site: "Head office", b_site: "Branch", a_vlan: 100, b_vlan: 100, rtt_ms: 20, loss_pct: 0, month_to_date: 5, price_per_mbps_month: 1, mbps_in: 2, mbps_out: 1, created_by: "user:ops@example.org", created_at: "2026-11-01T10:00:00Z" }]
      : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}
function show(children: React.ReactNode) { return render(<MemoryRouter>{children}</MemoryRouter>); }
beforeEach(() => {
  auth.user = { email: "person@example.org", role: "customer", customer_id: "org-a", org_role: "member" };
  auth.example = false;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  responses();
});

describe("example partner isolation", () => {
  it("shows live directory partners without example cards or their Connect action for a real customer", async () => {
    show(<OrderPage />);
    await screen.findByText("Cloud provider");
    expect(screen.queryByText("Example provider")).toBeNull();
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(within(screen.getByRole("article")).getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it.each(["example organisation", "staff preview"])("retains clearly labelled example cards for %s", async (mode) => {
    if (mode === "example organisation") auth.example = true;
    else auth.user = { email: "staff@example.org", role: "admin", customer_id: null };
    show(<OrderPage />);
    const heading = await screen.findByRole("heading", { name: "Example provider" });
    expect(within(heading.closest("article")!).getByText("Example data")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Cloud provider" })).toBeTruthy();
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each([
  ["viewer", { org_role: "viewer" as const }],
  ["read-scoped owner", { org_role: "owner" as const, scopes: ["connect:read"] }],
])("%s settings", (_label, access) => {
  beforeEach(() => { auth.user = { ...auth.user!, ...access }; });

  it("can inspect orders and drafts without drafting, connecting, confirming or cancelling", async () => {
    const view = show(<OrderPage />);
    await waitFor(() => expect(screen.getByText("Cloud provider")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Draft the order|Connect|Confirm|Cancel/ })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "What would you like to change?" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(screen.getByText("You have view-only access to this draft.")).toBeTruthy();
    expect(view.container.querySelector("form")).toBeNull();
    expect(screen.queryByRole("button", { name: /Confirm|Cancel/ })).toBeNull();
  });

  it("can open circuit facts, charges and charts without changing network settings", async () => {
    const view = show(<Fabric />);
    await waitFor(() => expect(screen.getByText("Office link")).toBeTruthy());
    expect(screen.getByText("Cloud router: On")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /Connect a cloud|Join two sites/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View Office link" }));
    expect(screen.getByText("Charges this month")).toBeTruthy();
    expect(view.container.querySelector("form")).toBeNull();
    expect(screen.queryByRole("button", { name: /Save|Rotate|Switch off|Delete/ })).toBeNull();
  });

  it("sees internet routes, firewall rules and forwards without radio controls or edit actions", async () => {
    const view = show(<Internet />);
    await waitFor(() => expect(screen.getByText("Business access")).toBeTruthy());
    expect(screen.getByText("Customer portal")).toBeTruthy();
    expect(screen.getByText("ExaCarib hub")).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("button", { name: /Add a rule|Add a forward|Edit rule|Edit forward/ })).toBeNull();
    expect(view.container.querySelector("form")).toBeNull();
  });

  it("retains box template/software information without provisioning or update controls", async () => {
    show(<BoxTemplates />);
    await waitFor(() => expect(screen.getByText("Office ports")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /New template|Change|Remove|Stop following/ })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    cleanup();
    show(<BoxSoftware />);
    await waitFor(() => expect(screen.getByText("1.0.0")).toBeTruthy());
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /Send to the ticked boxes/ })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Version" })).toBeNull();
  });
});

describe("existing operational permissions", () => {
  it("keeps member internet, private-link and ordering actions available", async () => {
    show(<Internet />);
    await waitFor(() => expect(screen.getAllByRole("radio")).toHaveLength(3));
    expect(screen.getByRole("button", { name: "Add a rule" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add a forward" })).toBeTruthy();
    cleanup();
    show(<Fabric />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Manage Office link" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "Connect a cloud" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Cloud router" })).toBeTruthy();
    cleanup();
    show(<OrderPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Open" })).toBeTruthy());
    expect(screen.getByRole("textbox", { name: "What would you like to change?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("button", { name: "Confirm" })).toBeTruthy();
  });

  it("retains manager-only box updates rather than expanding them to members", async () => {
    show(<BoxSoftware />);
    await waitFor(() => expect(screen.getByText("1.0.0")).toBeTruthy());
    expect(screen.queryByRole("checkbox")).toBeNull();
    cleanup();
    auth.user = { ...auth.user!, org_role: "owner" };
    show(<BoxSoftware />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Update Head office" })).toBeTruthy());
    expect(screen.getByRole("combobox", { name: "Version" })).toBeTruthy();
  });

  it("keeps the encryption report read-only", async () => {
    auth.user = { ...auth.user!, org_role: "viewer" };
    const view = show(<Encryption />);
    await waitFor(() => expect(screen.getByText("Internet traffic follows your configured route.")).toBeTruthy());
    expect(view.container.querySelector("form")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
