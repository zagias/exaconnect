// Browser test of the phone system (docs/commai/voice-uat.md): two people sign in to the
// portal's browser phone in Chromium with a fake microphone and call each other.
// Checks ringing, answer, audio both ways (WebRTC stats), mute, hang-up, decline to
// voicemail, a missing extension's message, voicemail, and optionally a menu and a queue.
//   UAT_BASE=https://connect.exacarib.com A_EMAIL=.. A_PW=.. A_EXT=200 \
//   B_EMAIL=.. B_PW=.. B_EXT=201 [MENU_EXT=500] [QUEUE_EXT=600] node browser-call.mjs
// Passwords come from the environment only and are never printed.
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";
const BASE = process.env.UAT_BASE || "https://localhost";
const SHOTS = process.env.UAT_SHOTS || "uat-shots";
mkdirSync(SHOTS, { recursive: true });
async function browser() {
  return chromium.launch({ ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}), args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required"] });
}
async function signIn(b, email, pw) {
  const ctx = await b.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 950 }, permissions: ["microphone"] });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => console.log(email, "pageerror", String(e)));
  p.on("console", (m) => { if (m.type() === "error") console.log(email, "console", m.text()); });
  await p.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await p.getByLabel("Work email").fill(email);
  await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").waitFor();
  await p.getByLabel("Password").fill(pw);
  const login = p.waitForResponse((r) => r.url().includes("/auth/login"));
  await p.getByRole("button", { name: /Sign in/ }).click();
  const r = await login;
  if (r.status() !== 200) throw new Error(`${email} sign-in ${r.status()}`);
  await p.locator("aside, nav").first().waitFor();
  await p.waitForLoadState("networkidle");
  return p;
}

const shot = (p, n) => p.screenshot({ path: `${SHOTS}/${n}.png` });
const status = (p) => p.locator('p[role="status"][aria-live="polite"]').innerText();
const hook = () => {
  const Orig = window.RTCPeerConnection;
  window.__pcs = [];
  window.RTCPeerConnection = function (...a) { const pc = new Orig(...a); window.__pcs.push(pc); return pc; };
  window.RTCPeerConnection.prototype = Orig.prototype;
};
async function audio(p) {
  return p.evaluate(async () => {
    const pc = window.__pcs[window.__pcs.length - 1];
    if (!pc) return null;
    const out = { ice: pc.iceConnectionState };
    (await pc.getStats()).forEach((s) => {
      if (s.type === "inbound-rtp" && s.kind === "audio") Object.assign(out, { rxPackets: s.packetsReceived, rxBytes: s.bytesReceived, rxLevel: s.audioLevel, rxEnergy: s.totalAudioEnergy, lost: s.packetsLost, jitter: s.jitter });
      if (s.type === "outbound-rtp" && s.kind === "audio") Object.assign(out, { txPackets: s.packetsSent, txBytes: s.bytesSent });
      if (s.type === "codec" && /audio/.test(s.mimeType)) out.codec = s.mimeType;
    });
    return out;
  });
}
async function phone(b, email, pw, tag) {
  const p = await signIn(b, email, pw);
  await p.context().addInitScript(hook);
  await p.goto(`${BASE}/commai/voice/phone`, { waitUntil: "networkidle" });
  await p.getByRole("button", { name: "Sign in to the browser phone" }).click();
  await p.getByText("Signed in", { exact: true }).waitFor({ timeout: 15000 });
  await shot(p, `${tag}-signed-in`);
  console.log(tag, "status:", await status(p));
  return p;
}
const results = [];
const check = (name, ok, detail = "") => { results.push([ok ? "PASS" : "FAIL", name, detail]); console.log(ok ? "PASS" : "FAIL", name, detail); };

const b = await browser();
const a = await phone(b, process.env.A_EMAIL, process.env.A_PW, "10-a");
const c = await phone(b, process.env.B_EMAIL, process.env.B_PW, "11-b");
check("both browser phones signed in", true);

// 1. A calls B, B answers, audio both ways, A hangs up.
await a.getByPlaceholder("Extension or number").fill(process.env.B_EXT);
await a.getByRole("button", { name: "Call" }).click();
const rang = await c.getByRole("button", { name: "Answer" }).waitFor({ timeout: 15000 }).then(() => true, () => false);
await shot(c, "12-b-incoming");
check(`extension ${process.env.B_EXT} rings`, rang, await status(c));
await shot(a, "13-a-ringing");
if (rang) {
  await c.getByRole("button", { name: "Answer" }).click();
  await a.waitForFunction(() => /On a call/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 15000 }).catch(() => {});
  await a.waitForTimeout(6000);
  const sa = await audio(a), sb = await audio(c);
  await shot(a, "14-a-on-call");
  await shot(c, "15-b-on-call");
  check("caller sees On a call", /On a call/.test(await status(a)), await status(a));
  check("answerer sees On a call", /On a call/.test(await status(c)), await status(c));
  check("audio reaches the caller", sa?.rxPackets > 100 && sa?.rxEnergy > 0, JSON.stringify(sa));
  check("audio reaches the answerer", sb?.rxPackets > 100 && sb?.rxEnergy > 0, JSON.stringify(sb));
  // Mute and keypad tones while on the call
  await a.getByRole("button", { name: "Mute" }).click();
  check("mute toggles", (await a.getByRole("button", { name: "Unmute" }).count()) === 1);
  await a.getByRole("button", { name: "Unmute" }).click();
  await a.getByRole("button", { name: "Hang up" }).click();
  await c.waitForFunction(() => /Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 10000 }).catch(() => {});
  await c.waitForTimeout(3000);
  await shot(c, "16-b-ended");
  const sc = await status(c), sa2 = await status(a);
  check("hang-up ends the call on both sides and stays ended", /Call ended/.test(sc) && /Call ended/.test(sa2), `${sa2} / ${sc}`);
}
// 2. B calls A, A declines.
await c.getByPlaceholder("Extension or number").fill(process.env.A_EXT);
await c.getByRole("button", { name: "Call" }).click();
const rang2 = await a.getByRole("button", { name: "Decline" }).waitFor({ timeout: 15000 }).then(() => true, () => false);
check(`extension ${process.env.A_EXT} rings (call the other way)`, rang2, await status(a));
if (rang2) {
  await a.getByRole("button", { name: "Decline" }).click();
  await c.waitForFunction(() => /On a call|Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 10000 }).catch(() => {});
  await c.waitForTimeout(5000);
  const sd = await audio(c);
  await shot(c, "17-b-declined-to-voicemail");
  check("declined call goes to voicemail, caller hears the greeting", /On a call/.test(await status(c)) && sd?.rxEnergy > 0, `${await status(c)} ${JSON.stringify(sd)}`);
  if (await c.getByRole("button", { name: "Hang up" }).count()) await c.getByRole("button", { name: "Hang up" }).click();
}
// 3. A number that does not exist.
await a.waitForTimeout(1000);
await a.getByPlaceholder("Extension or number").fill("299");
await a.getByRole("button", { name: "Call" }).click();
await a.waitForFunction(() => /Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 15000 }).catch(() => {});
await shot(a, "18-a-missing-extension");
check("missing extension gives a plain message", /doesn't exist/.test(await a.locator("main").innerText()), await status(a));
// 4. Voicemail.
await a.getByPlaceholder("Extension or number").fill("*97");
await a.getByRole("button", { name: "Call" }).click();
await a.waitForFunction(() => /On a call|Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 15000 }).catch(() => {});
await a.waitForTimeout(4000);
const sv = await audio(a);
await shot(a, "19-a-voicemail");
check("voicemail answers with audio", /On a call/.test(await status(a)) && sv?.rxEnergy > 0, `${await status(a)} ${JSON.stringify(sv)}`);
if (await a.getByRole("button", { name: "Hang up" }).count()) await a.getByRole("button", { name: "Hang up" }).click();
// 5. Voice menu: spoken greeting, press 1, reaches B.
if (process.env.MENU_EXT) {
  await a.waitForTimeout(1500);
  await a.getByPlaceholder("Extension or number").fill(process.env.MENU_EXT);
  await a.getByRole("button", { name: "Call" }).click();
  await a.waitForFunction(() => /On a call|Call ended/.test(document.querySelector('p[role="status"][aria-live="polite"]')?.textContent || ""), null, { timeout: 15000 }).catch(() => {});
  await a.waitForTimeout(4000);
  const sm = await audio(a);
  check("menu answers and speaks its greeting", /On a call/.test(await status(a)) && sm?.rxEnergy > 0, `${await status(a)} ${JSON.stringify(sm)}`);
  await a.getByRole("button", { name: "1", exact: true }).click();
  const rang3 = await c.getByRole("button", { name: "Answer" }).waitFor({ timeout: 15000 }).then(() => true, () => false);
  check("pressing 1 on the keypad reaches the second tester", rang3, await status(c));
  if (rang3) {
    await c.getByRole("button", { name: "Answer" }).click();
    await c.waitForTimeout(4000);
    const s3 = await audio(c);
    check("audio after the menu", s3?.rxEnergy > 0, JSON.stringify(s3));
    await shot(c, "20-b-from-menu");
  }
  if (await a.getByRole("button", { name: "Hang up" }).count()) await a.getByRole("button", { name: "Hang up" }).click();
  await c.waitForTimeout(1500);
  if (await c.getByRole("button", { name: "Hang up" }).count()) await c.getByRole("button", { name: "Hang up" }).click();
}
// 6. Call queue: hold music, then the agent rings.
if (process.env.QUEUE_EXT) {
  await a.waitForTimeout(1500);
  await a.getByPlaceholder("Extension or number").fill(process.env.QUEUE_EXT);
  await a.getByRole("button", { name: "Call" }).click();
  await a.waitForTimeout(5000);
  const sq = await audio(a);
  check("queue answers with hold music", /On a call/.test(await status(a)) && sq?.rxEnergy > 0, `${await status(a)} ${JSON.stringify(sq)}`);
  const rang4 = await c.getByRole("button", { name: "Answer" }).waitFor({ timeout: 20000 }).then(() => true, () => false);
  check("queue rings its member", rang4, await status(c));
  if (rang4) {
    await c.getByRole("button", { name: "Answer" }).click();
    await c.waitForTimeout(4000);
    check("queue call connects", /On a call/.test(await status(c)), await status(c));
    await shot(c, "21-b-from-queue");
  }
  if (await a.getByRole("button", { name: "Hang up" }).count()) await a.getByRole("button", { name: "Hang up" }).click();
}
await b.close();
const fails = results.filter((r) => r[0] === "FAIL").length;
console.log(`\n${results.length - fails} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
