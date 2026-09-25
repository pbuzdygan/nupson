const CACHE_NAME = "nupson-shell-v11";
const APP_SHELL = [
  "/",
  "/styles.css?v=7",
  "/dashboard.css?v=17",
  "/app.js?v=25",
  "/manifest.webmanifest",
  "/assets/favicon.ico",
  "/assets/favicon-16x16.png",
  "/assets/favicon-32x32.png",
  "/assets/favicon-48x48.png",
  "/assets/apple-touch-icon-120.png",
  "/assets/apple-touch-icon-152.png",
  "/assets/apple-touch-icon-167.png",
  "/assets/apple-touch-icon.png",
  "/assets/icon-192.png",
  "/assets/icon-512.png",
  "/assets/icon-maskable-192.png",
  "/assets/icon-maskable-512.png",
  "/assets/logo-mark.png",
  "/icons/chevron-down.svg",
  "/icons/check.svg",
  "/icons/device-desktop.svg",
  "/icons/device-floppy.svg",
  "/icons/eye.svg",
  "/icons/pencil.svg",
  "/icons/power.svg",
  "/icons/trash.svg",
  "/assets/login-banner.png",
  "/icons/bolt.svg",
  "/icons/chart-line.svg",
  "/icons/copy.svg",
  "/icons/download.svg",
  "/icons/history.svg",
  "/icons/info-circle.svg",
  "/icons/layout-dashboard.svg",
  "/icons/moon.svg",
  "/icons/plug-connected.svg",
  "/icons/refresh.svg",
  "/icons/server.svg",
  "/icons/settings.svg",
  "/icons/sun.svg",
  "/icons/users.svg",
  "/icons/webhook.svg"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(key => key.startsWith("nupson-") && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/")));
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      const update = fetch(request).then(response => {
        if (response.ok) caches.open(CACHE_NAME).then(cache => cache.put(request, response.clone()));
        return response;
      });
      return cached || update;
    })
  );
});
