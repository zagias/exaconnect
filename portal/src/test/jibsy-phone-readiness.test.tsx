// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

it("lets admins prepare reviewed rules while unfinished telephone AI still uses its fallback", () => {
  const propose = vi.fn();
  const fetch = vi.spyOn(globalThis, "fetch");
  render(<MemoryRouter><Routing v={voice()} propose={propose} /></MemoryRouter>);
  expect(screen.getByText(/Saved rules use their fallback until telephone AI checks pass/)).toBeTruthy();
  const rules = within(screen.getByRole("heading", { name: "AI agent rules" }).closest("section.card")! as HTMLElement);
  fireEvent.change(rules.getByLabelText("Name"), { target: { value: "Enquiries" } });
  fireEvent.change(rules.getByLabelText("When"), { target: { value: "always" } });
  fireEvent.click(rules.getByRole("button", { name: "Prepare rule" }));
  expect(propose).toHaveBeenCalledWith([{ op: "save_ai_rule", name: "Enquiries", condition: "always", fallback: "voicemail", enabled: true }], "prepare rule Enquiries");
  expect(fetch).not.toHaveBeenCalled();
  expect((rules.getByRole("option", { name: "Ring group" }) as HTMLOptionElement).disabled).toBe(true);
  expect((rules.getByRole("option", { name: "Queue" }) as HTMLOptionElement).disabled).toBe(true);
  expect(Array.from(document.querySelectorAll<HTMLOptionElement>('option[value="ai"]')).every((option) => option.disabled)).toBe(true);
  expect(screen.getByRole("link", { name: "View telephone AI setup and readiness" }).getAttribute("href")).toBe("/commai/voice/ai");
});

it("gates the number-search screen on provider readiness", () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]"));
  render(<MemoryRouter><Numbers base="/example" v={voice()} /></MemoryRouter>);
  expect(screen.getByText(/New numbers and number transfers are unavailable/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Search" })).toBeNull();
  expect(fetch.mock.calls.some(([url]) => String(url).includes("numbers/search"))).toBe(false);
});
