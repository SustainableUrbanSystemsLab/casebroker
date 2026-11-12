// Runs the SHIPPED formatErrors (pulled out of dashboard.html) on a report shaped
// like the one pasted on 2026-11-12: many parked cases with one reason, a few real
// failures. The failures must come first and in full; the parked ones once.
const fs = require('fs');
const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
const i = src.indexOf('window.formatErrors = ');
const j = src.indexOf('window.copyAllErrors = ');
if (i < 0 || j < 0) throw new Error('formatErrors not found');
const window = {};
eval(src.slice(i, j));

const reason = 'misplaced: sampler-v3 scan mirrored this western tile across the equator';
const parked = Array.from({ length: 20 }, (_, k) => ({
  case_id: 'v2-p' + String(k).padStart(14, '0'), state: 'quarantined', attempts: 0, max_attempts: 3,
  lat: -40.7, lon: -73.9, city_cluster: 'c-46_-63', recipe: 'r', last_error: reason,
  updated_at: 1760906779, parked: true,
  history: [{ ts: 1760906779, event: 'quarantined', worker_id: null, detail: reason }],
}));
const failed = [{
  case_id: 'v2-f000', state: 'quarantined', attempts: 3, max_attempts: 3, lat: 1, lon: 2,
  city_cluster: 'c1_2', recipe: 'r', last_error: 'meshing exited 1: snappy 137', updated_at: 1760906000,
  parked: false, history: [{ ts: 1760906000, event: 'quarantined', worker_id: 'w1', host: 'h1', detail: 'meshing exited 1: snappy 137' }],
}];
const out = window.formatErrors({ total: 21, returned: 21, truncated: false, parked: 20,
                                   cases: [...parked.slice(0, 10), ...failed, ...parked.slice(10)] }, 'https://b');
const fail = (m) => { console.log(out); console.error('FAIL: ' + m); process.exit(1); };
if (!out.includes('21 case(s) with an error -- 1 failed, 20 parked by an operator')) fail('header');
if (out.split('=' .repeat(72)).length - 1 !== 2) fail('one block for the failure, one for the parked');
if (out.includes('20 x  misplaced') === false) fail('parked reason counted once');
if (out.indexOf('v2-f000') > out.indexOf('Parked by an operator')) fail('failures first');
const common = out.slice(out.indexOf('Most common'), out.indexOf('='.repeat(72)));
if (common.includes('misplaced')) fail('parked cases are not in the failure histogram');
for (const c of parked) if (out.split(c.case_id).length !== 2) fail('each parked id exactly once: ' + c.case_id);
// No parked case at all: the old paste, unchanged in shape.
const plain = window.formatErrors({ total: 1, returned: 1, truncated: false, cases: failed }, 'https://b');
if (plain.includes('parked')) fail('no parked section when nothing is parked');
console.log('errors paste ok');
