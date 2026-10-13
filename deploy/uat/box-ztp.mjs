// Zero-touch installation (ADR 0050), tested as ExaCarib staff do it: a fresh box is
// plugged into an internet line and calls home; staff find its serial number in
// Operations > Boxes and assign it to the customer's location; the box joins Connect
// by itself and shows Online, with nobody touching the box.
// Run by lab/ci/checks/box-ztp.sh after each deploy:
//   PORTAL_BASE=https://connect.exacarib.com PORTAL_EMAIL=... PORTAL_PW=... SERIAL=... ORG_NAME=... LOCATION_NAME=... node box-ztp.mjs
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const E = process.env;
const P = E.PORTAL_BASE.replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const br = await chromium.launch(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {});
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);

let q;
try {
  const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 900 } });
  q = await ctx.newPage();
  q.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
  await q.goto(P + "/");
  await q.getByLabel("Work email").fill(E.PORTAL_EMAIL); await q.getByRole("button", { name: "Continue" }).click();
  await q.getByLabel("Password").fill(E.PORTAL_PW); await q.getByRole("button", { name: "Sign in" }).click();
  await q.waitForSelector(".shell main#main", { timeout: 15000 });
  await q.goto(P + "/admin/boxes");
  const row = q.locator(`tr[data-serial="${E.SERIAL}"]`);
  await row.waitFor({ timeout: 30000 }).then(() => ok("the box that called home is in Operations > Boxes"), () => bad("box " + E.SERIAL + " not listed"));
  const first = await row.innerText();
  first.includes("Waiting to be assigned") && /key /.test(first) ? ok("listed as waiting, with its key") : bad("row before assigning: " + first.slice(0, 200));
  await shot(q, "21_boxes_waiting");
  const pick = row.getByLabel(`Location for ${E.SERIAL}`);
  const option = await pick.locator("option", { hasText: `${E.ORG_NAME} · ${E.LOCATION_NAME}` }).first().getAttribute("value");
  await pick.selectOption(option);
  let seen = "";
  for (let i = 0; i < 40; i++) {
    await q.waitForTimeout(5000);
    seen = await row.innerText().catch(() => "");
    if (seen.includes("Online")) break;
  }
  seen.includes("Assigned") || seen.includes("Online") ? ok("assigned to the location from the portal") : bad("after assigning: " + seen.slice(0, 200));
  seen.includes("Online") && seen.includes(E.LOCATION_NAME) ? ok("the box joined by itself and shows Online at its location") : bad("box never came online: " + seen.slice(0, 200));
  await shot(q, "22_boxes_online");
  await q.goto(P + "/admin/agents");
  const agent = q.locator("tr", { hasText: E.SITE_NAME });
  await agent.getByText("Box console").waitFor({ timeout: 30000 }).then(() => ok("Agents lists the zero-touch box"), () => bad("not on Agents"));
} catch (e) {
  bad("stopped: " + String(e).split("\n")[0].slice(0, 300));
  // What the page showed when it stopped, for the lab log.
  if (q) console.log("      page " + q.url() + ": " + (await q.locator("body").innerText({ timeout: 3000 }).catch(() => "")).replace(/\s+/g, " ").slice(0, 300));
}
await br.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
