// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConnectWorkspaceTabs } from "../ConnectWorkspaceTabs";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function show(path: string) {
  return render(<MemoryRouter><ConnectWorkspaceTabs path={path} /></MemoryRouter>);
}

describe("Connect workspaces", () => {
  it.each([
    ["/sites", "Connected sites"],
    ["/sites/site-a", "Connected sites"],
    ["/locations", "Add or connect"],
    ["/locations/?search=Kingston#install", "Add or connect"],
    ["/org/locations", "Add or connect"],
  ])("keeps the Sites journey together at %s", (path, label) => {
    show(path);
    const nav = screen.getByRole("navigation", { name: "Sites workspace" });
    expect(within(nav).getAllByRole("link")).toHaveLength(2);
    expect(within(nav).getByRole("link", { current: "page" }).textContent).toBe(label);
    expect(within(nav).getByRole("link", { name: "Connected sites" }).getAttribute("href")).toBe("/sites");
    expect(within(nav).getByRole("link", { name: "Add or connect" }).getAttribute("href")).toBe("/locations");
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  it.each([
    ["/decisions?site_id=site-a", "Routing history"],
    ["/insights", "Risk and cost alerts"],
    ["/notices", "Carrier notices"],
  ])("keeps activity views together at %s with one current page", (path, label) => {
    show(path);
    const nav = screen.getByRole("navigation", { name: "Activity workspace" });
    expect(within(nav).getAllByRole("link")).toHaveLength(3);
    expect(within(nav).getAllByRole("link", { current: "page" })).toHaveLength(1);
    expect(within(nav).getByRole("link", { current: "page" }).textContent).toBe(label);
    expect(within(nav).getAllByRole("link").every((link) => !link.hasAttribute("tabindex"))).toBe(true);
  });

  it.each(["/", "/traffic/applications", "/metering", "/fabric", "/account", "/sites-other", "/commai"])("adds no unrelated controls at %s", (path) => {
    const view = show(path);
    expect(view.container.childElementCount).toBe(0);
  });

  it("keeps the active link in view inside an overflowing mobile workspace without scrolling the page", () => {
    const view = show("/decisions");
    const nav = screen.getByRole("navigation", { name: "Activity workspace" });
    const next = within(nav).getByRole("link", { name: "Carrier notices" });
    const scrollTo = vi.fn();
    Object.defineProperties(nav, { scrollWidth: { value: 460 }, clientWidth: { value: 280 }, scrollTo: { value: scrollTo } });
    Object.defineProperties(next, { offsetLeft: { value: 320 }, offsetWidth: { value: 140 } });
    view.rerender(<MemoryRouter><ConnectWorkspaceTabs path="/notices" /></MemoryRouter>);
    expect(scrollTo).toHaveBeenCalledWith({ left: 180 });
    expect(scrollTo.mock.instances[0]).toBe(nav);
  });
});
