// The box console (ADR 0046), tested as someone at a branch uses it: a fresh box,
// the setup code from its label, the port wizard, joining Connect with the box
// setup code from the portal, the box's own status and diagnostics; then the
// portal shows the box online and managed locally too, and "Managed from" switches
// both ways between the portal and the box (ADR 0049).
// Run by lab/ci/checks/box-console.sh against a throwaway box after each deploy, and locally:
//   BOX_BASE=https://127.0.0.1:9443 BOX_SETUP_CODE=... JOIN_CODE=EXA1.... WAN_IFACE=eth0 \
//   PORTAL_BASE=http://localhost:5173 PORTAL_EMAIL=... PORTAL_PW=... SITE_NAME=... node box-console.mjs
// Optional: WAN_MODE (keep|static|dhcp), WAN_ADDR, WAN_GW, LAN_IFACE, LAN_ADDR, PING_TARGET,
// WAN_URL (the box on its internet side: must refuse), UAT_SHOTS (a folder for screenshots).
import { mkdirSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { chromium } from "playwright";

const E = process.env;
const B = E.BOX_BASE.replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
const PW = randomBytes(15).toString("base64url");
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const br = await chromium.launch(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {});
async function page() {
  const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 860 } });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
  p.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) bad("console error: " + m.text().slice(0, 160)); });
  return p;
}
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);
const text = (p) => p.locator("main").innerText();

try {
  const p = await page();
  // 1. A fresh box asks for its setup code and an admin password.
  await p.goto(B + "/");
  await p.getByRole("heading", { name: "Set up this ExaCarib box" }).waitFor({ timeout: 15000 });
  ok("fresh box asks for its setup code");
  await shot(p, "01_box_setup_code");
  await p.getByLabel("Setup code").fill("WRONG-CODE1");
  await p.getByLabel("Admin password").fill(PW);
  await p.getByLabel("Password again").fill(PW);
  await p.getByRole("button", { name: "Set password and continue" }).click();
  (await p.getByRole("alert").innerText({ timeout: 5000 })).includes("not right") ? ok("a wrong setup code is refused") : bad("wrong setup code not refused");
  await p.getByLabel("Setup code").fill(E.BOX_SETUP_CODE.toLowerCase());
  await p.getByRole("button", { name: "Set password and continue" }).click();

  // 2. The port wizard.
  await p.getByRole("heading", { name: "Choose which port goes where" }).waitFor({ timeout: 10000 });
  ok("password set; the wizard opens at the ports step");
  const wan = p.locator(".port", { hasText: E.WAN_IFACE }).first();
  await wan.getByLabel(`Use of ${E.WAN_IFACE}`).selectOption("carrier-a");
  await wan.getByLabel(`How ${E.WAN_IFACE} gets its address`).selectOption(E.WAN_MODE || "keep");
  if ((E.WAN_MODE || "keep") === "static") {
    await wan.getByLabel("Address and size").fill(E.WAN_ADDR);
    await wan.getByLabel("Gateway").fill(E.WAN_GW);
  }
  if (E.LAN_IFACE) {
    const lan = p.locator(".port", { hasText: E.LAN_IFACE }).first();
    await lan.getByLabel(`Use of ${E.LAN_IFACE}`).selectOption("lan");
    if (E.LAN_ADDR) {
      await lan.getByLabel(`How ${E.LAN_IFACE} gets its address`).selectOption("static");
      await lan.getByLabel("Address and size").fill(E.LAN_ADDR);
    } else {
      await lan.getByLabel(`How ${E.LAN_IFACE} gets its address`).selectOption("keep");
    }
  }
  await shot(p, "02_box_ports");
  await p.getByRole("button", { name: "Save and continue" }).click();
  await p.getByRole("heading", { name: "Join this box to Connect" }).waitFor({ timeout: 30000 });
  ok("ports applied and kept; the wizard moves on to joining");

  // 3. Join with the box setup code from the portal.
  await shot(p, "03_box_join"); // before the code goes in: no token in the picture
  await p.getByLabel("Box setup code").fill(E.JOIN_CODE);
  await p.getByRole("button", { name: "Join Connect" }).click();
  await p.getByRole("heading", { name: "This box is set up" }).waitFor({ timeout: 45000 });
  ok("the box joined Connect with the box setup code");
  await shot(p, "04_box_done");

  // 4. Its own status: connected to ExaCarib, its ports and roles.
  await p.getByRole("link", { name: "See the box's status" }).click();
  let connected = false;
  for (let i = 0; i < 30 && !connected; i++) {
    await p.waitForTimeout(3000);
    connected = (await text(p)).includes("Connected");
  }
  connected ? ok("overview shows it connected to ExaCarib") : bad("overview never showed Connected: " + (await text(p)).slice(0, 200));
  const ov = await text(p);
  ov.includes("Internet link 1") ? ok("overview shows the internet port") : bad("overview has no internet port");
  !E.LAN_IFACE || ov.includes("Office network") ? ok("overview shows the office port") : bad("overview has no office port");
  await shot(p, "05_box_overview");

  // 5. Diagnostics.
  await p.getByRole("link", { name: "Diagnostics" }).click();
  await p.getByLabel("Test").selectOption("controller");
  await p.getByRole("button", { name: "Run" }).click();
  await p.locator("pre.out").filter({ hasText: /answered|Cannot reach/ }).waitFor({ timeout: 30000 });
  (await p.locator("pre.out").innerText()).includes("200 OK") ? ok("diagnostics reach ExaCarib") : bad("diagnostics: " + (await p.locator("pre.out").innerText()).slice(0, 160));
  if (E.PING_TARGET) {
    await p.getByLabel("Test").selectOption("ping");
    await p.getByLabel("Address or name").fill(E.PING_TARGET);
    await p.getByRole("button", { name: "Run" }).click();
    await p.locator("pre.out").filter({ hasText: /packets transmitted|ping:/ }).waitFor({ timeout: 30000 });
    (await p.locator("pre.out").innerText()).includes(" 0% packet loss") ? ok(`ping ${E.PING_TARGET} from the box`) : bad("ping: " + (await p.locator("pre.out").innerText()).slice(-160));
  }
  await shot(p, "06_box_diagnostics");

  // 6. Signing out and in again; a wrong password is refused.
  await p.getByRole("button", { name: "Sign out" }).click();
  await p.getByRole("heading", { name: "Sign in to the box console" }).waitFor({ timeout: 10000 });
  await p.getByLabel("Password").fill("not-the-password");
  await p.getByRole("button", { name: "Sign in" }).click();
  (await p.getByRole("alert").innerText({ timeout: 5000 })).includes("not right") ? ok("a wrong password is refused") : bad("wrong password not refused");
  await p.getByLabel("Password").fill(PW);
  await p.getByRole("button", { name: "Sign in" }).click();
  await p.getByRole("link", { name: "Settings" }).waitFor({ timeout: 10000 });
  ok("signs in again with the new password");
  await p.getByRole("link", { name: "Settings" }).click();
  await p.getByRole("heading", { name: "About this box" }).waitFor({ timeout: 10000 });
  await shot(p, "07_box_settings");

  // 7. The console does not answer on the box's internet side: the box's firewall
  // drops it (ADR 0047), and the console itself would refuse it with 403.
  if (E.WAN_URL) {
    const r = await p.request.get(E.WAN_URL.replace(/\/$/, "") + "/api/state", { ignoreHTTPSErrors: true, failOnStatusCode: false, timeout: 6000 })
      .then((x) => x.status(), (e) => "no answer (" + String(e.message).split("\n")[0].slice(0, 60) + ")");
    r === 403 || String(r).startsWith("no answer") ? ok("the console is closed on the internet port: " + r) : bad("console on the internet port answered " + r);
  }

  // 8. The portal shows the box online and managed locally too.
  if (E.PORTAL_BASE && E.PORTAL_EMAIL) {
    const q = await page();
    const P = E.PORTAL_BASE.replace(/\/$/, "");
    await q.goto(P + "/");
    await q.getByLabel("Work email").fill(E.PORTAL_EMAIL); await q.getByRole("button", { name: "Continue" }).click();
    await q.getByLabel("Password").fill(E.PORTAL_PW); await q.getByRole("button", { name: "Sign in" }).click();
    await q.waitForSelector(".shell main#main", { timeout: 15000 });
    await q.goto(P + "/admin/agents");
    const row = q.locator("tr", { hasText: E.SITE_NAME });
    await row.waitFor({ timeout: 15000 });
    // The box reports its console with telemetry, every 10 seconds; the page refreshes as often.
    await row.getByText("Box console").waitFor({ timeout: 30000 }).catch(() => {});
    (await row.innerText()).includes("Box console") ? ok("Agents lists the box with its box console") : bad("Agents row: " + (await row.innerText()).slice(0, 160));
    await shot(q, "08_portal_agents");
    if (E.LOCATION_NAME) {
      await q.goto(P + "/org/locations");
      // ExaCarib staff choose which organisation the screens act for.
      if (E.ORG_NAME) {
        const pick = q.locator("label.picker select:visible").first();
        await pick.waitFor({ timeout: 15000 }).catch(() => {});
        await pick.selectOption({ label: E.ORG_NAME }).catch((e) => bad("could not choose " + E.ORG_NAME + ": " + String(e).split("\n")[0].slice(0, 120)));
        await q.waitForTimeout(1500);
      }
      const loc = q.locator("section.loc", { hasText: E.LOCATION_NAME });
      let seen = "";
      for (let i = 0; i < 10; i++) {
        seen = await loc.innerText().catch(() => "");
        if (seen.includes("Box online") && seen.includes("Box console")) break;
        await q.waitForTimeout(3000);
        await q.reload();
      }
      seen.includes("Box online") && seen.includes("Box console") ? ok("Locations shows the box online, with its box console") : bad("Locations: " + seen.slice(0, 200));
      await loc.scrollIntoViewIfNeeded().catch(() => {});
      await shot(q, "09_portal_locations");
    }
    await q.locator("a", { hasText: E.SITE_NAME }).first().click().catch(() => {});
    if (E.SITE_ID) await q.goto(P + "/sites/" + E.SITE_ID);
    await q.getByText("Also managed on the box itself").waitFor({ timeout: 15000 }).then(() => ok("the site page says the box is also managed locally"), () => bad("site page has no box console note"));
    await shot(q, "10_portal_site");

    // 9. Managed from (ADR 0049): handed to the cloud in the portal, the console turns
    // read-only; taken back on the box in an emergency, the portal follows.
    const pressed = (pg, name) => pg.getByRole("group", { name: "Managed from" }).getByRole("button", { name, exact: true });
    const until = async (fn, tries = 15, wait = 3000) => { for (let i = 0; i < tries; i++) { if (await fn().catch(() => false)) return true; await q.waitForTimeout(wait); } return false; };
    await pressed(q, "Cloud").click();
    (await until(async () => (await pressed(q, "Cloud").getAttribute("aria-pressed")) === "true", 5, 1000)) ? ok("the portal hands the box to the cloud") : bad("portal switch to Cloud did not stick");
    await p.getByRole("link", { name: "Settings" }).click();
    const boxCloud = await until(async () => { await p.reload(); return p.getByRole("heading", { name: "Managed from: Cloud" }).isVisible({ timeout: 4000 }); });
    boxCloud ? ok("the box picks up Cloud from the portal") : bad("box console never showed Managed from: Cloud: " + (await text(p)).slice(0, 160));
    await shot(p, "11_box_managed_cloud");
    await p.getByRole("link", { name: "Ports" }).click();
    await p.getByRole("heading", { name: "Ports on this box" }).waitFor({ timeout: 10000 });
    const saveBtn = await p.getByRole("button", { name: "Save the ports" }).count();
    saveBtn === 0 && (await text(p)).includes("managed from the Connect portal") ? ok("managed from the cloud, the console's ports are read-only") : bad("ports still editable in cloud mode");
    const refused = await p.evaluate(async () => (await fetch("/api/network", { method: "POST", headers: { "Content-Type": "application/json", "X-Exa-Box": "1" }, body: JSON.stringify({ wan: [] }) })).status);
    refused === 409 ? ok("the box refuses a port change while managed from the cloud") : bad("port change in cloud mode answered " + refused);
    await p.getByRole("link", { name: "Settings" }).click();
    await p.getByLabel("Admin password").fill("not-the-password");
    await p.getByRole("button", { name: "Take back control" }).click();
    (await p.getByRole("alert").innerText({ timeout: 5000 })).includes("not right") ? ok("taking back control needs the right password") : bad("take back with a wrong password not refused");
    await p.getByLabel("Admin password").fill(PW);
    await p.getByRole("button", { name: "Take back control" }).click();
    (await until(async () => (await pressed(p, "On site").getAttribute("aria-pressed")) === "true", 5, 1000)) ? ok("the box console takes back control (On site)") : bad("take back did not switch the box to On site");
    const portalSite = await until(async () => { await q.reload(); return (await pressed(q, "On site").getAttribute("aria-pressed", { timeout: 5000 })) === "true"; });
    portalSite ? ok("the portal follows the box: On site") : bad("portal never showed On site after the box took back control");
    await shot(q, "12_portal_managed_site");
    await pressed(q, "On site and cloud").click();
    const boxBoth = await until(async () => { await p.reload(); return (await pressed(p, "On site and cloud").getAttribute("aria-pressed", { timeout: 4000 })) === "true"; });
    boxBoth ? ok("switched to both in the portal, the box follows") : bad("box never showed On site and cloud");

    // 10. Ports from the portal (ADR 0051): the office network starts handing out
    // addresses, set in the portal; the box tries it, still reaches ExaCarib, keeps it.
    await q.reload();
    const ports = q.getByRole("region", { name: "Box ports" });
    await ports.getByRole("button", { name: "Change the ports" }).click();
    (await q.getByLabel("Address and size for " + (E.LAN_IFACE || "eth1")).inputValue({ timeout: 5000 })) === E.LAN_ADDR
      ? ok("the portal's port form starts from the box's own ports") : bad("portal port form did not show the office address");
    await ports.getByLabel("Hand out addresses on the office network").check();
    await shot(q, "13_portal_box_ports");
    await ports.getByRole("button", { name: "Send to the box" }).click();
    const kept = await until(async () => { await q.reload(); return (await q.getByRole("region", { name: "Box ports" }).innerText({ timeout: 5000 })).includes("The box is using the ports set here"); }, 20);
    kept ? ok("the box takes the ports set in the portal and keeps them") : bad("portal ports never kept: " + (await q.getByRole("region", { name: "Box ports" }).innerText().catch(() => "")).slice(0, 200));
    await shot(q, "14_portal_box_ports_kept");
    const net = await p.evaluate(async () => (await (await fetch("/api/network", { headers: { "X-Exa-Box": "1" } })).json()).network);
    net && net.lan_dhcp && String(net.updated_by || "").startsWith("Connect portal") ? ok("the box hands out addresses on the office network, set from the portal") : bad("box network after portal change: " + JSON.stringify(net).slice(0, 200));
    await p.getByRole("link", { name: "Overview" }).click();
    await p.getByText("Ports last set in the Connect portal").waitFor({ timeout: 10000 }).then(() => ok("the box console says the ports were set in the portal"), () => bad("box overview has no note about the portal's ports"));
    await shot(p, "15_box_overview_portal_ports");

    // 11. Templates (ADR 0052): this box's ports saved as a template, then the box made
    // to follow it from Box templates; the box takes the template's ports and keeps them.
    const tpl = "Lab template " + (E.SITE_NAME || "box").slice(-6);
    await q.reload();
    const card = q.getByRole("region", { name: "Box ports" });
    await card.getByRole("button", { name: "Save as a template" }).click();
    await card.getByLabel("Template name").fill(tpl);
    await card.getByRole("button", { name: "Save the template" }).click();
    await card.getByText("Saved the template " + tpl).waitFor({ timeout: 10000 }).then(() => ok("the box's ports are saved as a template"), () => bad("saving the box's ports as a template failed"));
    await q.goto(P + "/box-templates");
    if (E.ORG_NAME) {
      const pick = q.locator("label.picker select:visible").first();
      await pick.waitFor({ timeout: 15000 }).catch(() => {});
      await pick.selectOption({ label: E.ORG_NAME }).catch(() => {});
    }
    const sec = q.getByRole("region", { name: "Template " + tpl });
    await sec.waitFor({ timeout: 15000 });
    await sec.getByLabel(E.SITE_NAME + " follows " + tpl).check();
    await shot(q, "16_portal_box_templates");
    await sec.getByRole("button", { name: "Send to these boxes" }).click();
    await q.getByText(E.SITE_NAME + ": sent to the box").waitFor({ timeout: 10000 }).then(() => ok("Box templates sends the template to the box"), async () => bad("template not sent: " + (await text(q)).slice(0, 200)));
    const using = await until(async () => {
      await q.reload();
      const row = q.getByRole("region", { name: "Template " + tpl }).locator(`tr[data-site="${E.SITE_NAME}"]`);
      return (await row.innerText({ timeout: 5000 })).includes("Box is using it");
    }, 20);
    using ? ok("the box follows the template and keeps its ports") : bad("box never showed as using the template");
    await shot(q, "17_portal_box_template_kept");
  }
} catch (e) {
  bad("stopped: " + String(e).split("\n")[0].slice(0, 300));
}
await br.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
