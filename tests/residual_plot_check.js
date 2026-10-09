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
// How a direction ended is read from the node's verdict entries, which the page keeps per case.
var partsCache = new Map();
eval(grab('solveStatusClass'));
// The reading itself is one block between markers (its own check: tests/convergence_check.js).
eval(src.slice(src.indexOf('// convergence:pure begin'), src.indexOf('// convergence:pure end'))
  .replace(/^  const /gm, '  var '));
eval(grab('verdictsOf'));
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

// -- how the direction on show ended ------------------------------------------------
const finishedCase = { case_id: 'v2-abc', state: 'done' };
const finishedData = (fields) => ({
  // Two directions, so there is a picker and the picker has the status in it.
  direction: 'case_349', directions: [{ direction: 'case_349', source: 'reports', n: 10, end_time: 2000 },
                                      { direction: 'case_350', source: 'reports', n: 4, end_time: 2000 }],
  series: { iterations: [100, 500, 942], fields, complete: true },
});
const k = { p: [1e-2, 1e-3, 1.15e-4], epsilon: [5e-2, 1e-2, 7.3e-3] };
const solveDone = { current: null, finished: { case_349: { status: 'converged', iterations: 942 } } };
// Before the verdicts are known: the word the node sent, and no claim that there is no record.
let cs = residualInner(finishedCase, solveDone, finishedData(k));
check(/case_349 · converged</.test(cs) && /class="rs-verdict"/.test(cs), 'the picker and the note say what the solve report said');
check(/Stopped at iteration 942 of 2,000, so a stop criterion fired/.test(cs), 'stopped short of the cap says so, from the report alone');
check(!/No verdict record/.test(cs), 'a verdict still on its way is not "none"');
// With the verdict: the basis, in words, beside the curve it explains.
partsCache.set('v2-abc', { at: 0, verdicts: new Map([['case_349', {
  exit: 0, converged: true, met_residual_control: false, converged_by: 'fieldStationarity', verdict: 'converged',
  last_residuals: { p: 1.15e-4, epsilon: 7.3e-3 }, last_iteration: 942, end_time: 2000 }]]) });
cs = residualInner(finishedCase, solveDone, finishedData(k));
check(/case_349 · converged · shear</.test(cs), 'the picker names the basis');
check(/<span class="vbadge ok">converged · shear<\/span>/.test(cs), 'the badge');
check(/wall shear on every patch stopped moving/.test(cs) && /highest on a stop field: p 1\.15e-4/.test(cs),
  'the note says how it converged, and holds p, not epsilon, against the tolerance');
check(/epsilon is drawn but never stops a solve/.test(cs), 'epsilon is explained where it is drawn');
check(!/epsilon is drawn/.test(residualInner(finishedCase, solveDone, finishedData({ p: [1e-2, 1e-4] }))), 'and only where it is');
// A direction that merely reached the cap is not green.
partsCache.set('v2-abc', { at: 0, verdicts: new Map([['case_349', {
  exit: 0, converged: false, verdict: 'ended-without-meeting-tolerances', last_residuals: { p: 3.2e-3 }, last_iteration: 2000, end_time: 2000 }]]) });
cs = residualInner(finishedCase, solveDone, finishedData(k));
check(/<span class="vbadge warn">hit the cap<\/span>/.test(cs) && /case_349 · hit the cap</.test(cs), 'the cap is a warning, in the picker too');
// Loaded, and this direction has none.
partsCache.set('v2-abc', { at: 0, verdicts: new Map([['case_000', null]]) });
check(/No verdict record for this direction/.test(residualInner(finishedCase,
  { current: null, finished: { case_349: { status: 'plateaued', iterations: 942 } } }, finishedData(k))), 'loaded and absent says so');
check(/this node did not report which/.test(residualInner(finishedCase, solveDone, finishedData(k))),
  'and for a "converged" it says what the report does show');
// Running: nothing to say yet.
check(!/rs-verdict/.test(residualInner({ case_id: 'v2-abc', state: 'leased' }, { current: 'case_349' }, finishedData(k))), 'a running direction has no verdict yet');
// Whatever a node sends is text.
partsCache.set('v2-abc', { at: 0, verdicts: new Map([['case_349', { converged: false, verdict: evil, plateau_field: evil, last_residuals: { [evil]: 1 } }]]) });
check(!/<img/.test(residualInner(finishedCase, { current: null, finished: { case_349: { status: evil } } }, finishedData(k))), 'a verdict a node made up is escaped');

// -- the tolerance it ran with, as a dashed line ------------------------------------------
const tolY = (svg) => [...svg.matchAll(/<line class="rs-tol"[^>]*y1="([\d.]+)"/g)].map((m) => Number(m[1]));
// Every curve above the tolerance (the case that prompted this: p stopped at 1.15e-4 over 1e-4,
// k at 1e-3 over 1e-5): the axis reaches down to it, so the line is drawn inside the frame.
const above = { iterations: [1, 500, 942], fields: { p: [1, 1e-2, 1.15e-4] } };
same(residualDecades(above), [-4, 0], 'the curves alone end at 1e-4');
const drawn = residualSvg(above, { id: 'x', tol: [{ value: 1e-5, fields: ['k'] }, { value: 1e-6, fields: ['U'] }] });
const ys = tolY(drawn), g = residualGeom();
same(ys.length, 2, 'one dashed line per tolerance');
check(ys.every((y) => y >= g.T && y <= g.H - g.B), 'inside the frame, though every curve stayed above them');
check(ys[1] > ys[0], 'the tighter tolerance lower down');
check(/class="rs-tol-label"[^>]*>1e-5 on k</.test(drawn) && />1e-6 on U</.test(drawn), 'each says what it is');
same(tolY(residualSvg(above, { id: 'x' })).length, 0, 'none without a tolerance');
same(tolY(residualSvg(above, { id: 'x', tol: [{ value: 0, fields: [] }, { value: -1, fields: [] }] })).length, 0, 'nor for a nonsense one');
check(!/<script/.test(residualSvg(above, { id: 'x', tol: [{ value: 1e-4, fields: ['<script>'] }] })), 'a field name is escaped');
// From the verdict entry, on the finished direction's chart and in its note.
partsCache.set('v2-abc', { at: 0, verdicts: new Map([['case_349', {
  exit: 0, converged: true, met_residual_control: false, converged_by: 'fieldStationarity', verdict: 'converged',
  residual_control: { tolerance: 1e-4, fields: ['p', 'U', 'k'] },
  last_residuals: { p: 1.15e-4, epsilon: 7.3e-3 }, last_iteration: 942, end_time: 2000 }]]) });
cs = residualInner(finishedCase, solveDone, finishedData(k));
same(tolY(cs).length, 1, 'the chart draws it');
check(/residual tolerance of 1e-4 on p, U and k was not met \(highest on a stop field: p 1\.15e-4\)/.test(cs), 'the note holds p against it');
check(/dashed: the tolerance it ran with/.test(cs), 'the basis line names the dashes');
check(tolY(residualInner({ case_id: 'v2-abc', state: 'leased' }, { current: 'case_349' }, finishedData(k))).length === 0,
  'a running direction has no verdict, so no line');
partsCache.clear();

console.log(bad === 0 ? `all ${checks} residual chart cases hold` : `${bad} of ${checks} mismatches`);
process.exit(bad === 0 ? 0 : 1);
