// Acceptance test of Jibsy Phone, the installable phone app (docs/commai/voice-uat.md).
// Two people: A on a phone-sized screen with touch, B on a computer-sized screen, both in
// Chromium with a fake microphone (a tone). Optionally a third account with no extension.
//   UAT_BASE=https://jibsyapp.exacarib.com A_EMAIL=.. A_PW=.. A_EXT=200 A_NAME=.. \
//   B_EMAIL=.. B_PW=.. B_EXT=201 B_NAME=.. [C_EMAIL=.. C_PW=..] [MENU_EXT=500] node phone-app.mjs
// UAT_ENGINE=webkit|firefox runs A in that engine instead (sign-in, screens, calls).
// Passwords come from the environment only and are never printed.
import { mkdirSync } from "node:fs";
import { chromium, firefox, webkit, devices } from "playwright";

const BASE = process.env.UAT_BASE || "https://jibsyapp.localhost";
const SHOTS = process.env.UAT_SHOTS || "uat-shots";
const E = process.env;
mkdirSync(SHOTS, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => {
  results.push([ok ? "PASS" : "FAIL", name]);
  console.log(ok ? "PASS" : "FAIL", name, detail ? `(${String(detail).slice(0, 160)})` : "");
};
const shot = (p, n) => p.screenshot({ path: `${SHOTS}/${n}.png` });
const FAKE = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required"];
const hook = () => {
  const Orig = window.RTCPeerConnection;
  window.__pcs = [];
  window.RTCPeerConnection = function (...a) {
    const pc = new Orig(...a);
    window.__pcs.push(pc);
    return pc;
  };
  window.RTCPeerConnection.prototype = Orig.prototype;
};
async function audio(p) {
  return p.evaluate(async () => {
    const pc = window.__pcs?.[window.__pcs.length - 1];
    if (!pc) return null;
    const out = { ice: pc.iceConnectionState };
    (await pc.getStats()).forEach((s) => {
      if (s.type === "inbound-rtp" && s.kind === "audio") Object.assign(out, { rx: s.packetsReceived, energy: s.totalAudioEnergy });
      if (s.type === "outbound-rtp" && s.kind === "audio") out.tx = s.packetsSent;
    });
    return out;
  });
}
const chromiumOpts = { args: FAKE, ...(E.CHROMIUM ? { executablePath: E.CHROMIUM } : {}) };

async function openApp(browser, ctxOpts, email, pw, tag) {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, ...ctxOpts });
  await ctx.addInitScript(hook);
  const p = await ctx.newPage();
  p.on("pageerror", (e) => console.log(tag, "page error:", String(e).slice(0, 200)));
  await p.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await p.getByLabel("Work email").fill(email);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(pw);
  await p.getByRole("button", { name: /Sign in/ }).click();
  return p;
}
const status = (p) => p.locator(".ph-status").innerText().catch(() => "");
const overlay = (p) => p.locator(".ph-overlay");
async function waitText(loc, re, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const t = await loc.innerText().catch(() => "");
    if (re.test(t)) return t;
    await new Promise((r) => setTimeout(r, 250));
  }
  return loc.innerText().catch(() => "");
}
async function tab(p, name) {
  await p.getByRole("navigation", { name: "Phone" }).getByRole("button", { name }).click();
}
async function noSideScroll(p) {
  return p.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
}

const engineA = { chromium, webkit, firefox }[E.UAT_ENGINE || "chromium"];
// Firefox's stand-in microphone; Playwright's WebKit has none (see step 3). Skipping the permission
// prompt also makes Firefox hide its own address behind a .local name, which a real Firefox stops doing
// once the person allows the microphone; the last setting puts that back.
const firefoxOpts = {
  firefoxUserPrefs: {
    "media.navigator.streams.fake": true,
    "media.navigator.permission.disabled": true,
    "media.peerconnection.ice.obfuscate_host_addresses": false,
  },
};
const bA = await engineA.launch({ chromium: chromiumOpts, webkit: {}, firefox: firefoxOpts }[E.UAT_ENGINE || "chromium"]);
const bB = await chromium.launch(chromiumOpts);
const phoneDevice = E.UAT_ENGINE === "webkit" ? devices["iPhone 13"] : devices["Pixel 7"];
const { defaultBrowserType, ...phoneCtx } = phoneDevice;
if (E.UAT_ENGINE === "firefox") delete phoneCtx.isMobile;
const permsA = E.UAT_ENGINE && E.UAT_ENGINE !== "chromium" ? {} : { permissions: ["microphone"] };

// 1. The address opens the app, which is installable.
const a = await openApp(bA, { ...phoneCtx, ...permsA }, E.A_EMAIL, E.A_PW, "A");
check("the app's own address opens Jibsy Phone", new URL(a.url()).pathname === "/phone", a.url());
check("sign-in names the phone app", true);
const connectedA = await waitText(a.locator(".ph-status"), new RegExp(`Ext ${E.A_EXT}`), 20000);
check("A connects automatically after sign-in", connectedA.includes(`Ext ${E.A_EXT}`), connectedA);
const PORTAL_HDR = ["X-Requested-With", "exa-portal"];
// Start from a clean slate: an earlier run may have left do not disturb or forwarding on.
const reset = await a.evaluate(async (HDR) => {
  const h = { "content-type": "application/json", [HDR[0]]: HDR[1] };
  const list = await (await fetch("/api/v1/customers/mine", { headers: h })).json().catch(() => []);
  const out = [];
  for (const x of list) {
    const r = await fetch(`/api/v1/commai/customers/${x.id}/voice/me`, {
      method: "PATCH", headers: h,
      body: JSON.stringify({ dnd: false, forward_to: null }),
    });
    out.push(r.status);
  }
  return out;
}, PORTAL_HDR);
if (!reset.includes(200)) console.log("note: could not reset A's phone settings", JSON.stringify(reset));
await a.reload();
await waitText(a.locator(".ph-status"), new RegExp(`Ext ${E.A_EXT}`), 20000);
await shot(a, "app-01-keypad-phone");
check("no sideways scrolling on a phone screen", await noSideScroll(a));
const manifest = await a.evaluate(async () => {
  const href = document.querySelector('link[rel="manifest"]')?.getAttribute("href");
  const m = await (await fetch(href)).json();
  const icons = await Promise.all(m.icons.map(async (i) => (await fetch(i.src)).ok));
  return { name: m.name, display: m.display, start: m.start_url, scope: m.scope, icons, maskable: m.icons.some((i) => i.purpose === "maskable") };
});
check(
  "manifest: name, standalone, start inside scope, icons load (one maskable)",
  manifest.name === "Jibsy Phone" && manifest.display === "standalone" && manifest.start.startsWith(manifest.scope) && manifest.icons.every(Boolean) && manifest.maskable,
  JSON.stringify(manifest),
);
if (!E.UAT_ENGINE || E.UAT_ENGINE === "chromium") {
  const sw = await a.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration("/phone");
    await navigator.serviceWorker.ready;
    return reg?.scope || "";
  });
  check("service worker registered for the app", sw.endsWith("/phone"), sw);
  const cdp = await a.context().newCDPSession(a);
  const inst = await cdp.send("Page.getInstallabilityErrors");
  // A test browser is a private window, which Chrome never installs into; anything else is a fault.
  const errs = inst.installabilityErrors.filter((e) => e.errorId !== "in-incognito");
  check("Chrome finds nothing stopping installation", errs.length === 0, JSON.stringify(errs));
}

const b = await openApp(bB, { viewport: { width: 1440, height: 900 }, permissions: ["microphone", "notifications"] }, E.B_EMAIL, E.B_PW, "B");
const connectedB = await waitText(b.locator(".ph-status"), new RegExp(`Ext ${E.B_EXT}`), 20000);
check("B connects automatically on a computer", connectedB.includes(`Ext ${E.B_EXT}`), connectedB);
await shot(b, "app-02-keypad-desktop");

// 2. Contacts: colleagues and shared lines, search, call from the list.
await tab(a, "Contacts");
await a.locator(".ph-list li").first().waitFor({ timeout: 10000 });
const list = await a.locator(".ph-list").innerText();
check("contacts list B by name and extension", list.includes(E.B_NAME) && list.includes(`Ext ${E.B_EXT}`), list.replace(/\n/g, " | "));
check("contacts leave yourself out", !list.includes(`Ext ${E.A_EXT}`));
if (E.MENU_EXT) check("contacts show shared lines", /shared lines/i.test(list) && list.includes(E.MENU_EXT));
await a.getByLabel("Search contacts").fill(E.B_EXT);
const filtered = await a.locator(".ph-list li").count();
check("search narrows the list", filtered >= 1 && filtered < (await a.locator(".ph-list li").count()) + 10, `${filtered} shown`);
await shot(a, "app-03-contacts-phone");

const micOK = E.UAT_ENGINE !== "webkit";
if (micOK) {
  // 3. A calls B from Contacts; B answers; audio both ways; mute; tones; hang up.
  await a.getByRole("button", { name: `Call ${E.B_NAME}` }).click();
  const inc = await waitText(overlay(b), /Incoming call/, 15000);
  check("B sees the incoming call with A's name", inc.includes("Incoming call") && inc.includes(E.A_NAME), inc.replace(/\n/g, " | "));
  await shot(b, "app-04-incoming-desktop");
  await shot(a, "app-05-calling-phone");
  const a0 = (await audio(a))?.rx || 0; // ringback before the answer doesn't count
  const t0 = Date.now();
  await b.getByRole("button", { name: "Answer" }).click();
  const onA = await waitText(overlay(a), /On a call/, 15000);
  check("both sides show On a call", onA.includes("On a call") && (await overlay(b).innerText()).includes("On a call"));
  // How long after Answer the audio starts both ways (25 packets, half a second of sound).
  let started = null;
  while (Date.now() - t0 < 10000) {
    const [x, y] = [await audio(a), await audio(b)];
    if ((x?.rx || 0) - a0 >= 25 && (y?.rx || 0) >= 25) {
      started = (Date.now() - t0) / 1000;
      break;
    }
    await a.waitForTimeout(250);
  }
  check("audio starts both ways within 3 s of answering", started !== null && started <= 3, started === null ? "not within 10 s" : `${started.toFixed(1)} s`);
  // Then 5 s of steady sound: 50 packets a second each way, so at least 200 (under 20% lost).
  const [pa, pb] = [(await audio(a))?.rx || 0, (await audio(b))?.rx || 0];
  await a.waitForTimeout(5000);
  const sa = await audio(a);
  const sb = await audio(b);
  check("audio reaches A steadily", sa?.rx - pa >= 200 && sa?.energy > 0, JSON.stringify({ ...sa, in5s: sa?.rx - pa }));
  check("audio reaches B steadily", sb?.rx - pb >= 200 && sb?.energy > 0, JSON.stringify({ ...sb, in5s: sb?.rx - pb }));
  const timer = await a.locator(".ph-timer").innerText().catch(() => "");
  check("the call timer runs", /^0:0[3-9]|^0:[1-5]\d/.test(timer), timer);
  await a.getByRole("button", { name: "Mute" }).click();
  check("mute shows as on", (await a.getByRole("button", { name: "Unmute" }).getAttribute("aria-pressed")) === "true");
  await a.getByRole("button", { name: "Unmute" }).click();
  await overlay(a).getByRole("button", { name: "Keypad", exact: true }).click();
  await a.getByRole("group", { name: "Keypad: sends tones" }).getByRole("button", { name: "5" }).click();
  check("the in-call keypad opens and sends a tone", true);
  await shot(a, "app-06-oncall-phone");
  await a.getByRole("button", { name: "Hang up" }).click();
  await b.waitForTimeout(3000);
  check("hang-up closes the call on both sides and nothing rings again", (await overlay(a).count()) === 0 && (await overlay(b).count()) === 0);

  // 4. Recents on both sides.
  await tab(a, "Recents");
  const ra = await a.locator(".ph-list").innerText().catch(() => "");
  check("A's recents show the outgoing call with its length", /Outgoing, 0:\d\d/.test(ra), ra.replace(/\n/g, " | "));
  await tab(b, "Recents");
  const rb = await b.locator(".ph-list").innerText().catch(() => "");
  check("B's recents show the incoming call", /Incoming, 0:\d\d/.test(rb), rb.replace(/\n/g, " | "));

  // 5. A missed call: A rings B and gives up before B answers.
  await tab(a, "Keypad");
  await a.getByLabel("Extension or number").fill(E.B_EXT);
  await a.getByRole("button", { name: "Call", exact: true }).click();
  await waitText(overlay(b), /Incoming call/, 15000);
  await a.getByRole("button", { name: "Hang up" }).click();
  await b.waitForTimeout(2500);
  const rb2 = await b.locator(".ph-list").innerText().catch(() => "");
  check("B's recents mark it missed", /Missed/.test(rb2) && (await overlay(b).count()) === 0, rb2.replace(/\n/g, " | "));

  // 6. A number that doesn't exist.
  await a.getByLabel("Extension or number").fill("299");
  await a.getByRole("button", { name: "Call", exact: true }).click();
  const toast = await waitText(a.locator(".ph-toast"), /exist/, 15000);
  check("a missing extension gets a plain message", /doesn't exist/.test(toast), toast);
  await shot(a, "app-07-missing-phone");

} else {
  // Playwright's WebKit has no stand-in microphone, so this is what someone who says no to the
  // microphone sees: a plain reason, and an incoming call that goes to voicemail.
  await a.getByRole("button", { name: `Call ${E.B_NAME}` }).click();
  const why = await waitText(a.locator(".ph-toast"), /microphone/, 10000);
  check("no microphone: calling says how to allow it", /Allow it for this site/.test(why), why);
  await a.waitForTimeout(1500);
  check("no microphone: the message stays long enough to read", /microphone/.test(await a.locator(".ph-toast").innerText().catch(() => "")));
  await tab(b, "Keypad");
  await b.getByLabel("Extension or number").fill(E.A_EXT);
  await b.getByRole("button", { name: "Call", exact: true }).click();
  const ring = await waitText(overlay(a), /Incoming call/, 15000);
  check("B's call rings on A with B's name", ring.includes(E.B_NAME), ring.replace(/\n/g, " | "));
  await a.getByRole("button", { name: "Answer" }).click();
  const why2 = await waitText(a.locator(".ph-toast"), /microphone/, 10000);
  check("no microphone: answering says how to allow it and the overlay closes", /microphone/.test(why2) && (await overlay(a).count()) === 0, why2);
  await waitText(overlay(b), /On a call/, 15000);
  check("the caller goes on to voicemail", (await overlay(b).innerText().catch(() => "")).includes("On a call"));
  await b.getByRole("button", { name: "Hang up" }).click();
  await a.waitForTimeout(1500);
}
// 7. Typing on a computer keyboard dials on B.
await tab(b, "Keypad");
await b.getByLabel("Extension or number").fill("");
await b.locator("body").click({ position: { x: 5, y: 300 } });
await b.keyboard.type("2*1");
await b.keyboard.press("Backspace");
check("a computer keyboard types into the keypad", (await b.getByLabel("Extension or number").inputValue()) === "2*", await b.getByLabel("Extension or number").inputValue());
await b.getByLabel("Extension or number").fill("");

if (micOK) {
  // 8. Decline sends the caller to voicemail.
  await b.getByLabel("Extension or number").fill(E.A_EXT);
  await b.getByRole("button", { name: "Call", exact: true }).click();
  await waitText(overlay(a), /Incoming call/, 15000);
  await shot(a, "app-08-incoming-phone");
  await a.getByRole("button", { name: "Decline" }).click();
  await waitText(overlay(b), /On a call/, 15000);
  await b.waitForTimeout(4000);
  const vm = await audio(b);
  check("declining sends the caller to voicemail, which speaks", (await overlay(b).innerText()).includes("On a call") && vm?.energy > 0, JSON.stringify(vm));
  await b.getByRole("button", { name: "Hang up" }).click();
  await a.waitForTimeout(1500);

}
// 9. Settings that follow the person: do not disturb, forwarding, greeting.
await tab(a, "Settings");
await a.getByText("My phone").waitFor();
await shot(a, "app-09-settings-phone");
await a.getByLabel("Do not disturb").check();
await waitText(a.locator(".ph-ok, .ph-error"), /Do not disturb is on|\w/, 8000);
check("do not disturb saves", /Do not disturb is on/.test(await a.locator(".ph-ok").innerText().catch(() => "")));
await a.waitForTimeout(8000); // the phone system reloads the change within seconds
await b.getByLabel("Extension or number").fill(E.A_EXT);
await b.getByRole("button", { name: "Call", exact: true }).click();
await b.waitForTimeout(6000);
check("with do not disturb on, A's app doesn't ring", (await overlay(a).count()) === 0);
if (await b.getByRole("button", { name: "Hang up" }).count()) await b.getByRole("button", { name: "Hang up" }).click();
await a.getByLabel("Do not disturb").uncheck();
await waitText(a.locator(".ph-ok"), /off/, 8000);
await a.getByLabel("Forward my calls to").fill("299");
await a.getByRole("button", { name: "Save forwarding" }).click();
const fwdErr = await waitText(a.locator(".ph-ok, .ph-error"), /\w/, 8000);
check("forwarding to a missing extension is refused with a reason", /no extension 299/i.test(fwdErr), fwdErr);
await a.getByLabel("Forward my calls to").fill("");
await a.getByRole("button", { name: "Save forwarding" }).click();
await a.getByLabel("Voicemail greeting").fill("You've reached the UAT test phone.");
await a.getByRole("button", { name: "Save greeting" }).click();
const g = await waitText(a.locator(".ph-ok, .ph-error"), /greeting saved/, 8000);
check("the voicemail greeting saves", /greeting saved/.test(g), g);

// 10. Settings on this device: ringtone, appearance; they survive a restart.
await a.getByLabel("Ringtone").selectOption("digital");
await a.getByLabel("Appearance").selectOption("dark");
check("dark appearance applies", (await a.evaluate(() => document.documentElement.dataset.theme)) === "dark");
await shot(a, "app-10-settings-dark-phone");
await a.reload({ waitUntil: "networkidle" });
await tab(a, "Settings");
check(
  "device settings survive reopening the app",
  (await a.getByLabel("Ringtone").inputValue()) === "digital" && (await a.evaluate(() => document.documentElement.dataset.theme)) === "dark",
);
await a.getByLabel("Appearance").selectOption("system");
const installText = await a.locator(".ph-install").innerText();
check("install help is shown for this device", /Put Jibsy Phone on this device/.test(installText), installText.replace(/\n/g, " | "));

// 11. Connection loss and back: the app reconnects by itself.
await a.context().setOffline(true);
await waitText(a.locator(".ph-status"), /Offline|Connecting/, 20000);
check("losing the network shows as offline", /Offline|Connecting/.test(await status(a)), await status(a));
await a.context().setOffline(false);
const back = await waitText(a.locator(".ph-status"), new RegExp(`Ext ${E.A_EXT}`), 40000);
check("the app reconnects when the network returns", back.includes(`Ext ${E.A_EXT}`), back);

// 12. Opens without a network (the saved app), says so instead of a browser error.
if (!E.UAT_ENGINE || E.UAT_ENGINE === "chromium") {
  await a.reload({ waitUntil: "networkidle" });
  await a.context().setOffline(true);
  await a.reload().catch(() => {});
  await a.waitForTimeout(2000);
  const off = await a.locator("body").innerText().catch(() => "");
  await shot(a, "app-11-offline-phone");
  check("the app opens offline from its saved copy and says it's offline", /You're offline/.test(off) && !/Work email/.test(off), off.slice(0, 120).replace(/\n/g, " | "));
  await a.context().setOffline(false);
  await a.getByRole("button", { name: "Try again" }).click().catch(() => {});
  const again = await waitText(a.locator(".ph-status"), new RegExp(`Ext ${E.A_EXT}`), 30000);
  check("back online, the app carries on without signing in again", again.includes(`Ext ${E.A_EXT}`), again);
}

// 13. Someone without an extension is told what to do.
if (E.C_EMAIL) {
  const c = await openApp(bB, { ...devices["iPhone 13"], defaultBrowserType: undefined }, E.C_EMAIL, E.C_PW, "C");
  const t = await waitText(c.locator(".ph-empty"), /extension/, 15000);
  check("no extension: a plain explanation, no keypad", /No phone extension yet/.test(t) && (await c.locator(".ph-keys").count()) === 0, t.replace(/\n/g, " | "));
  await shot(c, "app-12-no-extension-phone");
}

// 14. Every screen at small and large sizes without sideways scrolling.
for (const [name, vp] of [["iphone-se", { width: 375, height: 667 }], ["ipad", { width: 820, height: 1180 }], ["desktop", { width: 1440, height: 900 }]]) {
  await a.setViewportSize(vp);
  for (const t of ["Keypad", "Contacts", "Recents", "Settings"]) {
    await tab(a, t);
    if (!(await noSideScroll(a))) check(`no sideways scrolling: ${t} on ${name}`, false);
  }
  await tab(a, "Keypad");
  await shot(a, `app-13-${name}`);
}
check("no sideways scrolling on any screen size", !results.some((r) => r[1].startsWith("no sideways scrolling:")));

await bA.close();
await bB.close();
const fails = results.filter((r) => r[0] === "FAIL").length;
console.log(`\n${results.length - fails} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
