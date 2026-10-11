// ExaCarib box console (ADR 0046). Plain JavaScript, no build step: the agent
// embeds this file. Every value from the box goes into the page as text.
"use strict";

const main = document.getElementById("main");
const PATHS = { "carrier-a": "Internet link 1", "carrier-b": "Internet link 2", sat: "Satellite" };
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
  const views = { "/": overview, "/setup": setup, "/ports": portsView, "/connect": connectView, "/diagnostics": diagView, "/settings": settingsView };
  (views[r] || overview)().catch((e) => show(note("bad", e.message)));
}
window.addEventListener("hashchange", route);

function pendingBanner(p, after) {
  if (!p) return null;
  const left = Math.max(0, Math.round((new Date(p.revert_at).getTime() - Date.now()) / 1000));
  return note("warn", h("span", {}, `A port change is waiting to be kept. If nobody keeps it, the box goes back to the earlier ports in ${left} s. `,
    h("button", { class: "button small", onclick: async () => { await api("network/confirm", {}); (after || route)(); } }, "Keep the change")));
}

async function overview() {
  const o = await api("overview");
  if (!o.setup.network || !o.setup.joined) {
    show(head("Set-up", "Finish setting up this box", "Two short steps: choose which port goes where, then join the box to Connect with the setup code from the ExaCarib portal."),
      steps(o.setup.network ? (o.setup.joined ? 3 : 2) : 1, o.setup),
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/setup" }, o.setup.network ? "Join Connect" : "Choose the ports")));
    if (o.setup.network || o.setup.joined) main.append(h("div", { class: "mt24" }), ...overviewCards(o).filter(Boolean));
    return;
  }
  show(head("Overview", o.controller.name || o.hostname, `This box runs ExaCarib Connect at this location. It keeps forwarding on its last settings if it loses touch with ExaCarib.`),
    pendingBanner(o.pending), ...overviewCards(o));
  timer = setInterval(async () => {
    try { const n = await api("overview"); if ((location.hash.slice(1) || "/") === "/") show(head("Overview", n.controller.name || n.hostname, "This box runs ExaCarib Connect at this location. It keeps forwarding on its last settings if it loses touch with ExaCarib."), pendingBanner(n.pending), ...overviewCards(n)); } catch { /* next time */ }
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
  const d = await api("network");
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
  h("div", { class: "actions" }, h("button", { class: "button", type: "submit" }, wizard ? "Save and continue" : "Save the ports"),
    h("span", { class: "small muted" }, "If the console loses touch after a change, the box goes back to the earlier ports after 2 minutes.")));
  show(wizard ? head("Set-up · step 1 of 2", "Choose which port goes where", "Pick the port each internet link is plugged into, and the port for the office network. Most internet links set themselves up automatically.")
    : head("Ports", "Ports", "Which port carries each internet link, and the office network. ExaCarib routes traffic over these links."),
  wizard ? steps(1, setupState) : null, pendingBanner(d.pending), f);
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

// ---- settings ----

async function settingsView() {
  const o = await api("overview");
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
  show(head("Settings", "Settings", "This console's sign-in, and details to give ExaCarib support."), f,
    h("section", { class: "card" }, h("h2", {}, "About this box"), h("table", {}, h("tbody", {},
      h("tr", {}, h("th", { scope: "row" }, "Host name"), h("td", { class: "mono" }, o.hostname)),
      h("tr", {}, h("th", { scope: "row" }, "Software"), h("td", { class: "mono" }, o.version)),
      h("tr", {}, h("th", { scope: "row" }, "Console address"), h("td", { class: "mono" }, o.console_url || location.origin)),
      h("tr", {}, h("th", { scope: "row" }, "Console certificate"), h("td", { class: "mono small break" }, o.cert_sha256 || "–",
        h("span", { class: "sub" }, "SHA-256. Your browser warns about this certificate once because the box made it itself; check it matches what exa-agent console-code prints on the box."))),
      o.last_change ? h("tr", {}, h("th", { scope: "row" }, "Last change here"), h("td", {}, o.last_change.what, h("span", { class: "sub" }, `${o.last_change.by}, ${ago(o.last_change.at)}`))) : null))));
}

start().catch((e) => show(note("bad", e.message)));
