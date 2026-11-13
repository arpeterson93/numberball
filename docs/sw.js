/* MLN Gameday service worker - push notifications only (no offline caching yet).
 * The sender (tools/push_send.py) sends JSON: { title, body, url, icon }  (icon: a team logo; iOS ignores it and shows the app icon).
 * Tapping a notification focuses an open tab and navigates it to `url` (the usual ?game=..&play=.. deep link), or opens one. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

// Delivery receipts (see apps-script/Code.gs push_received): after showing a notification the phone tells the sheet, so a
// missing alert can be traced. Same endpoint as docs/js/config.js FAVORITES_ENDPOINT. Anonymous: a short hash of the push
// endpoint (matches the id push_send.py logs), the title and the first line of the body. Never allowed to break an alert.
var RECEIPT_URL = "https://script.google.com/macros/s/AKfycbwm83cpSNFulJ8i4Lmvj6kraY1ssHl4sNOYyBhWetSEgd4HDeakQ9yxSTg_fl4SskZ66g/exec";
var deviceId = "";
function receipt(d, shown) {
  var id = deviceId ? Promise.resolve(deviceId) : self.registration.pushManager.getSubscription().then(function (sub) {
    if (!sub) throw new Error("no subscription");
    return crypto.subtle.digest("SHA-1", new TextEncoder().encode(sub.endpoint)).then(function (buf) {
      deviceId = Array.prototype.map.call(new Uint8Array(buf), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("").slice(0, 6);
      return deviceId;
    });
  });
  return id.then(function (id) {
    return fetch(RECEIPT_URL, { method: "POST", mode: "no-cors", headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({ action: "push_received", id: id, title: d.title || "", body: String(d.body || "").split("\n")[0], shown: shown }) });
  }).catch(function () {});
}

self.addEventListener("push", function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (err) { d = { body: event.data ? event.data.text() : "" }; }
  var opts = {
    body: d.body || "",
    icon: d.icon || "apple-touch-icon.png",
    badge: "favicon.png",
    data: { url: d.url || "./" },
  };
  event.waitUntil(self.registration.showNotification(d.title || "MLN Gameday", opts).then(function () { return receipt(d, true); },
    function (err) { return receipt(d, false).then(function () { throw err; }); }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = new URL((event.notification.data && event.notification.data.url) || "./", self.location.href).href;
  // iOS is unreliable about delivering a tap to the page (a postMessage can land before the page is awake, openWindow can open
  // the start page instead of the link), so the link is also written to the cache first; the page checks it whenever it
  // loads or comes to the front (docs/js/push.js) and goes there itself.
  var stash = self.caches.open("mln-push").then(function (cache) {
    return cache.put(new Request(self.registration.scope + "__pending"), new Response(JSON.stringify({ url: target, t: Date.now() })));
  }).catch(function () {});
  var chain = stash.then(function () { return self.clients.matchAll({ type: "window", includeUncontrolled: true }); }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).origin === new URL(target).origin && "postMessage" in c) {
        // WindowClient.navigate() is unreliable in an installed iOS web app (it comes to the front on whatever screen it was on),
        // so hand the URL to the page and let it navigate itself
        return c.focus().then(function () { c.postMessage({ type: "mln-open", url: target }); });
      }
    }
    return self.clients.openWindow(target);
  });
  // a receipt for the tap itself (title "CLICK: ..."), so a tap that never reaches the page can be told from one that does
  event.waitUntil(Promise.all([chain, receipt({ title: "CLICK: " + (event.notification.title || ""), body: target }, true)]));
});
