// The dashboard's service worker: what shows a push from the broker with no tab
// open (casebroker/push.py sends them; app.py serves this at /sw.js).
//
// A file of its own, beside the one-file dashboard, because a browser will only
// run a service worker from a script URL of the site's own -- not from inline
// code, not from a blob. It is kept to the three things a worker must do: show a
// push, fold a batch into the one already on screen, and on a click bring the
// dashboard forward on the place the notice is about. Nothing is cached, and
// nothing here intercepts the page's own requests: the dashboard works exactly
// as it did without it.
"use strict";

self.addEventListener("install", () => self.skipWaiting());
// Take over open dashboards at once, so a click can talk to the tab that
// registered this rather than only to tabs opened after it.
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function listed(items, n) {
  const shown = items.slice(0, 3);
  return shown.join(", ") + (n > shown.length ? ` and ${(n - shown.length).toLocaleString()} more` : "");
}

async function show(d) {
  const tag = typeof d.tag === "string" && d.tag ? d.tag : "casebroker-" + (d.kind || "notice");
  let title = d.title || "Case broker";
  let body = d.body || "";
  let url = d.url || "/";
  let items = Array.isArray(d.items) ? d.items.map(String) : [];
  let count = Number(d.count) || items.length || 1;
  // A batch kind ("3 cases finished") adds to the notice of its kind still on
  // screen instead of replacing it: the tag replaces, so without this a quiet
  // night's first batch would be gone by morning, overwritten by the last.
  // Dismissing it starts the count over, which is "since you last looked".
  if (d.many && items.length) {
    try {
      const prev = (await self.registration.getNotifications({ tag }))[0];
      const was = prev && prev.data && Array.isArray(prev.data.items) ? prev.data : null;
      if (was && was.items.length) {
        count += Number(was.count) || was.items.length;
        items = items.concat(was.items.filter((x) => !items.includes(x))).slice(0, 20);
        title = String(d.many.title || title).replace("{n}", count.toLocaleString());
        body = listed(items, count);
        url = d.many.url || url;
      }
    } catch (e) { /* getNotifications is missing in places; the push still shows */ }
  }
  return self.registration.showNotification(title, {
    body, tag,
    renotify: d.renotify !== false,
    timestamp: d.ts ? d.ts * 1000 : Date.now(),
    data: { url, items, count, kind: d.kind || null },
  });
}

self.addEventListener("push", (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch (e) {
    d = { title: "Case broker", body: event.data ? event.data.text() : "" };
  }
  // Every push must show something: a browser revokes the subscription of a worker
  // that receives pushes and shows nothing (userVisibleOnly).
  event.waitUntil(show(d || {}));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let target;
  try {
    target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
  } catch (e) {
    target = new URL("/", self.location.origin);
  }
  // Only ever this dashboard: a push names a place in it, never another site.
  if (target.origin !== self.location.origin) target = new URL("/", self.location.origin);
  event.waitUntil((async () => {
    const tabs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const tab = tabs.find((c) => new URL(c.url).origin === self.location.origin);
    if (tab) {
      // The tab moves itself (dashboard.html, "casebroker-open"): a change of
      // fragment is a step it already understands, and keeps everything loaded.
      await tab.focus();
      tab.postMessage({ type: "casebroker-open", url: target.href });
      return;
    }
    await self.clients.openWindow(target.href);
  })());
});

// A push service may rotate a subscription (Firefox does when one expires). The new
// one is posted with the old one's endpoint, so the broker carries its kinds over
// and drops the old row. The session cookie rides along, as on any same-origin
// request; signed out, the broker refuses it and the page offers to turn push on.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription;
    const key = old && old.options ? old.options.applicationServerKey : null;
    const sub = event.newSubscription
      || (key ? await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }) : null);
    if (!sub) return;
    await fetch("/v1/push/subscriptions", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON(), replaces: old ? old.endpoint : null }),
    });
  })().catch(() => {}));
});
