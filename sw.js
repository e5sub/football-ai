// Never cache API responses.  In particular, auth/me and csrf responses are
// user/session specific; caching either makes a browser appear logged out after
// a refresh and leaves POST requests carrying an expired CSRF token.
const CACHE_NAME = "football-ai-command-center-v9";
const CORE_ASSETS = [
  "./",
  "./index.html",
  "./data/matches.json",
  "./data/analysis_archive.json",
  "./data/jc_history.json",
  "./使用说明.txt",
  "./免责声明.txt"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE_ASSETS)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Authentication, administration and live data must always reach the
  // server.  Besides avoiding stale data, this prevents a cached 401/403 from
  // being replayed after the user signs in.
  if (url.pathname.startsWith("/api/")) return;

  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request, "./index.html"));
    return;
  }
  if (url.pathname.endsWith(".json")) {
    event.respondWith(networkFirst(event.request));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      return response;
    }))
  );
});

async function networkFirst(request, fallbackPath) {
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) => setTimeout(() => reject(new Error("network timeout")), 6500))
    ]);
    const copy = response.clone();
    caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
    return response;
  } catch (error) {
    return (await caches.match(request)) || (fallbackPath ? await caches.match(fallbackPath) : Response.error());
  }
}
