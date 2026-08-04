// Never cache API responses.  In particular, auth/me and csrf responses are
// user/session specific; caching either makes a browser appear logged out after
// a refresh and leaves POST requests carrying an expired CSRF token.
const CACHE_NAME = "football-ai-command-center-v15";
const CORE_ASSETS = [
  "./",
  "./index.html",
  "./calculator.html",
  "./login.html",
  "./account.html",
  "./admin.html",
  "./assets/calculator/bg_header.png",
  "./assets/calculator/icon_back.png",
  "./assets/calculator/icon_more1.png",
  "./assets/calculator/bg_tip.png",
  "./assets/calculator/clean.png",
  "./data/matches.json",
  "./data/analysis_archive.json",
  "./data/jc_history.json"
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
    const pageFallbacks = new Set(["/calculator.html", "/login.html", "/account.html", "/admin.html"]);
    const fallbackPath = pageFallbacks.has(url.pathname) ? `.${url.pathname}` : "./index.html";
    event.respondWith(networkFirst(event.request, fallbackPath));
    return;
  }
  if (url.pathname.endsWith(".json")) {
    event.respondWith(networkFirst(event.request));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      }
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
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
    }
    return response;
  } catch (error) {
    return (await caches.match(request)) || (fallbackPath ? await caches.match(fallbackPath) : Response.error());
  }
}
