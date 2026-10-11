/* Jibsy Phone service worker: the app opens without a network (it then says it is
 * offline), a call notification brings the app to the front, and a push message
 * from ExaCarib rings a closed app (ADR 0045). API calls, the phone connection and
 * sign-in are never cached. */
const CACHE = "jibsy-phone-v2";
const RING_SECONDS = 30; // the caller hears ringing about this long while the app opens
const SHELL = ["/phone", "/phone.webmanifest", "/phone/icon-192.png", "/phone/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("jibsy-phone-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/verto") return;
  // The app page: the network first, so an update shows at once; the saved copy offline.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put("/phone", copy));
          return r;
        })
        .catch(() => caches.match("/phone")),
    );
    return;
  }
  // Built files have content hashes in their names: the saved copy is always right.
  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/phone/")) {
    e.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((r) => {
            if (r.ok) {
              const copy = r.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return r;
          }),
      ),
    );
  }
});

// A call for this person while the app is closed (or asleep): show it until it's answered
// or the caller gives up. Tapping it opens the app, which connects and answers.
self.addEventListener("push", (e) => {
  let m = {};
  try {
    m = e.data ? e.data.json() : {};
  } catch {
    /* not ours */
  }
  if (m.type === "test") {
    e.waitUntil(
      self.registration.showNotification("Jibsy Phone", {
        body: "Ringing works on this device. Calls will show here even when the app is closed.",
        tag: "jibsy-test",
        icon: "/phone/icon-192.png",
        badge: "/phone/icon-192.png",
        data: { url: "/phone#settings" },
      }),
    );
    return;
  }
  if (m.type !== "call") return;
  const who = m.name ? `${m.name} (${m.from})` : m.from || "Unknown caller";
  const at = (m.at || 0) * 1000 || Date.now();
  e.waitUntil(
    self.registration
      .showNotification("Incoming call", {
        body: `${who} is calling your extension ${m.ext || ""}`.trim(),
        tag: "jibsy-call",
        renotify: true,
        requireInteraction: true,
        icon: "/phone/icon-192.png",
        badge: "/phone/icon-192.png",
        vibrate: [400, 200, 400, 200, 400],
        actions: [{ action: "answer", title: "Answer" }],
        data: { url: `/phone?answer=${encodeURIComponent(m.call || "1")}&from=${encodeURIComponent(m.from || "")}#keypad`, at, from: m.from || "" },
      })
      // The caller has stopped hearing ringing by now: take the notification away.
      .then(() => new Promise((r) => setTimeout(r, RING_SECONDS * 1000)))
      .then(() => self.registration.getNotifications({ tag: "jibsy-call" }))
      .then((list) => list.forEach((n) => n.data && n.data.at === at && n.close())),
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  // A call notification: answer it unless it's too old to still be ringing.
  const fresh = data.at && Date.now() - data.at < (RING_SECONDS + 15) * 1000;
  const url = data.url && (fresh || !data.url.includes("answer=")) ? data.url : "/phone";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      const open = list.find((c) => new URL(c.url).pathname.startsWith("/phone"));
      if (open) {
        if (url.includes("answer=")) open.postMessage({ type: "answer", from: data.from || "" });
        return open.focus();
      }
      return self.clients.openWindow(url);
    }),
  );
});
