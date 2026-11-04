// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parsePlanningBrief, PlanningBriefImport } from "../pages/assistant/PlanningBrief";

const brief = { format: "xakore-planning-brief", version: 1, offer: "Connect", facts: [{ label: "Sites", value: "Twenty shops" }], gaps: ["Independent backup?"] };
afterEach(() => cleanup());

describe("reviewed website brief import", () => {
  it("preserves all 24 notes and open questions", () => {
    const full = { ...brief, facts: Array.from({ length: 24 }, (_, i) => ({ label: "Site " + i, value: "A reviewed requirement" })) };
    expect(parsePlanningBrief(JSON.stringify(full))).toEqual(full);
  });

  it.each([
    { ...brief, customer_id: "foreign" }, { ...brief, version: true },
    { ...brief, actions: [{ action: "storm_mode" }] }, { ...brief, facts: Array(25).fill(brief.facts[0]) },
    { ...brief, facts: [{ label: "Site", value: "x".repeat(501) }] },
    { ...brief, facts: [{ label: "Password", value: "hunter-two" }] },
    { ...brief, facts: [{ label: "Account", value: "4111 1111 1111 1111" }] },
    { ...brief, facts: [], gaps: [] },
    { ...brief, facts: [{ label: "Site", value: "text\0invalid" }] },
    { ...brief, facts: Array(24).fill({ label: "Note", value: "界".repeat(500) }) },
  ])("rejects invalid, sensitive or authority-bearing input", (value) => {
    expect(() => parsePlanningBrief(JSON.stringify(value))).toThrow();
  });

  it("accepts open questions without inventing facts", () => {
    expect(parsePlanningBrief(JSON.stringify({ ...brief, facts: [] })).facts).toEqual([]);
  });

  it("requires a preview and explicit organisation choice; importing makes no network request", () => {
    const use = vi.fn(), network = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("No request expected"));
    render(<PlanningBriefImport organisation="Coral" initialOpen onUse={use} />);
    fireEvent.change(screen.getByLabelText("Paste your planning brief"), { target: { value: JSON.stringify(brief) } });
    fireEvent.click(screen.getByRole("button", { name: "Review notes" }));
    expect(use).not.toHaveBeenCalled();
    expect(network).not.toHaveBeenCalled();
    expect(screen.getByText(/not verified account facts/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use these notes for Coral" }));
    expect(use).toHaveBeenCalledWith(brief);
    expect(network).not.toHaveBeenCalled();
    network.mockRestore();
  });

  it("can edit or discard without using notes", () => {
    const use = vi.fn();
    render(<PlanningBriefImport organisation="Coral" initialOpen onUse={use} />);
    fireEvent.change(screen.getByLabelText("Paste your planning brief"), { target: { value: JSON.stringify(brief) } });
    fireEvent.click(screen.getByRole("button", { name: "Review notes" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit pasted brief" }));
    expect((screen.getByLabelText("Paste your planning brief") as HTMLTextAreaElement).value).toBe(JSON.stringify(brief));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(use).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Paste your planning brief")).toBeNull();
  });
});

