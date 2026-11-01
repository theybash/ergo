/* A service worker that does nothing on purpose.
   It exists so the browser offers "Add to Home Screen". It caches nothing and
   intercepts nothing, so the page always comes from the server. */

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});
