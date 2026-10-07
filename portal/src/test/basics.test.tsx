// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { num } from "../api";
import { ago, ExampleTag, StatusPill } from "../components";
import { RowActions } from "../menu";
import { ReleasesAdmin } from "../pages/ReleasesAdmin";
import { PageHead } from "../ui";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("formatting", () => {
  it("reads numbers the controller sends as strings", () => {
    expect(num("12.5")).toBe(12.5);
    expect(num("")).toBeNull();
    expect(num(null)).toBeNull();
    expect(num("n/a")).toBeNull();
  });
  it("says how long ago", () => {
    expect(ago(null)).toBe("never");
    expect(ago(new Date(Date.now() - 30_000).toISOString())).toBe("30 s ago");
    expect(ago(new Date(Date.now() - 2 * 3600_000).toISOString())).toBe("2 h ago");
  });
});

describe("components", () => {
  it("status pills always carry a word, not only a colour", () => {
    render(<StatusPill health="bad">Revoked</StatusPill>);
    expect(screen.getByText("Revoked").className).toContain("pill bad");
  });
  it("labels example data", () => {
    render(<ExampleTag />);
    expect(screen.getByText("Example data")).toBeTruthy();
  });
  it("page heads show no example tag without an example organisation", () => {
    render(
      <MemoryRouter>
        <PageHead eyebrow="Admin" title="Administration" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Administration" })).toBeTruthy();
    expect(screen.queryByText("Example data")).toBeNull();
  });
});

describe("row actions", () => {
  it("opens with the keyboard and runs the chosen action", async () => {
    const onSelect = vi.fn();
    render(<RowActions label="site-a" items={[{ label: "Revoke certificate", danger: true, onSelect }]} />);
    const button = screen.getByRole("button", { name: /site-a/ });
    fireEvent.keyDown(button, { key: "ArrowDown" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Revoke certificate" }));
    await waitFor(() => expect(onSelect).toHaveBeenCalledOnce());
  });
});

describe("releases screen", () => {
  it("lists releases with their result and the running commit", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          running: { version: "0.2.0", commit: "abc123def456" },
          releases: [
            {
              id: 2,
              commit: "222222222222",
              previous: "111111111111",
              started_at: "2026-10-07T01:00:00Z",
              finished_at: "2026-10-07T01:05:00Z",
              status: "rolled_back",
              kind: "release",
              backed_up: true,
              detail: "portal not served",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <MemoryRouter>
        <ReleasesAdmin />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Rolled back")).toBeTruthy());
    expect(screen.getByText("abc123def456")).toBeTruthy();
    expect(screen.getByText("portal not served")).toBeTruthy();
    expect(screen.getByText("2222222")).toBeTruthy();
  });
});
