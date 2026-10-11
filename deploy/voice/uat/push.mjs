// Live test: Jibsy Phone rings when it's closed (ADR 0045, docs/commai/voice-uat.md).
// A turns ringing on for a device, closes the app, and B calls A. The phone system must
// wake A's device with an encrypted push message; A's own service worker must show the
// call notification; opening it (what a tap does) must bring the app up, connected,
// answering the call while B still hears ringing, with audio both ways.
//
// The push message goes to a stand-in push service, http://push-check:8099 (a container on
// the compose network that saves what it receives in PUSH_DIR), because a test browser has
// no Apple or Google push service. The test decrypts it with the device keys it made, then
// hands it to A's real service worker the way Chrome does when a push arrives.
//   UAT_BASE=https://jibsyapp.exacarib.com PUSH_DIR=/push A_EMAIL=.. A_PW=.. A_EXT=..
//   B_EMAIL=.. B_PW=.. B_EXT=.. node push.mjs
// Passwords come from the environment only and are never printed.
import crypto from "node:crypto";
import { readdirSync, readFileSync, mkdirSync } from "node:fs";
import { chromium, devices } from "playwright";

const E = process.env;
const BASE = (E.UAT_BASE || "https://jibsyapp.localhost").replace(/\/$/, "");
const SHOTS = E.UAT_SHOTS || "uat-shots";
const PUSH_DIR = E.PUSH_DIR || "/push";
mkdirSync(SHOTS, { recursive: true });
let fails = 0, passes = 0;
const check = (name, ok, detail = "") => {
  ok ? passes++ : fails++;
  console.log(ok ? "PASS" : "FAIL", name, detail ? `(${String(detail).slice(0, 200)})` : "");
};
const shot = (p, n) => p.screenshot({ path: `${SHOTS}/${n}.png` }).catch(() => {});
// Chrome's new headless mode: the old one refuses every notification whatever is granted.
const FAKE = ["--headless=new", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required"];
const HDR = { "content-type": "application/json", "X-Requested-With": "exa-portal" };
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
    const out = {};
    (await pc.getStats()).forEach((s) => {
      if (s.type === "inbound-rtp" && s.kind === "audio") Object.assign(out, { rx: s.packetsReceived, energy: s.totalAudioEnergy });
    });
    return out;
  });
}
async function waitText(loc, re, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const t = await loc.innerText().catch(() => "");
    if (re.test(t)) return t;
    await new Promise((r) => setTimeout(r, 250));
  }
  return loc.innerText().catch(() => "");
}
async function signIn(ctx, email, pw, path = "/") {
  const p = await ctx.newPage();
  await p.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  await p.getByLabel("Work email").fill(email);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(pw);
  await p.getByRole("button", { name: /Sign in/ }).click();
  return p;
}

// The device's side of Web Push (RFC 8291): a P-256 key pair and an auth secret.
const ecdh = crypto.createECDH("prime256v1");
ecdh.generateKeys();
const authSecret = crypto.randomBytes(16);
const b64u = (b) => Buffer.from(b).toString("base64url");
function decrypt(body) {
  const salt = body.subarray(0, 16);
  const idlen = body[20];
  const asPub = body.subarray(21, 21 + idlen);
  const ct = body.subarray(21 + idlen);
  const shared = ecdh.computeSecret(asPub);
  const info = Buffer.concat([Buffer.from("WebPush: info\0"), ecdh.getPublicKey(), asPub]);
  const ikm = Buffer.from(crypto.hkdfSync("sha256", shared, authSecret, info, 32));
  const cek = Buffer.from(crypto.hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(crypto.hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  const d = crypto.createDecipheriv("aes-128-gcm", cek, nonce);
  d.setAuthTag(ct.subarray(ct.length - 16));
  const plain = Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]);
  return JSON.parse(plain.subarray(0, plain.lastIndexOf(2)).toString());
}
const device = crypto.randomBytes(8).toString("hex");
const endpoint = `http://push-check:8099/${device}`;
const received = () => readdirSync(PUSH_DIR).filter((f) => f.startsWith(device) && f.endsWith(".bin")).sort();

const browser = await chromium.launch({ args: FAKE, ...(E.CHROMIUM ? { executablePath: E.CHROMIUM } : {}) });
const origin = new URL(BASE).origin;
const ctxA = await browser.newContext({ ignoreHTTPSErrors: true, ...devices["Pixel 7"], defaultBrowserType: undefined });
await ctxA.grantPermissions(["microphone", "notifications"], { origin });
await ctxA.addInitScript(hook);
const ctxB = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 800 }, permissions: ["microphone"] });
await ctxB.addInitScript(hook);
let customer = "";

try {
  // 1. A opens the app; Settings offers ringing when closed.
  const a = await signIn(ctxA, E.A_EMAIL, E.A_PW);
  const on = await waitText(a.locator(".ph-status"), new RegExp(`Ext ${E.A_EXT}`), 20000);
  check("A's app connects", on.includes(`Ext ${E.A_EXT}`), on);
  await a.getByRole("navigation", { name: "Phone" }).getByRole("button", { name: "Settings" }).click();
  const section = await waitText(a.locator(".ph-ring-closed"), /Ring this device when Jibsy Phone is closed/, 10000);
  check("Settings offer: ring this device when Jibsy Phone is closed", /Ring this device/.test(section), section.replace(/\n/g, " | "));
  await shot(a, "push-01-settings");
  const sw = await a.evaluate(async () => (await navigator.serviceWorker.ready).scope);
  check("the app's service worker is ready", sw.endsWith("/phone"), sw);

  // 2. This device's subscription, as the toggle sends it (the test browser has no real push
  //    service, so the device keys are the test's own and the address is the stand-in).
  const sub = await a.evaluate(
    async ({ endpoint, p256dh, auth, HDR }) => {
      const list = await (await fetch("/api/v1/customers/mine", { headers: HDR })).json();
      for (const c of list) {
        const st = await fetch(`/api/v1/commai/customers/${c.id}/voice/webphone/push`, { headers: HDR });
        if (!st.ok) continue;
        const s = await st.json();
        const r = await fetch(`/api/v1/commai/customers/${c.id}/voice/webphone/push`, {
          method: "POST", headers: HDR, body: JSON.stringify({ endpoint, keys: { p256dh, auth } }),
        });
        return { customer: c.id, available: s.available, status: r.status, text: (await r.text()).slice(0, 160) };
      }
      return { status: 0, text: "no business with a phone" };
    },
    { endpoint, p256dh: b64u(ecdh.getPublicKey()), auth: b64u(authSecret), HDR },
  );
  customer = sub.customer || "";
  check("ringing is available and the device is saved", sub.available && sub.status === 200, JSON.stringify(sub));
  const t = await a.evaluate(
    async ({ c, HDR }) => (await fetch(`/api/v1/commai/customers/${c}/voice/webphone/push/test`, { method: "POST", headers: HDR })).json(),
    { c: customer, HDR },
  );
  const testMsg = await (async () => {
    for (let i = 0; i < 20; i++) {
      const f = received();
      if (f.length) return decrypt(readFileSync(`${PUSH_DIR}/${f[f.length - 1]}`));
      await new Promise((r) => setTimeout(r, 250));
    }
    return null;
  })();
  check("Send a test notification reaches the device, encrypted for it", t.reached >= 1 && testMsg?.type === "test", JSON.stringify({ t, testMsg }));

  // 3. A closes the app. A page in the same browser (not the app) stays, like the phone's OS.
  const host = await ctxA.newPage();
  await host.goto(`${BASE}/phone.webmanifest`);
  await a.close();
  await host.waitForTimeout(1500);
  const before = received().length;

  // 4. B calls A.
  const b = await signIn(ctxB, E.B_EMAIL, E.B_PW);
  await waitText(b.locator(".ph-status"), new RegExp(`Ext ${E.B_EXT}`), 20000);
  await b.getByLabel("Extension or number").fill(E.A_EXT);
  const t0 = Date.now();
  await b.getByRole("button", { name: "Call", exact: true }).click();
  let file = null;
  while (Date.now() - t0 < 12000) {
    const f = received();
    if (f.length > before) {
      file = f[f.length - 1];
      break;
    }
    await b.waitForTimeout(200);
  }
  const msg = file ? decrypt(readFileSync(`${PUSH_DIR}/${file}`)) : null;
  check("calling a closed app sends its device a push message", !!msg, file ? "" : "nothing within 12 s");
  check("the message names the call, the caller and the extension", msg?.type === "call" && msg?.from === E.B_EXT && msg?.ext === E.A_EXT && !!msg?.call, JSON.stringify(msg));
  const headers = file ? JSON.parse(readFileSync(`${PUSH_DIR}/${file.replace(/\.bin$/, ".json")}`, "utf8")) : {};
  check("sent urgently, signed with ExaCarib's key, short-lived", headers.urgency === "high" && /^vapid t=.+, k=.+/.test(headers.authorization || "") && Number(headers.ttl) <= 60, JSON.stringify({ u: headers.urgency, ttl: headers.ttl }));
  check("B hears it ringing meanwhile", /Calling|Ringing/i.test(await b.locator(".ph-overlay").innerText().catch(() => "")));

  // 5. The push reaches A's service worker, which shows the call notification.
  const cdp = await ctxA.newCDPSession(host);
  const regs = [];
  cdp.on("ServiceWorker.workerRegistrationUpdated", (e) => regs.push(...e.registrations));
  await cdp.send("ServiceWorker.enable");
  await host.waitForTimeout(500);
  const reg = regs.find((r) => r.scopeURL.endsWith("/phone") && !r.isDeleted);
  if (msg && reg) {
    await cdp.send("ServiceWorker.deliverPushMessage", { origin, registrationId: reg.registrationId, data: JSON.stringify(msg) });
  }
  let notes = [];
  for (let i = 0; i < 20 && !notes.length; i++) {
    await host.waitForTimeout(250);
    notes = await host.evaluate(async () => {
      const r = await navigator.serviceWorker.getRegistration("/phone");
      return (await r.getNotifications({ tag: "jibsy-call" })).map((n) => ({ title: n.title, body: n.body, data: n.data, actions: n.actions?.length }));
    });
  }
  const n = notes[0];
  check("A's device shows an Incoming call notification with B's number", n?.title === "Incoming call" && n.body.includes(E.B_EXT), JSON.stringify(notes));
  check("the notification offers Answer and opens the app to answer", n?.actions === 1 && /^\/phone\?answer=/.test(n?.data?.url || ""), n?.data?.url);

  // 6. A taps it: the app opens, connects and answers; the call has audio both ways.
  const tapped = Date.now();
  const a2 = await ctxA.newPage();
  await a2.goto(`${BASE}${n?.data?.url || "/phone"}`);
  const live = await waitText(a2.locator(".ph-overlay"), /On a call/, 25000);
  const secs = ((Date.now() - tapped) / 1000).toFixed(1);
  check("the app opens signed in and answers the call", live.includes("On a call"), `${secs} s after the tap; ${live.replace(/\n/g, " | ")}`);
  check("B is connected to A", (await waitText(b.locator(".ph-overlay"), /On a call/, 5000)).includes("On a call"));
  check("the whole call took under 25 s from dialling to answer", Date.now() - t0 < 25000, `${((Date.now() - t0) / 1000).toFixed(1)} s`);
  await a2.waitForTimeout(1000);
  const [x0, y0] = [(await audio(a2))?.rx || 0, (await audio(b))?.rx || 0];
  await a2.waitForTimeout(4000);
  const [x1, y1] = [await audio(a2), await audio(b)];
  check("audio reaches A", (x1?.rx || 0) - x0 >= 150 && x1?.energy > 0, JSON.stringify({ ...x1, in4s: (x1?.rx || 0) - x0 }));
  check("audio reaches B", (y1?.rx || 0) - y0 >= 150 && y1?.energy > 0, JSON.stringify({ ...y1, in4s: (y1?.rx || 0) - y0 }));
  check("the address bar no longer says answer, so a reload doesn't answer again", !a2.url().includes("answer="), a2.url());
  await shot(a2, "push-02-answered");
  await a2.getByRole("button", { name: "Hang up" }).click();
  await b.waitForTimeout(2000);
  check("hang-up ends the call on both sides", (await a2.locator(".ph-overlay").count()) === 0 && (await b.locator(".ph-overlay").count()) === 0);

  // 7. With the app open, calls don't send a push (it rings in the app as before).
  const quiet = received().length;
  await b.getByLabel("Extension or number").fill(E.A_EXT);
  await b.getByRole("button", { name: "Call", exact: true }).click();
  const ring = await waitText(a2.locator(".ph-overlay"), /Incoming call/, 15000);
  check("with the app open, it rings in the app and no push is sent", ring.includes("Incoming call") && received().length === quiet, `${received().length - quiet} pushes`);
  await b.getByRole("button", { name: "Hang up" }).click();
  await a2.waitForTimeout(1500);

  // 8. Turning it off removes the device.
  const off = await a2.evaluate(
    async ({ c, endpoint, HDR }) => {
      await fetch(`/api/v1/commai/customers/${c}/voice/webphone/push`, { method: "DELETE", headers: HDR, body: JSON.stringify({ endpoint }) });
      return (await (await fetch(`/api/v1/commai/customers/${c}/voice/webphone/push?endpoint=${encodeURIComponent(endpoint)}`, { headers: HDR })).json()).this_device;
    },
    { c: customer, endpoint, HDR },
  );
  check("turning ringing off removes the device", off === false);
  customer = "";
} catch (e) {
  check("test stopped: " + String(e).slice(0, 300), false);
}
await browser.close();
console.log(`${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
