// The organisation's own SIP trunks, tested as a voice admin uses them (ADR 0044): the owner
// of "Trunk check organisation" adds three simulated trunks on Phone > SIP trunks, gives Head
// office a primary and a secondary and the organisation a default, checks the route, then
// places test calls on Phone > Billing while trunks refuse calls or go down, and checks each
// call goes to the next trunk. Also adds a number on a trunk and moves it between locations.
// Run by lab/ci/checks/voice-trunks.sh against the live portal after each deploy, and locally:
//   UAT_BASE=https://localhost OWNER_EMAIL=... OWNER_PW=... HEAD_EXT=200 BRANCH_EXT=201 node trunks.mjs
// deploy/voice/uat/trunk_users.py makes the organisation fresh before each run.
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const E = process.env;
const B = (E.UAT_BASE || "https://localhost").replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const check = (cond, m, got = "") => (cond ? ok(m) : bad(got ? `${m} (got: ${String(got).slice(0, 200)})` : m));
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);

const br = await chromium.launch(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {});
const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } });
const p = await ctx.newPage();
p.on("dialog", (d) => d.accept());
p.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
p.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad(`API ${r.status()} ${r.url()}`); });

async function trunksPage() {
  await p.goto(B + "/commai/voice/trunks");
  await p.getByRole("heading", { name: "Your SIP trunks" }).waitFor({ timeout: 15000 });
  await p.waitForLoadState("networkidle");
}
async function addTrunk(name) {
  await p.getByRole("button", { name: "Add a trunk" }).click();
  const f = p.getByRole("form", { name: "Add a trunk" });
  await f.getByLabel("Name").fill(name);
  await f.getByLabel("Kind").selectOption("simulated");
  await f.getByRole("button", { name: "Add trunk" }).click();
  await p.getByRole("table", { name: "SIP trunks" }).getByText(name).waitFor({ timeout: 10000 });
}
async function testAs(name, how) {
  await p.getByLabel(`Test ${name} as`).selectOption(how);
  await p.waitForLoadState("networkidle");
  await p.waitForTimeout(500);
}
async function testCall(ext, to) {
  await p.goto(B + "/commai/voice/billing");
  const form = p.locator("form").filter({ has: p.getByLabel("AI agent seconds") });
  await form.waitFor({ timeout: 15000 });
  await form.getByLabel("From extension").selectOption(ext);
  await form.getByLabel("To").fill(to);
  const answer = p.waitForResponse((r) => r.url().includes("/voice/calls/simulate"));
  await form.getByRole("button").first().click();
  const r = await answer;
  const body = await r.json().catch(() => ({}));
  await p.waitForTimeout(300);
  const shown = (await p.getByLabel("Carriers tried").innerText().catch(() => "")).trim();
  return { body, shown };
}

try {
  await p.goto(B + "/");
  await p.getByLabel("Work email").fill(E.OWNER_EMAIL);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(E.OWNER_PW);
  await p.getByRole("button", { name: "Sign in" }).click();
  await p.waitForSelector(".shell main#main", { timeout: 15000 });
  ok("owner signed in");

  // 1. No trunks yet: calls go out as before.
  await trunksPage();
  check(await p.getByText("No trunks of your own yet").isVisible(), "SIP trunks screen shows none yet");
  const before = await testCall(E.HEAD_EXT, "+1 868 555 0190");
  check(before.body.allowed === true && (before.body.route || []).every((a) => !a.carrier.startsWith("org-")), "without trunks a call goes out through ExaCarib", JSON.stringify(before.body.reason));

  // 2. Three trunks, Head office primary and secondary, an organisation default.
  await trunksPage();
  for (const n of ["Primary test trunk", "Secondary test trunk", "Default test trunk"]) await addTrunk(n);
  ok("three simulated trunks added");
  const pick = async (label, name) => {
    const sel = p.getByLabel(label);
    const value = await sel.locator("option", { hasText: name }).getAttribute("value");
    await sel.selectOption(value);
    await p.waitForLoadState("networkidle");
    await p.waitForTimeout(400);
  };
  await pick("Primary trunk for Head office", "Primary test trunk");
  await pick("Secondary trunk for Head office", "Secondary test trunk");
  await pick("Organisation default trunk", "Default test trunk");
  await trunksPage();
  const prim = await p.getByLabel("Primary trunk for Head office").locator("option:checked").innerText();
  const sec = await p.getByLabel("Secondary trunk for Head office").locator("option:checked").innerText();
  const def = await p.getByLabel("Organisation default trunk").locator("option:checked").innerText();
  check(prim === "Primary test trunk" && sec === "Secondary test trunk" && def === "Default test trunk", "Head office primary and secondary and the default are saved", `${prim} / ${sec} / ${def}`);
  check((await p.getByLabel("Primary trunk for Branch office").locator("option:checked").innerText()) === "Organisation default", "Branch office uses the organisation default");

  // 3. The route test lists them in order.
  const rt = p.getByRole("form", { name: "Test a route" });
  await rt.getByLabel("From extension").fill(E.HEAD_EXT);
  await rt.getByLabel("To").fill("+1 868 555 0191");
  await rt.getByRole("button", { name: "Show route" }).click();
  const route = p.getByRole("status", { name: "Route" });
  await route.waitFor({ timeout: 10000 });
  const steps = await route.locator("li").allInnerTexts();
  check(steps.length >= 3 && steps[0].includes("Primary test trunk") && steps[1].includes("Secondary test trunk") && steps[2].includes("Default test trunk"), "route test: primary, secondary, default in order", steps.join(" | "));
  check((await route.innerText()).includes("at Head office"), "route test knows the caller's location");
  await rt.getByLabel("From extension").fill(E.BRANCH_EXT);
  await rt.getByRole("button", { name: "Show route" }).click();
  await p.waitForTimeout(800);
  const branch = await route.locator("li").allInnerTexts();
  check(branch[0]?.includes("Default test trunk") && !branch.join(" ").includes("Primary test trunk"), "Branch office calls go to the default trunk", branch.join(" | "));
  await shot(p, "01_trunks_set_up");

  // 4. Calls: the primary carries it; then fails over to the secondary; then the default.
  let c = await testCall(E.HEAD_EXT, "+1 868 555 0192");
  check(c.body.allowed && c.shown === "Primary test trunk: carried it", "call from Head office goes out on its primary", c.shown || c.body.reason);
  check(!(c.body.charges || []).some((x) => x.kind === "call"), "a call on your own trunk has no ExaCarib call charge");
  await trunksPage();
  await testAs("Primary test trunk", "refuse");
  c = await testCall(E.HEAD_EXT, "+1 868 555 0193");
  check(c.body.allowed && c.shown === "Primary test trunk: refused, then Secondary test trunk: carried it", "primary refuses: the secondary carries the call", c.shown || c.body.reason);
  await shot(p, "02_failover_secondary");
  await trunksPage();
  await testAs("Secondary test trunk", "down");
  check(await p.getByRole("row", { name: /Secondary test trunk/ }).locator("span.pill", { hasText: "Down" }).isVisible(), "a trunk that is down shows Down");
  c = await testCall(E.HEAD_EXT, "+1 868 555 0194");
  check(c.body.allowed && c.shown === "Primary test trunk: refused, then Default test trunk: carried it", "secondary down: skipped, the default carries the call", c.shown || c.body.reason);
  c = await testCall(E.BRANCH_EXT, "+1 868 555 0195");
  check(c.body.allowed && c.shown === "Default test trunk: carried it", "Branch office call goes out on the default", c.shown || c.body.reason);
  await shot(p, "03_failover_default");

  // 5. A number on a trunk, at a location, then moved.
  await trunksPage();
  const nf = p.getByRole("form", { name: "Add a number on your trunk" });
  await nf.getByLabel("A number your carrier gives you").fill("+1 868 555 0170");
  await nf.getByLabel("Arrives on").selectOption({ label: "Primary test trunk" });
  await nf.getByLabel("Location").selectOption({ label: "Head office" });
  await nf.getByRole("button", { name: "Add number" }).click();
  const loc = p.getByLabel("Location of +18685550170");
  await loc.waitFor({ timeout: 10000 });
  check((await loc.locator("option:checked").innerText()) === "Head office", "number on the primary trunk is at Head office");
  await loc.selectOption({ label: "Branch office" });
  await p.waitForLoadState("networkidle");
  await trunksPage();
  check((await p.getByLabel("Location of +18685550170").locator("option:checked").innerText()) === "Branch office", "number moved to Branch office");
  await shot(p, "04_numbers");

  // 6. Back to working: the primary carries calls again.
  await testAs("Primary test trunk", "");
  await testAs("Secondary test trunk", "");
  c = await testCall(E.HEAD_EXT, "+1 868 555 0196");
  check(c.body.allowed && c.shown === "Primary test trunk: carried it", "primary working again carries the call", c.shown || c.body.reason);
} catch (e) {
  bad("test stopped: " + String(e).slice(0, 300));
  await shot(p, "99_error").catch(() => {});
}
await br.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
