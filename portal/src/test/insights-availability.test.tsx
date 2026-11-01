// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Insight } from "../api";
import Insights from "../pages/Insights";

vi.mock("../auth", () => ({ useAuth: () => ({ user: { role: "customer", org_role: "viewer", customer_id: "org-a" } }) }));
vi.mock("../customer", () => ({
  useCustomer: () => ({ current: { id: "org-a", name: "Coral", sites: [] } }),
  useStormToggle: () => ({ current: { sites: [] }, busy: false, error: null, toggle: vi.fn() }),
  who: (actor: string | null) => actor ?? "unknown",
}));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function show(response: () => Promise<Response>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(response);
  return render(<MemoryRouter><Insights /></MemoryRouter>);
}

describe("alert availability", () => {
  it("withholds the empty-state claim until the alert request completes", async () => {
    let complete!: (response: Response) => void;
    show(() => new Promise((resolve) => { complete = resolve; }));
    expect(screen.getByText("Loading alerts…")).toBeTruthy();
    expect(screen.queryByText("Nothing to flag right now.")).toBeNull();
    await act(async () => complete(new Response("[]", { status: 200 })));
    await screen.findByText("Nothing to flag right now.");
    expect(screen.queryByText("Loading alerts…")).toBeNull();
  });

  it("does not present a failed initial check as an empty alert list", async () => {
    show(async () => new Response(JSON.stringify({ detail: "Alert check failed for this test" }), { status: 503 }));
    await screen.findByText("Alert check failed for this test");
    expect(screen.getByText("Current alert status is unavailable.")).toBeTruthy();
    expect(screen.queryByText("Nothing to flag right now.")).toBeNull();
  });

  it.each([false, true])("marks retained alerts as last known after a refresh failure (has alerts: %s)", async (hasAlert) => {
    let refresh!: () => void;
    vi.spyOn(globalThis, "setInterval").mockImplementation(((callback: TimerHandler, delay?: number) => {
      if (delay === 30_000) refresh = callback as () => void;
      return 1;
    }) as typeof setInterval);
    const alert: Insight = { id: 1, customer_id: "org-a", kind: "anomaly", severity: "warning", title: "Connection needs review",
      detail: "Recent latency exceeded its usual range.", site: null, path: null, carrier: null, example: false,
      first_seen: "2026-11-01T10:00:00Z", last_seen: "2026-11-01T10:00:00Z", resolved_at: null,
      acknowledged_at: null, acknowledged_by: null, data: {} };
    let failing = false;
    show(async () => failing
      ? new Response(JSON.stringify({ detail: "Alert refresh failed for this test" }), { status: 503 })
      : new Response(JSON.stringify(hasAlert ? [alert] : []), { status: 200 }));
    await screen.findByText(hasAlert ? alert.title : "Nothing to flag right now.");
    failing = true;
    await act(async () => refresh());
    await screen.findByText("Showing last known alerts. Current alert status is unavailable.");
    expect(screen.queryByText("Nothing to flag right now.")).toBeNull();
    if (hasAlert) expect(screen.getByText(alert.title)).toBeTruthy();
  });
});
