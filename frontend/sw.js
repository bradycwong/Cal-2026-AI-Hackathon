// sw.js — self-unregistering "kill" worker.
//
// The app no longer uses a service worker. The live UI is FrontendTest/, served
// fresh from the network; an earlier shell-caching worker cached now-nonexistent
// /static/* paths. This stub replaces it: on activate it deletes every cache,
// unregisters itself, and reloads open clients so they pick up the live UI.
// Browsers re-fetch /sw.js periodically, so a stale registration from an earlier
// visit gets cleaned up automatically the next time this is served.
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      try {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => caches.delete(k)));
      } catch (_) {}
      try {
        await self.registration.unregister();
      } catch (_) {}
      try {
        const clients = await self.clients.matchAll({ type: "window" });
        clients.forEach((c) => c.navigate(c.url));
      } catch (_) {}
    })()
  );
});

// No fetch handler on purpose: every request goes straight to the network.
