// Calling platforms (ADR 0056), tested as a voice admin uses them: the owner of "Platform check
// organisation" joins Microsoft Teams on Phone > Teams and Zoom, adds a person with a Jibsy number
// and an extension, reads the set-up guide, runs test calls both ways (and while the simulated link
// refuses calls), sends Microsoft's TXT value, joins Zoom and enters its addresses, and then makes a
// real call from the browser phone to the Teams person's extension: the simulated link answers with
// an echo, so audio must come back. Run by lab/ci/checks/voice-platforms.sh against the live portal
// after each deploy, and locally:
//   UAT_BASE=https://localhost OWNER_EMAIL=... OWNER_PW=... OWNER_EXT=200 NUMBER=+18685559450 node platforms.mjs
// deploy/voice/uat/platform_users.py makes the organisation fresh before each run.
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const E = process.env;
const B = (E.UAT_BASE || "https://localhost").replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const check = (cond, m, got = "") => (cond ? ok(m) : bad(got ? `${m} (got: ${String(got).slice(0, 240)})` : m));
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);

const br = await chromium.launch({
  ...(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {}),
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
});
const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, permissions: ["microphone"] });
await ctx.addInitScript(() => {
  const Orig = window.RTCPeerConnection;
  window.__pcs = [];
  window.RTCPeerConnection = function (...a) { const pc = new Orig(...a); window.__pcs.push(pc); return pc; };
  window.RTCPeerConnection.prototype = Orig.prototype;
});
const p = await ctx.newPage();
p.on("dialog", (d) => d.accept());
p.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
p.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad(`API ${r.status()} ${r.url()}`); });

const card = (name) => p.locator("section.card").filter({ has: p.getByRole("region", { name: `${name} link` }) });
async function page() {
  await p.goto(B + "/commai/voice/platforms");
  await p.getByRole("heading", { name: "Calling platforms" }).waitFor({ timeout: 15000 });
  await p.waitForLoadState("networkidle");
}
async function patchWait(fn) {
  const done = p.waitForResponse((r) => r.url().includes("/voice/platforms") && r.request().method() !== "GET");
  await fn();
  const r = await done;
  await p.waitForLoadState("networkidle");
  await p.waitForTimeout(300);
  return r;
}
async function testCall(name, who, direction, to = "") {
  const c = card(name);
  await c.getByLabel("Test with").selectOption({ label: who });
  await c.getByLabel("Test direction").selectOption(direction);
  if (direction === "from") await c.getByLabel("Test call to").fill(to);
  const answer = p.waitForResponse((r) => r.url().includes("/test"));
  await c.getByRole("button", { name: "Test the call" }).click();
  const body = await (await answer).json().catch(() => ({}));
  await p.waitForTimeout(200);
  const shown = (await c.getByRole("status", { name: "Test call result" }).innerText().catch(() => "")).trim();
  return { body, shown };
}
async function audio() {
  return p.evaluate(async () => {
    const pc = window.__pcs[window.__pcs.length - 1];
    if (!pc) return null;
    const out = {};
    (await pc.getStats()).forEach((s) => {
      if (s.type === "inbound-rtp" && s.kind === "audio") Object.assign(out, { rxPackets: s.packetsReceived, rxEnergy: s.totalAudioEnergy });
    });
    return out;
  });
}
const status = () => p.locator('p[role="status"][aria-live="polite"]').innerText().catch(() => "");

try {
  await p.goto(B + "/");
  await p.getByLabel("Work email").fill(E.OWNER_EMAIL);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(E.OWNER_PW);
  await p.getByRole("button", { name: "Sign in" }).click();
  await p.waitForSelector(".shell main#main", { timeout: 15000 });
  ok("owner signed in");

  // 1. The screen lists the platforms; joining Teams starts simulated, with its SIP address.
  await page();
  const list = await p.getByRole("list", { name: "Calling platforms" }).innerText();
  check(["Microsoft Teams", "Zoom Phone", "Webex Calling", "Google Voice", "Genesys Cloud", "NICE CXone", "Talkdesk"].every((n) => list.includes(n)), "seven platforms listed", list);
  await patchWait(() => p.getByRole("button", { name: "Join Microsoft Teams" }).click());
  const teams = card("Microsoft Teams");
  await teams.waitFor({ timeout: 10000 });
  const tText = await teams.innerText();
  check(/Simulated/.test(tText) && /-teams\.sip\.exacarib\.com:5061/.test(tText), "Teams joined, simulated, with its own SIP address", tText);
  check(tText.includes("sip.pstnhub.microsoft.com"), "Microsoft's addresses are built in");

  // 2. A person with a Jibsy number and an extension.
  await teams.getByRole("button", { name: "Add a person" }).click();
  const f = teams.getByRole("form", { name: "Add to Microsoft Teams" });
  await f.getByLabel("Name").fill("Dee Teams");
  await f.getByLabel("Teams sign-in").fill("dee@platform-check.example");
  await f.getByLabel("Jibsy number").selectOption({ label: E.NUMBER });
  await f.getByLabel("Extension").fill("450");
  await patchWait(() => f.getByRole("button", { name: "Add" }).click());
  const people = await teams.getByRole("table").innerText().catch(() => "");
  check(people.includes("Dee Teams") && people.includes(E.NUMBER) && people.includes("450"), "person added with number and extension", people);

  // 3. The set-up guide: PowerShell with their number.
  await teams.getByText("Set up Microsoft Teams").click();
  const ps = await teams.getByLabel("Microsoft Teams PowerShell").innerText();
  check(ps.includes("New-CsOnlinePSTNGateway") && ps.includes(`-PhoneNumber "${E.NUMBER}"`) && ps.includes("dee@platform-check.example"), "guide has the PowerShell for this person", ps);
  await shot(p, "platforms-teams");

  // 4. Test calls both ways.
  let t = await testCall("Microsoft Teams", "Dee Teams", "to");
  check(t.body.ok === true && /Call goes through/.test(t.shown), "test call: Jibsy rings the Teams person", t.shown);
  t = await testCall("Microsoft Teams", "Dee Teams", "from", E.OWNER_EXT);
  check(t.body.ok === true && t.shown.includes("Platform test owner"), "test call: Teams person rings a Jibsy extension", t.shown);
  t = await testCall("Microsoft Teams", "Dee Teams", "from", "+1 868 555 0190");
  check(t.body.ok === true && t.shown.includes(E.NUMBER), "test call: Teams person calls out showing their number", t.shown);
  t = await testCall("Microsoft Teams", "Dee Teams", "from", "299");
  check(t.body.ok === false && /no extension 299/.test(t.shown), "test call: a missing extension is explained", t.shown);
  await patchWait(() => teams.getByLabel("Test Microsoft Teams as").selectOption("refuse"));
  t = await testCall("Microsoft Teams", "Dee Teams", "to");
  check(t.body.ok === false && /Call fails/.test(t.shown) && /refused/.test(t.shown), "test call fails while Teams refuses calls", t.shown);
  await patchWait(() => card("Microsoft Teams").getByLabel("Test Microsoft Teams as").selectOption(""));

  // 5. Microsoft's TXT value goes to ExaCarib.
  const dns = card("Microsoft Teams").getByRole("form", { name: "Microsoft 365 domain check" });
  await dns.getByLabel("TXT value from Microsoft 365").fill("ms=ms12345678");
  await patchWait(() => dns.getByRole("button", { name: "Send to ExaCarib" }).click());
  check(/ExaCarib will publish it/.test(await card("Microsoft Teams").innerText()), "TXT value sent to ExaCarib");

  // 6. Zoom: live needs Zoom's addresses; a too-wide range is refused.
  await patchWait(() => p.getByRole("button", { name: "Join Zoom Phone" }).click());
  const zoom = card("Zoom Phone");
  await patchWait(() => zoom.getByLabel("Zoom Phone mode").selectOption("live"));
  check(/Calls can't flow until Jibsy has: Zoom Phone's SIP addresses/.test(await card("Zoom Phone").innerText()), "live Zoom without addresses says what is missing");
  const za = card("Zoom Phone").getByRole("form", { name: "Zoom Phone addresses" });
  await za.getByLabel("Zoom Phone's SIP addresses").fill("byoc.zoom.example");
  await za.getByLabel("Signalling IP addresses").fill("0.0.0.0/0");
  await patchWait(() => za.getByRole("button", { name: "Save addresses" }).click());
  check(/too wide/.test(await card("Zoom Phone").innerText()), "a too-wide address range is refused");
  await za.getByLabel("Signalling IP addresses").fill("198.51.100.0/28");
  await patchWait(() => za.getByRole("button", { name: "Save addresses" }).click());
  check(!/Calls can't flow/.test(await card("Zoom Phone").innerText()), "with Zoom's addresses nothing is missing");
  await shot(p, "platforms-zoom");
  await patchWait(() => card("Zoom Phone").getByRole("button", { name: "Remove" }).click());
  check((await card("Zoom Phone").count()) === 0, "Zoom removed");

  // 7. A real call: the browser phone calls the Teams person's extension; the simulated link echoes.
  await p.goto(B + "/commai/voice/phone", { waitUntil: "networkidle" });
  await p.getByRole("button", { name: "Sign in to the browser phone" }).click();
  await p.getByText("Signed in", { exact: true }).waitFor({ timeout: 20000 });
  await p.getByPlaceholder("Extension or number").fill("450");
  await p.getByRole("button", { name: "Call" }).click();
  await p.waitForFunction(() => /On a call|Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 20000 }).catch(() => {});
  await p.waitForTimeout(6000);
  const st = await status();
  const au = await audio();
  await shot(p, "platforms-call");
  check(/On a call/.test(st), "browser phone call to the Teams person's extension is answered", st);
  check(au?.rxPackets > 100 && au?.rxEnergy > 0, "audio comes back from the (simulated) Teams side", JSON.stringify(au));
  if (await p.getByRole("button", { name: "Hang up" }).count()) await p.getByRole("button", { name: "Hang up" }).click();
} catch (e) {
  bad("test stopped: " + String(e).split("\n")[0]);
  await shot(p, "platforms-stopped");
}
await br.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
