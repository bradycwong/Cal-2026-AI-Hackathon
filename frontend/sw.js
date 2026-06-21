// sw.js — cache the app shell so the page loads instantly / offline.
// Only the static shell is cached; live data (/api/*) and the event/audio
// sockets (/ws/*) always go to the network and are never intercepted.
//
// Bump CACHE when the shell changes to evict the previous version on activate.
const CACHE = "lab-shell-v2";
const SHELL = ["/", "/static/styles.css", "/static/app.js"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isShellRequest(request, url) {
  if (request.mode === "navigate") return true; // the page itself ("/")
  return SHELL.includes(url.pathname);
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  if (!isShellRequest(request, url)) return;

  // Stale-while-revalidate: answer from cache instantly, refresh in background.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(request, { ignoreSearch: true });
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
