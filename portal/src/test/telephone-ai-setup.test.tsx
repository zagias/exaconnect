// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import PhoneAISetup, { type PhoneSetup } from "../pages/commai/voice/PhoneAISetup";
import type { VoiceOverview } from "../pages/commai/voice/types";
import { TABS } from "../nav";

const voice = (staff = false): VoiceOverview => ({
  is_admin: true, is_exacarib: staff, can_spend: false, me: null,
  policy: { recording_access: "none" }, provider: { name: "unconfigured", live: false }, ai_phone_ready: false,
});
const setup = (ready = false): PhoneSetup => ({
  ready, status: ready ? "ready" : "setup_required", human_answered: false, can_manage_agent: true,
  summary: ready ? "Recent evidence permits telephone AI; your call rules still decide when it answers."
    : "Telephone AI needs setup or fresh evidence. Keep the existing phone route or fallback.",
  blockers: ready ? [] : ["livekit", "number_routing", "pbx"], routing: { active_numbers: ready ? 1 : 0, routed_numbers: ready ? 1 : 0 },
  components: [
    { id: "livekit", title: "Phone media service", status: ready ? "configured" : "missing", responsibility: "operator",
      detail: "Connection settings do not prove working media.", next_action: "Ask the operator to configure the private media service.",
      missing: ready ? [] : ["EXA_LIVEKIT_API_KEY"] },
    { id: "assistant", title: "Business assistant", status: "configured", responsibility: "business", setup_action: "agent_profile",
      detail: "The business assistant and model are enabled.", next_action: "Review your agent profile." },
    { id: "number_routing", title: "Number and call fallback", status: ready ? "configured" : "missing", responsibility: "business", setup_action: "numbers",
      detail: "An active number needs an AI rule and fallback.", next_action: "Prepare your number and call rules." },
    { id: "pbx", title: "Patched phone system", status: ready ? "verified" : "blocked", responsibility: "operator",
      detail: "Disabling transfers on an affected PBX is mitigation only.", next_action: "Ask the operator to install a supported patched release.",
      version: ready ? "1.11.3" : "1.10.12", checked_at: "2026-11-01T12:00:00Z", expires_at: "2026-11-01T12:05:00Z" },
  ],
});
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("provides real setup destinations and ownership without enabling or probing telephone AI", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(response(setup()));
  render(<MemoryRouter><PhoneAISetup base="/commai/customers/fictional-org" v={voice()} onRefresh={vi.fn()} /></MemoryRouter>);
  await screen.findByText("Setup required");
  expect(screen.getByText("Ask the operator to configure the private media service.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Open agent profile" }).getAttribute("href")).toBe("/commai/ai");
  expect(screen.getByRole("link", { name: "Manage numbers" }).getAttribute("href")).toBe("/commai/voice/numbers");
  expect(screen.getByRole("link", { name: "Prepare AI call rules" }).getAttribute("href")).toBe("/commai/voice/routing#ai-rules");
  expect(screen.getByRole("link", { name: "Connect your own SIP trunk" }).getAttribute("href")).toBe("/commai/voice/trunks");
  expect(screen.queryByText("EXA_LIVEKIT_API_KEY")).toBeNull();
  expect(screen.queryByText("Operator details")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(fetch.mock.calls).toHaveLength(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/commai/customers/fictional-org/voice/phone/setup");
  expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("shows private operator configuration names only to platform operators", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(setup()));
  render(<MemoryRouter><PhoneAISetup base="/example" v={voice(true)} onRefresh={vi.fn()} /></MemoryRouter>);
  await screen.findByText("EXA_LIVEKIT_API_KEY");
  expect(screen.getByText("1.10.12")).toBeTruthy();
  expect(screen.getAllByText("Operator details")).toHaveLength(2);
  expect(screen.queryByText(/person answered/i)).toBeNull();
});

it("withholds an unavailable agent-profile link and uses the explicit access path", async () => {
  const data = setup();
  data.can_manage_agent = false;
  data.components[1].setup_action = "apps_plans";
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response(data));
  render(<MemoryRouter><PhoneAISetup base="/example" v={voice()} onRefresh={vi.fn()} /></MemoryRouter>);
  await screen.findByText("Setup required");
  expect(screen.queryByRole("link", { name: "Open agent profile" })).toBeNull();
  expect(screen.getByRole("link", { name: "Review Apps & plans" }).getAttribute("href")).toBe("/account/apps");
});

it("does not call the voice-admin endpoint for a personal-phone user", () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  render(<MemoryRouter><PhoneAISetup base="/example" v={{ ...voice(), is_admin: false }} onRefresh={vi.fn()} /></MemoryRouter>);
  expect(screen.getByText("Ask a voice admin to review telephone AI setup.")).toBeTruthy();
  expect(fetch).not.toHaveBeenCalled();
});

it("does not turn a loading response into either a readiness claim or an empty setup", () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
  render(<MemoryRouter><PhoneAISetup base="/example" v={voice()} onRefresh={vi.fn()} /></MemoryRouter>);
  expect(screen.getByText("Checking telephone AI setup…")).toBeTruthy();
  expect(screen.queryByText("Setup required")).toBeNull();
  expect(screen.queryByText("Ready for call routing")).toBeNull();
});

it("removes cached readiness after a failed refresh and never treats it as enabled call routing", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response(setup(true)))
    .mockResolvedValueOnce(response({ detail: "Status temporarily unavailable" }, 503));
  const refresh = vi.fn();
  render(<MemoryRouter><PhoneAISetup base="/example" v={{ ...voice(), ai_phone_ready: true }} onRefresh={refresh} /></MemoryRouter>);
  await screen.findByText("Ready for call routing");
  expect(screen.queryByText(/AI enabled|AI is answering|person answered/i)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await screen.findByText("Readiness could not be checked");
  expect(screen.queryByText("Ready for call routing")).toBeNull();
  expect(screen.queryByRole("link", { name: "Review AI call rules" })).toBeNull();
  expect(refresh).toHaveBeenCalledOnce();
  expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("does not retain another organisation's readiness while the selected organisation loads", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(response(setup(true)))
    .mockImplementationOnce(() => new Promise<Response>(() => {}));
  const page = (base: string) => <MemoryRouter><PhoneAISetup base={base} v={voice()} onRefresh={vi.fn()} /></MemoryRouter>;
  const view = render(page("/commai/customers/first"));
  await screen.findByText("Ready for call routing");
  view.rerender(page("/commai/customers/second"));
  await waitFor(() => expect(screen.getByText("Checking telephone AI setup…")).toBeTruthy());
  expect(screen.queryByText("Ready for call routing")).toBeNull();
});

it("makes the setup journey discoverable in the existing Phone navigation", () => {
  expect(TABS["/commai/voice"]).toContainEqual(["/commai/voice/ai", "Telephone AI", "setup readiness speech phone handover fallback"]);
});
