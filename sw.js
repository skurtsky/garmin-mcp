const CACHE_NAME = "garmin-mcp-shell-v2";

// Written at the very end of a complete dashboard render
// (DASHBOARD_COMPLETE_MARKER in tools/dashboard.py). Only a page carrying it
// is cached, so a failed or cut-off load never becomes the cached copy.
const COMPLETE_MARKER = "<!--dashboard-complete-->";

// When the last write (a gear form POST, an API call) went through. The
// dashboard navigation that follows one — e.g. the form's redirect back to
// /dashboard?tab=gear — must show the change, so for a little while after a
// write the dashboard comes from the network, not the cache.
const AFTER_WRITE_MS = 30 * 1000;
let lastWriteAt = 0;

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

// The cache key for a dashboard page, or null for anything else. The open
// tab and Activity filter are applied by the page itself from its URL, so
// they're left out of the key and every tab shares one cached copy; ?week=
// changes the data, so it stays in. A gear-form error redirect is never
// cached.
function dashboardKey(url) {
  const u = new URL(url);
  if (u.origin !== self.location.origin || u.pathname !== "/dashboard" || u.searchParams.has("error")) return null;
  u.searchParams.delete("tab");
  u.searchParams.delete("filter");
  u.searchParams.sort();
  u.hash = "";
  return u.toString();
}

// Cache a dashboard response once it has arrived in full (and only if it's
// complete). Reads a clone, so the page itself still streams in as it comes.
function cacheDashboard(key, response) {
  return response.clone().text().then((text) => {
    if (!text.includes(COMPLETE_MARKER)) return false;
    return caches.open(CACHE_NAME)
      .then((cache) => cache.put(key, new Response(text, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      })))
      .then(() => true);
  });
}

function offlineFallback(request) {
  return caches.match(request, { ignoreSearch: true })
    .then((cached) => cached || caches.match("/dashboard", { ignoreSearch: true }));
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") { lastWriteAt = Date.now(); return; }
  if (event.request.mode !== "navigate") return;

  const key = dashboardKey(event.request.url);
  if (key) {
    // Dashboard: open instantly from the last copy when there is one. The
    // page then checks how old that copy is and asks for a fresh one
    // ("refresh-dashboard" below) if it's stale.
    const justWrote = Date.now() - lastWriteAt < AFTER_WRITE_MS;
    event.respondWith(
      (justWrote ? Promise.resolve(undefined) : caches.open(CACHE_NAME).then((cache) => cache.match(key))).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          if (response.ok) event.waitUntil(cacheDashboard(key, response).catch(() => {}));
          return response;
        }).catch(() => offlineFallback(event.request));
      })
    );
    return;
  }

  // Everything else: network first, the cache only when offline.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => offlineFallback(event.request))
  );
});

// The dashboard asks for a fresh copy of itself; replies once it's cached
// (ok: true, and the page reloads onto it) or couldn't be had (ok: false,
// and the page keeps showing what it has).
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "refresh-dashboard" || !data.url) return;
  const key = dashboardKey(data.url);
  const reply = (ok) => event.source && event.source.postMessage({ type: "dashboard-refreshed", ok });
  if (!key) { reply(false); return; }
  event.waitUntil(
    fetch(data.url, { cache: "no-store", credentials: "same-origin" })
      .then((response) => (response.ok ? cacheDashboard(key, response) : false))
      .catch(() => false)
      .then(reply)
  );
});
