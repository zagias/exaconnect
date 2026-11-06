// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "../auth";
import type { DesignReview, User } from "../api";
import Commercial from "../pages/Commercial";
import { clearPlanningQuoteHandoff, queuePlanningQuote } from "../pages/assistant/planningQuoteHandoff";
import type { PlanningBrief } from "../pages/assistant/PlanningBrief";

const organisation = vi.hoisted(() => ({ current: { id: "org-a", name: "Coral" } }));
vi.mock("../customer", () => ({ useCustomer: () => organisation, who: (name: string) => name }));
const owner: User = { email: "owner@example.invalid", role: "customer", customer_id: "org-a", org_role: "owner" };
const brief: PlanningBrief = { format: "xakore-planning-brief", version: 1, offer: "Connect",
  facts: Array.from({ length: 24 }, (_, i) => ({ label: `Need ${i}`, value: i === 0 ? "20 existing shops" : "Confirmed requirement " + "x".repeat(200) })),
  gaps: ["New locations and carrier availability still need checking."] };
const review: DesignReview = { required: true, status: "pending", current_digest: "a".repeat(64), approved_digest: null,
  reviewed_by: null, reviewed_at: null, note: null };
const totals = { one_off_total: "150.00", monthly_total: "0.00", usage_estimate: "0.00", contract_total: "150.00", needs_approval: false, example: false };
const item = { id: "item-a", code: "design.link", name: "Pilot link", product: "connect", active: true, example: false,
  unit: "Mbps", charge_type: "one_off", fulfilment: "link", max_discount_pct: "10", price_text: "USD 15 per Mbps",
  price: { example: false, unit_price: "15" }, planning_match: { reason: "Optional service for the reviewed scope.", price_available: true, price_text: "USD 15 per Mbps" } };
const line = { ...item, item_id: "item-a", position: 1, quantity: "10", config: { location: "Pilot A", carrier: "Fictional carrier", underlay_type: "fibre" }, description: "Pilot link",
  list_amount: "150", amount: "150", term_discount_pct: "0", discount_pct: "0", needs_approval: false };
function quote(priced = false) {
  return { ...totals, ...(priced ? {} : { one_off_total: "0.00", contract_total: "0.00" }), id: "quote-a", number: "Q-TEST", customer_id: "org-a", customer: "Coral", title: "Pilot scope",
    currency: "USD", term_months: 12, valid_until: "2026-12-31", status: "draft", state: "draft", state_label: "Draft",
    notes: "", lines: priced ? [line] : [], order: null, planning_source: brief, design_review: { ...review }, pricing_status: priced ? "priced" : "pending", pricing_complete: priced };
}
function response(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }); }
function app(path = "/quotes/new?from=planning") {
  return <MemoryRouter initialEntries={[path]}><AuthGate><Routes>
    <Route path="/quotes/*" element={<Commercial />} /><Route path="/admin/quotes/*" element={<Commercial />} />
  </Routes></AuthGate></MemoryRouter>;
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function session(handler?: (url: string, init?: RequestInit) => Promise<Response> | Response, user = owner) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return response(user);
    if (handler) return handler(url, init);
    return response({ detail: "Unexpected test request" }, 500);
  });
}
function posts(fetch: ReturnType<typeof session>, path: string) {
  return fetch.mock.calls.filter(([url, init]) => String(url).endsWith(path) && init?.method === "POST");
}
async function confirmOrganisation() {
  fireEvent.click(await screen.findByRole("checkbox", { name: /Quote for Coral/ }));
}
beforeEach(() => { organisation.current = { id: "org-a", name: "Coral" }; clearPlanningQuoteHandoff(); });
afterEach(() => { cleanup(); clearPlanningQuoteHandoff(); vi.restoreAllMocks(); });

describe("design-to-quote workflow", () => {
  it("saves an explicitly confirmed scope without presenting an unpriced draft as free", async () => {
    const fetch = session((url, init) => {
      if (url.endsWith("/quotes") && init?.method === "POST") return response(quote(), 201);
      if (url.endsWith("/quotes/quote-a")) return response(quote());
      return response({ ...totals, lines: [], pricing_complete: false, design_review: review });
    });
    const storage = vi.spyOn(Storage.prototype, "setItem");
    queuePlanningQuote(owner, "org-a", brief);
    render(app());
    const save = await screen.findByRole("button", { name: "Save draft for Coral" });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    await confirmOrganisation(); fireEvent.click(save);
    await screen.findByText("Scope draft — pricing pending. No priced offer is ready.");
    const body = JSON.parse(String(posts(fetch, "/quotes")[0][1]?.body));
    expect(body).toMatchObject({ customer_id: "org-a", planning_source: brief, lines: [] });
    expect(body.creation_key).toMatch(/^[0-9a-f-]{36}$/);
    expect(storage).not.toHaveBeenCalled();
    expect(screen.queryByText(/^USD 0\.00$/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Finish quote" })).toBeNull();
    expect(posts(fetch, "/send")).toHaveLength(0);
    expect(posts(fetch, "/accept")).toHaveLength(0);
  });

  it("offers real priced matches with empty quantities and suppresses stale price replies", async () => {
    const first = deferred<Response>(), second = deferred<Response>();
    const fetch = session((url, init) => {
      if (url.endsWith("/design-options")) return response({ customer_id: "org-a", source: brief, product: "connect", warnings: [], questions: [], items: [
        item, { ...item, code: "unpriced", name: "Unpriced service", price: null, planning_match: { ...item.planning_match, price_available: false, blocked_reason: "Needs pricing" } },
        { ...item, code: "example", name: "Example service", example: true },
        { ...item, code: "example-price", name: "Example price", price: { example: true } },
      ] });
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (url.endsWith("/preview") && body.lines?.[0]?.quantity === "10") return first.promise;
      if (url.endsWith("/preview") && body.lines?.[0]?.quantity === "20") return second.promise;
      return response({ ...totals, lines: [], pricing_complete: false });
    });
    queuePlanningQuote(owner, "org-a", brief); render(app());
    await confirmOrganisation(); fireEvent.click(screen.getByRole("button", { name: "Find matching items" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add Pilot link" }));
    expect((screen.getByRole("button", { name: "Add Unpriced service" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText("Example service")).toBeNull(); expect(screen.queryByText("Example price")).toBeNull();
    const quantity = screen.getByRole("spinbutton", { name: /Quantity for Pilot link/ }) as HTMLInputElement;
    expect(quantity.value).toBe(""); expect((screen.getByRole("combobox", { name: "Link type" }) as HTMLSelectElement).value).toBe("");
    fireEvent.change(screen.getByRole("combobox", { name: "Link type" }), { target: { value: "fibre" } });
    fireEvent.change(quantity, { target: { value: "10" } });
    await waitFor(() => expect(posts(fetch, "/preview").some(([, init]) => JSON.parse(String(init?.body)).lines?.[0]?.quantity === "10")).toBe(true));
    fireEvent.change(quantity, { target: { value: "20" } });
    await waitFor(() => expect(posts(fetch, "/preview").some(([, init]) => JSON.parse(String(init?.body)).lines?.[0]?.quantity === "20")).toBe(true));
    await act(async () => second.resolve(response({ ...totals, one_off_total: "300.00", contract_total: "300.00", lines: [line], pricing_complete: true })));
    await screen.findAllByText("USD 300.00");
    await act(async () => first.resolve(response({ ...totals, lines: [line], pricing_complete: true })));
    expect(screen.queryByText("USD 150.00")).toBeNull();
    expect((screen.getByRole("button", { name: "Save draft for Coral" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("retains one creation identity after an uncertain save and later edits", async () => {
    const uncertain = deferred<Response>(); let attempt = 0;
    const fetch = session((url, init) => {
      if (url.endsWith("/quotes") && init?.method === "POST") return ++attempt === 1 ? uncertain.promise : response(quote(), 201);
      if (url.endsWith("/quotes/quote-a")) return response(quote());
      return response({ ...totals, lines: [], pricing_complete: false });
    });
    queuePlanningQuote(owner, "org-a", brief); render(app()); await confirmOrganisation();
    fireEvent.click(screen.getByRole("button", { name: "Save draft for Coral" }));
    fireEvent.click(screen.getByRole("button", { name: "Saving…" }));
    expect(posts(fetch, "/quotes")).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "Title" }).matches(":disabled")).toBe(true);
    await act(async () => uncertain.reject(new Error("Connection ended before confirmation.")));
    await screen.findByText(/Check Quotes before starting another/);
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: "Revised pilot scope" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft for Coral" }));
    await screen.findByText("Scope draft — pricing pending. No priced offer is ready.");
    const bodies = posts(fetch, "/quotes").map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies).toHaveLength(2); expect(bodies[0].creation_key).toBe(bodies[1].creation_key);
    expect(bodies[1].title).toBe("Revised pilot scope");
  });

  it("discards late catalogue results when the working organisation changes", async () => {
    const options = deferred<Response>();
    const fetch = session((url) => url.endsWith("/design-options") ? options.promise : response({ ...totals, lines: [] }));
    queuePlanningQuote(owner, "org-a", brief); const view = render(app()); await confirmOrganisation();
    fireEvent.click(screen.getByRole("button", { name: "Find matching items" }));
    organisation.current = { id: "org-b", name: "Seagrape" }; view.rerender(app());
    await screen.findByText("Planning quote unavailable");
    await act(async () => options.resolve(response({ customer_id: "org-a", source: brief, items: [item], questions: [], warnings: [] })));
    expect(screen.queryByRole("button", { name: "Add Pilot link" })).toBeNull();
    expect(posts(fetch, "/quotes")).toHaveLength(0);
  });

  it("preserves saved fulfilment details until catalogue metadata is available", async () => {
    const catalogue = deferred<Response>();
    const q = quote(true);
    const fetch = session((url) => {
      if (url.endsWith("/catalogue/items")) return catalogue.promise;
      if (url.endsWith("/preview")) return response({ ...totals, lines: [line], pricing_complete: true });
      return response(q);
    });
    render(app("/quotes/quote-a/edit"));
    await screen.findByDisplayValue("Pilot scope");
    await confirmOrganisation();
    const save = screen.getByRole("button", { name: "Save draft for Coral" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Notes on the quote" }), { target: { value: "Reviewed delivery notes" } });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 350)); });
    expect(posts(fetch, "/preview")).toHaveLength(0);
    await act(async () => catalogue.resolve(response({ items: [item] })));
    await waitFor(() => expect(save.disabled).toBe(false));
    const preview = JSON.parse(String(posts(fetch, "/preview").at(-1)?.[1]?.body));
    expect(preview.lines[0].config).toEqual(line.config);
    expect(preview.planning_source).toEqual(brief);
    expect(preview.creation_key).toBeUndefined();
    fireEvent.click(save);
    await screen.findByText("Quote Q-TEST");
    const edits = fetch.mock.calls.filter(([, init]) => init?.method === "PUT");
    expect(edits).toHaveLength(1);
    expect(JSON.parse(String(edits[0][1]?.body)).lines[0].config).toEqual(line.config);
  });

  it("requires a separate staff attestation and never sends as a side effect", async () => {
    const staff: User = { email: "staff@example.invalid", role: "admin", customer_id: null };
    let q = quote(true);
    const fetch = session((url) => {
      if (url.endsWith("/design-review")) {
        q = { ...q, design_review: { ...review, status: "approved", approved_digest: review.current_digest,
          reviewed_by: "user:staff@example.invalid", reviewed_at: "2026-11-04T12:00:00Z", note: "Scope checked." } };
        return response(q);
      }
      return response(q);
    }, staff);
    render(app("/admin/quotes/quote-a"));
    await screen.findByText("Review design scope");
    expect(screen.getByText("Quantity: 10 Mbps")).toBeTruthy();
    expect(screen.getByText("Catalogue item: design.link")).toBeTruthy();
    expect(screen.getByText("Fictional carrier")).toBeTruthy();
    expect(screen.getByText("Pilot A")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send to the organisation" })).toBeNull();
    const approve = screen.getByRole("button", { name: "Approve design scope" }) as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Commercial review note" }), { target: { value: "Scope and supplier assumptions checked." } });
    fireEvent.click(screen.getByRole("checkbox", { name: /I have reviewed these requirements/ }));
    fireEvent.click(approve);
    await screen.findByRole("button", { name: "Send to the organisation" });
    expect(posts(fetch, "/design-review")).toHaveLength(1);
    expect(JSON.parse(String(posts(fetch, "/design-review")[0][1]?.body))).toMatchObject({ digest: review.current_digest });
    expect(posts(fetch, "/send")).toHaveLength(0); expect(posts(fetch, "/accept")).toHaveLength(0);
  });
});
