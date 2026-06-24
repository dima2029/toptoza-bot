// ТОП-ТОЗА — минимальный service worker (нужен для установки как приложение)
const CACHE = "toptoza-v1";
const ASSETS = ["/static/style.css", "/static/logo.png",
                "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

// network-first: всегда свежие данные, кэш только как запасной вариант офлайн
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
