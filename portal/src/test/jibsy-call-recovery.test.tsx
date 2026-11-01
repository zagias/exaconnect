// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AiAgents from "../pages/commai/AiAgents";

vi.mock("../pages/commai/lib", () => ({ useCommaiBase: () => "/commai/customers/business-a" }));
const speech = { speak: vi.fn(), cancel: vi.fn() };
let turn: (value: Response) => void;
let starting: (value: Response) => void;
let delayStart = false;
const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const show = () => render(<MemoryRouter initialEntries={["/commai/ai/call"]}>
  <Routes><Route path="/commai/ai/*" element={<AiAgents />} /></Routes>
</MemoryRouter>);

beforeEach(() => {
  delayStart = false;
  speech.speak.mockClear(); speech.cancel.mockClear();
  vi.stubGlobal("speechSynthesis", speech);
  vi.stubGlobal("SpeechSynthesisUtterance", class { constructor(public text: string) {} });
  vi.stubGlobal("fetch", vi.fn(async (url) => {
    const path = String(url);
    if (path.endsWith("/ai/status")) return response({ live: true, speech: { note: "Browser speech" } });
    if (path.endsWith("/ai/profile")) return response({ business_language: "en" });
    if (path.endsWith("/ai/calls")) {
      if (delayStart) return new Promise<Response>((resolve) => { starting = resolve; });
      return response({ conversation_id: "test-call", greeting: "Hello caller", handler: "ai" });
    }
    if (path.endsWith("/turns")) return new Promise<Response>((resolve) => { turn = resolve; });
    return response({});
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("allows End call during a pending reply and never speaks the late response", async () => {
  show();
  fireEvent.click(screen.getByRole("button", { name: "Start a call" }));
  await screen.findByText("Hello caller");
  fireEvent.change(screen.getByLabelText("Or type what the caller says"), { target: { value: "Question" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(turn).toBeTypeOf("function"));
  const end = screen.getByRole("button", { name: "End call" });
  expect((end as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(end);
  expect(screen.getByRole("button", { name: "Start another call" })).toBeTruthy();
  turn(response({ reply: "Late reply", handed_over: false, messages: [{ author_kind: "ai", body: "Late reply" }] }));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/test-call/end"))).toBe(true));
  expect(speech.speak).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Late reply")).toBeNull();
});

it("ends an active test call when the operator leaves the screen", async () => {
  const page = show();
  fireEvent.click(screen.getByRole("button", { name: "Start a call" }));
  await screen.findByText("Hello caller");
  page.unmount();
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/test-call/end"))).toBe(true));
  expect(speech.cancel).toHaveBeenCalled();
});

it("cleans up a delayed call start after the operator leaves", async () => {
  delayStart = true;
  const page = show();
  fireEvent.click(screen.getByRole("button", { name: "Start a call" }));
  await waitFor(() => expect(starting).toBeTypeOf("function"));
  page.unmount();
  starting(response({ conversation_id: "late-call", greeting: "Too late", handler: "ai" }));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/late-call/end"))).toBe(true));
  expect(speech.speak).not.toHaveBeenCalled();
});
