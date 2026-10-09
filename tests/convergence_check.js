// Runs the SHIPPED convergence-reading code -- sliced out of dashboard.html between its
// markers, not copied -- on the verdict entries a node really writes, and on the ones it
// used to. Exits non-zero on the first disagreement. Driven by tests/test_convergence_panel.py.
//
// The entries are the shapes MetaFOAM.Lib's ConvergenceCheck and Eddy3DCli's
// NativeCaseRunner.AddConvergenceEvidence produce (Eddy3D, TestConvergenceEvidence.cs).
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const from = html.indexOf('// convergence:pure begin');
const to = html.indexOf('// convergence:pure end');
if (from < 0 || to < 0 || to < from) throw new Error('convergence block not found in dashboard.html');
const block = html.slice(from, to);
// The two helpers the page defines once and every function in it shares: read from the
// page, so a change to them is a change to what is tested.
const helper = (name) => {
  const m = html.match(new RegExp('^  const ' + name + ' = .*$', 'm'));
  if (!m) throw new Error('the page no longer defines ' + name);
  return m[0];
};

const ctx = {};
vm.createContext(ctx);
vm.runInContext(helper('isObj') + '\n' + helper('finite') + '\n' + block +
  '\nthis.api = { convergenceOf, highestStopResidual, convergenceTally, residualTolerances, toleranceText, tolText };', ctx);
const plain = (x) => JSON.parse(JSON.stringify(x));      // objects made in the vm carry its prototypes
const { convergenceOf, highestStopResidual, convergenceTally, residualTolerances, toleranceText, tolText } = ctx.api;
const conv = (status, v) => plain(convergenceOf(status, v));

// What a node writes (NativeCaseRunner: exit, converged, met_residual_control, converged_by, verdict,
// then the evidence): the solver met residualControl ...
const byResidual = {
  exit: 0, converged: true, met_residual_control: true, converged_by: 'residualControl', verdict: 'converged',
  last_residuals: { Ux: 8.46e-7, Uy: 3.15e-7, Uz: 1.1e-5, p: 1.15e-5, k: 1.16e-6, epsilon: 7.3e-3 },
  worst_residual_field: 'epsilon', worst_residual: 7.3e-3, last_iteration: 942, end_time: 2000,
};
// ... or it did not, and the wall shear stopped moving (the case on the page that prompted this) ...
const byShear = {
  exit: 0, converged: true, met_residual_control: false, converged_by: 'fieldStationarity', verdict: 'converged',
  last_residuals: { Ux: 8.46e-7, Uy: 3.15e-7, Uz: 1.1e-5, p: 1.15e-4, k: 1.16e-6, epsilon: 7.3e-3 },
  worst_residual_field: 'epsilon', worst_residual: 7.3e-3, last_iteration: 942, end_time: 2000,
};
// ... or neither, and it merely reached the cap with residuals finite and not rising.
const atCap = {
  exit: 0, converged: false, met_residual_control: false, converged_by: null, verdict: 'ended-without-meeting-tolerances',
  last_residuals: { p: 3.2e-3, Ux: 8e-7, epsilon: 9e-3 }, last_iteration: 2000, end_time: 2000,
};
const plateaued = {
  exit: 3, converged: false, met_residual_control: false, converged_by: null, verdict: 'plateaued',
  last_residuals: { p: 3.2e-3, Ux: 8.1e-7 }, plateau_field: 'p', plateau_log_drop: 0.12, last_iteration: 1868, end_time: 2000,
};
const capHeld = {
  exit: 4, converged: false, met_residual_control: false, converged_by: null, verdict: 'diverging',
  max_u: 100, max_u_at_cap: true, u_cap: 100, last_iteration: 600, end_time: 2000,
};

// -- the one word "converged" is three things, and the page now says which ------------------------
const a = conv('converged', byResidual), b = conv('converged', byShear), c = conv('converged', atCap);
assert.deepStrictEqual([a.kind, b.kind, c.kind], ['converged', 'stationary', 'capped']);
assert.deepStrictEqual([a.label, b.label, c.label], ['converged', 'converged · shear', 'hit the cap']);
// The solve report carried "converged" for all three. That is the whole problem.
assert.match(a.why, /met its residual tolerance/);
assert.match(a.why, /iteration 942 of 2,000/);
assert.match(a.why, /epsilon never counts/, 'what the reader will otherwise take for an unconverged field');
assert.ok(!/highest/.test(a.why), 'a direction that met its tolerance needs no "how far off"');

assert.match(b.why, /tolerance was not met/);
assert.match(b.why, /highest on a stop field: p 1\.15e-4/, 'epsilon is not the field to point at');
assert.ok(!/epsilon 7\.30e-3/.test(b.why));
assert.match(b.why, /wall shear on every patch stopped moving/);
assert.match(b.why, /0\.1 %/);
assert.match(b.why, /iteration 942 of 2,000/);

assert.match(c.why, /iteration 2,000 of 2,000/);
assert.match(c.why, /Neither test was met/);
assert.match(c.why, /highest on a stop field: p 3\.20e-3/);
assert.match(c.why, /ended-without-meeting-tolerances/);

// -- the others ----------------------------------------------------------------------------------------
const p = conv('plateaued', plateaued);
assert.deepStrictEqual([p.kind, p.label], ['plateaued', 'plateaued']);
assert.match(p.why, /p dropped only 0\.12 decades/);
const d = conv('diverging', capHeld);
assert.deepStrictEqual([d.kind, d.label], ['diverging', 'diverging']);
assert.match(d.why, /velocity cap \(100 m\/s\) was still clipping/);
assert.match(conv('diverging', { exit: 4, verdict: 'diverging', last_residuals: { p: 3 } }).why, /Residuals rose/);
for (const word of ['stopped', 'running', 'unknown']) {
  const r = conv(word, { exit: 6, converged: false, verdict: word });
  assert.deepStrictEqual([r.kind, r.label], [word, word]);
  assert.ok(r.why.length > 0, word);
}

// -- no record of how: the node's own word, and no claim beyond it -----------------------------------------
for (const v of [null, undefined, 'x', [], 7]) {
  const r = conv('converged', v);
  assert.deepStrictEqual([r.kind, r.label, r.why, r.record], ['converged', 'converged', '', false], String(v));
}
assert.deepStrictEqual(conv(undefined, null).label, 'finished');
// What the solve report itself saw can still say something: stopped short of the cap, so a
// stop criterion fired -- but not which, and nothing is claimed about which.
const early = plain(convergenceOf('converged', null, { iterations: 942, cap: 2000 }));
assert.match(early.why, /Stopped at iteration 942 of 2,000, so a stop criterion fired/);
assert.match(early.why, /did not report which/);
const late = plain(convergenceOf('converged', null, { iterations: 2000, cap: 2000 }));
assert.match(late.why, /Ran to its cap, 2,000 iterations/);
assert.match(late.why, /reached the cap quietly/);
for (const seen of [{}, { iterations: 942 }, { cap: 2000 }, { iterations: 'x', cap: 0 }, null]) {
  assert.strictEqual(plain(convergenceOf('converged', null, seen)).why, '', JSON.stringify(seen));
}
assert.strictEqual(plain(convergenceOf('plateaued', null, { iterations: 100, cap: 2000 })).why, '', 'only "converged" is ever in doubt');
assert.deepStrictEqual(conv('', null).label, 'finished');
// A verdict that is only the old two keys: no basis recorded, so none is claimed.
const old = conv('converged', { verdict: 'converged' });
assert.deepStrictEqual([old.kind, old.label, old.why], ['converged', 'converged', '']);
// From before converged_by: converged, residualControl not met -- the wall shear is all that is left.
assert.strictEqual(conv('converged', { converged: true, met_residual_control: false, verdict: 'converged' }).kind, 'stationary');
assert.strictEqual(conv('converged', { converged: true, met_residual_control: true, verdict: 'converged' }).kind, 'converged');

// -- the highest residual among the fields that can stop a solve ------------------------------------------------
assert.strictEqual(highestStopResidual(byShear.last_residuals), 'p 1.15e-4', 'not epsilon, which floors near 1e-2');
assert.strictEqual(highestStopResidual({ epsilon: 7e-3 }), '', 'epsilon alone is no stop field');
assert.strictEqual(highestStopResidual({ omega: 2e-5, k: 1e-6 }), 'omega 2.00e-5', 'a k-omega model stops on omega');
assert.strictEqual(highestStopResidual({ p: null, Ux: 'x', k: 1e-6 }), 'k 1.00e-6', 'a residual that is not a number is skipped');
for (const bad of [null, undefined, 3, [], {}]) assert.strictEqual(highestStopResidual(bad), '');

// -- the case in one line: the directions to look at first ---------------------------------------------------------
const rows = [
  ['converged', byResidual], ['converged', byResidual], ['converged', byShear],
  ['converged', atCap], ['plateaued', plateaued], ['converged', null], ['converged', byResidual],
];
const tally = plain(convergenceTally(rows.map(([s, v]) => convergenceOf(s, v))));
assert.deepStrictEqual(tally.map((t) => [t.kind, t.n]), [
  ['plateaued', 1], ['capped', 1], ['stationary', 1], ['converged', 4],
], 'worst first; a direction with no record is counted with the word it carried');
assert.strictEqual(tally[1].label, 'hit the cap');
assert.deepStrictEqual(plain(convergenceTally([])), []);

// -- the tolerance the direction ran with (residual_control in its entry) ---------------------------------
const tols = (v) => plain(residualTolerances(v));
// One tolerance for every field, as WindRunSettings.ApplyResidualControl writes it today.
assert.deepStrictEqual(tols({ residual_control: { tolerance: 1e-4, fields: ['p', 'U', 'k'] } }),
  [{ value: 1e-4, fields: ['p', 'U', 'k'] }]);
// Per field, as a hand-edited fvSolution may say: grouped, loosest first, a pattern kept as written.
assert.deepStrictEqual(tols({ residual_control: { tolerance: { p: 1e-4, U: 1e-5, '(k|omega)': 1e-5 } } }),
  [{ value: 1e-4, fields: ['p'] }, { value: 1e-5, fields: ['U', '(k|omega)'] }]);
assert.deepStrictEqual(tols({ residual_control: { tolerance: 5e-4 } }), [{ value: 5e-4, fields: [] }]);
for (const v of [null, {}, { residual_control: null }, { residual_control: {} }, { residual_control: { tolerance: 0 } },
                 { residual_control: { tolerance: 'x' } }, { residual_control: { tolerance: { p: -1, U: null } } }, 7]) {
  assert.strictEqual(residualTolerances(v), null, JSON.stringify(v));
}
assert.strictEqual(toleranceText(residualTolerances({ residual_control: { tolerance: { p: 1e-4, U: 1e-5, k: 1e-5 } } })),
  '1e-4 on p; 1e-5 on U and k');
assert.strictEqual(toleranceText(null), '');
assert.deepStrictEqual([tolText(1e-4), tolText(1.5e-4), tolText(1e-3)], ['1e-4', '1.50e-4', '1e-3']);
// ... and in what the note says: "met" and "not met" mean little without the number.
const rc = { residual_control: { tolerance: 1e-4, fields: ['p', 'U', 'k'] } };
assert.match(conv('converged', { ...byResidual, ...rc }).why, /met its residual tolerance of 1e-4 on p, U and k at iteration 942/);
assert.match(conv('converged', { ...byShear, ...rc }).why,
  /residual tolerance of 1e-4 on p, U and k was not met \(highest on a stop field: p 1\.15e-4\)/);
assert.match(conv('converged', { ...atCap, ...rc }).why, /not the solver's residual tolerance of 1e-4 on p, U and k \(highest/);
assert.deepStrictEqual(conv('converged', { ...byShear, ...rc }).tolerances, [{ value: 1e-4, fields: ['p', 'U', 'k'] }]);
// Without it, what was said before.
assert.match(conv('converged', byResidual).why, /on p, U and k \(and omega in a k-omega model; epsilon never counts\)/);

console.log('convergence: ' + [a, b, c].map((x) => x.label).join(' | ') +
            ' are told apart; epsilon is never the field pointed at; a missing record claims nothing');
