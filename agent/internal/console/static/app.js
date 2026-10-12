// ExaCarib box console (ADR 0046). Plain JavaScript, no build step: the agent
// embeds this file. Every value from the box goes into the page as text.
"use strict";

const main = document.getElementById("main");
const PATHS = { "carrier-a": "Internet link 1", "carrier-b": "Internet link 2", sat: "Satellite" };
const MANAGED = { site: "On site", cloud: "Cloud", both: "On site and cloud" };
const MANAGED_LINE = {
  site: "Only this console changes the box's settings. The Connect portal still runs routing and shows the box.",
  cloud: "The Connect portal changes the box's settings. This console shows them but cannot change them, apart from taking back control in an emergency.",
  both: "This console and the Connect portal can both change the box's settings. The latest change wins.",
};
const MODES = { dhcp: "Automatic (DHCP)", static: "Fixed address", pppoe: "PPPoE", keep: "Leave as it is" };
let state = null;
let timer = null;

// h("div", {class: "x"}, child, "text") builds an element; strings become text nodes.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Exa-Box": "1" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, { credentials: "same-origin", ...opt });
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (r.status === 401 && path !== "login") { state = null; route(); throw new Error((data && data.detail) || "Sign in again."); }
  if (!r.ok) throw new Error((data && data.detail) || `The box answered ${r.status}.`);
  return data;
}

function note(kind, text) { return h("div", { class: "note " + kind, role: kind === "bad" ? "alert" : "status" }, text); }
function pill(health, text) { return h("span", { class: "pill " + health }, text); }
function head(eyebrow, title, line) { return h("div", { class: "head" }, h("div", { class: "eyebrow" }, eyebrow), h("h1", {}, title), line ? h("p", {}, line) : null); }
function show(...nodes) { main.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false)); }
function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "kB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
function ago(t) {
  if (!t || t.startsWith("0001")) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(t).getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return new Date(t).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function duration(s) {
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${hh} h` : hh ? `${hh} h ${m} min` : `${m} min`;
}
function pathTag(p) { return p ? h("span", { class: "tag" + (p === "sat" ? " sat" : "") }, PATHS[p] || p) : null; }
function busy(btn, text) { const old = btn.textContent; btn.disabled = true; btn.textContent = text; return () => { btn.disabled = false; btn.textContent = old; }; }

// ---- signed out ----

function claimView() {
  const err = h("div");
  const f = h("form", { class: "card narrow", onsubmit: async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    if (fd.get("password") !== fd.get("again")) { err.replaceChildren(note("bad", "The two passwords are not the same.")); return; }
    const done = busy(f.querySelector("button"), "Setting up…");
    try { await api("claim", { code: fd.get("code"), password: fd.get("password") }); await start(); location.hash = "#/setup"; }
    catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  } },
  h("div", { class: "eyebrow" }, "New box"),
  h("h1", {}, "Set up this ExaCarib box"),
  h("p", { class: "muted" }, "Enter the setup code from the box's label. If it has no label, run exa-agent console-code on the box itself to see it. Then choose the password for this console."),
  h("div", { class: "fields" },
    h("label", {}, "Setup code", h("input", { name: "code", required: true, autocomplete: "off", placeholder: "ABCDE-FGHJK", autocapitalize: "characters" })),
    h("label", {}, "Admin password", h("input", { name: "password", type: "password", required: true, minlength: 10, autocomplete: "new-password" }), h("span", { class: "hint" }, "At least 10 characters. The user name is admin.")),
    h("label", {}, "Password again", h("input", { name: "again", type: "password", required: true, autocomplete: "new-password" }))),
  err,
  h("div", { class: "actions" }, h("button", { class: "button" }, "Set password and continue")));
  show(f);
}

function signinView() {
  const err = h("div");
  const f = h("form", { class: "card narrow", onsubmit: async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    const done = busy(f.querySelector("button"), "Signing in…");
    try { await api("login", { user: fd.get("user"), password: fd.get("password") }); await start(); }
    catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  } },
  h("div", { class: "eyebrow" }, state.name || state.hostname || "ExaCarib box"),
  h("h1", {}, "Sign in to the box console"),
  h("p", { class: "muted" }, "For people at this location. Your organisation also manages this box in the ExaCarib portal."),
  h("div", { class: "fields" },
    h("label", {}, "User name", h("input", { name: "user", value: "admin", required: true, autocomplete: "username" })),
    h("label", {}, "Password", h("input", { name: "password", type: "password", required: true, autocomplete: "current-password" }))),
  err,
  h("div", { class: "actions" }, h("button", { class: "button" }, "Sign in")),
  h("p", { class: "small muted mt16" }, "Forgot the password? Someone with access to the box can run exa-agent console-reset on it."));
  show(f);
}

// ---- signed in ----

function chrome() {
  const signedIn = state && state.stage === "ready";
  document.getElementById("tabs").hidden = !signedIn;
  const who = document.getElementById("who");
  who.replaceChildren();
  if (state) who.append(h("span", { class: "mono small" }, state.name || state.hostname || ""));
  if (signedIn) who.append(h("button", { class: "button secondary small", onclick: async () => { await api("logout", {}); await start(); } }, "Sign out"));
  const cur = "#" + (location.hash.slice(1).split("?")[0] || "/");
  for (const a of document.querySelectorAll("#tabs a")) {
    const on = a.getAttribute("href") === cur || (cur === "#/setup" && a.getAttribute("href") === "#/");
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  document.getElementById("foot").textContent = `ExaCarib Connect · box console${state && state.version ? " · " + state.version : ""}`;
}

async function start() {
  state = await api("state");
  route();
}

function route() {
  clearInterval(timer);
  timer = null;
  chrome();
  if (!state) { start().catch((e) => show(note("bad", e.message))); return; }
  if (state.stage === "claim") return claimView();
  if (state.stage === "signin") return signinView();
  const r = location.hash.slice(1) || "/";
  const views = { "/": overview, "/setup": setup, "/ports": portsView, "/connect": connectView, "/diagnostics": diagView, "/monitoring": monitoringView, "/settings": settingsView };
  (views[r] || overview)().catch((e) => show(note("bad", e.message)));
}
window.addEventListener("hashchange", route);

function pendingBanner(p, after) {
  if (!p) return null;
  const left = Math.max(0, Math.round((new Date(p.revert_at).getTime() - Date.now()) / 1000));
  return note("warn", h("span", {}, `A port change is waiting to be kept. If nobody keeps it, the box goes back to the earlier ports in ${left} s. `,
    h("button", { class: "button small", onclick: async () => { await api("network/confirm", {}); (after || route)(); } }, "Keep the change")));
}

const ZTP_WORDS = {
  calling: "Calling ExaCarib…",
  waiting: "Waiting for ExaCarib to assign this box to a location. Nothing to do here: it joins by itself once assigned.",
  assigned: "Assigned to a location; joining Connect now.",
  joined: "Joined Connect by zero-touch.",
};

// Zero-touch (ADR 0050): what the box hears when it calls home.
function ztpNote(z) {
  if (!z || !z.on || z.state === "joined") return null;
  const line = z.state === "error" ? `Zero-touch: ${z.detail || "no answer from ExaCarib yet"}. The box keeps trying; you can also set it up below.`
    : `Zero-touch: ${ZTP_WORDS[z.state] || z.state}`;
  return note(z.state === "error" ? "warn" : "good", h("span", {}, line, " ", h("span", { class: "mono small" }, `Serial ${z.serial}`)));
}

function cloudBanner(m) {
  if (!m || m.mode !== "cloud") return null;
  return note("warn", h("span", {}, "This box is managed from the Connect portal, so its settings are read-only here. ",
    h("a", { href: "#/settings" }, "Take back control")));
}

const PORTAL_WORDS = {
  trying: ["warn", "Trying ports set in the Connect portal. The box keeps them once it can still reach ExaCarib with them, and puts the earlier ports back otherwise."],
  kept: ["good", "Ports last set in the Connect portal"],
  undone: ["bad", "Ports set in the Connect portal were put back"],
  refused: ["bad", "Ports set in the Connect portal were not used"],
  replaced: ["none", "Ports set in the Connect portal were changed again on this box"],
};

function portalNote(p) {
  if (!p || !p.at || !PORTAL_WORDS[p.state]) return null;
  const [kind, words] = PORTAL_WORDS[p.state];
  if (p.state === "kept" && Date.now() - Date.parse(p.at) > 86400000) return null;
  return note(kind === "none" ? "warn" : kind, `${words}${p.by ? ` (${p.by}, ${ago(p.at)})` : ""}${p.detail ? `: ${p.detail}` : ""}.`);
}

async function overview() {
  const o = await api("overview");
  if (!o.setup.network || !o.setup.joined) {
    show(head("Set-up", "Finish setting up this box", "Two short steps: choose which port goes where, then join the box to Connect with the setup code from the ExaCarib portal."),
      ztpNote(o.ztp),
      steps(o.setup.network ? (o.setup.joined ? 3 : 2) : 1, o.setup),
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/setup" }, o.setup.network ? "Join Connect" : "Choose the ports")));
    if (o.setup.network || o.setup.joined) main.append(h("div", { class: "mt24" }), ...overviewCards(o).filter(Boolean));
    return;
  }
  show(head("Overview", o.controller.name || o.hostname, `This box runs ExaCarib Connect at this location. It keeps forwarding on its last settings if it loses touch with ExaCarib.`),
    cloudBanner(o.managed), portalNote(o.portal), pendingBanner(o.pending), ...overviewCards(o));
  timer = setInterval(async () => {
    try { const n = await api("overview"); if ((location.hash.slice(1) || "/") === "/") show(head("Overview", n.controller.name || n.hostname, "This box runs ExaCarib Connect at this location. It keeps forwarding on its last settings if it loses touch with ExaCarib."), cloudBanner(n.managed), portalNote(n.portal), pendingBanner(n.pending), ...overviewCards(n)); } catch { /* next time */ }
  }, 5000);
}

function overviewCards(o) {
  const c = o.controller;
  const ctlPill = c.state === "connected" ? pill("ok", "Connected") : c.state === "unreachable" ? pill("bad", "Cannot reach ExaCarib") : c.state === "starting" ? pill("warn", "Starting") : pill("none", "Not joined yet");
  const wan = o.ports.filter((p) => p.role === "wan");
  const upWan = wan.filter((p) => p.oper === "up" || p.oper === "unknown").length;
  const tunUp = o.tunnels.filter((t) => t.handshake_age_s >= 0 && t.handshake_age_s < 180).length;
  const cards = [
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("div", { class: "eyebrow" }, "ExaCarib Connect"), ctlPill,
        h("p", { class: "small muted mt6" }, c.joined ? `Last heard ${ago(c.last_contact)}. Settings version ${c.applied_version || "–"}.` : "Join the box to Connect to manage it from the portal.")),
      h("div", { class: "card" }, h("div", { class: "eyebrow" }, "Internet links"), h("div", { class: "stat" }, `${upWan} of ${wan.length} up`),
        h("p", { class: "small muted m0" }, wan.map((p) => `${PATHS[p.path] || p.path}: ${p.name}`).join(" · ") || "No internet ports chosen yet.")),
      h("div", { class: "card" }, h("div", { class: "eyebrow" }, "Tunnels to ExaCarib"), h("div", { class: "stat" }, `${tunUp} of ${o.tunnels.length} up`),
        h("p", { class: "small muted m0" }, o.tunnels.length ? "One encrypted tunnel over each internet link." : "Tunnels appear once ExaCarib sends this box its settings.")),
      h("div", { class: "card" }, h("div", { class: "eyebrow" }, "This box"), h("div", { class: "stat mono stat-sm" }, o.hostname),
        h("p", { class: "small muted m0" }, `Up ${duration(o.uptime_s)}. ${o.version}`))),
    c.last_error ? note("bad", `The last settings from ExaCarib did not apply: ${c.last_error}`) : null,
    portsTable(o.ports),
    tunnelsCard(o),
    routingCard(o),
  ];
  if (o.last_change) cards.push(h("p", { class: "small muted" }, `Last change on the box: ${o.last_change.what}, by ${o.last_change.by}, ${ago(o.last_change.at)}.`));
  return cards;
}

function linkState(p) {
  if (!p.up) return pill("none", "Switched off");
  if (p.oper === "down" || p.oper === "lowerlayerdown") return pill("bad", "No cable or no signal");
  return pill("ok", p.speed_mbps ? `Up, ${p.speed_mbps >= 1000 ? p.speed_mbps / 1000 + " Gb/s" : p.speed_mbps + " Mb/s"}` : "Up");
}

function portsTable(ports) {
  return h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Ports"), h("a", { href: "#/ports", class: "small" }, "Change the ports")),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Port", "Used for", "Link", "Addresses", "Received", "Sent"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ports.map((p) => h("tr", {},
        h("td", {}, h("span", { class: "mono" }, p.name), h("span", { class: "sub mono" }, p.mac)),
        h("td", {}, p.role === "wan" ? pathTag(p.path) : p.role === "lan" ? h("span", { class: "tag lan" }, "Office network") : h("span", { class: "muted" }, "Not used")),
        h("td", {}, linkState(p)),
        h("td", { class: "mono small" }, p.addresses.length ? p.addresses.join("\n").split("\n").map((a) => h("div", {}, a)) : h("span", { class: "muted" }, "none")),
        h("td", { class: "mono small" }, bytes(p.rx_bytes)),
        h("td", { class: "mono small" }, bytes(p.tx_bytes))))))));
}

function tunnelsCard(o) {
  const t = o.tunnels;
  const steer = o.steering || [];
  return h("section", { class: "card" }, h("h2", {}, "Tunnels and paths"),
    t.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Tunnel", "Path", "Handshake", "Fast failure check", "Latency", "Jitter", "Loss"].map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", {}, t.map((x) => h("tr", {},
        h("td", { class: "mono" }, x.name, x.underlay ? h("span", { class: "sub" }, "over " + x.underlay) : null),
        h("td", {}, pathTag(x.path)),
        h("td", {}, x.handshake_age_s < 0 ? pill("none", "No handshake yet") : x.handshake_age_s < 180 ? pill("ok", `${x.handshake_age_s} s ago`) : pill("bad", `${Math.round(x.handshake_age_s / 60)} min ago`)),
        h("td", {}, !x.bfd ? h("span", { class: "muted" }, "–") : x.bfd === "up" ? pill("ok", "Up") : pill("bad", x.bfd === "down" ? "Down" : x.bfd)),
        h("td", { class: "mono" }, x.probe && x.probe.received ? `${x.probe.rtt_avg_ms.toFixed(1)} ms` : "–"),
        h("td", { class: "mono" }, x.probe && x.probe.received ? `${x.probe.jitter_ms.toFixed(1)} ms` : "–"),
        h("td", { class: "mono" }, x.probe && x.probe.sent ? `${x.probe.loss_pct.toFixed(1)}%` : "–")))))) :
      h("p", { class: "muted m0" }, o.controller.joined ? "No tunnels yet. ExaCarib sends them once the location's links are set up in the portal." : "Tunnels come once the box has joined Connect."),
    steer.length ? h("div", { class: "mt16" }, h("h3", {}, "Where each kind of traffic goes now"),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Traffic", "Path", ""].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, steer.map((s) => h("tr", {}, h("td", {}, s.class + (s.dst ? " to " + s.dst : "")),
          h("td", {}, s.paused ? pill("warn", "Paused") : s.path ? pathTag(s.path) : h("span", { class: "muted" }, "Follows routing")),
          h("td", { class: "small muted" }, s.failover ? "Moved here by the box: its first path is down" : ""))))))) : null);
}

function routingCard(o) {
  return h("section", { class: "card" }, h("h2", {}, "Routing"),
    o.bgp.length ? h("div", { class: "table-wrap mb16" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Neighbour", "Tunnel", "Session", "Routes received"].map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", {}, o.bgp.map((b) => h("tr", {}, h("td", { class: "mono" }, b.address), h("td", { class: "mono" }, b.tunnel),
        h("td", {}, b.state === "Established" ? pill("ok", "Established") : pill("bad", b.state)), h("td", { class: "mono" }, String(b.prefixes))))))) : null,
    h("details", {}, h("summary", {}, `Routing table (${o.routes.length} routes)`),
      h("div", { class: "table-wrap mt8" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Destination", "Via", "Port", "Source", "Metric"].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, o.routes.map((r) => h("tr", {}, h("td", { class: "mono" }, r.dst), h("td", { class: "mono" }, r.gateway || "direct"),
          h("td", { class: "mono" }, r.dev || ""), h("td", { class: "small muted" }, r.protocol || ""), h("td", { class: "mono" }, r.metric ? String(r.metric) : ""))))))));
}

function steps(now, setup) {
  const s = [["Choose the ports", setup.network], ["Join Connect", setup.joined], ["Done", setup.network && setup.joined]];
  return h("ol", { class: "steps", "aria-label": "Set-up steps" }, s.map(([t, done], i) =>
    h("li", { class: done ? "done" : i + 1 === now ? "now" : "", "aria-current": i + 1 === now ? "step" : null }, h("span", { class: "n" }, done ? "✓" : String(i + 1)), t)));
}

// ---- set-up wizard ----

async function setup() {
  const o = await api("overview");
  if (!o.setup.network) return portsForm(true, o.setup);
  if (!o.setup.joined) return joinForm(true, o.setup);
  show(head("Set-up", "This box is set up", "It has joined Connect. ExaCarib now sends it its tunnels and routing, and it shows as online in the portal under your organisation's locations."),
    steps(3, o.setup), h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "See the box's status")));
}

async function portsView() { return portsForm(false); }

async function portsForm(wizard, setupState) {
  const [d, mg] = await Promise.all([api("network"), api("managed")]);
  const readOnly = mg.managed.mode === "cloud";
  const cur = d.network || { wan: [], lan: null, lan_dhcp: false };
  const roleOf = (name) => {
    const w = (cur.wan || []).find((p) => p.iface === name);
    if (w) return { role: w.path, ...w };
    if (cur.lan && cur.lan.iface === name) return { role: "lan", ...cur.lan };
    return { role: "" };
  };
  const err = h("div");
  const rows = d.ports.map((p) => portRow(p, roleOf(p.name), d.caps, cur));
  const f = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.replaceChildren();
    const body = { wan: [], lan: null, lan_dhcp: false };
    for (const r of rows) {
      const v = r.value();
      if (!v) continue;
      if (v.role === "lan") { body.lan = v.port; body.lan_dhcp = v.dhcp; } else body.wan.push(v.port);
    }
    body.wan.sort((a, b) => Object.keys(PATHS).indexOf(a.path) - Object.keys(PATHS).indexOf(b.path));
    const done = busy(f.querySelector("button[type=submit]"), "Applying…");
    try {
      const res = await api("network", body);
      try {
        await api("network/confirm", {});
      } catch {
        err.replaceChildren(note("warn", `The box's office address changed. Open ${res.console_url || "its new address"} within 2 minutes and sign in to keep the change; otherwise the box goes back to the earlier ports.`));
        done();
        return;
      }
      if (wizard) { location.hash = "#/setup"; route(); }
      else { err.replaceChildren(note("good", "Ports saved and kept.")); done(); }
    } catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  } },
  h("div", { class: "card-head" }, h("h2", {}, "Ports on this box"), h("span", { class: "small muted" }, "Plug in a cable to see which port is which: its link turns to Up.")),
  rows.map((r) => r.el),
  err,
  readOnly ? null : h("div", { class: "actions" }, h("button", { class: "button", type: "submit" }, wizard ? "Save and continue" : "Save the ports"),
    h("span", { class: "small muted" }, "If the console loses touch after a change, the box goes back to the earlier ports after 2 minutes.")));
  if (readOnly) for (const el of f.querySelectorAll("input, select")) el.disabled = true;
  show(wizard ? head("Set-up · step 1 of 2", "Choose which port goes where", "Pick the port each internet link is plugged into, and the port for the office network. Most internet links set themselves up automatically.")
    : head("Ports", "Ports", "Which port carries each internet link, and the office network. ExaCarib routes traffic over these links."),
  wizard ? steps(1, setupState) : null, readOnly ? cloudBanner(mg.managed) : null, pendingBanner(d.pending), f);
}

function portRow(p, cur, caps, net) {
  const role = h("select", { name: "role", "aria-label": `Use of ${p.name}` },
    h("option", { value: "" }, "Not used"),
    Object.entries(PATHS).map(([v, t]) => h("option", { value: v }, t)),
    h("option", { value: "lan" }, "Office network (LAN)"));
  role.value = cur.role || "";
  const fields = h("div", { class: "port-fields" });
  const inputs = {};
  const mk = (name, label, attrs, hint) => { inputs[name] = h("input", { name, ...attrs }); return h("label", {}, label, inputs[name], hint ? h("span", { class: "hint" }, hint) : null); };
  const mode = h("select", { name: "mode", "aria-label": `How ${p.name} gets its address` });
  const dhcpBox = h("input", { type: "checkbox", name: "dhcp" });
  const draw = () => {
    fields.replaceChildren();
    const r = role.value;
    if (!r) return;
    const lan = r === "lan";
    const allowed = lan ? ["static", "keep"] : ["dhcp", "static", "pppoe", "keep"];
    const was = mode.value || cur.mode || (lan ? "static" : "dhcp");
    mode.replaceChildren(...allowed.map((m) => h("option", { value: m, disabled: (m === "pppoe" && !caps.pppoe) || (m === "dhcp" && !caps.dhcp_client) }, MODES[m] + (m === "pppoe" && !caps.pppoe ? " (needs ppp)" : ""))));
    mode.value = allowed.includes(was) ? was : allowed[0];
    fields.append(h("label", {}, "How it gets its address", mode));
    const m = mode.value;
    const v = (k) => (cur.role === r ? cur[k] || "" : "");
    const first = p.addresses.find((a) => a.includes(".")) || "";
    if (m === "static") {
      fields.append(mk("address", "Address and size", { value: v("address") || first, placeholder: lan ? "192.168.10.1/24" : "203.0.113.10/29", required: true, class: "mono" }, "Like 192.168.10.1/24"));
      fields.append(mk("gateway", lan ? "Gateway (usually empty)" : "Gateway", { value: v("gateway"), placeholder: lan ? "" : "203.0.113.9", required: !lan, class: "mono" }));
      fields.append(mk("dns", "DNS servers", { value: (cur.role === r && cur.dns ? cur.dns.join(", ") : ""), placeholder: "1.1.1.1, 8.8.8.8", class: "mono" }, "Optional, separated by commas"));
    }
    if (m === "pppoe") {
      fields.append(mk("pppoe_user", "PPPoE user name", { value: v("pppoe_user"), required: true, autocomplete: "off" }));
      fields.append(mk("pppoe_password", "PPPoE password", { type: "password", value: v("pppoe_password"), required: true, autocomplete: "new-password" }, "From your internet provider"));
    }
    if (lan) {
      dhcpBox.checked = !!(cur.role === "lan" && net.lan_dhcp);
      dhcpBox.disabled = !caps.dhcp_server;
      fields.append(h("label", { class: "check" }, dhcpBox, caps.dhcp_server ? "Hand out addresses to computers on this network (DHCP)" : "Handing out addresses needs dnsmasq on the box"));
    }
  };
  role.addEventListener("change", draw);
  mode.addEventListener("change", () => { const keep = mode.value; draw(); mode.value = keep; draw(); });
  draw();
  const el = h("div", { class: "port" },
    h("div", { class: "port-head" }, h("div", {}, h("strong", { class: "mono" }, p.name), " ", h("span", { class: "small muted mono" }, p.mac)),
      h("div", { class: "small" }, linkState(p), " ", h("span", { class: "mono muted" }, p.addresses.join(", ")))),
    h("label", {}, "Used for", role), fields);
  return {
    el,
    value: () => {
      const r = role.value;
      if (!r) return null;
      const port = { iface: p.name, mode: mode.value };
      if (r !== "lan") port.path = r;
      for (const k of ["address", "gateway", "pppoe_user", "pppoe_password"]) if (inputs[k] && fields.contains(inputs[k]) && inputs[k].value.trim()) port[k] = inputs[k].value.trim();
      if (inputs.dns && fields.contains(inputs.dns) && inputs.dns.value.trim()) port.dns = inputs.dns.value.split(/[\s,]+/).filter(Boolean);
      return { role: r, port, dhcp: r === "lan" && dhcpBox.checked };
    },
  };
}

// ---- join ----

async function connectView() {
  const o = await api("overview");
  if (!o.controller.joined) return joinForm(false, o.setup);
  const c = o.controller;
  show(head("Connect", "Joined to ExaCarib Connect", "ExaCarib manages this box's tunnels, routing and traffic rules. Ports and this console stay under local control too."),
    h("section", { class: "card" }, h("table", {}, h("tbody", {},
      h("tr", {}, h("th", { scope: "row" }, "Box name"), h("td", { class: "mono" }, c.name)),
      h("tr", {}, h("th", { scope: "row" }, "ExaCarib address"), h("td", { class: "mono" }, c.url)),
      h("tr", {}, h("th", { scope: "row" }, "Connection"), h("td", {}, c.state === "connected" ? pill("ok", "Connected") : c.state === "starting" ? pill("warn", "Starting") : pill("bad", "Cannot reach ExaCarib"), h("span", { class: "sub" }, `Last heard ${ago(c.last_contact)}`))),
      h("tr", {}, h("th", { scope: "row" }, "Settings version"), h("td", { class: "mono" }, String(c.applied_version || "–")))))),
    h("p", { class: "small muted" }, "To move this box to another location, revoke it in the portal and set it up again."));
}

function joinForm(wizard, setupState) {
  const err = h("div");
  const manual = h("details", {}, h("summary", {}, "Enter the details by hand instead"),
    h("div", { class: "fields mt12" },
      h("label", {}, "ExaCarib address", h("input", { name: "controller", value: "https://connect.exacarib.com:8443", class: "mono" })),
      h("label", {}, "Enrolment token", h("input", { name: "token", autocomplete: "off", class: "mono" })),
      h("label", {}, "CA fingerprint", h("input", { name: "fingerprint", autocomplete: "off", class: "mono" })),
      h("label", {}, "Box name", h("input", { name: "name", autocomplete: "off", class: "mono", placeholder: "kingston-hq" }), h("span", { class: "hint" }, "The location's Connect name, as shown in the portal"))));
  const f = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.replaceChildren();
    const fd = new FormData(f);
    const code = (fd.get("code") || "").trim();
    const body = code ? { code } : { controller: fd.get("controller"), token: fd.get("token"), fingerprint: fd.get("fingerprint"), name: fd.get("name") };
    const done = busy(f.querySelector("button"), "Joining…");
    try {
      await api("join", body);
      state = await api("state");
      if (wizard) { location.hash = "#/setup"; route(); } else route();
    } catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  } },
  h("h2", {}, "Box setup code"),
  h("p", { class: "muted" }, "In the ExaCarib portal, open Organisation › Locations, choose Connect this location (or Get a new install code), and copy the box setup code. It works once."),
  h("label", {}, h("span", { class: "sr-only" }, "Box setup code"), h("textarea", { name: "code", placeholder: "EXA1.…", autocomplete: "off", spellcheck: "false", "aria-label": "Box setup code" })),
  h("div", { class: "mt12" }, manual),
  err,
  h("div", { class: "actions" }, h("button", { class: "button" }, "Join Connect"),
    h("span", { class: "small muted" }, "The box only connects out, on port 8443. It makes its own keys and sends only the public one.")));
  show(wizard ? head("Set-up · step 2 of 2", "Join this box to Connect", "Paste the box setup code from the portal. The box then gets its tunnels and routing from ExaCarib.")
    : head("Connect", "Join this box to Connect", "Paste the box setup code from the portal."),
  wizard ? steps(2, setupState) : null, f);
}

// ---- diagnostics ----

async function diagView() {
  const d = await api("network");
  const out = h("pre", { class: "out", hidden: true, "aria-live": "polite" });
  const err = h("div");
  const tool = h("select", { name: "tool" }, h("option", { value: "ping" }, "Ping"), h("option", { value: "traceroute" }, "Traceroute"),
    h("option", { value: "dns" }, "Look up a name (DNS)"), h("option", { value: "controller" }, "Reach ExaCarib"));
  const target = h("input", { name: "target", value: "1.1.1.1", class: "mono", required: true, "aria-label": "Address or name" });
  const iface = h("select", { name: "iface" }, h("option", { value: "" }, "Any (normal routing)"), d.ports.map((p) => h("option", { value: p.name }, `${p.name}${p.role === "wan" ? " · " + PATHS[p.path] : p.role === "lan" ? " · office" : ""}`)));
  tool.addEventListener("change", () => {
    target.required = tool.value !== "controller";
    if (tool.value === "controller") target.value = "";
    else if (!target.value) target.value = "1.1.1.1";
    target.placeholder = tool.value === "controller" ? "Leave empty for the address this box joined" : "1.1.1.1 or exacarib.com";
  });
  const f = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.replaceChildren();
    out.hidden = false;
    out.textContent = "Running…";
    const done = busy(f.querySelector("button"), "Running…");
    try { const r = await api("diag", { tool: tool.value, target: target.value, iface: iface.value }); out.textContent = r.output || "(no output)"; }
    catch (x) { out.hidden = true; err.replaceChildren(note("bad", x.message)); }
    done();
  } },
  h("div", { class: "port-fields" }, h("label", {}, "Test", tool), h("label", {}, "Address or name", target), h("label", {}, "From port", iface)),
  err,
  h("div", { class: "actions" }, h("button", { class: "button" }, "Run")), out);
  show(head("Diagnostics", "Diagnostics", "Check the box can reach the internet and ExaCarib over each link."), f);
}

// ---- monitoring (ADR 0048) ----

async function monitoringView() {
  const m = await api("monitoring");
  const err = h("div");
  const sn = m.snmp;
  const field = (label, input, hint) => h("label", {}, label, input, hint ? h("span", { class: "hint" }, hint) : null);
  const secret = (name, set, label) => h("input", { name, type: "password", autocomplete: "new-password", minlength: 8, maxlength: 64,
    placeholder: set ? "Unchanged" : "", "aria-label": label });
  const v2c = h("input", { type: "checkbox", name: "v2c", checked: sn.v2c });
  const v3 = h("input", { type: "checkbox", name: "v3", checked: sn.v3 });
  const traps = h("div", { class: "fields" });
  const trapRow = (t) => {
    const row = h("div", { class: "port-fields trap" },
      field("Monitoring system", h("input", { name: "host", value: t.host || "", class: "mono", required: true, placeholder: "10.20.0.5" })),
      field("Port", h("input", { name: "port", type: "number", min: 1, max: 65535, value: t.port || 162, class: "mono" })),
      field("Version", h("select", { name: "version" }, ["v2c", "v3"].map((v) => h("option", { value: v, selected: t.version === v }, v === "v3" ? "SNMPv3 (the user above)" : "v2c")))),
      field("Community (v2c)", secret("community", t.community_set, "Trap community"), t.community_set ? "Leave empty to keep it" : "Empty: the polling community"),
      h("div", { class: "actions m0" }, h("button", { type: "button", class: "button secondary small", onclick: () => row.remove() }, "Remove")));
    return row;
  };
  for (const t of m.traps) traps.append(trapRow(t));
  const sl = m.syslog || {};
  const f = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    err.replaceChildren();
    const fd = new FormData(f);
    const body = {
      snmp: { v2c: v2c.checked, community: fd.get("community") || "", v3: v3.checked, v3_user: fd.get("v3_user") || "", v3_auth: fd.get("v3_auth") || "",
        v3_priv: fd.get("v3_priv") || "", allowed: String(fd.get("allowed") || "").split(/[\s,]+/).filter(Boolean),
        location: fd.get("location") || "", contact: fd.get("contact") || "" },
      traps: [...traps.querySelectorAll(".trap")].map((r) => ({ host: r.querySelector("[name=host]").value.trim(), port: Number(r.querySelector("[name=port]").value) || 162,
        version: r.querySelector("[name=version]").value, community: r.querySelector("[name=community]").value })),
      syslog: fd.get("syslog_host") ? { host: String(fd.get("syslog_host")).trim(), port: Number(fd.get("syslog_port")) || 514, proto: fd.get("syslog_proto") } : null,
    };
    const done = busy(f.querySelector("button[type=submit]"), "Saving…");
    try { await api("monitoring", body); await monitoringView(); main.prepend(note("good", "Monitoring saved. The box's SNMP agent and syslog now use these settings.")); }
    catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  } },
  h("h2", {}, "SNMP polling"),
  h("p", { class: "muted small" }, "Your monitoring system reads the box's interfaces, traffic, CPU, memory and disk (standard MIB-II and HOST-RESOURCES). It answers on the office side and over Connect, never on an internet port."),
  h("div", { class: "fields" },
    h("label", { class: "check" }, v2c, "SNMP v2c (read-only community)"),
    field("Community", secret("community", sn.community_set, "Community"), sn.community_set ? "Leave empty to keep it" : "8 to 64 characters"),
    h("label", { class: "check mt8" }, v3, "SNMPv3 (SHA authentication, AES privacy)"),
    h("div", { class: "port-fields" },
      field("User", h("input", { name: "v3_user", value: sn.v3_user || "", class: "mono", autocomplete: "off" })),
      field("Authentication password", secret("v3_auth", sn.v3_passwords_set, "Authentication password"), sn.v3_passwords_set ? "Leave empty to keep it" : "8 to 64 characters"),
      field("Privacy password", secret("v3_priv", sn.v3_passwords_set, "Privacy password"), sn.v3_passwords_set ? "Leave empty to keep it" : "Different from the authentication password")),
    field("Who may poll", h("textarea", { name: "allowed", rows: 2, placeholder: "10.20.0.0/24" }, sn.allowed.join("\n")), "Address ranges, one per line. Empty: any private address."),
    h("div", { class: "port-fields" },
      field("Location", h("input", { name: "location", value: sn.location || "", maxlength: 120 })),
      field("Contact", h("input", { name: "contact", value: sn.contact || "", maxlength: 120 })))),
  h("h2", { class: "mt24" }, "Traps"),
  h("p", { class: "muted small" }, "Sent when a port goes up or down, a link or tunnel fails (BFD), traffic moves to another link, the box loses ExaCarib, someone polls with a wrong community, or someone changes the box here."),
  traps,
  h("div", { class: "actions" }, h("button", { type: "button", class: "button secondary small", onclick: () => { if (traps.children.length < 4) traps.append(trapRow({ version: "v2c" })); } }, "Add a trap destination")),
  h("table", { class: "mt12" }, h("tbody", {},
    h("tr", {}, h("th", { scope: "row" }, "Engine ID (for v3 traps)"), h("td", { class: "mono small break" }, m.engine_id)),
    h("tr", {}, h("th", { scope: "row" }, "ExaCarib trap OIDs"), h("td", { class: "mono small break" }, `${m.trap_oid}.0.n (kind, detail, box in ${m.trap_oid}.1.1 to .1.3)`)))),
  h("h2", { class: "mt24" }, "Syslog"),
  h("p", { class: "muted small" }, "The box's log (agent, routing, firewall, sign-ins) goes to your collector in RFC 5424 format. Empty: off."),
  h("div", { class: "port-fields" },
    field("Collector", h("input", { name: "syslog_host", value: sl.host || "", class: "mono", placeholder: "10.20.0.6" })),
    field("Port", h("input", { name: "syslog_port", type: "number", min: 1, max: 65535, value: sl.port || 514, class: "mono" })),
    field("Protocol", h("select", { name: "syslog_proto" }, ["udp", "tcp"].map((p) => h("option", { value: p, selected: (sl.proto || "udp") === p }, p.toUpperCase()))))),
  err,
  h("div", { class: "actions" }, h("button", { type: "submit", class: "button" }, "Save monitoring"),
    h("button", { type: "button", class: "button secondary", onclick: async (e) => {
      const done = busy(e.target, "Sending…");
      try { const r = await api("monitoring/test", {}); err.replaceChildren(note("good", r.detail)); } catch (x) { err.replaceChildren(note("bad", x.message)); }
      done();
    } }, "Send a test trap and log message")));

  const keyOut = h("div");
  const kf = h("form", { class: "port-fields mt12", onsubmit: async (e) => {
    e.preventDefault();
    const name = kf.querySelector("[name=name]").value;
    try {
      const k = await api("apikeys", { name });
      keyOut.replaceChildren(note("good", h("span", {}, "Copy this key now; the box shows it only once: ", h("code", { class: "mono break" }, k.key))));
      kf.reset();
      keyList.replaceChildren(...(await api("monitoring")).api_keys.map(keyRow));
    } catch (x) { keyOut.replaceChildren(note("bad", x.message)); }
  } }, field("Name", h("input", { name: "name", required: true, maxlength: 60, placeholder: "Zabbix" })), h("div", { class: "actions m0" }, h("button", { class: "button secondary" }, "Create a read-only key")));
  const keyRow = (k) => h("tr", {}, h("td", {}, k.name), h("td", { class: "small" }, ago(k.created)), h("td", { class: "small" }, ago(k.last_used)),
    h("td", {}, h("button", { class: "button secondary small", onclick: async () => {
      try { await api("apikeys/revoke", { id: k.id }); keyList.replaceChildren(...(await api("monitoring")).api_keys.map(keyRow)); } catch (x) { keyOut.replaceChildren(note("bad", x.message)); }
    } }, "Remove")));
  const keyList = h("tbody", {}, m.api_keys.map(keyRow));
  const keys = h("section", { class: "card" }, h("h2", {}, "Read-only API"),
    h("p", { class: "muted small" }, "Monitoring tools can read this box's status as JSON from the office side: GET /api/overview, /api/network and /api/monitoring with the header Authorization: Bearer <key>. Keys cannot change anything."),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Key", "Created", "Last used", ""].map((x) => h("th", { scope: "col" }, x)))), keyList)),
    kf, keyOut);

  show(head("Monitoring", "Monitoring", "Let your own monitoring system watch this box: SNMP, traps, syslog and a read-only API."),
    !m.caps.snmp || !m.caps.syslog ? note("warn", "This box lacks " + [!m.caps.snmp ? "an SNMP agent (snmpd)" : null, !m.caps.syslog ? "rsyslog" : null].filter(Boolean).join(" and ") + ". The ExaCarib box image has both.") : null,
    f, keys);
}

// ---- settings ----

function softwareCard(u) {
  const msg = h("div");
  const card = h("section", { class: "card" }, h("h2", {}, "Software"));
  if (!u.ab) {
    card.append(h("p", { class: "muted" }, `This box runs ${u.version}, installed from the package. Update it with apt; Debian's security updates install on their own.`));
    return card;
  }
  const last = u.last;
  const res = { ok: pill("ok", "Kept"), rolled_back: pill("warn", "Went back"), trying: pill("none", "Trying") };
  const wd = u.watchdog || {};
  card.append(
    u.trial ? note("warn", "This software is on trial: the box keeps it once it is healthy, or goes back on its own.") : null,
    h("table", {}, h("tbody", {},
      h("tr", {}, h("th", { scope: "row" }, "Running"), h("td", {}, h("span", { class: "mono" }, u.version), h("span", { class: "sub" }, `Slot ${u.slot}`))),
      h("tr", {}, h("th", { scope: "row" }, "Other slot"), h("td", {}, u.other && u.other.version ? h("span", { class: "mono" }, u.other.version) : h("span", { class: "muted" }, "Empty"), h("span", { class: "sub" }, `Slot ${u.other ? u.other.slot : "–"}: an update goes here, and the box can go back to it`))),
      h("tr", {}, h("th", { scope: "row" }, "Watchdog"), h("td", {}, wd.state === "active" ? pill("ok", "On") : pill("warn", wd.device ? "Not running" : "No watchdog found"), wd.device ? h("span", { class: "sub" }, `${wd.device}: restarts the box if it hangs`) : null)),
      last ? h("tr", {}, h("th", { scope: "row" }, "Last update"), h("td", {}, res[last.result] || last.result, " ", h("span", { class: "mono" }, last.version || ""), h("span", { class: "sub" }, `${last.why} · ${ago(last.at)}`))) : null)),
    msg);
  const restart = (text) => {
    show(head("Settings", "Restarting", text), note("warn", "The box is restarting. This page reconnects by itself; it can take a few minutes. If the new software is not healthy, the box goes back to the old one on its own."));
    const wait = setInterval(async () => { try { const r = await fetch("/api/state", { cache: "no-store" }); if (r.ok) { clearInterval(wait); location.hash = "#/settings"; location.reload(); } } catch { /* still down */ } }, 5000);
  };
  const poll = () => {
    const t = setInterval(async () => {
      let x;
      try { x = await api("update"); } catch { clearInterval(t); restart("Installing the new software."); return; }
      if (x.running) { msg.replaceChildren(note("warn", x.progress || "Installing…")); return; }
      clearInterval(t);
      if (x.error) msg.replaceChildren(note("bad", x.error)); else restart("Installing the new software.");
    }, 3000);
  };
  const install = (v) => h("button", { class: "button", onclick: async (e) => {
    if (!confirm(`Install ${v}? The box restarts (about a minute without internet) and keeps the new software only if it comes up healthy.`)) return;
    const done = busy(e.target, "Starting…");
    try { await api("update", { version: v }); msg.replaceChildren(note("warn", "Installing…")); poll(); } catch (x) { msg.replaceChildren(note("bad", x.message)); done(); }
  } }, `Install ${v}`);
  const acts = h("div", { class: "actions" },
    h("button", { class: "button secondary", onclick: async (e) => {
      const done = busy(e.target, "Checking…");
      try {
        const l = await api("update/latest");
        msg.replaceChildren(l.version === u.version ? note("good", `This box has the newest software, ${l.version}.`) : h("div", { class: "actions" }, note("good", `Version ${l.version} is available.`), install(l.version)));
      } catch (x) { msg.replaceChildren(note("bad", x.message)); }
      done();
    } }, "Check for new software"),
    u.other && u.other.version && !u.trial ? h("button", { class: "button secondary", onclick: async (e) => {
      if (!confirm(`Go back to ${u.other.version}? The box restarts.`)) return;
      busy(e.target, "Restarting…");
      try { await api("update/rollback", {}); restart(`Going back to ${u.other.version}.`); } catch (x) { msg.replaceChildren(note("bad", x.message)); }
    } }, `Go back to ${u.other.version}`) : null);
  card.append(acts);
  if (u.running) { msg.replaceChildren(note("warn", u.progress || "Installing…")); poll(); }
  return card;
}

async function settingsView() {
  const [o, u] = await Promise.all([api("overview"), api("update").catch(() => null)]);
  const err = h("div");
  const f = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    if (fd.get("new") !== fd.get("again")) { err.replaceChildren(note("bad", "The two new passwords are not the same.")); return; }
    const done = busy(f.querySelector("button"), "Saving…");
    try { await api("password", { current: fd.get("current"), new: fd.get("new") }); f.reset(); err.replaceChildren(note("good", "Password changed. Other sessions were signed out.")); }
    catch (x) { err.replaceChildren(note("bad", x.message)); }
    done();
  } },
  h("h2", {}, "Admin password"),
  h("div", { class: "fields w420" },
    h("label", {}, "Current password", h("input", { name: "current", type: "password", required: true, autocomplete: "current-password" })),
    h("label", {}, "New password", h("input", { name: "new", type: "password", required: true, minlength: 10, autocomplete: "new-password" })),
    h("label", {}, "New password again", h("input", { name: "again", type: "password", required: true, autocomplete: "new-password" }))),
  err, h("div", { class: "actions" }, h("button", { class: "button" }, "Change the password")));
  show(head("Settings", "Settings", "Where this box is managed from, this console's sign-in, the box's software, and details to give ExaCarib support."), managedCard(o), u ? softwareCard(u) : null, f,
    h("section", { class: "card" }, h("h2", {}, "About this box"), h("table", {}, h("tbody", {},
      h("tr", {}, h("th", { scope: "row" }, "Host name"), h("td", { class: "mono" }, o.hostname)),
      h("tr", {}, h("th", { scope: "row" }, "Software"), h("td", { class: "mono" }, o.version)),
      o.ztp && o.ztp.serial ? h("tr", {}, h("th", { scope: "row" }, "Serial number"), h("td", { class: "mono" }, o.ztp.serial,
        o.ztp.key_id ? h("span", { class: "sub" }, `Zero-touch key ${o.ztp.key_id}: ExaCarib's device list shows the same.`) : null)) : null,
      h("tr", {}, h("th", { scope: "row" }, "Console address"), h("td", { class: "mono" }, o.console_url || location.origin)),
      h("tr", {}, h("th", { scope: "row" }, "Console certificate"), h("td", { class: "mono small break" }, o.cert_sha256 || "–",
        h("span", { class: "sub" }, "SHA-256. Your browser warns about this certificate once because the box made it itself; check it matches what exa-agent console-code prints on the box."))),
      o.last_change ? h("tr", {}, h("th", { scope: "row" }, "Last change here"), h("td", {}, o.last_change.what, h("span", { class: "sub" }, `${o.last_change.by}, ${ago(o.last_change.at)}`))) : null))));
}

function managedCard(o) {
  const m = o.managed || { mode: "both" };
  const err = h("div");
  const joined = o.setup && o.setup.joined;
  const since = m.at && !m.at.startsWith("0001") ? h("p", { class: "small muted" }, `Set by ${m.by || "someone"}, ${ago(m.at)}.`) : null;
  if (m.mode === "cloud") {
    const f = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const done = busy(f.querySelector("button"), "Taking back control…");
      try { await api("managed", { mode: "site", password: new FormData(f).get("password") }); route(); }
      catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
    } },
    h("p", {}, "In an emergency, for example when the portal cannot be reached or a change from it has cut off the office, take back control here. The box keeps its settings, switches to On site, and tells the portal."),
    h("div", { class: "fields w420" }, h("label", {}, "Admin password", h("input", { name: "password", type: "password", required: true, autocomplete: "current-password" }))),
    err, h("div", { class: "actions" }, h("button", { class: "button secondary" }, "Take back control")));
    return h("section", { class: "card", id: "managed" }, h("h2", {}, "Managed from: Cloud"), h("p", {}, MANAGED_LINE.cloud), since, f);
  }
  const pick = (mode) => async (e) => {
    const done = busy(e.target, "Saving…");
    try { await api("managed", { mode }); route(); }
    catch (x) { err.replaceChildren(note("bad", x.message)); done(); }
  };
  return h("section", { class: "card", id: "managed" }, h("h2", {}, "Managed from"),
    h("p", {}, MANAGED_LINE[m.mode]), since,
    h("div", { class: "segmented", role: "group", "aria-label": "Managed from" },
      Object.entries(MANAGED).map(([v, t]) => h("button", { type: "button", class: "button small" + (v === m.mode ? "" : " secondary"), "aria-pressed": v === m.mode ? "true" : "false",
        disabled: v === m.mode || (v === "cloud" && !joined), onclick: pick(v) }, t))),
    joined ? null : h("p", { class: "small muted" }, "Join the box to Connect before handing it to the cloud."),
    h("p", { class: "small muted" }, "Switching keeps the box's current settings."), err);
}

start().catch((e) => show(note("bad", e.message)));
