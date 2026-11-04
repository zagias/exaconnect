// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import { Classes } from "../pages/Classes";
import LocationsPage from "../pages/org/Locations";
import type { OrgLocation } from "../pages/org/common";
import Traffic from "../pages/Traffic";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "org-a", name: "Coral", sites: [] } }),
  who: (actor: string) => actor.replace(/^user:/, ""),
}));

function location(id: string, name: string): OrgLocation {
  return { id, name, address_line1: `${name} Road`, address_line2: "", city: "Port of Spain", island: "Trinidad",
    country: "TT", postcode: "", timezone: "America/Port_of_Spain", latitude: null, longitude: null,
    connect: null, phone: null, hours: null };
}
const rules = ["Alpha", "Beta"].map((name, index) => ({ id: index + 1, name: `${name} rule`, class_name: "voice",
  site_ids: [], apps: [], ports: index ? "tcp:8443" : "tcp:443", dst_subnets: [], src_subnets: [], vlans: [],
  domains: [], dscp: [], enabled: true, source: "customer", created_by: "user:owner@example.org" }));
const classes = ["alpha", "beta"].map((name, index) => ({ name, description: index ? "Second class" : "First class",
  priority: "interactive", preferred_path: null, dscp: [], ports: "", subnets: [], ordinal: 10,
  max_latency_ms: 100, max_jitter_ms: null, max_loss_pct: 1, allow_satellite: true, builtin: false }));
const install = { site: "new-site", token: "test-one-time-code", expires_at: "2030-01-01T00:00:00Z",
  ca_fingerprint: "test-fingerprint", agent_url: "https://agent.example.org", public_agent_url: "https://agent.example.org" };
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function responses(options: { products?: string[]; write?: (path: string, init: RequestInit) => Promise<Response> } = {}) {
  const locations = [location("a", "Alpha"), location("b", "Beta")];
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input).replace("/api/v1", "");
    if (init?.method && init.method !== "GET") {
      if (options.write) return options.write(path, init);
      const body = JSON.parse(String(init.body ?? "{}"));
      if (path === "/orgs/org-a/locations") {
        const saved = location("new", body.name);
        locations.push(saved);
        return json({ id: saved.id, name: saved.name });
      }
      if (path.endsWith("/connect")) {
        const saved = locations.find((item) => path === `/orgs/org-a/locations/${item.id}/connect`)!;
        saved.connect = { id: `site-${saved.id}`, name: "Shared site label", node_id: null, last_seen: null, online: false, links: 1 };
        return json({ install });
      }
      return json({ id: path.split("/").at(-1), name: body.name });
    }
    return json(path === "/orgs/org-a/locations" ? { locations, products: options.products ?? ["connect"], hub_ready: true }
      : path === "/customers/org-a/rules" ? rules
      : path.startsWith("/classes?") ? classes : []);
  });
  return { fetch, locations };
}
type Mode = "location" | "rule" | "class";
function show(mode: Mode) {
  return render(<MemoryRouter initialEntries={[mode === "rule" ? "/traffic/rules" : "/locations"]}>
    {mode === "rule" ? <Routes><Route path="/traffic/*" element={<Traffic />} /></Routes>
      : mode === "location" ? <LocationsPage /> : <Classes />}
  </MemoryRouter>);
}
async function ready(mode: Mode) {
  await screen.findByRole("button", { name: mode === "location" ? "Add a location" : mode === "rule" ? "Edit Alpha rule" : "Edit alpha" });
  if (mode === "location") await screen.findByRole("region", { name: "Alpha" });
}
function editButton(mode: Mode, target: "Alpha" | "Beta") {
  return mode === "location" ? within(screen.getByRole("region", { name: target })).getByRole("button", { name: "Edit" })
    : screen.getByRole("button", { name: `Edit ${mode === "rule" ? `${target} rule` : target.toLowerCase()}` });
}
function field(mode: Mode) {
  return screen.getByRole("textbox", { name: mode === "location" ? "Street address" : mode === "rule" ? "Ports" : "Description" }) as HTMLInputElement;
}
function saveButton(mode: Mode) {
  return screen.getByRole("button", { name: `Save ${mode}` });
}
function writes(fetch: ReturnType<typeof responses>["fetch"]) {
  return fetch.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "PUT");
}

beforeEach(() => {
  auth.user = { email: "owner@example.org", role: "customer", customer_id: "org-a", org_role: "owner" };
  vi.spyOn(window, "confirm").mockReturnValue(false);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each<Mode>(["location", "rule", "class"])("%s draft identity", (mode) => {
  it("keeps a rejected switch intact, then loads and saves the accepted target's own values", async () => {
    const { fetch } = responses();
    show(mode);
    await ready(mode);
    fireEvent.click(editButton(mode, "Alpha"));
    fireEvent.change(field(mode), { target: { value: "Unsaved Alpha draft" } });
    fireEvent.click(editButton(mode, "Beta"));
    expect(field(mode).value).toBe("Unsaved Alpha draft");
    expect(writes(fetch)).toHaveLength(0);
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(editButton(mode, "Beta"));
    const expected = mode === "location" ? "Beta Road" : mode === "rule" ? "tcp:8443" : "Second class";
    expect(field(mode).value).toBe(expected);
    fireEvent.click(saveButton(mode));
    await waitFor(() => expect(writes(fetch)).toHaveLength(1));
    const [path, init] = writes(fetch)[0] as [string, RequestInit];
    expect(path).toBe(mode === "location" ? "/api/v1/orgs/org-a/locations/b"
      : mode === "rule" ? "/api/v1/customers/org-a/rules/2" : "/api/v1/customers/org-a/classes/beta");
    const body = JSON.parse(String(init.body));
    expect(mode === "location" ? body.address_line1 : mode === "rule" ? body.ports : body.description).toBe(expected);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull());
  });

  it("blocks target switching and cancellation while a save is in flight", async () => {
    let release!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { release = resolve; });
    const { fetch } = responses({ write: () => pending });
    show(mode);
    await ready(mode);
    fireEvent.click(editButton(mode, "Alpha"));
    fireEvent.click(saveButton(mode));
    expect((editButton(mode, "Beta") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(editButton(mode, "Beta"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(writes(fetch)).toHaveLength(1);
    expect(window.confirm).not.toHaveBeenCalled();
    await act(async () => { release(json({ id: "a", name: "Alpha" })); });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull());
  });
});

it.each<Mode>(["rule", "class"])("starts a clean new %s after explicitly discarding an existing draft", async (mode) => {
  responses();
  show(mode);
  await ready(mode);
  fireEvent.click(editButton(mode, "Alpha"));
  fireEvent.change(field(mode), { target: { value: "Unsaved draft" } });
  vi.mocked(window.confirm).mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: `Add a ${mode}` }));
  const name = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
  expect(name.value).toBe("");
  expect(name.readOnly).toBe(false);
  expect(field(mode).value).toBe("");
});

describe("location connection continuation", () => {
  it("connects the newly returned location ID and follows that location's registration status", async () => {
    const { fetch, locations } = responses();
    show("location");
    await ready("location");
    fireEvent.click(screen.getByRole("button", { name: "Add a location" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "New branch" } });
    fireEvent.click(screen.getByRole("button", { name: "Add and connect location" }));
    await screen.findByRole("heading", { name: "Connect New branch" });
    fireEvent.change(screen.getByRole("textbox", { name: "Carrier" }), { target: { value: "Local carrier" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect and get the install code" }));
    const panel = await screen.findByRole("region", { name: "Install the XaKore box at New branch" });
    expect(fetch.mock.calls.some(([path, init]) => String(path) === "/api/v1/orgs/org-a/locations/new/connect" && init?.method === "POST")).toBe(true);
    await waitFor(() => expect(within(panel).getByRole("link", { name: "Check site connection" }).getAttribute("href")).toBe("/sites/site-new"));
    expect(within(panel).getByText("Waiting for box registration")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search locations" }), { target: { value: "" } });
    // An unrelated site's matching display name must not advance this installation.
    locations[0].connect = { id: "other-site", name: "Shared site label", node_id: "other-box", last_seen: new Date().toISOString(), online: true, links: 1 };
    fireEvent.click(within(panel).getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "Alpha" }).textContent).toContain("Box online"));
    expect(within(panel).getByText("Waiting for box registration")).toBeTruthy();
    locations.find((item) => item.id === "new")!.connect!.node_id = "new-box";
    fireEvent.click(within(panel).getByRole("button", { name: "Refresh status" }));
    await within(panel).findByText("Box registered; waiting for its first report");
    locations.find((item) => item.id === "new")!.connect!.online = true;
    locations.find((item) => item.id === "new")!.connect!.last_seen = new Date().toISOString();
    fireEvent.click(within(panel).getByRole("button", { name: "Refresh status" }));
    await within(panel).findByText("Box online");
    expect(within(panel).queryByText("Within SLA")).toBeNull();
    fetch.mockImplementationOnce(async () => json({ detail: "Monitoring unavailable" }, 503));
    fireEvent.click(within(panel).getByRole("button", { name: "Refresh status" }));
    await within(panel).findByText("Registration status unavailable");
    expect(within(panel).queryByText("Box online")).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Close instructions" }));
    expect(screen.queryByRole("region", { name: "Install the XaKore box at New branch" })).toBeNull();
  });

  it.each(["read-scoped owner", "organisation without Connect"])("retains ordinary location creation for a %s", async (kind) => {
    if (kind === "read-scoped owner") auth.user = { ...auth.user!, scopes: ["connect:read"] };
    const { fetch } = responses({ products: kind === "organisation without Connect" ? ["commai"] : ["connect"] });
    show("location");
    await ready("location");
    fireEvent.click(screen.getByRole("button", { name: "Add a location" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "New branch" } });
    fireEvent.click(screen.getByRole("button", { name: "Add location" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull());
    expect(screen.queryByRole("heading", { name: "Connect New branch" })).toBeNull();
    expect(fetch.mock.calls.some(([path]) => String(path).endsWith("/connect"))).toBe(false);
  });

  it("does not reuse carrier drafts after an accepted switch to another connection target", async () => {
    const { fetch } = responses();
    show("location");
    await ready("location");
    const connectAt = (name: string) => within(screen.getByRole("region", { name })).getByRole("button", { name: "Connect this location" });
    fireEvent.click(connectAt("Alpha"));
    fireEvent.change(screen.getByRole("textbox", { name: "Carrier" }), { target: { value: "Alpha draft carrier" } });
    fireEvent.click(connectAt("Beta"));
    expect((screen.getByRole("textbox", { name: "Carrier" }) as HTMLInputElement).value).toBe("Alpha draft carrier");
    vi.mocked(window.confirm).mockReturnValue(true);
    fireEvent.click(connectAt("Beta"));
    expect((screen.getByRole("textbox", { name: "Carrier" }) as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "Carrier" }), { target: { value: "Beta carrier" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect and get the install code" }));
    await screen.findByRole("region", { name: "Install the XaKore box at Beta" });
    const [, init] = fetch.mock.calls.find(([path]) => String(path).endsWith("/locations/b/connect"))!;
    expect(JSON.parse(String(init?.body)).links[0].carrier).toBe("Beta carrier");
  });
});
