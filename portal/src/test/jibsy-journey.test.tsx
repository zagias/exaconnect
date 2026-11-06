// @vitest-environment jsdom
import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "../api";
import AiAgents from "../pages/commai/AiAgents";
import Inbox from "../pages/commai/Inbox";
import Onboarding from "../pages/commai/Onboarding";
import Workflows from "../pages/commai/Workflows";
import Jibsy from "../pages/commai";
import { ManagementControls, ManagementNote, useJibsyManagement } from "../pages/commai/management";

const state = vi.hoisted(() => ({ user: null as User | null }));
vi.mock("../auth", () => ({ useAuth: () => ({ user: state.user }) }));
vi.mock("../customer", () => ({ useCustomer: () => ({ current: { id: "org-a", name: "Coral" } }), who: (v: string) => v }));
vi.mock("../pages/commai/live", () => ({ useLive: () => {} }));
vi.mock("../pages/commai/i18n", () => ({
  I18nProvider: ({ children }: { children: ReactNode }) => children,
  DraftLabel: () => null,
  useT: () => ({ t: (key: string) => ({ "inbox.title": "Inbox", "inbox.view.open": "Open", "inbox.view.all": "All" }[key] ?? key) }),
}));

const profile = { enabled: true, name: "Coral assistant", tone: "Friendly", greeting: "Hello", business_language: "en", languages: ["en"], memory: false, holding_reply: "A person will help", escalation: { keywords: [], max_ai_replies: 8, on_missing_knowledge: "escalate" } };
let seat = "agent";
let permissions = ["manage_integrations", "manage_teams"];
let rolesFail = false;
let allEmpty = false;
let reviewed = false;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function show(page: ReactNode, path: string, route: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path={route} element={page} /></Routes></MemoryRouter>);
}
function AccessProbe() {
  const access = useJibsyManagement("/commai/customers/org-a");
  return <><ManagementNote access={access} /><ManagementControls canManage={access.canManage}><button>Change settings</button></ManagementControls></>;
}
const disabled = (element: HTMLElement) => element.matches(":disabled");

beforeEach(() => {
  state.user = { email: "person@example.org", role: "customer", customer_id: "org-a", org_role: "member" };
  seat = "agent"; permissions = ["manage_integrations", "manage_teams"]; rolesFail = false; allEmpty = false; reviewed = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/me/settings")) return response({ profile: { seat } });
    if (path.endsWith("/roles/me")) return rolesFail ? response({ detail: "Access lookup failed" }, 503) : response({ has_roles: true, permissions });
    if (path.endsWith("/ai/status")) return response({ live: true, model: "Configured AI", enabled: true, mode: "human_first", speech: { note: "Browser speech" } });
    if (path.endsWith("/ai/profile")) return response(profile);
    if (path.endsWith("/ai/knowledge/source-a")) return response({ id: "source-a", title: "Opening hours", source_url: "", body: "Open Monday to Friday, 8am to 5pm." });
    if (path.includes("/ai/knowledge/search")) return response({ hits: [], contradictory: false });
    if (path.endsWith("/ai/knowledge")) return response([{ id: "source-a", title: "Opening hours", source_url: "", approved: true, chunks: 1, updated_at: "2026-11-01T10:00:00Z" }]);
    if (path.includes("/conversations?")) {
      const query = new URL(path, "https://example.invalid").searchParams;
      return response({ items: query.get("view") === "all" && !allEmpty && !query.get("q") ? [{ id: "resolved-a", state: "resolved", channel: "web", contact_name: "Existing customer", preview: "Resolved question", priority: "normal", created_at: "2026-11-01T10:00:00Z" }] : [], next: null });
    }
    if (path.endsWith("/onboarding")) return response({ batch: "batch-a", drafts: [
      { id: "wf-a", kind: "workflow", title: "Welcome workflow", content: { description: "Welcome new customers" }, status: reviewed ? "approved" : "draft", source: "rules" },
      { id: "ch-a", kind: "channel", title: "Website chat", content: { channel: "web", steps: ["Connect website chat"] }, status: reviewed ? "approved" : "draft", source: "rules" },
    ] });
    if (path.endsWith("/workflows/missing")) return response({ detail: "Workflow not found" }, 404);
    return response([]);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("inbox journey", () => {
  it("distinguishes an empty Open view from existing conversation history", async () => {
    show(<Inbox />, "/commai", "/commai");
    await screen.findByText("No open conversations");
    expect(screen.queryByText("No conversations yet")).toBeNull();
    expect(screen.queryByRole("link", { name: "View channels" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View all conversations" }));
    await screen.findByText("Existing customer");
  });

  it("offers channel guidance only for empty All and clears a failed search", async () => {
    allEmpty = true;
    show(<Inbox />, "/commai", "/commai");
    fireEvent.click(await screen.findByRole("button", { name: "View all conversations" }));
    await screen.findByText("No conversations yet");
    expect(screen.getByRole("link", { name: "View channels" }).getAttribute("href")).toBe("/commai/channels");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "unmatched" } });
    fireEvent.submit(screen.getByRole("search"));
    await screen.findByText("No conversations match");
    fireEvent.click(screen.getByRole("button", { name: "Clear search and filters" }));
    await screen.findByText("No conversations yet");
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("");
  });
});

describe("settings access", () => {
  it.each(["member", "viewer"] as const)("retains read-only settings for an organisation %s", (org_role) => {
    state.user!.org_role = org_role;
    render(<AccessProbe />);
    expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(true);
    expect(screen.getByText(/An organisation owner or admin can change them/)).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps internal seats and scoped owners read-only", async () => {
    state.user!.org_role = "owner"; seat = "internal";
    render(<AccessProbe />);
    await screen.findByText(/Your Jibsy seat can view these settings/);
    expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(true);
    cleanup();
    state.user!.scopes = ["commai:read"];
    render(<AccessProbe />);
    expect(screen.getByText(/does not have settings access/)).toBeTruthy();
    expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(true);
  });

  it("keeps staff admins available without customer seat checks", () => {
    state.user = { email: "staff@example.org", role: "admin", customer_id: null };
    render(<AccessProbe />);
    expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("explains pending and failed permission reads and recovers after retry", async () => {
    state.user!.org_role = "owner"; rolesFail = true;
    render(<AccessProbe />);
    expect(screen.getByText("Checking your settings access…")).toBeTruthy();
    expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(true);
    await screen.findByText(/Your settings access could not be checked/);
    rolesFail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(disabled(screen.getByRole("button", { name: "Change settings" }))).toBe(false));
  });

  it.each([
    ["manage_teams", false, true],
    ["manage_integrations", true, false],
  ] as const)("honours %s separately for mode and agent profile", async (permission, profileEditable, modeEditable) => {
    state.user!.org_role = "owner"; permissions = [permission];
    show(<AiAgents />, "/commai/ai", "/commai/ai/*");
    await screen.findByLabelText("Name customers see");
    await waitFor(() => expect(screen.queryByText("Checking your settings access…")).toBeNull());
    expect(disabled(screen.getByLabelText("Name customers see"))).toBe(!profileEditable);
    expect(disabled(screen.getByRole("radio", { name: /AI first/ }))).toBe(!modeEditable);
  });

  it("preserves source inspection and approved-knowledge search for members", async () => {
    show(<AiAgents />, "/commai/ai/knowledge", "/commai/ai/*");
    await screen.findByText("Opening hours");
    fireEvent.click(screen.getByRole("button", { name: "More actions for source Opening hours" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "View source" }));
    await screen.findByText("Open Monday to Friday, 8am to 5pm.");
    expect(screen.getByRole("button", { name: "Close source" })).toBeTruthy();
    expect(disabled(screen.getByRole("button", { name: "Search approved knowledge" }))).toBe(false);
    expect(screen.queryByRole("button", { name: "Add source" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Approve" })).toBeNull();
  });
});

it("keeps draft summaries visible without approval controls, then links reviewed work to its next steps", async () => {
  const page = show(<Onboarding />, "/commai/setup", "/commai/setup/*");
  await screen.findByText("Welcome new customers");
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(disabled(screen.getByRole("button", { name: "Draft my set-up" }))).toBe(true);
  page.unmount(); reviewed = true;
  show(<Onboarding />, "/commai/setup", "/commai/setup/*");
  await screen.findByText("Review complete.");
  expect(screen.getByRole("link", { name: "Review and publish in Workflows" }).getAttribute("href")).toBe("/commai/workflows");
  expect(screen.getByRole("link", { name: "Finish channel setup" }).getAttribute("href")).toBe("/commai/channels");
});

it.each([
  ["/commai/unknown", "/commai/*", "Jibsy page not found", "Go to Inbox", "jibsy"],
  ["/commai/ai/unknown", "/commai/ai/*", "AI agent page not found", "Open agent profile", "ai"],
  ["/commai/workflows/unknown/nested", "/commai/workflows/*", "Workflow page not found", "View workflows", "workflows"],
  ["/commai/workflows/missing", "/commai/workflows/*", "Workflow not found", "Back to workflows", "workflows"],
])("provides recovery for %s", async (path, route, text, link, page) => {
  show(page === "jibsy" ? <Jibsy /> : page === "ai" ? <AiAgents /> : <Workflows />, path, route);
  await screen.findByText(text);
  expect(screen.getByRole("link", { name: link })).toBeTruthy();
});
