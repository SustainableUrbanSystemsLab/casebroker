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
const esc = (s) => String(s);
eval(grab('progressLabel'));
eval(grab('progressParse'));
eval(grab('progressFraction'));
eval(grab('progressRunning'));
eval(grab('progressPhase'));
const SOLVER_STEP = /(foamrun|simplefoam|urbanmicroclimatefoam|foammultirun|potentialfoam)/i;

// The exact strings MetaFOAM.Lib.Tests/TestNodeProgress.cs pins on the writer side.
// A step count names the step RUNNING, so the work behind it is i-1 of n; a
// "dirs" count is directions FINISHED, so it is i of n. Reading the first as the
// second is what drew a full bar the instant a multi-hour solve began.
const cases = [
  ['mesh 3/5 · 03_snappyHexMesh', 0.4, 'mesh'],
  ['solve 3/8 dirs · iter 412/2000', (3 + 412/2000) / 8, 'solve'],
  ['solve 0/8 dirs · starting', 0, 'solve'],
  ['solve 9/8 dirs · iter 2100/2000', 1, 'solve'],
  ['mesh 2/4', 0.25, 'mesh'],
  ['site geometry', null, ''],
  ['build-case', null, ''],
  ['archiving', null, ''],
  ['alive', null, ''],
  ['mesh x/y', null, 'mesh'],
  ['solve 3/0 dirs', null, 'solve'],
  // Eddy3D ae59812f (#938): the solve line names the direction, phase and rung after the
  // iteration -- read off the log being written, because the old line sat on
  // 'solve 0/32 dirs · starting' for five hours while the solver was at 1,594. The
  // iteration stays the FIRST pair after the separator; the stage text never has one.
  ['solve 0/32 dirs · case_000 iter 1594/2000 · main, rung 2 (default)', (0 + 1594/2000) / 32, 'solve'],
  ['solve 5/32 dirs · case_056 iter 212/2000 · warm-up to 400, rung 1 (fast)', (5 + 212/2000) / 32, 'solve'],
  ['solve 5/32 dirs · case_056 starting · main, rung 3 (robust)', 5 / 32, 'solve'],
  ['solve · progress unreadable (IOException)', null, 'solve'],
];
// Shapes the current writer no longer emits, but that workers in the field still
// send: a node built before the grammar existed, and runner/run_case.sh, which is
// what every cluster worker runs. Both carry a real count, so both get a bar.
// JS-only on purpose -- the C# side writes one format and need not read these.
const legacy = [
  ['step 3/5: 03_snappyHexMesh', 0.4],
  ['case_270 [3/8 dirs] iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)', 3 / 8],
  // The line the field reported. One step of two really is finished, so the bar
  // fills to there and the second step is drawn as the ACTIVE unit -- neither a
  // full bar (confidently wrong) nor no bar at all (throws away a true number).
  ['step 2/2: 01_foamRun', 0.5],
  ['step 1/1: 01_urbanMicroclimateFoam', 0],
  // A newer node counts iterations, and then nothing is unknown.
  ['solve 3/8 dirs · iter 412/2000', (3 + 412 / 2000) / 8],
];
let bad = 0;
for (const [line, want] of legacy) {
  const got = progressFraction(line);
  if (want === null ? got !== null : Math.abs(got - want) > 1e-9) {
    bad++; console.log(`LEGACY MISMATCH ${JSON.stringify(line)} -> ${got} (want ${want})`);
  }
}

for (const [line, want, phase] of cases) {
  const got = progressFraction(line);
  const ok = want === null ? got === null : Math.abs(got - want) < 1e-9;
  const pOk = progressPhase(line) === phase;
  if (!ok || !pOk) { bad++; console.log(`MISMATCH ${JSON.stringify(line)} -> ${got} (want ${want}), phase ${progressPhase(line)} (want ${phase})`); }
}
// The ACTIVE span: how much of the bar is the unit under way. Mirrors
// NodeProgress.RunningSpanOf, and it is what replaced suppressing the bar.
const running = [
  ['step 2/2: 01_foamRun', 0.5],
  ['mesh 3/5 · 03_snappyHexMesh', 0.2],
  ['step 1/1: 01_urbanMicroclimateFoam', 1],
  // Nothing is under way once the line says how far into it we are.
  ['solve 3/8 dirs · iter 412/2000', 0],
  ['alive', 0],
];
for (const [line, want] of running) {
  const got = progressRunning(line);
  if (Math.abs(got - want) > 1e-9) { bad++; console.log(`RUNNING MISMATCH ${JSON.stringify(line)} -> ${got} (want ${want})`); }
}

// The words beside the bar. A step whose program IS the solver says "Solving",
// which is the one thing "step 2/2" alone does not tell an operator.
const labels = [
  ['step 2/2: 01_foamRun', 'Solving · step 2/2 · foamRun'],
  ['mesh 3/5 · 03_snappyHexMesh', 'Meshing · step 3/5 · snappyHexMesh'],
  ['solve 3/8 dirs · iter 412/2000', 'Solving · dir 4/8 · iter 412/2000'],
  ['solve 0/8 dirs · starting', 'Solving · dir 1/8'],
  // The last direction must not read as a ninth.
  ['solve 8/8 dirs · starting', 'Solving · dir 8/8'],
];
for (const [line, want] of labels) {
  const got = progressParse(line).label;
  if (got !== want) { bad++; console.log(`LABEL MISMATCH ${JSON.stringify(line)} -> ${JSON.stringify(got)} (want ${JSON.stringify(want)})`); }
}

console.log(bad === 0
  ? `all ${cases.length + legacy.length + running.length + labels.length} strings agree with the C# writer`
  : `${bad} mismatches`);
process.exit(bad === 0 ? 0 : 1);
