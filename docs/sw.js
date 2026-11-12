/* MLN Gameday service worker - push notifications only (no offline caching yet).
 * The sender (tools/push_send.py) sends JSON: { title, body, url, tag, icon }.
 * Tapping a notification focuses an open tab and navigates it to `url` (the usual ?game=..&play=.. deep link), or opens one. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (err) { d = { body: event.data ? event.data.text() : "" }; }
  var opts = {
    body: d.body || "",
    icon: d.icon || "apple-touch-icon.png",
    badge: "favicon.png",
    tag: d.tag || undefined,          // same tag replaces the earlier notification (e.g. one per game)
    renotify: !!d.tag,
    data: { url: d.url || "./" },
  };
  event.waitUntil(self.registration.showNotification(d.title || "MLN Gameday", opts));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = new URL((event.notification.data && event.notification.data.url) || "./", self.location.href).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).origin === new URL(target).origin && "navigate" in c) {
        return c.focus().then(function () { return c.navigate(target); });
      }
    }
    return self.clients.openWindow(target);
  }));
});
