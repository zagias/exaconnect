"use strict";
/*
 * ExaCarib CommAI website chat (ADR 0018).
 *
 * Install with one tag:
 *   <script src="https://HOST/api/v1/commai/widget/v1.js" data-key="wk_..." async></script>
 *
 * Signed-in customers: the business's own site signs a short token with its
 * widget secret (HS256 JWT: sub, email, name, exp) and passes it with
 * data-user-token="..." or ExaCaribChat.identify(token). Only then does the
 * widget show the customer's past conversations.
 *
 * Built from widget/src/widget.ts with portal/node_modules/.bin/tsc -p widget (no
 * dependencies). Do not edit the built file by hand.
 */
(function () {
    const w = window;
    if (w.ExaCaribChat)
        return; // loaded twice
    const script = document.currentScript;
    const apiOrigin = script && script.src ? new URL(script.src).origin : location.origin;
    const COPY = {
        open: "Open chat",
        close: "Close chat",
        send: "Send",
        placeholder: "Write a message",
        attach: "Attach a file",
        online: "We're here",
        aiOnline: "Our assistant answers first; our team is close by",
        offline: "We're away just now",
        notSent: "Not sent. Select to try again.",
        sending: "Sending…",
        contactTitle: "So we can reply if you leave",
        contactSkip: "Not now",
        save: "Save",
        name: "Your name",
        email: "Your email",
        phone: "Your phone number, with country code",
        when: "A good time to call (optional)",
        message: "Your message",
        offlineSend: "Send message",
        offlineDone: "Thank you. We'll reply by email.",
        callback: "Ask for a call back",
        callbackSend: "Request a call",
        callbackDone: "Thank you. We'll call you back.",
        back: "Back to chat",
        history: "Your conversations",
        you: "You",
        team: "Team",
        assistant: "Assistant",
        poweredBy: "ExaCarib CommAI",
        tooBig: "That file is too large.",
        wrongType: "You can send images, PDFs and plain text.",
    };
    const ICON_CHAT = '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path fill="currentColor" d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>';
    const ICON_CLOSE = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg>';
    const ICON_CLIP = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/></svg>';
    const CSS = `
:host { all: initial; }
* { box-sizing: border-box; }
.root { --exa: #155EEF; --ink: #10213D; --muted: #52647A; --line: #DFE6EE; --page: #F3F6FB;
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; color: var(--ink); }
.launcher { position: fixed; bottom: 20px; width: 56px; height: 56px; border-radius: 50%; border: 0;
  background: var(--exa); color: #fff; cursor: pointer; box-shadow: 0 6px 20px rgba(7,24,46,.25);
  display: grid; place-items: center; z-index: 2147483000; }
.launcher:focus-visible, button:focus-visible, textarea:focus-visible, input:focus-visible { outline: 3px solid #8DCFFF; outline-offset: 2px; }
.badge { position: absolute; top: -2px; right: -2px; min-width: 20px; height: 20px; border-radius: 10px;
  background: #B42318; color: #fff; font-size: 12px; display: grid; place-items: center; padding: 0 5px; }
.right { right: 20px; } .left { left: 20px; }
.panel { position: fixed; bottom: 88px; width: 370px; max-width: calc(100vw - 32px); height: 560px;
  max-height: calc(100vh - 120px); background: #fff; border: 1px solid var(--line); border-radius: 8px;
  box-shadow: 0 12px 40px rgba(7,24,46,.22); display: flex; flex-direction: column; overflow: hidden;
  z-index: 2147483000; }
.panel.inline { position: relative; bottom: auto; right: auto; left: auto; width: 100%; max-width: 380px;
  height: 480px; box-shadow: none; z-index: auto; }
@media (max-width: 480px) { .panel:not(.inline) { inset: 0; width: 100vw; max-width: 100vw; height: 100%;
  max-height: 100%; border-radius: 0; bottom: 0; } }
.head { background: var(--exa); color: #fff; padding: 14px 16px; display: flex; gap: 12px; align-items: flex-start; }
.head h2 { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0; }
.head p { margin: 2px 0 0; font-size: 13px; opacity: .92; }
.head .grow { flex: 1; min-width: 0; }
.icon { background: transparent; border: 0; color: inherit; cursor: pointer; padding: 4px; border-radius: 5px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
.on { background: #8DF0B9; } .off { background: #FFD08A; }
.log { flex: 1; overflow-y: auto; padding: 14px; background: var(--page); display: flex; flex-direction: column; gap: 8px; }
.msg { max-width: 82%; padding: 8px 12px; border-radius: 8px; white-space: pre-wrap; word-wrap: break-word; }
.msg.you { align-self: flex-end; background: var(--exa); color: #fff; border-bottom-right-radius: 2px; }
.msg.them { align-self: flex-start; background: #fff; border: 1px solid var(--line); border-bottom-left-radius: 2px; }
.who { display: block; font-size: 12px; color: var(--muted); margin-bottom: 2px; }
.msg.you .who { color: #E9F0FF; }
.meta { font-size: 12px; color: var(--muted); align-self: flex-end; }
.meta.failed { color: #B42318; cursor: pointer; text-decoration: underline; background: none; border: 0; font: inherit; font-size: 12px; }
.att { display: block; margin-top: 6px; color: inherit; }
.att img { max-width: 100%; border-radius: 5px; display: block; }
.compose { border-top: 1px solid var(--line); padding: 10px; display: flex; gap: 8px; align-items: flex-end; background: #fff; }
textarea { flex: 1; resize: none; border: 1px solid var(--line); border-radius: 5px; padding: 8px 10px;
  font: inherit; color: var(--ink); max-height: 120px; min-height: 40px; }
.btn { background: var(--exa); color: #fff; border: 0; border-radius: 5px; padding: 9px 14px; font: inherit;
  font-weight: 600; cursor: pointer; }
.btn.secondary { background: #fff; color: var(--exa); border: 1px solid var(--line); }
.btn:disabled { opacity: .6; cursor: default; }
.form { padding: 14px; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; flex: 1; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); }
.form input, .form textarea { border: 1px solid var(--line); border-radius: 5px; padding: 8px 10px; font: inherit; color: var(--ink); }
.card { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.card h3 { margin: 0; font-size: 14px; font-weight: 650; }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.note { font-size: 13px; color: var(--muted); margin: 0; }
.err { font-size: 13px; color: #B42318; margin: 0; }
.foot { text-align: center; font-size: 11px; color: var(--muted); padding: 4px 0 8px; background: #fff; }
.link { background: none; border: 0; color: var(--exa); cursor: pointer; font: inherit; font-size: 13px; padding: 0; text-decoration: underline; }
.conv { text-align: left; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
`;
    function el(tag, attrs = {}, ...kids) {
        const e = document.createElement(tag);
        for (const k of Object.keys(attrs)) {
            if (k === "class")
                e.className = attrs[k];
            else if (k === "html")
                e.innerHTML = attrs[k]; // only ever our own static icons
            else
                e.setAttribute(k, attrs[k]);
        }
        for (const kid of kids)
            e.append(kid);
        return e;
    }
    function randomId() {
        const a = new Uint8Array(12);
        crypto.getRandomValues(a);
        return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
    }
    function load(k) {
        try {
            return JSON.parse(localStorage.getItem(k) || "{}");
        }
        catch (_a) {
            return {};
        }
    }
    function save(k, v) {
        try {
            localStorage.setItem(k, JSON.stringify(v));
        }
        catch (_a) {
            /* private mode: the chat still works for this page */
        }
    }
    const DEFAULTS = {
        title: "Chat with us",
        greeting: "Hello. How can we help today?",
        colour: "#155EEF",
        position: "right",
        mode: "human_first",
        ai: false,
        online: true,
        hours: "",
        offline_message: "We're away just now. Leave a message and we'll reply by email.",
        callbacks: true,
        attachments: { enabled: true, max_bytes: 2 * 1024 * 1024, types: ["application/pdf", "image/gif", "image/jpeg", "image/png", "image/webp", "text/plain"] },
        ask_contact: "after_first",
    };
    class Chat {
        constructor(key, userToken, previewMode = false) {
            this.userToken = userToken;
            this.previewMode = previewMode;
            this.cfg = DEFAULTS;
            this.host = el("div");
            this.messages = [];
            this.view = "chat";
            this.isOpen = false;
            this.unread = 0;
            this.session = null;
            this.sessionReady = null;
            this.conversations = [];
            this.doneText = "";
            this.showContact = false;
            this.base = `${apiOrigin}/api/v1/commai/widget/${encodeURIComponent(key)}`;
            this.storeKey = `exacarib-chat:${key}`;
            this.stored = previewMode ? {} : load(this.storeKey);
        }
        // ---- network ------------------------------------------------------------------------
        async call(method, path, body, auth = true) {
            const headers = {};
            if (body !== undefined)
                headers["Content-Type"] = "application/json";
            if (auth && this.session)
                headers["X-Widget-Session"] = this.session.token;
            const r = await fetch(this.base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), mode: "cors", credentials: "omit" });
            let data = {};
            try {
                data = await r.json();
            }
            catch (_a) {
                /* empty */
            }
            if (data && typeof data === "object" && data.session)
                this.setSession(data.session);
            if (!r.ok) {
                if (r.status === 401 && auth && path !== "/session") {
                    this.session = null;
                    this.sessionReady = null;
                }
                throw new Error(typeof data.detail === "string" ? data.detail : `The chat service answered ${r.status}.`);
            }
            return data;
        }
        setSession(s) {
            this.session = s;
            this.stored.token = s.token;
            this.stored.exp = s.expires_at;
            save(this.storeKey, this.stored);
        }
        ensureSession() {
            if (this.sessionReady)
                return this.sessionReady;
            this.sessionReady = (async () => {
                const now = Date.now() / 1000;
                if (!this.userToken && this.stored.token && (this.stored.exp || 0) > now + 30) {
                    this.session = { token: this.stored.token, expires_at: this.stored.exp || 0, signed_in: false };
                }
                let out;
                try {
                    out = await this.call("POST", "/session", this.userToken ? { user_token: this.userToken } : {}, !this.userToken);
                }
                catch (e) {
                    if (this.userToken || !this.session)
                        throw e;
                    this.session = null; // expired or rotated: start afresh
                    this.stored = {};
                    out = await this.call("POST", "/session", {}, false);
                }
                this.setSession(out);
                this.conversations = out.conversations || [];
                if (this.userToken && !this.stored.conv && this.conversations.length)
                    this.stored.conv = this.conversations[0].id;
                if (this.stored.conv && !this.conversations.some((c) => c.id === this.stored.conv)) {
                    this.stored.conv = undefined; // another visitor's or an old session's
                }
                save(this.storeKey, this.stored);
            })();
            this.sessionReady.catch(() => {
                this.sessionReady = null;
            });
            return this.sessionReady;
        }
        // ---- start ----------------------------------------------------------------------------
        async start() {
            this.mount();
            try {
                this.cfg = { ...DEFAULTS, ...(await this.call("GET", "/config", undefined, false)) };
            }
            catch (_a) {
                this.host.remove(); // not allowed on this site, or the key is off: stay invisible
                return;
            }
            this.applyConfig();
            this.heartbeat();
            if (this.stored.conv || this.userToken) {
                this.ensureSession()
                    .then(() => this.poll())
                    .catch(() => undefined);
            }
        }
        preview(container, cfg) {
            this.cfg = { ...DEFAULTS, ...cfg };
            container.append(this.host);
            this.build(true);
            this.applyConfig();
            this.messages = [
                { id: "p1", from: "you", body: "Hello, do you open on Saturdays?", attachments: [], status: "", at: "" },
                { id: "p2", from: this.cfg.mode === "ai_first" ? "assistant" : "team", body: "Yes, from 9 to 1. Can I help with anything else?", attachments: [], status: "sent", at: "" },
            ];
            this.isOpen = true;
            this.render();
        }
        heartbeat() {
            // text/plain so it needs no preflight: the setup screen sees the page even before it's allowed.
            const page = location.origin + location.pathname;
            fetch(`${this.base}/heartbeat`, { method: "POST", mode: "cors", credentials: "omit", headers: { "Content-Type": "text/plain" }, body: JSON.stringify({ page }) }).catch(() => undefined);
        }
        // ---- DOM --------------------------------------------------------------------------------
        mount() {
            document.body.append(this.host);
            this.build(false);
        }
        build(inline) {
            const shadow = this.host.attachShadow({ mode: "open" });
            shadow.append(el("style", {}, CSS));
            this.root = el("div", { class: "root" });
            shadow.append(this.root);
            this.launcher = el("button", { class: "launcher", type: "button", "aria-label": COPY.open, "aria-expanded": "false", html: ICON_CHAT });
            this.launcher.addEventListener("click", () => (this.isOpen ? this.close() : this.open()));
            this.panel = el("div", { class: `panel${inline ? " inline" : ""}`, role: "dialog", "aria-modal": "false" });
            this.panel.hidden = !inline;
            this.panel.addEventListener("keydown", (e) => {
                if (e.key === "Escape" && !inline)
                    this.close();
            });
            if (!inline)
                this.root.append(this.launcher);
            this.root.append(this.panel);
        }
        applyConfig() {
            this.root.style.setProperty("--exa", /^#[0-9a-f]{6}$/i.test(this.cfg.colour) ? this.cfg.colour : "#155EEF");
            this.launcher.classList.add(this.cfg.position === "left" ? "left" : "right");
            this.panel.classList.add(this.cfg.position === "left" ? "left" : "right");
            this.panel.setAttribute("aria-label", this.cfg.title);
            if (!this.cfg.online && this.cfg.mode !== "ai_first" && !this.stored.conv)
                this.view = "offline";
            this.render();
        }
        open() {
            if (!this.panel)
                return;
            this.isOpen = true;
            this.unread = 0;
            this.panel.hidden = false;
            this.launcher.setAttribute("aria-expanded", "true");
            this.launcher.setAttribute("aria-label", COPY.close);
            this.launcher.innerHTML = ICON_CLOSE;
            this.render();
            if (this.stored.conv)
                this.ensureSession().then(() => this.poll()).catch(() => undefined);
            setTimeout(() => { var _a; return (_a = (this.input || this.panel.querySelector("input"))) === null || _a === void 0 ? void 0 : _a.focus(); }, 30);
        }
        close() {
            if (!this.panel)
                return;
            this.isOpen = false;
            this.panel.hidden = true;
            this.launcher.setAttribute("aria-expanded", "false");
            this.launcher.setAttribute("aria-label", COPY.open);
            this.launcher.innerHTML = ICON_CHAT;
            this.launcher.focus();
            this.renderBadge();
        }
        renderBadge() {
            var _a;
            (_a = this.launcher.querySelector(".badge")) === null || _a === void 0 ? void 0 : _a.remove();
            if (this.unread > 0 && !this.isOpen)
                this.launcher.append(el("span", { class: "badge", "aria-label": `${this.unread} new` }, String(this.unread)));
        }
        render() {
            const p = this.panel;
            p.textContent = "";
            const status = this.cfg.online || this.cfg.ai;
            const sub = el("p", {}, el("span", { class: `dot ${status ? "on" : "off"}`, "aria-hidden": "true" }), this.cfg.ai ? COPY.aiOnline : status ? COPY.online : COPY.offline);
            const closeBtn = el("button", { class: "icon", type: "button", "aria-label": COPY.close, html: ICON_CLOSE });
            closeBtn.addEventListener("click", () => this.close());
            const head = el("div", { class: "head" }, el("div", { class: "grow" }, el("h2", {}, this.cfg.title), sub));
            if (!this.panel.classList.contains("inline"))
                head.append(closeBtn);
            p.append(head);
            if (this.view === "offline")
                p.append(this.offlineForm());
            else if (this.view === "callback")
                p.append(this.callbackForm());
            else if (this.view === "done")
                p.append(el("div", { class: "form" }, el("p", { role: "status" }, this.doneText), this.backButton()));
            else if (this.view === "history")
                p.append(this.historyView());
            else
                this.renderChat();
            p.append(el("div", { class: "foot" }, COPY.poweredBy));
        }
        backButton() {
            const b = el("button", { class: "btn secondary", type: "button" }, COPY.back);
            b.addEventListener("click", () => {
                this.view = "chat";
                this.render();
            });
            return b;
        }
        renderChat() {
            this.log = el("div", { class: "log", role: "log", "aria-live": "polite", "aria-label": "Messages" });
            this.log.append(el("div", { class: "msg them" }, el("span", { class: "who" }, this.cfg.ai ? COPY.assistant : COPY.team), this.cfg.greeting));
            if (!this.cfg.online && this.cfg.mode !== "ai_first")
                this.log.append(el("p", { class: "note" }, `${this.cfg.offline_message} ${this.cfg.hours}`));
            for (const m of this.messages)
                this.log.append(this.messageEl(m));
            if (this.showContact)
                this.log.append(this.contactCard());
            const links = el("div", { class: "row" });
            if (this.cfg.callbacks)
                links.append(this.linkTo(COPY.callback, "callback"));
            if (this.conversations.length > 1)
                links.append(this.linkTo(COPY.history, "history"));
            if (links.childNodes.length)
                this.log.append(links);
            this.panel.append(this.log);
            const form = el("form", { class: "compose" });
            this.input = el("textarea", { rows: "1", "aria-label": COPY.placeholder, placeholder: COPY.placeholder, maxlength: "4000" });
            this.input.addEventListener("keydown", (e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    form.requestSubmit();
                }
            });
            form.addEventListener("submit", (e) => {
                e.preventDefault();
                const text = this.input.value.trim();
                if (!text)
                    return;
                this.input.value = "";
                this.send(text, []);
            });
            if (this.cfg.attachments.enabled) {
                const file = el("input", { type: "file", class: "sr", accept: this.cfg.attachments.types.join(","), "aria-label": COPY.attach, tabindex: "-1" });
                const clip = el("button", { class: "icon", type: "button", "aria-label": COPY.attach, html: ICON_CLIP, style: "color: var(--muted)" });
                clip.addEventListener("click", () => file.click());
                file.addEventListener("change", () => {
                    const f = file.files && file.files[0];
                    file.value = "";
                    if (f)
                        this.attach(f);
                });
                form.append(clip, file);
            }
            form.append(this.input, el("button", { class: "btn", type: "submit" }, COPY.send));
            this.panel.append(form);
            this.scroll();
        }
        linkTo(label, view) {
            const b = el("button", { class: "link", type: "button" }, label);
            b.addEventListener("click", () => {
                this.view = view;
                this.render();
            });
            return b;
        }
        messageEl(m) {
            const mine = m.from === "you";
            const who = mine ? COPY.you : m.from === "assistant" ? COPY.assistant : COPY.team;
            const box = el("div", { class: `msg ${mine ? "you" : "them"}` }, el("span", { class: "who" }, who));
            if (m.body)
                box.append(m.body);
            for (const a of m.attachments) {
                const href = `${this.base}/files/${encodeURIComponent(a.id)}?s=${encodeURIComponent(this.session ? this.session.token : "")}`;
                const link = el("a", { class: "att", href, target: "_blank", rel: "noopener noreferrer" });
                if (a.type.startsWith("image/") && !this.previewMode)
                    link.append(el("img", { src: href, alt: a.name }));
                else
                    link.append(a.name);
                box.append(link);
            }
            if (!m.pending)
                return box;
            const frag = document.createDocumentFragment();
            frag.append(box);
            if (m.pending === "failed") {
                const retry = el("button", { class: "meta failed", type: "button" }, COPY.notSent);
                retry.addEventListener("click", () => this.send(m.body, m.attachments.map((a) => a.id), m));
                frag.append(retry);
            }
            else
                frag.append(el("span", { class: "meta" }, COPY.sending));
            return frag;
        }
        scroll() {
            if (this.log)
                this.log.scrollTop = this.log.scrollHeight;
        }
        // ---- sending --------------------------------------------------------------------------
        async send(text, attachmentIds, retry, attachments = []) {
            var _a;
            if (this.previewMode)
                return;
            const m = retry || { id: `local-${randomId()}`, from: "you", body: text, attachments, status: "", at: "", clientId: randomId() };
            m.pending = "sending";
            if (!retry)
                this.messages.push(m);
            this.render();
            try {
                await this.ensureSession();
                const body = { body: text, client_id: m.clientId, attachments: attachmentIds };
                if (this.stored.conv)
                    body.conversation_id = this.stored.conv;
                const out = await this.call("POST", "/messages", body);
                const first = !this.stored.conv;
                this.stored.conv = out.conversation_id;
                save(this.storeKey, this.stored);
                Object.assign(m, out.message);
                m.pending = undefined;
                if (first && !((_a = this.session) === null || _a === void 0 ? void 0 : _a.signed_in) && !this.stored.contactGiven && this.cfg.ask_contact === "after_first")
                    this.showContact = true;
                this.render();
                this.poll();
            }
            catch (_b) {
                m.pending = "failed";
                this.render();
            }
        }
        async attach(f) {
            if (f.size > this.cfg.attachments.max_bytes)
                return this.flash(COPY.tooBig);
            if (!this.cfg.attachments.types.includes(f.type))
                return this.flash(COPY.wrongType);
            try {
                await this.ensureSession();
                const data = await new Promise((resolve, reject) => {
                    const r = new FileReader();
                    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
                    r.onerror = () => reject(r.error);
                    r.readAsDataURL(f);
                });
                const a = await this.call("POST", "/files", { name: f.name.slice(0, 120), type: f.type, data });
                await this.send("", [a.id], undefined, [a]);
            }
            catch (e) {
                this.flash(e.message);
            }
        }
        flash(text) {
            if (!this.log)
                return;
            const p = el("p", { class: "err", role: "alert" }, text);
            this.log.append(p);
            this.scroll();
            setTimeout(() => p.remove(), 6000);
        }
        // ---- polling --------------------------------------------------------------------------------
        async poll() {
            if (this.timer)
                clearTimeout(this.timer);
            if (!this.stored.conv || !this.session || this.previewMode)
                return;
            try {
                const last = [...this.messages].reverse().find((m) => !m.pending && m.at);
                const q = `?conversation_id=${encodeURIComponent(this.stored.conv)}${last ? `&after=${encodeURIComponent(last.at)}` : ""}`;
                const out = await this.call("GET", `/messages${q}`);
                const known = new Set(this.messages.map((m) => m.id));
                const fresh = out.items.filter((m) => !known.has(m.id));
                if (fresh.length) {
                    this.messages.push(...fresh);
                    const theirs = fresh.filter((m) => m.from !== "you").length;
                    if (!this.isOpen) {
                        this.unread += theirs;
                        this.renderBadge();
                    }
                    else if (this.view === "chat") {
                        for (const m of fresh)
                            this.log.insertBefore(this.messageEl(m), this.log.lastElementChild && this.log.lastElementChild.classList.contains("row") ? this.log.lastElementChild : null);
                        this.scroll();
                    }
                }
            }
            catch (_a) {
                /* try again on the next tick */
            }
            const delay = document.hidden ? 30000 : this.isOpen ? 4000 : 15000;
            this.timer = window.setTimeout(() => this.poll(), delay);
        }
        // ---- forms --------------------------------------------------------------------------------------
        field(label, input) {
            return el("label", {}, label, input);
        }
        contactCard() {
            const name = el("input", { autocomplete: "name", maxlength: "200" });
            const email = el("input", { type: "email", autocomplete: "email", maxlength: "255" });
            const err = el("p", { class: "err", role: "alert" });
            const card = el("form", { class: "card" }, el("h3", {}, COPY.contactTitle), this.field(COPY.name, name), this.field(COPY.email, email), err);
            const skip = el("button", { class: "btn secondary", type: "button" }, COPY.contactSkip);
            skip.addEventListener("click", () => {
                this.showContact = false;
                this.stored.contactGiven = true;
                save(this.storeKey, this.stored);
                this.render();
            });
            card.append(el("div", { class: "row" }, el("button", { class: "btn", type: "submit" }, COPY.save), skip));
            card.addEventListener("submit", async (e) => {
                e.preventDefault();
                try {
                    await this.call("POST", "/contact", { name: name.value.trim(), email: email.value.trim() });
                    this.showContact = false;
                    this.stored.contactGiven = true;
                    save(this.storeKey, this.stored);
                    this.render();
                }
                catch (x) {
                    err.textContent = x.message;
                }
            });
            return card;
        }
        offlineForm() {
            const name = el("input", { autocomplete: "name", maxlength: "200" });
            const email = el("input", { type: "email", autocomplete: "email", required: "", maxlength: "255" });
            const msg = el("textarea", { rows: "4", required: "", maxlength: "4000" });
            const err = el("p", { class: "err", role: "alert" });
            const submit = el("button", { class: "btn", type: "submit" }, COPY.offlineSend);
            const form = el("form", { class: "form" }, el("p", { class: "note" }, this.cfg.offline_message), el("p", { class: "note" }, this.cfg.hours), this.field(COPY.name, name), this.field(COPY.email, email), this.field(COPY.message, msg), err, submit);
            if (this.cfg.callbacks)
                form.append(this.linkTo(COPY.callback, "callback"));
            if (this.cfg.mode !== "human_only" || this.stored.conv)
                form.append(this.linkTo(COPY.back, "chat"));
            form.addEventListener("submit", async (e) => {
                e.preventDefault();
                submit.disabled = true;
                try {
                    await this.ensureSession();
                    await this.call("POST", "/offline", { name: name.value.trim(), email: email.value.trim(), message: msg.value.trim(), client_id: randomId() });
                    this.doneText = COPY.offlineDone;
                    this.view = "done";
                    this.render();
                }
                catch (x) {
                    err.textContent = x.message;
                    submit.disabled = false;
                }
            });
            return form;
        }
        callbackForm() {
            const name = el("input", { autocomplete: "name", maxlength: "200" });
            const phone = el("input", { type: "tel", autocomplete: "tel", required: "", maxlength: "40", placeholder: "+1 868 555 0100" });
            const when = el("input", { maxlength: "100" });
            const err = el("p", { class: "err", role: "alert" });
            const submit = el("button", { class: "btn", type: "submit" }, COPY.callbackSend);
            const form = el("form", { class: "form" }, this.field(COPY.name, name), this.field(COPY.phone, phone), this.field(COPY.when, when), err, el("div", { class: "row" }, submit, this.backButton()));
            form.addEventListener("submit", async (e) => {
                e.preventDefault();
                submit.disabled = true;
                try {
                    await this.ensureSession();
                    const out = await this.call("POST", "/callback", { name: name.value.trim(), phone: phone.value.trim(), when: when.value.trim(), client_id: randomId() });
                    this.stored.conv = out.conversation_id;
                    save(this.storeKey, this.stored);
                    this.doneText = COPY.callbackDone;
                    this.view = "done";
                    this.render();
                }
                catch (x) {
                    err.textContent = x.message;
                    submit.disabled = false;
                }
            });
            return form;
        }
        historyView() {
            const list = el("div", { class: "form" }, el("h3", {}, COPY.history));
            for (const c of this.conversations) {
                const b = el("button", { class: "btn secondary conv", type: "button" }, `${new Date(c.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })} · ${c.state.replace(/_/g, " ")}`);
                b.addEventListener("click", () => {
                    this.stored.conv = c.id;
                    save(this.storeKey, this.stored);
                    this.messages = [];
                    this.view = "chat";
                    this.render();
                    this.poll();
                });
                list.append(b);
            }
            list.append(this.backButton());
            return list;
        }
        identify(token) {
            this.userToken = token;
            this.sessionReady = null;
            this.stored = {};
            this.messages = [];
            this.ensureSession()
                .then(() => {
                this.render();
                this.poll();
            })
                .catch(() => undefined);
        }
    }
    // ---- boot ----------------------------------------------------------------------------------------
    const settings = w.ExaCaribChatSettings || {};
    const key = (script && script.dataset.key) || settings.key || "";
    const userToken = (script && script.dataset.userToken) || settings.userToken || "";
    let chat = null;
    w.ExaCaribChat = {
        identify(token) {
            if (chat)
                chat.identify(token);
        },
        open() {
            if (chat)
                chat.open();
        },
        close() {
            if (chat)
                chat.close();
        },
        preview(container, config) {
            const p = new Chat("preview", "", true);
            p.preview(container, config);
            return () => container.textContent = "";
        },
    };
    if (key) {
        chat = new Chat(key, userToken);
        const go = () => chat && chat.start();
        if (document.readyState === "loading")
            document.addEventListener("DOMContentLoaded", go);
        else
            go();
    }
})();
