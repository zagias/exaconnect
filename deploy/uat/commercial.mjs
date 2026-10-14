// Quote to invoice, tested as people use it (ADR 0055): ExaCarib staff build a quote from the
// catalogue with a discount above an item's limit, ask for approval, approve and send it; the
// organisation's owner accepts it on a phone-sized screen; the order sets up the plan, two
// sites and a link by itself; staff mark the box and installation done; usage is recorded;
// and the month's draft invoice carries the contract, the one-off charges and the rated usage.
// Run by lab/ci/checks/commercial.sh against the live portal after each deploy, and locally:
//   UAT_BASE=http://localhost:4173 STAFF_EMAIL=... STAFF_PW=... OWNER_EMAIL=... OWNER_PW=... \
//   ORG_ID=... ORG_NAME=... node commercial.mjs
// deploy/uat/commercial_users.py makes the organisation and removes the last run's.
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const E = process.env;
const B = (E.UAT_BASE || "http://localhost:4173").replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));
const br = await chromium.launch(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {});

async function session(email, pw, width = 1440) {
  const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width, height: 900 } });
  // Staff work on the test organisation (the "Working on" switcher remembers it here).
  await ctx.addInitScript((id) => { try { localStorage.setItem("exa.customer", id); } catch { /* none */ } }, E.ORG_ID);
  const p = await ctx.newPage();
  p.on("dialog", (d) => d.accept(d.type() === "prompt" ? "Shipped, test run" : undefined));
  p.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
  p.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad(`API ${r.status()} ${r.url()}`); });
  await p.goto(B + "/");
  await p.getByLabel("Work email").fill(email);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(pw);
  await p.getByRole("button", { name: /Sign in/ }).click();
  await p.waitForSelector(".shell main#main", { timeout: 15000 });
  return p;
}
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);
const call = async (p, method, path, data) => {
  const r = await p.request.fetch(`${B}/api/v1${path}`, {
    method, data, headers: { "x-requested-with": "exa-portal" }, ignoreHTTPSErrors: true,
  });
  return { status: r.status(), body: await r.json().catch(() => ({})) };
};

try {
  // 1. Staff: the catalogue, then a quote built in the portal and priced live.
  const staff = await session(E.STAFF_EMAIL, E.STAFF_PW);
  await staff.goto(B + "/admin/catalogue");
  await staff.getByText("connect.engineer-hours").waitFor({ timeout: 15000 });
  check((await staff.getByText("Example data").count()) > 0, "catalogue shows its example prices, labelled");
  await staff.goto(B + "/admin/quotes/new");
  await staff.getByText(`Quote for ${E.ORG_NAME}`).waitFor({ timeout: 15000 });
  await staff.getByLabel("Item 1").selectOption("connect.plan");
  const add = async (n, code, qty, extra) => {
    await staff.getByRole("button", { name: "Add an item" }).click();
    await staff.getByLabel(`Item ${n}`).selectOption(code);
    const box = staff.locator("form .card-inset").nth(n - 1);
    await box.locator('input[type="number"]').first().fill(qty);
    if (extra) await extra(box);
  };
  await add(2, "connect.site", "2", async (b) => {
    await b.locator('input[type="number"]').nth(1).fill("10");
    await b.getByPlaceholder("Kingston HQ, Montego Bay").fill("Kingston HQ, Montego Bay");
  });
  await add(3, "connect.link", "100", async (b) => {
    await b.getByPlaceholder("Kingston HQ").fill("Kingston HQ");
    await b.getByPlaceholder("Flow").fill("Test Carrier");
  });
  await add(4, "connect.managed", "2");
  await add(5, "connect.box", "2");
  await add(6, "connect.install", "2", (b) => b.locator('input[type="number"]').nth(1).fill("30"));
  await add(7, "connect.engineer-hours", "5");
  await staff.getByLabel("Contract term (months)").fill("36");
  // Example prices: sites 150, commit 6/Mbps (plan price list); managed 120 less 10% for 36 months;
  // box 450; installation 300 less 30%; engineer time 85/hour after 2 included.
  const priced = await staff.getByText("40,416.00").waitFor({ timeout: 15000 }).then(() => true, () => false);
  check(priced, "builder prices the quote live: USD 40,416.00 over 36 months");
  check((await staff.getByText("Needs approval").count()) === 1, "30% off installation is flagged for approval");
  await shot(staff, "01_builder");
  await staff.getByRole("button", { name: "Save draft" }).click();
  await staff.getByRole("button", { name: "Ask for approval" }).click();
  await staff.getByPlaceholder("Launch customer, two sites").fill("Live test run");
  await staff.getByRole("button", { name: "Approve", exact: true }).click();
  await staff.getByRole("button", { name: "Send to the organisation" }).click();
  const sent = await staff.getByText(`Sent to ${E.ORG_NAME}.`).waitFor({ timeout: 10000 }).then(() => true, () => false);
  check(sent, "quote approved and sent");
  const number = (await staff.locator(".bill-doc-head h2").innerText()).split(/\s/)[0];

  // 2. The owner, on a phone: reads the quote and accepts it.
  const owner = await session(E.OWNER_EMAIL, E.OWNER_PW, 390);
  await owner.goto(B + "/quotes");
  await owner.getByRole("link", { name: number }).click();
  await owner.getByLabel("Full name").fill("Lab Test Owner");
  await shot(owner, "02_accept_phone");
  await owner.getByRole("button", { name: "Accept and order" }).click();
  await owner.waitForURL(/\/orders\//, { timeout: 15000 });
  await owner.getByText("Waiting", { exact: true }).first().waitFor({ timeout: 10000 });
  check((await owner.getByText("Done", { exact: true }).count()) === 5, "order set up the plan, sites, link, service and usage item by itself");
  check((await owner.getByText("Waiting", { exact: true }).count()) === 2, "box and installation wait for ExaCarib staff");
  await shot(owner, "03_order_phone");
  const orderPath = new URL(owner.url()).pathname;

  // 3. Staff mark the manual items done.
  await staff.goto(B + "/admin" + orderPath);
  for (const name of ["ExaCarib box", "Site installation"]) {
    await staff.locator("tr", { hasText: name }).getByRole("button").first().click();
    await staff.getByRole("menuitem", { name: "Mark done" }).click();
    await staff.waitForTimeout(1000);
  }
  const done = await staff.getByText("Completed", { exact: true }).first().waitFor({ timeout: 10000 }).then(() => true, () => false);
  check(done, "order completed once staff marked the box and installation done");
  await shot(staff, "04_order_staff");

  // 4. Usage, then the month's draft invoice.
  const at = new Date().toISOString();
  const events = [2, 1.5, 1.5].map((q, i) => ({ meter: "engineer_hours", quantity: q, at, ref: `lab-${Date.now()}-${i}` }));
  const u = await call(staff, "POST", "/usage-events", { customer_id: E.ORG_ID, events });
  check(u.status === 200 && u.body.added === 3, `usage recorded (${u.status} ${JSON.stringify(u.body).slice(0, 80)})`);
  const again = await call(staff, "POST", "/usage-events", { customer_id: E.ORG_ID, events });
  check(again.body.duplicates === 3, "the same usage sent again counts once");
  const period = at.slice(0, 7);
  const g = await call(staff, "POST", "/billing/invoices/generate", { period, customer_id: E.ORG_ID });
  const draft = (g.body.drafts || [])[0];
  check(g.status === 200 && draft, `draft invoice generated (${g.status})`);
  if (draft) {
    const kinds = new Set(draft.lines.map((l) => l.kind));
    check(["recurring", "credit", "one_off", "rated", "site", "commit"].every((k) => kinds.has(k)), `invoice has contract, discount, one-off, usage, site and link lines (${[...kinds].join(", ")})`);
    const amt = (kind, start) => draft.lines.filter((l) => l.kind === kind && l.description.startsWith(start)).map((l) => l.amount);
    check(JSON.stringify(amt("one_off", "ExaCarib box")) === '["900.00"]' && JSON.stringify(amt("one_off", "Site installation")) === '["420.00"]', "one-off charges: box 900.00, installation 420.00");
    check(JSON.stringify(amt("rated", "Engineer time")) === '["255.00"]', "usage rated: 5 hours, 2 included, 3 x 85 = 255.00");
    const sum = draft.lines.reduce((s, l) => s + Math.round(Number(l.amount) * 100), 0);
    check(sum === Math.round(Number(draft.subtotal) * 100), "invoice subtotal is the sum of its lines");
    const page = await staff.context().newPage();
    const r = await page.goto(`${B}/api/v1/billing/invoices/${draft.id}/print`);
    check(r.status() === 200 && (await page.content()).includes("Engineer time"), "printable invoice shows the rated usage");
  }

  // 5. The owner sees what was bought on Billing.
  const desk = await session(E.OWNER_EMAIL, E.OWNER_PW);
  await desk.goto(B + "/billing");
  const bought = await desk.getByText("What you've bought").waitFor({ timeout: 15000 }).then(() => true, () => false);
  check(bought && (await desk.getByText("Managed network service x 2").count()) > 0, "owner's Billing page shows the contract");
  await shot(desk, "05_billing");
} catch (e) {
  bad("test stopped: " + String(e).split("\n")[0].slice(0, 300));
}
await br.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
