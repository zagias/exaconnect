/* Jibsy Phone service worker: the app opens without a network (it then says it is
 * offline), and a call notification brings the app to the front. API calls, the
 * phone connection and sign-in are never cached. */
const CACHE = "jibsy-phone-v1";
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

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      const open = list.find((c) => new URL(c.url).pathname.startsWith("/phone"));
      return open ? open.focus() : self.clients.openWindow("/phone");
    }),
  );
});
