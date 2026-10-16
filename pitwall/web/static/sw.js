// Pitwall service worker. Caches only the app shell (CSS, JS, fonts, icons) and an offline
// page. Pages are never cached: they hold personal data and spoiler-protected results.
const VERSION = "pitwall-v1";
const SHELL = [
  "/static/style.css",
  "/static/fonts/fonts.css",
  "/static/app.js",
  "/static/icons/logo.svg",
  "/static/offline.html",
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(VERSION).then(function (c) { return c.addAll(SHELL); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== VERSION; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    // Stale-while-revalidate for the shell.
    event.respondWith(
      caches.open(VERSION).then(function (cache) {
        return cache.match(req).then(function (hit) {
          const fresh = fetch(req).then(function (resp) {
            if (resp.ok) cache.put(req, resp.clone());
            return resp;
          }).catch(function () { return hit; });
          return hit || fresh;
        });
      })
    );
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(function () { return caches.match("/static/offline.html"); }));
  }
});
