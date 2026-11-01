// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { PeopleAndNumbers, Routing } from "../pages/commai/voice/PhoneSystem";
import { Numbers } from "../pages/commai/voice/Global";
import type { VoiceOverview } from "../pages/commai/voice/types";

const voice = (ready = false): VoiceOverview => ({
  is_admin: true, is_exacarib: false, can_spend: true, me: null,
  policy: { recording_access: "none" }, provider: { name: "unconfigured", live: false, number_ordering_ready: ready },
  users: [], sites: [], numbers: [], devices: [], ai_rules: [], menus: [], ring_groups: [], queues: [], hours: [],
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("keeps local phone setup available without offering fictional number purchases", () => {
  render(<MemoryRouter><PeopleAndNumbers base="/example" v={voice()} propose={vi.fn()} /></MemoryRouter>);
  expect(screen.getByRole("button", { name: "Check new site" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Check new number" })).toBeNull();
  expect(screen.getByText("Number ordering unavailable")).toBeTruthy();
  expect(screen.getByRole("link", { name: "connect your own SIP trunk" })).toBeTruthy();
  expect(screen.queryByText(/phase 3|Simulated provider/)).toBeNull();
});

it("does not present unfinished telephone AI as a working call destination", () => {
  render(<MemoryRouter><Routing v={voice()} propose={vi.fn()} /></MemoryRouter>);
  expect(screen.getByText(/Telephone AI is not available yet/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Check rule" })).toBeNull();
});

it("gates the number-search screen on provider readiness", () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]"));
  render(<MemoryRouter><Numbers base="/example" v={voice()} /></MemoryRouter>);
  expect(screen.getByText(/New numbers and number transfers are unavailable/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Search" })).toBeNull();
  expect(fetch.mock.calls.some(([url]) => String(url).includes("numbers/search"))).toBe(false);
});
