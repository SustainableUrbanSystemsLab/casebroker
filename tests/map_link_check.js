// Runs the SHIPPED map-link code -- sliced out of dashboard.html, not copied -- on a small
// synthetic site, and reads the link back the way geojson.io does. Exits non-zero on the first
// disagreement. Driven by tests/test_map_link.py.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const from = html.indexOf('const CORE_HALF_M');
const to = html.indexOf('  function placeHtml(');
if (from < 0 || to < 0) throw new Error('map-link block not found in dashboard.html');
const block = html.slice(from, to);

function load(src) {
  const ctx = { GEO_KEEP: 16, settledGeometry: () => null, document: {}, CSS: { escape: (s) => s } };
  vm.createContext(ctx);
  vm.runInContext(src + '\nthis.footprintMapUrl = footprintMapUrl;', ctx);
  return ctx.footprintMapUrl;
}
const footprintMapUrl = load(block);

// geojson.io: the fragment is read as a query string (one decode, "+" is a space, "&" splits),
// then the data: URL inside it is fetched, which decodes again and ends the data at a "#".
function readBack(url) {
  const hash = url.slice(url.indexOf('#') + 1);
  const data = new URLSearchParams(hash).get('data');
  assert.ok(data && data.startsWith('data:application/json,'), 'the link carries a data: URL');
  let body = data.slice('data:application/json,'.length);
  const cut = body.indexOf('#');
  if (cut >= 0) body = body.slice(0, cut);
  return JSON.parse(decodeURIComponent(body));
}

const lat0 = 22.5, lon0 = 88.3;
const my = 110540, mx = 111320 * Math.cos(lat0 * Math.PI / 180);
const ll = (x, y) => [+(lon0 + x / mx).toFixed(6), +(lat0 + y / my).toFixed(6)];
const box = (x, y, w, h) => [ll(x, y), ll(x + w, y), ll(x + w, y + h), ll(x, y + h), ll(x, y)];
// A 6 x 6 canopy raster over +-60 m: a 2 x 2 block of 12 m trees in the NORTH-WEST corner, one
// 25 m tree, and a 1 m shrub the model's own 2 m cut leaves out.
const n = 6, grid = new Array(n * n).fill(0);
grid[0 * n + 0] = grid[0 * n + 1] = grid[1 * n + 0] = grid[1 * n + 1] = 12;
grid[4 * n + 4] = 25;
grid[5 * n + 0] = 1;
const geo = {
  source: 'globalbuildingatlas', centre: [lat0, lon0],
  features: [
    { type: 'Feature', properties: { h: 6, v: 2 }, geometry: { type: 'Polygon', coordinates: [box(-40, -40, 10, 10)] } },
    // A courtyard block: the hole has to survive.
    { type: 'Feature', properties: { h: 15, v: 4 }, geometry: { type: 'Polygon', coordinates: [box(0, 0, 30, 30), box(10, 10, 10, 10)] } },
    { type: 'Feature', properties: { h: 55, v: 30 }, geometry: { type: 'MultiPolygon', coordinates: [[box(20, -40, 8, 8)], [box(35, -40, 8, 8)]] } },
    { type: 'Feature', properties: { h: null, v: null }, geometry: { type: 'Polygon', coordinates: [box(-50, 20, 6, 6)] } },
  ],
  canopy: { source: 'meta-wri-chm-v1', n, half_m: 60, min_canopy_m: 2, grid,
            vegetation: { label: 'Ficus (banyan)', f_per_m: 0.75 } },
};

// -- the bare link: what a case shows before its geometry is read ------------------------
const bare = footprintMapUrl(lat0, lon0, true, null);
const bareFc = readBack(bare.url);
assert.deepStrictEqual(bareFc.features.map((f) => f.properties.name),
  ['mesh domain (cylinder, r = 1,300 m)', 'sampled core (1,008 m)', 'site centre']);
assert.strictEqual(bareFc.features[0].properties.stroke, '#d97706', 'a colour survives both decodes');
assert.strictEqual(bareFc.features[1].properties['fill-opacity'], 0.12, 'the core keeps its wash when it is empty');
assert.ok(!/buildings/.test(bare.title), bare.title);

// A name outside ASCII (the box domain's) survives both decodes too.
assert.strictEqual(readBack(footprintMapUrl(lat0, lon0, false, null).url).features[0].properties.name,
  'mesh domain (±1,300 m)');

// -- the full link ----------------------------------------------------------------------------
const full = footprintMapUrl(lat0, lon0, true, geo);
const fc = readBack(full.url);
const byName = (re) => fc.features.filter((f) => re.test(f.properties.name));
assert.deepStrictEqual(fc.features.map((f) => f.properties.name), [
  'mesh domain (cylinder, r = 1,300 m)',
  'tree canopy 10-20 m (4 cells)', 'tree canopy 20 m and taller (1 cell)',
  'buildings under 10 m (1)', 'buildings 10-20 m (1)', 'buildings 40 m and taller (2)',
  'buildings without a height (1)',
  'sampled core (1,008 m)', 'site centre',
]);
assert.match(full.title, /with 4 buildings and the tree canopy/);
assert.strictEqual(byName(/^sampled core/)[0].properties['fill-opacity'], 0,
  'an outline once there is something inside it');

// The 2 x 2 block is ONE rectangle, and row 0 of the raster is its NORTH edge.
const block4 = byName(/^tree canopy 10-20/)[0].geometry.coordinates;
assert.strictEqual(block4.length, 1, 'adjacent cells merge into one polygon');
const ring = block4[0][0];
const step = 120 / n;
assert.deepStrictEqual(ring[0], ll(-60, 60 - 2 * step), 'south-west corner of the block');
assert.deepStrictEqual(ring[2], ll(-60 + 2 * step, 60), 'north-east corner: the top of the raster');
assert.deepStrictEqual(ring[0], ring[ring.length - 1], 'rings are closed');
assert.match(byName(/^tree canopy/)[0].properties.vegetation, /Ficus \(banyan\), f = 2 Cd LAD = 0.75/);

// The courtyard is still a hole; the MultiPolygon building contributes both of its parts.
const mid = byName(/^buildings 10-20/)[0].geometry.coordinates;
assert.strictEqual(mid[0].length, 2, 'outer ring and courtyard');
assert.strictEqual(byName(/^buildings 40 m/)[0].geometry.coordinates.length, 2);
assert.strictEqual(byName(/^buildings without/)[0].properties['fill-opacity'], 0, 'an untagged footprint is an outline');
for (const f of byName(/^buildings/)) {
  assert.strictEqual(f.properties.heights, 'GlobalBuildingAtlas LoD1, predicted', 'a prediction is labelled one');
}

// Nothing the query-string pass would split or blank reaches it raw.
const hash = full.url.slice(full.url.indexOf('#') + 1);
assert.ok(!/[&+#%](?![0-9A-F]{2})/.test(hash.replace(/%25/g, '')), 'no bare & + # or stray % in the fragment');

// -- over budget: the buildings go, and the link says so -----------------------------------
const tight = load(block.replace(/const MAP_URL_BUDGET = \d+;/, 'const MAP_URL_BUDGET = 4000;'));
const cut = tight(lat0, lon0, true, geo);
const cutFc = readBack(cut.url);
assert.ok(cut.url.length < full.url.length);
assert.deepStrictEqual(cutFc.features.map((f) => f.properties.name).filter((s) => /^buildings/.test(s)), []);
assert.strictEqual(cutFc.features.filter((f) => /^tree canopy/.test(f.properties.name)).length, 2, 'the canopy stays');
assert.match(cutFc.features[0].properties.note, /4 buildings left out/);
assert.match(cut.title, /4 buildings left out/);

console.log(`map link: ${fc.features.length} features, ${full.url.length} characters; ` +
            'the domain, the canopy and the buildings read back as geojson.io reads them');
