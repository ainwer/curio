(function () {
  "use strict";

  var CACHE_NAME = "curio-static-v1";
  var PRECACHE_URLS = [
    "/static/app.css",
    "/static/app.js",
    "/static/vendor/htmx.min.js",
    "/static/vendor/sse.js",
    "/static/vendor/katex/katex.min.css",
    "/static/vendor/katex/katex.min.js",
    "/static/vendor/katex/auto-render.min.js",
  ];

  self.addEventListener("install", function (event) {
    event.waitUntil(
      caches
        .open(CACHE_NAME)
        .then(function (cache) {
          return cache.addAll(PRECACHE_URLS);
        })
        .then(function () {
          return self.skipWaiting();
        })
    );
  });

  self.addEventListener("activate", function (event) {
    event.waitUntil(
      caches.keys().then(function (names) {
        return Promise.all(
          names
            .filter(function (name) {
              return name !== CACHE_NAME;
            })
            .map(function (name) {
              return caches.delete(name);
            })
        );
      })
    );
  });

  self.addEventListener("fetch", function (event) {
    var request = event.request;
    if (request.method !== "GET") {
      return;
    }
    var url = new URL(request.url);
    if (url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) {
      return;
    }
    // Stale-while-revalidate: serve the cached copy, refresh it in the background.
    event.respondWith(
      caches.open(CACHE_NAME).then(function (cache) {
        return cache.match(request).then(function (cached) {
          var network = fetch(request).then(function (response) {
            if (response.ok) {
              cache.put(request, response.clone());
            }
            return response;
          });
          return cached || network;
        });
      })
    );
  });
})();