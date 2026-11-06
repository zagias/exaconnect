// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Trunks from "../pages/commai/voice/Trunks";
import type { VoiceOverview } from "../pages/commai/voice/types";

const voice: VoiceOverview = {
  is_admin: true, is_exacarib: false, can_spend: false, me: null,
  policy: { recording_access: "none" }, provider: { name: "simulated", live: false }, ai_phone_ready: false,
};
const trunk = (required?: boolean) => ({
  id: "owned", key: "owned", name: "Own carrier", adapter: "sip", host: "carrier.example",
  port: 5061, transport: "tls", prefix: "", max_channels: 2, allow_ips: ["192.0.2.1"],
  enabled: true, health: "unknown", last_options_at: null, sim_fail: "",
  ...(required === undefined ? {} : { srtp_required: required, secure_ingress_ready: false }),
});
const view = (required?: boolean) => ({
  trunks: [trunk(required)], settings: { default_trunk_id: null, exacarib_fallback: false },
  locations: [], numbers: [],
});
const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("preserves an omitted legacy policy as unchecked", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => response(view()));
  render(<Trunks base="/example" v={voice} />);
  fireEvent.click(await screen.findByRole("button", { name: "Change" }));
  expect((screen.getByRole("checkbox", { name: "Require encrypted audio (SRTP)" }) as HTMLInputElement).checked).toBe(false);
});

it("preserves a required policy and sends a strict boolean", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () => response(view(true)));
  render(<Trunks base="/example" v={voice} />);
  fireEvent.click(await screen.findByRole("button", { name: "Change" }));
  expect((screen.getByRole("checkbox", { name: "Require encrypted audio (SRTP)" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Save trunk" }));
  await waitFor(() => expect(fetch.mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(true));
  const call = fetch.mock.calls.find(([, options]) => options?.method === "PATCH")!;
  expect(JSON.parse(call[1]!.body as string).srtp_required).toBe(true);
});

it("refuses a non-TLS save without silently clearing encryption", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () => response(view(true)));
  render(<Trunks base="/example" v={voice} />);
  fireEvent.click(await screen.findByRole("button", { name: "Change" }));
  fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "udp" } });
  expect((screen.getByRole("checkbox", { name: "Require encrypted audio (SRTP)" }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole("button", { name: "Save trunk" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Save trunk" }));
  expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("shows protected inbound as blocked and never claims carrier readiness", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => response(view(true)));
  render(<Trunks base="/example" v={voice} />);
  await screen.findByText(/Protected inbound setup is not verified/);
  expect(screen.queryByText(/carrier ready|carrier verified|calls verified/i)).toBeNull();
});
