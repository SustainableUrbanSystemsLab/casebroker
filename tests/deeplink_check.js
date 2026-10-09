// Runs the SHIPPED deep-link code -- sliced out of dashboard.html between its
// markers, not copied -- on links of every kind, including hostile ones.
// Exits non-zero on the first disagreement. Driven by tests/test_deeplinks.py.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const from = html.indexOf('// deep-links:pure begin');
const to = html.indexOf('// deep-links:pure end');
if (from < 0 || to < 0 || to < from) throw new Error('deep-link block not found in dashboard.html');
const block = html.slice(from, to);

const ctx = { URLSearchParams, encodeURIComponent };
vm.createContext(ctx);
vm.runInContext(block + '\nthis.api = { routeParams, parseRoute, routeHash, shareLinkUrl, shareMailto };', ctx);
const { routeParams, parseRoute, routeHash, shareLinkUrl, shareMailto } = ctx.api;

// What the page offers: the dashboard builds this from its own state select, its
// column list and its drawer tabs.
const allowed = {
  states: ['pending', 'leased', 'done', 'quarantined'],
  sorts: ['case_id', 'state', 'split', 'lcz', 'city_cluster', 'recipe', 'attempts', 'last_progress', 'updated_at'],
  tabs: ['setup', 'connection', 'users', 'sharing', 'machines', 'campaign', 'prefs'],
};
// Objects made inside the vm have its Object.prototype; deepStrictEqual compares prototypes.
const plain = (x) => JSON.parse(JSON.stringify(x));
const parse = (h) => plain(parseRoute(h, allowed));

// -- a link is the view, and the view is the link ---------------------------------------
const views = [
  {},
  { case: 'v2-00697fb4542aa4c6' },
  { case: 'v2-00697fb4542aa4c6', recipe: 'cyl-v6' },
  { state: 'quarantined' },
  { state: 'done', split: 'test', city: 'Abu Dhabi', label: 'campaign:v2-pilot', recipe: 'cyl-v6' },
  { sort: 'attempts', dir: 'asc', offset: 50 },
  { settings: 'users' },
  { settings: 'sharing', storage: true, dataset: true },
  { state: 'leased', offset: 25, storage: true },
];
for (const v of views) {
  assert.deepStrictEqual(parse(routeHash(v)), v, 'round trip: ' + routeHash(v));
}

// -- a page as it starts has no fragment at all -------------------------------------------
assert.strictEqual(routeHash({}), '');
assert.strictEqual(routeHash({ sort: 'updated_at', dir: 'desc' }), '', 'the default order says nothing');
assert.strictEqual(routeHash({ sort: 'attempts', dir: 'desc' }), '#sort=attempts', 'desc is the default direction');
assert.strictEqual(routeHash({ dir: 'asc' }), '#dir=asc');

// -- one view is one link: a fixed order, whatever order the route was built in ------------
assert.strictEqual(routeHash({ split: 'test', case: 'a', state: 'done' }), routeHash({ state: 'done', case: 'a', split: 'test' }));
assert.strictEqual(routeHash({ state: 'done', case: 'a', settings: 'users' }), '#case=a&state=done&settings=users');

// -- readable in a mail client and in the bar -----------------------------------------------
assert.strictEqual(routeHash({ city: 'Abu Dhabi' }), '#city=Abu%20Dhabi', 'a space is %20, not +');
assert.strictEqual(routeHash({ label: 'campaign:v2-pilot' }), '#label=campaign:v2-pilot', 'a label filter keeps its colon');
assert.strictEqual(parse('#city=Abu+Dhabi').city, 'Abu Dhabi', 'and a hand-written + still reads as a space');
assert.strictEqual(parse('#case=a%20b').case, 'a b');

// -- tolerant of how people write them -----------------------------------------------------
for (const h of ['#case=abc', 'case=abc', '#/case=abc', '#?case=abc']) assert.strictEqual(parse(h).case, 'abc', h);
for (const h of ['', '#', null, undefined, '#nothing=here', '#=', '#&&&']) assert.deepStrictEqual(parse(h), {}, String(h));
assert.deepStrictEqual([...routeParams('#a=1&b=2').keys()], ['a', 'b']);

// -- hostile: anything the page does not offer is dropped, never passed on ---------------------
const hostile = parse('#state=<script>alert(1)</script>&sort=updated_at;drop%20table&dir=up&offset=-5'
  + '&case=%00&settings=../../etc&split=' + 'x'.repeat(65) + '&city=' + 'y'.repeat(121));
assert.deepStrictEqual(hostile, { settings: 'setup' }, 'only an unknown tab falls back, and to the first one');
assert.deepStrictEqual(parse('#state=failed'), {}, 'a state the select does not list');
assert.deepStrictEqual(parse('#sort=password_hash'), {}, 'a column the table does not have');
for (const bad of ['0', '-1', '12.5', 'abc', 'Infinity', '10000001', '']) {
  assert.strictEqual(parse('#offset=' + bad).offset, undefined, 'offset=' + bad);
}
assert.strictEqual(parse('#offset=10000000').offset, 10000000);
assert.strictEqual(parse('#case=' + 'a'.repeat(128)).case.length, 128);
assert.strictEqual(parse('#case=' + 'a'.repeat(129)).case, undefined, 'an id has a length');
assert.strictEqual(parse('#case=a%0Ab').case, undefined, 'no control characters');
assert.strictEqual(parse('#case=%20%20').case, undefined, 'and not blank');
assert.strictEqual(parse('#case=v2-<img src=x onerror=alert(1)>').case, 'v2-<img src=x onerror=alert(1)>',
  'text is kept as text: the page only ever puts it in a field or escapes it');
assert.strictEqual(parse('#settings').settings, 'setup', 'a bare #settings opens the first tab');
assert.strictEqual(parse('#settings=sharing').settings, 'sharing');
assert.strictEqual(parse('#storage').storage, true);

// -- the secret of a share link is never part of a route ---------------------------------------
const withSecret = parse('#share=TOP-SECRET&case=abc');
assert.deepStrictEqual(withSecret, { case: 'abc' }, 'parse leaves it out');
assert.ok(!routeHash(withSecret).includes('SECRET'), 'so writing the route back removes it');
assert.strictEqual(routeHash(withSecret), '#case=abc');

// -- the link an admin sends ---------------------------------------------------------------------
const url = shareLinkUrl('https://broker.example/', 'tok_en-123', { case: 'v2-abc' });
assert.strictEqual(url, 'https://broker.example/#share=tok_en-123&case=v2-abc');
assert.strictEqual(shareLinkUrl('https://broker.example/', 'tok', {}), 'https://broker.example/#share=tok');
assert.strictEqual(shareLinkUrl('https://broker.example/', 'tok', null), 'https://broker.example/#share=tok');
assert.strictEqual(shareLinkUrl('https://b/', 'a b&c#d', {}), 'https://b/#share=a%20b%26c%23d', 'a token cannot break out of its place');
assert.strictEqual(routeParams(url.slice(url.indexOf('#'))).get('share'), 'tok_en-123', 'what the page reads back');
assert.deepStrictEqual(parse(url.slice(url.indexOf('#'))), { case: 'v2-abc' }, 'and the route that came with it');
// Nothing before the fragment: the server never sees the secret.
assert.ok(!url.slice(0, url.indexOf('#')).includes('tok'));

// -- the mail --------------------------------------------------------------------------------------
const m = shareMailto(url, 1900000000);
assert.ok(m.startsWith('mailto:?subject='));
assert.strictEqual((m.match(/&body=/g) || []).length, 1, 'the link\'s own & and # did not split the mail');
const fields = new URLSearchParams(m.slice('mailto:?'.length));
assert.match(fields.get('subject'), /read-only/);
const body = fields.get('body');
assert.ok(body.endsWith(url), 'the link is last, so a linkifying client has nothing after it to cut');
assert.ok(body.includes('\r\n'), 'CRLF, as RFC 6068 asks');
assert.match(body, /It works until .*March 1\d, 2030/);
assert.match(decodeURIComponent(shareMailto(url, null)), /until I take it back/);

console.log(`deep links: ${views.length} views round-trip; hostile fragments are dropped; ` +
            'a share secret never survives into a route; the link and the mail read back');
