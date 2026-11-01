// @vitest-environment jsdom
import { fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import widget from "../../../controller/exaconnect_controller/commai/static/widget.js?raw";

type ChatApi = { open(): void; close(): void };
let root: ShadowRoot;
let requests: { path: string; body: unknown }[];
let turns: { resolve: (value: Response) => void }[];
let starts: { resolve: (value: Response) => void }[];
let deferStart = false;
let nextCall = 0;
const speech = { speak: vi.fn(), cancel: vi.fn() };
const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
const api = () => (window as unknown as { ExaCaribChat: ChatApi }).ExaCaribChat;
const button = (text: string) => {
  const found = [...root.querySelectorAll("button")].find((node) => node.textContent === text);
  if (!found) throw new Error(`Missing button: ${text}`);
  return found;
};
const send = (text: string) => {
  const input = root.querySelector<HTMLInputElement>('input[aria-label="Or type what you want to say"]')!;
  fireEvent.change(input, { target: { value: text } });
  fireEvent.submit(input.closest("form")!);
};

beforeEach(async () => {
  requests = []; turns = []; starts = []; deferStart = false; nextCall = 0;
  speech.speak.mockClear(); speech.cancel.mockClear();
  localStorage.clear();
  Object.defineProperty(document, "readyState", { value: "complete", configurable: true });
  Object.assign(window, { ExaCaribChatSettings: { key: "test-widget" }, ExaCaribChat: undefined });
  vi.stubGlobal("speechSynthesis", speech);
  vi.stubGlobal("SpeechSynthesisUtterance", class { constructor(public text: string) {} });
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    const path = new URL(String(url)).pathname.replace("/api/v1/commai/widget/test-widget", "");
    requests.push({ path, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (path === "/config") return response({ ai_calls: true, ai: true, online: true, attachments: { enabled: false } });
    if (path === "/session") return response({ token: "session", expires_at: Date.now() / 1000 + 3600, conversations: [] });
    if (path === "/calls") {
      if (deferStart) return new Promise<Response>((resolve) => starts.push({ resolve }));
      return response({ conversation_id: `call-${++nextCall}`, greeting: "How can I help?" });
    }
    if (path.endsWith("/turns")) return new Promise<Response>((resolve) => turns.push({ resolve }));
    return response({});
  }));
  window.eval(widget);
  await waitFor(() => expect(document.querySelector("div")?.shadowRoot).toBeTruthy());
  root = document.querySelector("div")!.shadowRoot!;
  api().open();
  await waitFor(() => expect(button("Talk to our assistant")).toBeTruthy());
});

afterEach(() => {
  api()?.close();
  document.body.textContent = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("does not speak a late response after End call", async () => {
  fireEvent.click(button("Talk to our assistant"));
  await waitFor(() => expect(root.textContent).toContain("How can I help?"));
  send("First question");
  await waitFor(() => expect(turns).toHaveLength(1));
  fireEvent.click(button("End call"));
  expect(root.textContent).toContain("Call ended.");
  turns[0].resolve(response({ reply: "Late response", handed_over: false }));
  await waitFor(() => expect(speech.speak).toHaveBeenCalledTimes(1));
  expect(root.textContent).not.toContain("Late response");
});

it("serializes rapid turns and lets a new call proceed while an old request is hung", async () => {
  fireEvent.click(button("Talk to our assistant"));
  await waitFor(() => expect(root.textContent).toContain("How can I help?"));
  send("First"); send("Second");
  await waitFor(() => expect(turns).toHaveLength(1));
  turns[0].resolve(response({ reply: "First answer", handed_over: false }));
  await waitFor(() => expect(turns).toHaveLength(2));
  fireEvent.click(button("End call"));
  fireEvent.click(button("Back to chat"));
  fireEvent.click(button("Talk to our assistant"));
  await waitFor(() => expect(root.textContent).toContain("How can I help?"));
  send("New call");
  await waitFor(() => expect(turns).toHaveLength(3));
  expect(requests.filter((r) => r.path.endsWith("/turns")).map((r) => r.path)).toEqual([
    "/calls/call-1/turns", "/calls/call-1/turns", "/calls/call-2/turns",
  ]);
  turns[1].resolve(response({ reply: "Old call answer", handed_over: true }));
  turns[2].resolve(response({ reply: "New call answer", handed_over: false }));
  await waitFor(() => expect(root.textContent).toContain("New call answer"));
  expect(root.textContent).not.toContain("Old call answer");
});

it("ends a call created after the visitor cancelled connection", async () => {
  deferStart = true;
  fireEvent.click(button("Talk to our assistant"));
  await waitFor(() => expect(starts).toHaveLength(1));
  fireEvent.click(button("End call"));
  starts[0].resolve(response({ conversation_id: "late-start", greeting: "Too late" }));
  await waitFor(() => expect(requests.some((r) => r.path === "/calls/late-start/end")).toBe(true));
  expect(root.textContent).toContain("Call ended.");
  expect(speech.speak).not.toHaveBeenCalled();
});
