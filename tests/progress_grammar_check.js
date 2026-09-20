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
eval(grab('progressFraction'));
eval(grab('progressPhase'));

// The exact strings MetaFOAM.Lib.Tests/TestNodeProgress.cs pins on the writer side.
const cases = [
  ['mesh 3/5 · 03_snappyHexMesh', 0.6, 'mesh'],
  ['solve 3/8 dirs · iter 412/2000', (3 + 412/2000) / 8, 'solve'],
  ['solve 0/8 dirs · starting', 0, 'solve'],
  ['solve 9/8 dirs · iter 2100/2000', 1, 'solve'],
  ['mesh 2/4', 0.5, 'mesh'],
  ['site geometry', null, ''],
  ['build-case', null, ''],
  ['archiving', null, ''],
  ['alive', null, ''],
  ['mesh x/y', null, 'mesh'],
  ['solve 3/0 dirs', null, 'solve'],
];
// Shapes the current writer no longer emits, but that workers in the field still
// send: a node built before the grammar existed, and runner/run_case.sh, which is
// what every cluster worker runs. Both carry a real count, so both get a bar.
// JS-only on purpose -- the C# side writes one format and need not read these.
const legacy = [
  ['step 3/5: 03_snappyHexMesh', 0.6],
  ['case_270 [3/8 dirs] iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)', 3 / 8],
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
console.log(bad === 0 ? `all ${cases.length + legacy.length} strings agree with the C# writer` : `${bad} mismatches`);
process.exit(bad === 0 ? 0 : 1);
