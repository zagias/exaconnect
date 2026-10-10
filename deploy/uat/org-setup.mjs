// Organisation set-up, tested as people use it (ADR 0043): ExaCarib staff create an
// organisation; its new owner sets it up from scratch (company, three locations, connecting
// one, inviting a colleague); the colleague and staff each see the right screens.
// Run by lab/ci/checks/org-setup.sh against the live portal after each deploy, and locally:
//   UAT_BASE=http://localhost:5173 STAFF_EMAIL=... STAFF_PW=... node org-setup.mjs
// The organisation is named ORG_PREFIX + a random number, and its people use
// @setup-check.example addresses, so deploy/uat/setup_users.py removes them on the next run.
import { mkdirSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { chromium } from "playwright";

const E = process.env;
const B = (E.UAT_BASE || "http://localhost:5173").replace(/\/$/, "");
const OUT = E.UAT_SHOTS || "";
if (OUT) mkdirSync(OUT, { recursive: true });
const tag = randomBytes(3).toString("hex");
const ORG = (E.ORG_PREFIX || "Set-up check ") + tag;
const OWNER = `owner-${tag}@setup-check.example`;
const PW = randomBytes(18).toString("base64url");
let fails = 0, passes = 0;
const ok = (m) => { passes++; console.log("PASS", m); };
const bad = (m) => { fails++; console.log("FAIL", m); };
const br = await chromium.launch(E.CHROMIUM_PATH ? { executablePath: E.CHROMIUM_PATH } : {});
async function session() {
  const ctx = await br.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  p.on("dialog", (d) => d.accept());
  p.on("pageerror", (e) => bad("page error: " + String(e).slice(0, 160)));
  p.on("response", (r) => { if (r.url().includes("/api/") && r.status() >= 500) bad(`API ${r.status()} ${r.url()}`); });
  return p;
}
async function signIn(p, email, pw) {
  await p.goto(B + "/");
  await p.getByLabel("Work email").fill(email); await p.getByRole("button", { name: "Continue" }).click();
  await p.getByLabel("Password").fill(pw); await p.getByRole("button", { name: "Sign in" }).click();
  await p.waitForSelector(".shell main#main", { timeout: 15000 });
}
const shot = (p, n) => (OUT ? p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }) : null);
const side = (p) => p.locator("#shell-nav nav").innerText();

try {
  // 1. Staff: the switcher has ExaCarib operations; create the organisation there.
  const staff = await session();
  await signIn(staff, E.STAFF_EMAIL, E.STAFF_PW);
  (await side(staff)).includes('Admin') && !(await side(staff)).includes('Organisations') ? bad('staff menu still has the old Manage group') : ok('staff app menu has no staff tools in it');
  await staff.locator('.app-switch').click();
  await staff.getByRole('link', { name: /ExaCarib operations/ }).click(); await staff.waitForTimeout(800);
  await staff.waitForURL(/\/ops\/organisations/);
  const opsSide = await side(staff);
  ['Organisations', 'Sites and links', 'Agents', 'Releases', 'Support queue'].every((w) => opsSide.includes(w)) ? ok('operations menu lists staff screens') : bad('operations menu incomplete: ' + opsSide);
  await staff.getByLabel('Organisation name').fill(ORG);
  await staff.getByLabel("Owner's work email").fill(OWNER);
  await staff.getByLabel(/Jibsy by ExaCarib/).check();
  await staff.getByRole('button', { name: 'Create and invite the owner' }).click();
  await staff.getByText('is ready').waitFor({ timeout: 10000 });
  const link = (await staff.locator('pre.install-command').innerText()).trim();
  link.includes('/invite/') ? ok('organisation created with an owner invitation link') : bad('no invitation link');
  await shot(staff, '01_staff_created');

  // 2. The owner accepts and lands in the portal.
  const owner = await session();
  await owner.goto(link);
  await owner.getByLabel('Your name').fill('Marlene Joseph');
  await owner.getByLabel('Choose a password, at least 12 characters').fill(PW);
  await owner.getByLabel('Password again').fill(PW);
  await owner.getByRole('button', { name: /Create account and join/ }).click(); await owner.getByRole('button', { name: 'Start setting up' }).click();
  await owner.waitForSelector('.shell main#main', { timeout: 15000 });
  await owner.waitForTimeout(2000);
  (await owner.locator('.shell-user-role').first().innerText().catch(() => '')).length;
  const me = await owner.evaluate(async () => (await fetch('/api/v1/auth/me')).json());
  me.org_role === 'owner' ? ok('the invited person became the owner') : bad('owner role is ' + me.org_role);
  await owner.goto(B + '/');
  await owner.waitForTimeout(2000);
  await shot(owner, '02_owner_first_screen');
  (await owner.locator('.setup-band').count()) ? ok('set-up band shows on the first screen') : bad('no set-up band');
  (await owner.locator('main').innerText()).includes('Connect your first location') ? ok('overview says what to do first') : bad('overview first-run panel missing');

  // 3. Checklist.
  await owner.getByRole('link', { name: 'Continue set-up' }).click();
  await owner.waitForURL(/\/org\/setup/);
  await owner.waitForTimeout(1200);
  await shot(owner, '03_checklist_start');
  const steps = await owner.locator('.setup-step').count();
  steps === 5 ? ok('checklist has 5 steps for a two-plan organisation') : bad('checklist steps: ' + steps);
  const orgSide = await side(owner);
  ['Set-up checklist', 'Company details', 'Locations', 'People and access', 'Sign-in and security', 'Apps and plans', 'Billing', 'Profile and sign-in'].every((w) => orgSide.includes(w)) ? ok('Organisation menu has every set-up page') : bad('Organisation menu: ' + orgSide);

  // 4. Company details.
  await owner.getByRole('link', { name: 'Add company details' }).click();
  await owner.waitForURL(/\/org\/company/);
  await owner.getByLabel('Main address').fill('22 Frederick Street, Port of Spain');
  await owner.getByLabel('Time zone').fill('America/Port_of_Spain');
  await owner.getByLabel('Main phone').fill('+1 868 555 0100');
  await owner.getByRole('button', { name: 'Save details' }).click();
  await owner.getByText('✓ Saved').waitFor({ timeout: 8000 });
  ok('company details saved');

  // 5. Locations: three branches.
  await owner.goto(B + '/org/locations');
  for (const [n, a, c, i] of [['Head office', '22 Frederick Street', 'Port of Spain', 'Trinidad'], ['San Fernando branch', '5 High Street', 'San Fernando', 'Trinidad'], ['Scarborough branch', '3 Wilson Road', 'Scarborough', 'Tobago']]) {
    await owner.getByRole('button', { name: 'Add a location' }).click();
    await owner.getByLabel('Name', { exact: true }).fill(n);
    await owner.getByLabel('Street address').fill(a);
    await owner.getByLabel('Town or city').fill(c);
    await owner.getByLabel('Island').fill(i);
    await owner.getByLabel('Country (two letters)').fill('TT');
    await owner.getByRole('button', { name: 'Add location' }).click();
    await owner.getByRole('heading', { name: n }).waitFor({ timeout: 10000 });
  }
  ok('three locations added');
  await owner.waitForTimeout(800);
  const locText = await owner.locator('main').innerText();
  (locText.match(/Emergency address/g) || []).length >= 3 ? ok('each location has its phone site (emergency address)') : bad('phone sites missing on locations');

  // 6. Connect the head office.
  await owner.locator('section.loc', { hasText: 'Head office' }).getByRole('button', { name: 'Connect this location' }).click();
  await owner.getByLabel('Carrier').first().fill('Digicel');
  await owner.getByLabel('Speed you pay for (Mbps)').first().fill('200');
  await owner.getByRole('button', { name: 'Add another link' }).click();
  await owner.getByLabel('Carrier').nth(1).fill('Flow');
  await owner.getByLabel('Speed you pay for (Mbps)').nth(1).fill('100');
  await owner.getByRole('button', { name: 'Connect and get the install code' }).click();
  await owner.getByRole('heading', { name: /Install the ExaCarib box at Head office/ }).waitFor({ timeout: 10000 });
  (await owner.locator('pre.install-command').innerText()).includes('EXA_ENROL_TOKEN=') ? ok('install code shown after connecting a location') : bad('no install command');
  await shot(owner, '04_locations_install');
  await owner.getByRole('button', { name: 'Done' }).click();
  (await owner.locator('section.loc', { hasText: 'Head office' }).innerText()).includes('Waiting for the box') ? ok('head office shows it is waiting for the box') : bad('head office state wrong');

  // 7. Invite a colleague.
  await owner.goto(B + '/account/people');
  await owner.getByLabel('Email').fill(OWNER.replace('owner', 'agent'));
  await owner.getByRole('button', { name: 'Create invitation' }).click();
  const inv2 = (await owner.locator('[aria-label="Invitation link"] code').innerText()).trim();
  ok('colleague invited');

  // 8. Checklist progress, Jibsy's own pages, Overview.
  await owner.goto(B + '/org/setup');
  await owner.waitForTimeout(1500);
  const done = await owner.locator('.setup-step.done').count();
  done === 3 ? ok('checklist shows 3 of 5 done (company, locations, people)') : bad('done steps: ' + done);
  await shot(owner, '05_checklist_progress');
  await owner.goto(B + '/commai/settings/organisation');
  await owner.waitForTimeout(1500);
  const hoursText = await owner.locator('main').innerText();
  ['Head office', 'San Fernando branch', 'Scarborough branch'].every((n) => hoursText.includes(n)) ? ok('Jibsy opening hours list the same three locations') : bad('Jibsy locations differ');
  await owner.goto(B + '/commai/voice');
  await owner.waitForTimeout(1500);
  const phoneText = await owner.locator('main').innerText();
  phoneText.includes('San Fernando branch') ? ok('Phone sites are the same locations') : bad('Phone sites differ');
  await owner.goto(B + '/');
  await owner.waitForTimeout(1500);
  (await owner.locator('main').innerText()).includes('Waiting for 1 box to check in') ? ok('overview waits for the box') : bad('overview waiting text missing');
  await shot(owner, '06_overview_waiting');
  const staffLinks = await owner.locator('a[href="/admin"], a[href^="/ops"]').count();
  staffLinks === 0 ? ok('owner sees no ExaCarib staff screens') : bad('owner sees staff links');

  // 9. The colleague joins: no set-up checklist, sees their organisation.
  const agent = await session();
  await agent.goto(inv2.startsWith('http') ? inv2 : B + inv2);
  await agent.getByLabel('Your name').fill('Andre Pierre');
  await agent.getByLabel('Choose a password, at least 12 characters').fill(PW);
  await agent.getByLabel('Password again').fill(PW);
  await agent.getByRole('button', { name: /Create account and join/ }).click(); await agent.getByRole('button', { name: 'Open ExaCarib' }).click();
  await agent.waitForSelector('.shell main#main', { timeout: 15000 });
  await agent.waitForTimeout(1500);
  (await agent.locator('.setup-band').count()) === 0 ? ok('members get no set-up band') : bad('member sees set-up band');
  await agent.goto(B + '/org/locations');
  await agent.waitForTimeout(1500);
  const agentLoc = await agent.locator('main').innerText();
  agentLoc.includes('Scarborough branch') && !(await agent.getByRole('button', { name: 'Add a location' }).count()) ? ok('members see locations read-only') : bad('member locations view wrong');
  await shot(agent, '07_member_locations');

  // 10. Staff see the new organisation's progress.
  await staff.goto(B + '/ops/organisations');
  await staff.waitForTimeout(1500);
  const row = await staff.locator('tr', { hasText: ORG }).innerText();
  row.includes(OWNER) && /3 of 5/.test(row) ? ok('staff see owner and set-up progress') : bad('staff row: ' + row.replace(/\s+/g, ' '));
  await shot(staff, '08_staff_list');

  // 11. Every phone extension is accounted for on People and access: in the demo business,
  // ExaCarib's admin (ext 200) and the phone-only lab extension are not members.
  const demo = staff.locator("tr", { hasText: "Demo Organisation" });
  if (await demo.count()) {
    await demo.getByRole("button", { name: "Open" }).click();
    await staff.waitForURL(/\/org\/setup/);
    await staff.goto(B + "/account/people");
    await staff.getByRole("heading", { name: "Members" }).waitFor({ timeout: 10000 });
    await staff.waitForTimeout(1500);
    const others = staff.locator("section", { has: staff.getByRole("heading", { name: /not held by a member/ }) });
    const txt = (await others.count()) ? await others.innerText() : "";
    /\b200\b/.test(txt) ? ok("People and access lists the extensions no member holds (200)") : bad("extension 200 not on People and access");
    await shot(staff, "09_demo_people");
  }
} catch (e) {
  bad("walk-through stopped: " + String(e).split("\n")[0].slice(0, 200));
}
await br.close();
console.log(`\n${passes} passed, ${fails} failed`);
process.exit(fails ? 1 : 0);
