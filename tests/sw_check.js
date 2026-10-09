// Runs the SHIPPED service worker (casebroker/static/sw.js) against a stubbed browser: a push
// shows a notice, a batch folds into the one of its kind still on screen, a click brings the
// open dashboard forward on the place named (or opens one), and never another site. Exits
// non-zero on the first disagreement. Driven by tests/test_push.py.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync('casebroker/static/sw.js', 'utf8');
const ORIGIN = 'https://broker.example.org';

function worker({ onScreen = [], tabs = [] } = {}) {
  const handlers = {};
  const shown = [], opened = [], focused = [], messages = [];
  const self = {
    location: new URL(ORIGIN + '/sw.js'),
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => {},
    registration: {
      showNotification: async (title, opts) => { shown.push({ title, ...opts }); },
      getNotifications: async ({ tag }) => onScreen.filter((n) => n.tag === tag),
    },
    clients: {
      claim: async () => {},
      matchAll: async () => tabs.map((url) => ({
        url,
        focus: async function () { focused.push(url); return this; },
        postMessage: (m) => messages.push({ url, ...m }),
      })),
      openWindow: async (url) => { opened.push(url); },
    },
  };
  const ctx = { self, URL, Date, Number, String, Array, JSON, fetch: async () => ({}) };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  async function fire(type, event) {
    let done;
    event.waitUntil = (p) => { done = p; };
    handlers[type](event);
    await done;
  }
  return { fire, shown, opened, focused, messages };
}
const push = (payload) => ({ data: { json: () => payload, text: () => JSON.stringify(payload) } });

(async () => {
  // One notice, as the broker composes it.
  let w = worker();
  await w.fire('push', push({ kind: 'case_quarantined', tag: 'casebroker-case_quarantined', title: 'Case quarantined',
                              body: 'v2-1 · exit 64', url: '/#case=v2-1', ts: 1000, count: 1, items: ['v2-1'], renotify: true }));
  assert.strictEqual(w.shown.length, 1);
  assert.strictEqual(w.shown[0].title, 'Case quarantined');
  assert.strictEqual(w.shown[0].tag, 'casebroker-case_quarantined');
  assert.strictEqual(w.shown[0].data.url, '/#case=v2-1');
  assert.strictEqual(w.shown[0].timestamp, 1000 * 1000);
  console.log('a push is shown with its tag and its place');

  // A batch of the same kind while the last one is still on screen: it adds up.
  w = worker({ onScreen: [{ tag: 'casebroker-done', data: { items: ['a', 'b', 'c'], count: 3 } }] });
  await w.fire('push', push({ kind: 'case_done', tag: 'casebroker-done', title: '2 cases finished', body: 'd, e',
                              url: '/#state=done', count: 2, items: ['d', 'e'], renotify: false,
                              many: { title: '{n} cases finished', url: '/#state=done' } }));
  assert.strictEqual(w.shown[0].title, '5 cases finished');
  assert.strictEqual(w.shown[0].body, 'd, e, a and 2 more');
  assert.strictEqual(w.shown[0].renotify, false, 'a batch updates the notice without a sound');
  assert.deepStrictEqual(Array.from(w.shown[0].data.items), ['d', 'e', 'a', 'b', 'c']);
  assert.strictEqual(w.shown[0].data.count, 5);
  console.log('a batch folds into the notice of its kind still on screen');

  // Dismissed (nothing on screen): it starts over.
  w = worker();
  await w.fire('push', push({ kind: 'case_done', tag: 'casebroker-done', title: 'Case finished', body: 'v2-9 · COD-1',
                              url: '/#case=v2-9', count: 1, items: ['v2-9'], many: { title: '{n} cases finished', url: '/#state=done' } }));
  assert.strictEqual(w.shown[0].title, 'Case finished');
  assert.strictEqual(w.shown[0].data.url, '/#case=v2-9');
  console.log('a dismissed notice starts the count over');

  // Not JSON: still shown -- a push that shows nothing costs the subscription.
  w = worker();
  await w.fire('push', { data: { json: () => { throw new Error('not json'); }, text: () => 'hello' } });
  assert.strictEqual(w.shown[0].title, 'Case broker');
  assert.strictEqual(w.shown[0].body, 'hello');
  console.log('a push that is not JSON is still shown');

  // A click with the dashboard open: that tab, told where to go.
  const note = (url) => ({ notification: { data: { url }, close: () => {} } });
  w = worker({ tabs: ['https://other.example/', ORIGIN + '/#state=done'] });
  await w.fire('notificationclick', note('/#case=v2-1'));
  assert.deepStrictEqual(w.focused, [ORIGIN + '/#state=done']);
  assert.strictEqual(w.messages[0].type, 'casebroker-open');
  assert.strictEqual(w.messages[0].url, ORIGIN + '/#case=v2-1');
  assert.deepStrictEqual(w.opened, []);
  console.log('a click brings the open dashboard forward on the place named');

  // None open: one is opened there. And a notice naming another site opens the dashboard.
  w = worker();
  await w.fire('notificationclick', note('/#storage'));
  assert.deepStrictEqual(w.opened, [ORIGIN + '/#storage']);
  w = worker();
  await w.fire('notificationclick', note('https://evil.example/phish'));
  assert.deepStrictEqual(w.opened, [ORIGIN + '/']);
  console.log('a click opens the dashboard, and never another site');
})().catch((e) => { console.error(e); process.exit(1); });
