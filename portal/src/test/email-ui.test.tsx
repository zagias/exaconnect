// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import People from "../pages/People";
import Organisations from "../pages/ops/Organisations";
import { ForgotPage } from "../pages/Reset";
import MySettings from "../pages/me/MySettings";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "admin", email: "staff@example.org" } }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Harbour" }, select: vi.fn(), reload: vi.fn() }) }));
vi.mock("../pages/commai/voice/MySettings", () => ({ default: () => null }));

const email = "owner@example.org";
const path = "/invite/one-use-link";
const link = new URL(path, window.location.origin).toString();
const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const writeText = vi.fn().mockResolvedValue(undefined);
const outcomes = [
  { label: "SMTP acceptance", emailed: true, email_sender: "smtp", expected: `Invitation sent to ${email}.` },
  { label: "unconfigured email", emailed: false, email_sender: "unavailable", expected: "Email isn't available for this service." },
  { label: "SMTP failure", emailed: false, email_sender: "failed", expected: "We couldn't email this invitation." },
  { label: "sender name without acceptance", emailed: false, email_sender: "smtp", expected: "Email isn't available for this service." },
];

function reply(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("invitation email outcomes", () => {
  it.each(outcomes)("shows $label truthfully in People and retains the copyable invitation", async (outcome) => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/orgs/org-a/invites") return reply(init?.method === "POST"
        ? { id: "invite-a", email, role: "member", token: "one-use-link", path, url: link, emailed: outcome.emailed, email_sender: outcome.email_sender }
        : []);
      if (url === "/api/v1/orgs/org-a/members") return reply({ organisation: { id: "org-a", name: "Harbour" }, members: [], products: ["connect"], can_manage: true, your_role: "owner" });
      if (url === "/api/v1/orgs/org-a/people-extras") return reply({ by_user: {}, others: [] });
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<MemoryRouter><People /></MemoryRouter>);
    fireEvent.change(await screen.findByRole("textbox", { name: "Email" }), { target: { value: email } });
    fireEvent.click(screen.getByRole("button", { name: "Create invitation" }));
    const result = await screen.findByRole("region", { name: "Invitation link" });
    const status = within(result).getByRole("status").textContent;
    expect(status).toContain(outcome.expected);
    expect(status?.includes("Invitation sent to")).toBe(outcome.emailed);
    if (!outcome.emailed) expect(status).toContain(`Copy this link and send it to ${email}.`);
    expect(within(result).getByText(link)).toBeTruthy();
    fireEvent.click(within(result).getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(link);
  });

  it.each(outcomes)("shows $label truthfully for a new organisation and retains the owner link", async (outcome) => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/admin/organisations") return reply(init?.method === "POST"
        ? { id: "org-new", name: "Harbour", invite: { email, url: path, expires_at: "2026-11-08T12:00:00Z", emailed: outcome.emailed, email_sender: outcome.email_sender } }
        : []);
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<MemoryRouter><Organisations /></MemoryRouter>);
    fireEvent.change(screen.getByRole("textbox", { name: "Organisation name" }), { target: { value: "Harbour" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Owner's work email" }), { target: { value: email } });
    fireEvent.click(screen.getByRole("button", { name: "Create and invite the owner" }));
    const status = (await screen.findByRole("status")).textContent;
    expect(status).toContain(outcome.expected);
    expect(status?.includes("Invitation sent to")).toBe(outcome.emailed);
    if (!outcome.emailed) expect(status).toContain(`Copy this link and send it to ${email}.`);
    expect(screen.getByText(link)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Copy the link" }));
    expect(writeText).toHaveBeenCalledWith(link);
  });
});

describe("password reset requests", () => {
  it("keeps a configured-email response neutral about account existence and delivery", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ emailed: true }));
    render(<ForgotPage />);
    fireEvent.change(screen.getByRole("textbox", { name: "Email" }), { target: { value: email } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    const status = (await screen.findByRole("status")).textContent;
    expect(status).toContain(`If an account uses ${email}, check its inbox for a reset link.`);
    expect(status).not.toMatch(/on its way|email sent|link sent|queued/i);
  });

  it("gives an administrator fallback when email is unavailable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ emailed: false }));
    render(<ForgotPage />);
    fireEvent.change(screen.getByRole("textbox", { name: "Email" }), { target: { value: email } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    const status = (await screen.findByRole("status")).textContent;
    expect(status).toContain("Email isn't available for this service.");
    expect(status).toContain("Ask your administrator for help resetting your password.");
    expect(status).not.toMatch(/on its way|email sent|link sent|queued/i);
  });
});

describe("Jibsy notice preferences", () => {
  it("edits only in-app preferences without exposing or overwriting inactive email settings", async () => {
    const settings = {
      profile: { email, display_name: "Owner", given_name: "", family_name: "", managed_by_directory: false, seat: "agent", two_step: false },
      language: "en", languages_spoken: ["en"], language_choices: [{ code: "en", name: "English" }],
      availability: "online", timezone: "America/Port_of_Spain",
      notify: { assignment: { in_app: true, email: true }, mention: { in_app: false, email: true }, sla_warning: { in_app: true, email: false } },
      quiet_hours: { start: "22:00", end: "07:00" },
    };
    const patches: unknown[] = [];
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/commai/customers/org-a/me/settings") {
        if (init?.method === "PATCH") patches.push(JSON.parse(String(init.body)));
        return reply(settings);
      }
      if (url === "/api/v1/commai/customers/org-a/me/notifications") return reply([
        { kind: "mention", at: new Date().toISOString(), conversation_id: "conversation-a", text: "Ana mentioned you in a note" },
      ]);
      if (url === "/api/v1/commai/customers/org-a/me/sessions") return reply([]);
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<MemoryRouter><MySettings /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Notifications" });
    expect(within(section).getByText("These notices appear in Jibsy. Email notifications are not available.")).toBeTruthy();
    expect(within(section).getAllByRole("checkbox")).toHaveLength(3);
    expect(within(section).queryByRole("columnheader", { name: "By email" })).toBeNull();
    expect(within(section).queryByRole("checkbox", { name: /by email/i })).toBeNull();
    expect(within(section).queryByRole("group", { name: /quiet hours/i })).toBeNull();
    expect(section.querySelector('input[type="time"]')).toBeNull();
    expect((await within(section).findByRole("link", { name: "Ana mentioned you in a note" })).getAttribute("href")).toBe("/commai/c/conversation-a");

    fireEvent.click(within(section).getByRole("checkbox", { name: "A conversation is assigned to me: in Jibsy" }));
    fireEvent.click(within(section).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches).toEqual([
      { notify: { assignment: { in_app: false }, mention: { in_app: false }, sla_warning: { in_app: true } } },
    ]));
    await waitFor(() => expect(within(section).getByRole("status").textContent).toContain("Saved"));
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/me/notifications")).length).toBeGreaterThan(1);
    expect(within(section).getByRole("link", { name: "Ana mentioned you in a note" })).toBeTruthy();
  });
});
