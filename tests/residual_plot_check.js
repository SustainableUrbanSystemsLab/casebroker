// The residual chart's arithmetic and markup, executed as shipped.
//
// The chart is drawn from numbers a node sent, so what has to hold is not that
// one example looks right but that the odd ones cannot break it: a field the
// solver did not solve in some iteration (null), a residual of exactly zero (no
// logarithm), a series whose every value is null (a diverged solve), a single
// point, and field and direction names that are not what a node ought to send.
const fs = require('fs');
const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
const grab = (name) => {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error('not found: ' + name);
  let depth = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  throw new Error('unbalanced: ' + name);
};
// A `const` inside an eval is private to it; the page's own are file-level, so read them as var.
const line = (re) => { const m = src.match(re); if (!m) throw new Error('not found: ' + re); return m[0].replace(/^const /, 'var '); };

// What the functions below use, as the page defines it.
var window = { innerWidth: 1400 };
eval(line(/const isObj = .*;/));
eval(line(/const finite = .*;/));
eval(line(/const natural = .*;/));
eval(grab('esc'));
eval(line(/const RS_GEOM = \{[\s\S]*?\};/));
eval(line(/const RS_SLOT = .*;/));
var residualOff = new Map();
for (const f of ['residualGeom', 'residualSlot', 'residualDecades', 'residualDomain', 'residualTicks',
                 'residualPath', 'residualNearest', 'residualLast', 'residualValue', 'residualSvg',
                 'residualKeysHtml', 'residualInner']) eval(grab(f));

let bad = 0, checks = 0;
const fail = (m) => { bad++; console.log('MISMATCH ' + m); };
const check = (ok, m) => { checks++; if (!ok) fail(m); };
const same = (a, b, m) => check(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

// -- the decades --------------------------------------------------------------
same(residualDecades({ fields: { p: [1, 0.1, 3e-5, null], Ux: [0.5, 2e-6] } }), [-6, 0], 'a fall from 1 to 2e-6 spans 1e-6..1');
same(residualDecades({ fields: { p: [0.5, 0.4] } }), [-1, 1], 'values inside one decade still get two');
same(residualDecades({ fields: { p: [1.7, 0.2] } }), [-1, 1], 'a residual above 1 reaches the next decade up');
same(residualDecades({ fields: { p: [null, null], Ux: [0, -1] } }), null, 'nothing positive is nothing to draw');
same(residualDecades({ fields: {} }), null, 'no field at all');

// -- the x axis ---------------------------------------------------------------
same(residualDomain({ iterations: [1, 500, 853] }, 2000), [0, 2000], 'a running direction is drawn towards its cap, from 0');
same(residualDomain({ iterations: [1, 500, 853] }, 0), [0, 853], 'a finished one ends where it ended');
same(residualDomain({ iterations: [400, 900] }, 0), [400, 900], 'a series that does not start at 0 does not pretend to');
same(residualDomain({ iterations: [7] }, 0), [7, 8], 'one point still has a width');
same(residualDomain({ iterations: [1, 300] }, 100), [0, 300], 'a cap behind the data does not shrink it');

const ticks = residualTicks(0, 2000, 5);
same(ticks, [0, 500, 1000, 1500, 2000], 'ticks of 0..2000');
check(ticks.every((t) => !Object.is(t, -0)), 'no tick is negative zero (it prints as "-0")');
const odd = residualTicks(0, 853, 5);
check(odd.length >= 3 && odd.length <= 8 && odd.every((t) => t >= 0 && t <= 853), `ticks of 0..853 stay inside it: ${odd}`);
same(residualTicks(3, 3, 5), [3], 'a zero-width axis has its one tick');

// -- a field's line -----------------------------------------------------------
const px = (x) => x, py = (v) => -Math.log10(v);
const path = residualPath([1, 2, 3, 4, 5], [1, null, 0.1, 0.01, 0], px, py);
check(/^M1\.0 -?0\.0L1\.0 -?0\.0M3\.0 1\.0L4\.0 2\.0$/.test(path), `a gap ends a run, an isolated point is a dot, zero ends one: ${path}`);
same(residualPath([1, 2], [null, 0], px, py), '', 'nothing drawable is no path');
check(!/NaN|undefined|Infinity/.test(residualPath([1, 2, 3], [1e-300, 0.5, null], px, py)), 'no NaN in a path');

// -- the pointer --------------------------------------------------------------
const xs = [1, 14, 27, 400, 853];
same([0, 1, 9, 21, 300, 700, 900, 5000].map((x) => residualNearest(xs, x)), [0, 0, 1, 2, 3, 4, 4, 4], 'nearest point');
same(residualNearest([5], 99), 0, 'a series of one');
same(residualLast([0.5, 0.2, null, null]), 0.2, 'the last number skips trailing nulls');
same(residualLast([null]), null, 'a column with no number has no last');
same([residualValue(8.63e-5), residualValue(null), residualValue(undefined)], ['8.63e-5', '—', '—'], 'legend values');

// -- colours ------------------------------------------------------------------
const names = ['Ux', 'Uy', 'Uz', 'epsilon', 'k', 'p'];
same(names.map((n) => residualSlot(names, n)), [1, 2, 3, 6, 5, 4], 'each field the wind cases solve keeps its colour');
same([residualSlot(['Ux', 'omega', 'nuTilda'], 'nuTilda'), residualSlot(['Ux', 'omega', 'nuTilda'], 'omega')], [7, 8], 'others take the pink and the grey in turn');
check(Number.isInteger(residualSlot(['constructor'], 'constructor')), 'a field named like an Object property is not a colour');

// -- the chart ----------------------------------------------------------------
const series = {
  iterations: [1, 100, 200, 300, 400],
  fields: { Ux: [1, 0.1, 0.01, 1e-3, 1e-4], p: [1, 0.5, 0.2, 0.1, null], k: [0.9, null, null, null, null] },
};
const svg = residualSvg(series, { id: 'v2-abc', toEnd: 2000 });
check(svg.startsWith('<svg') && svg.endsWith('</svg>'), 'one svg');
check((svg.match(/class="rs-line"/g) || []).length === 3, 'a line per field with a drawable point');
check((svg.match(/<text/g) || []).length >= 5 + 3, 'decade labels and iteration labels');
check(/data-case="v2-abc"/.test(svg) && /data-x0="0"/.test(svg) && /data-x1="2000"/.test(svg), 'the pointer handler can map a pixel back to an iteration');
check(!/NaN|undefined|Infinity/.test(svg), 'no NaN in the markup');
const hidden = residualSvg(series, { id: 'x', off: new Set(['Ux']) });
check((hidden.match(/class="rs-line"/g) || []).length === 2, 'a switched-off field is not drawn');
check(!/<circle/.test(svg) && /<circle/.test(residualSvg(series, { id: 'x', dots: true })), 'dots only for a sparse series');
same(residualSvg({ iterations: [1, 2], fields: { p: [null, null] } }, { id: 'x' }), '', 'an all-null series draws nothing, for the caller to explain');
const none = residualSvg({ iterations: [1, 2], fields: { p: [0.5, 0.1] } }, { id: 'x', off: new Set(['p']) });
check(none.startsWith('<svg') && !/rs-line/.test(none), 'every field switched off leaves the axes');
check(!/<script|onerror/i.test(residualSvg(series, { id: '"><script>alert(1)</script>' })), 'the case id is escaped');
window.innerWidth = 375;
check(/viewBox="0 0 250 165"/.test(residualSvg(series, { id: 'x' })), 'a phone gets the smaller canvas');
window.innerWidth = 1400;

// -- what a node's strings can do ----------------------------------------------
const evil = '<img src=x onerror=alert(1)>';
const keys = residualKeysHtml({ iterations: [1], fields: { [evil]: [0.5], p: [0.1] } }, new Set());
check(!/<img/.test(keys) && keys.includes('&lt;img'), 'a field name is escaped in the legend');
const rec = { case_id: 'v2-abc', state: 'leased' };
const inner = residualInner(rec, { current: evil, finished: { [evil]: { status: evil } } }, {
  direction: evil, directions: [{ direction: evil, source: 'trace', n: 1, end_time: 2000 }, { direction: 'case_000', source: 'trace', n: 1 }],
  series: { iterations: [1], fields: { p: [0.5] }, complete: false },
});
check(!/<img/.test(inner), 'a direction name and its status are escaped in the picker and the legend');
check(/<select class="rs-dir"/.test(inner) && /selected/.test(inner), 'with several directions there is a picker');
const empty = residualInner(rec, null, { direction: null, directions: [], series: null });
check(/No residual history yet/.test(empty) && !/<svg/.test(empty), 'a live case with no series says so');
check(/sent none/.test(residualInner({ case_id: 'x', state: 'done' }, null, { directions: [], series: null })), 'a finished case with none says that instead');
const coarse = residualInner(rec, { current: 'case_000' }, {
  direction: 'case_000', directions: [{ direction: 'case_000', source: 'reports', n: 3, end_time: 2000 }],
  series: { iterations: [10, 20, 30], fields: { p: [0.9, 0.5, 0.3] }, complete: false },
});
check(/solve reports/.test(coarse) && /<circle/.test(coarse), 'a series made of reports says so and shows its points');
check(/iteration 30 \/ 2,000/.test(coarse), 'a running direction shows its cap');
check(!/ \/ 2,000/.test(residualInner({ case_id: 'x', state: 'done' }, { current: null }, {
  direction: 'case_000', directions: [{ direction: 'case_000', source: 'trace', n: 3, end_time: 2000 }],
  series: { iterations: [10, 20, 30], fields: { p: [0.9, 0.5, 0.3] }, total: 900, complete: true },
}).replace(/data-rest="[^"]*"/, '')), 'a finished direction does not show a cap it did not run to');

console.log(bad === 0 ? `all ${checks} residual chart cases hold` : `${bad} of ${checks} mismatches`);
process.exit(bad === 0 ? 0 : 1);
