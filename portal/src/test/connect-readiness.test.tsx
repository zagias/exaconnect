// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Insight, Integration, OnrampAdapterInfo, Order, OrderAction, User } from "../api";
import { ConnectCapabilitiesProvider, useConnectCapabilities } from "../ConnectCapabilities";
import Integrations from "../pages/Integrations";
import OrderPage from "../pages/Order";
import { InsightList } from "../pages/Insights";

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: auth.user }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "org-a", name: "Coral", sites: [{ id: "site-a", name: "Head office" }] } }),
  useStormToggle: () => ({ current: { sites: [] }, busy: false, error: null, toggle: vi.fn() }),
  who: (actor: string | null) => actor?.replace(/^user:/, "") ?? "unknown",
}));

const adapter = (key: string, configured: boolean, mode: "live" | "simulated"): OnrampAdapterInfo => ({
  key, name: key, configured, mode, docs: "", api: "", live_needs: "", speeds_mbps: [50], fields: [],
});
const integration = (mode: "live" | "simulated"): Integration => ({
  id: mode === "live" ? 1 : 2, customer_id: "org-a", carrier_id: null, provider: "webhook", provider_name: "Webhook",
  category: "webhook", name: `${mode} connection`, enabled: true, config: {}, event_types: ["*"], site_ids: [],
  min_severity: "info", origin: "portal", platform: "", last_status: "", last_delivery_at: null, secrets_set: ["url"],
  mode, created_at: "2026-11-01T10:00:00Z",
});
const watch: Insight = {
  id: 10, customer_id: "org-a", kind: "storm_warning", severity: "warning", title: "Weather warning",
  detail: "Check the backup connection at Head office.", site: "Head office", example: false,
  first_seen: "2026-11-01T10:00:00Z", last_seen: "2026-11-01T10:00:00Z",
  resolved_at: null, acknowledged_by: null, acknowledged_at: null, path: null, carrier: null, data: {},
};
function order(action: OrderAction): Order {
  return { id: 1, status: "draft", engine: "form", text: null, actions: [action], summary: ["Requested connection"],
    needs: [], problems: [], monthly_estimate: null, results: [], created_by: "user:ops@example.org",
    created_at: "2026-11-01T10:00:00Z", confirmed_by: null, confirmed_at: null };
}

function responses(options: { live?: boolean; ask?: boolean; adapters?: OnrampAdapterInfo[]; rows?: Integration[]; draft?: Order; failed?: boolean; simulatedTest?: boolean } = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (options.failed && path.endsWith("/integrations/catalogue"))
      return new Response(JSON.stringify({ detail: "Readiness unavailable" }), { status: 503 });
    const data = path.endsWith("/ai/status") ? { ask_enabled: options.ask === true, model: null, storm_watch: false }
      : path.endsWith("/integrations/catalogue") ? {
        live: options.live === true, events: [], onramp_adapters: options.adapters ?? [],
        providers: [{ key: "webhook", name: "Webhook", category: "webhook", receives_events: true, owners: ["customer"], fields: [{ name: "url", label: "Destination URL", required: true, secret: true, kind: "text" }] }],
      }
      : path.endsWith("/test") ? { status: options.simulatedTest ? "simulated" : "delivered", last_error: "" }
      : path.includes("/deliveries") ? []
      : /\/integrations(?:\?|$)/.test(path) ? options.rows ?? []
      : path.endsWith("/orders") ? options.draft ? [options.draft] : []
      : [];
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  });
}
function Probe() {
  const capabilities = useConnectCapabilities();
  return <output>{JSON.stringify({ loading: capabilities.loading, ask: capabilities.ask, integrations: capabilities.integrations, onramps: capabilities.onramps.map((a) => a.key), error: capabilities.error })}</output>;
}
function show(children: React.ReactNode, enabled = true) {
  return render(<MemoryRouter><ConnectCapabilitiesProvider enabled={enabled}>{children}</ConnectCapabilitiesProvider></MemoryRouter>);
}
const state = () => JSON.parse(screen.getByRole("status").textContent ?? "{}");

beforeEach(() => {
  auth.user = { role: "customer", email: "ops@example.org", customer_id: "org-a", org_role: "member" };
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("runtime readiness", () => {
  it("does not request optional services when Connect is disabled", () => {
    const fetch = responses({ live: true, ask: true });
    show(<Probe />, false);
    expect(state()).toMatchObject({ loading: false, ask: false, integrations: false, onramps: [] });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("requires both the live switch and configured live adapters", async () => {
    responses({ live: true, ask: true, adapters: [adapter("aws", true, "live"), adapter("azure", false, "live"), adapter("gcp", true, "simulated")] });
    show(<Probe />);
    await waitFor(() => expect(state().loading).toBe(false));
    expect(state()).toMatchObject({ ask: true, integrations: true, onramps: ["aws"] });
  });
  it("fails closed when readiness cannot be checked", async () => {
    responses({ live: true, failed: true });
    show(<Probe />);
    await waitFor(() => expect(state().loading).toBe(false));
    expect(state()).toMatchObject({ integrations: false, onramps: [], error: "Readiness unavailable" });
  });
});

describe("integration availability", () => {
  it("offers no customer configuration or simulated connections while live delivery is off", async () => {
    const fetch = responses({ rows: [integration("simulated")] });
    show(<Integrations />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Integrations are not available right now" })).toBeTruthy());
    expect(screen.queryByText("simulated connection")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add integration" })).toBeNull();
    expect(fetch.mock.calls.some(([path]) => /\/integrations\?/.test(String(path)))).toBe(false);
  });
  it("shows only live connections to customers and requires destination credentials", async () => {
    responses({ live: true, rows: [integration("live"), integration("simulated")] });
    show(<Integrations />);
    await waitFor(() => expect(screen.getByText("live connection")).toBeTruthy());
    expect(screen.queryByText("simulated connection")).toBeNull();
    expect((screen.getByLabelText("Destination URL") as HTMLInputElement).required).toBe(true);
  });
  it("keeps live data readable to viewers without configuration or write controls", async () => {
    auth.user = { ...auth.user!, org_role: "viewer" };
    responses({ live: true, rows: [integration("live")] });
    show(<Integrations />);
    await waitFor(() => expect(screen.getByText("live connection")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Add integration|Send test|Sync now/ })).toBeNull();
  });
  it("reports that a simulated test sent nothing if delivery is deactivated after loading", async () => {
    responses({ live: true, rows: [integration("live")], simulatedTest: true });
    show(<Integrations />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send test" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Send test" }));
    await waitFor(() => expect(screen.getByText(/Nothing was sent/)).toBeTruthy());
  });
  it("retains labelled staff configuration without claiming a preview sends events", async () => {
    auth.user = { role: "admin", customer_id: null, email: "staff@example.org" };
    responses({ rows: [integration("simulated")] });
    show(<Integrations />);
    await waitFor(() => expect(screen.getByText("simulated connection")).toBeTruthy());
    expect(screen.getByText(/Live delivery is off/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Preview test" })).toBeTruthy();
  });
});

describe("assistant availability in alert details", () => {
  it("omits the assistant suggestion while unavailable without hiding the alert or acknowledgement", async () => {
    responses({ ask: false });
    show(<><Probe /><InsightList items={[watch]} reload={vi.fn()} /></>);
    expect(screen.queryByRole("link", { name: "Ask what to do" })).toBeNull();
    await waitFor(() => expect(state().loading).toBe(false));
    expect(screen.queryByRole("link", { name: "Ask what to do" })).toBeNull();
    expect(screen.getByText(watch.detail)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeTruthy();
  });

  it("offers the alert's question only when the assistant is available", async () => {
    responses({ ask: true });
    show(<InsightList items={[watch]} reload={vi.fn()} />);
    const suggestion = await screen.findByRole("link", { name: "Ask what to do" });
    expect(suggestion.getAttribute("href")).toBe(`/ask?q=${encodeURIComponent(`What should we do about this: ${watch.title}`)}`);
  });
});

describe("dedicated cloud order readiness", () => {
  it.each([false, true])("blocks a dedicated cloud order until its adapter is ready (configured=%s)", async (configured) => {
    responses({ live: true, adapters: [adapter("aws", configured, configured ? "live" : "simulated")], draft: order({ action: "onramp", provider: "aws" }) });
    show(<OrderPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Open" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Confirm" }) as HTMLButtonElement).disabled).toBe(!configured));
    if (!configured) expect(screen.getByText(/dedicated cloud connection in this draft is not available/)).toBeTruthy();
  });
  it("keeps working VPN orders available when optional dedicated adapters are off", async () => {
    responses({ live: false, draft: order({ action: "cloud_circuit", provider: "aws", name: "VPN" }) });
    show(<OrderPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Open" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect((screen.getByRole("button", { name: "Confirm" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
