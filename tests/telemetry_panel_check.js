// The telemetry card's formatters, executed as shipped.
//
// These are time-direction and magnitude questions, which is exactly where a
// string assertion in Python would prove nothing: the bug being pinned here was
// a formatter that RAN correctly and answered the wrong question.
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
eval(grab('relTime'));
eval(grab('formatDuration'));

let bad = 0;
const fail = (m) => { bad++; console.log('MISMATCH ' + m); };
const now = Math.floor(Date.now() / 1000);

// -- a future instant is named, and named DIFFERENTLY at different distances --
// lease_expires is `now + lease_seconds`; the old formatter answered "just now"
// for every one of these, so a lease one second from lapsing and one with a day
// left printed the same string.
const ahead = [[1, '+1s'], [1200, '+20m'], [3540, '+59m'], [7200, '+2h'], [3 * 86400, '+3d']];
const seen = new Set();
for (const [d, label] of ahead) {
  const got = relTime(now + d);
  if (!/^in /.test(got)) fail(`${label} -> ${JSON.stringify(got)}; a future instant must read "in ..."`);
  if (/ago/.test(got)) fail(`${label} -> ${JSON.stringify(got)} says "ago" about the future`);
  seen.add(got);
}
if (seen.size !== ahead.length) fail(`the five future distances collapse to ${seen.size} distinct strings: ${[...seen]}`);

// -- and a lapsed lease is a real, different state ---------------------------
for (const [d, label] of [[-30, '-30s'], [-1200, '-20m'], [-32400, '-9h'], [-3 * 86400, '-3d']]) {
  const got = relTime(now + d);
  if (!/ ago$/.test(got)) fail(`${label} -> ${JSON.stringify(got)}; a past instant must read "... ago"`);
  if (/^in /.test(got)) fail(`${label} -> ${JSON.stringify(got)} says "in" about the past`);
}

// -- the seven past-valued call sites must not have changed shape ------------
// relTime is called for first_seen/last_seen/updated_at/last_progress_at and
// three more, all genuinely past. Their rendering is load-bearing elsewhere.
for (const [d, want] of [[-45, '45s ago'], [-3600, '1h ago'], [-86400 * 2, '2d ago']]) {
  const got = relTime(now + d);
  if (got !== want) fail(`past rendering changed: ${d}s -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// -- durations are CFD solves, not coffee breaks -----------------------------
// "480m 0s" for an 8-hour foamRun is not a duration anybody reads.
const durations = [[45, '45 s'], [90, '1m 30s'], [3599, '59m 59s'],
                   [3600 * 8, '8h 0m'], [3600 * 26, '1d 2h'], [86400 * 2, '2d 0h']];
for (const [sec, want] of durations) {
  const got = formatDuration(sec);
  if (got !== want) fail(`formatDuration(${sec}) -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
if (/^\d{3,}m/.test(formatDuration(3600 * 8))) fail('an 8-hour solve still renders as raw minutes');
if (formatDuration(null) !== '—') fail('a missing duration must stay an em dash');

console.log(bad === 0
  ? `all ${ahead.length + 4 + 3 + durations.length} telemetry formatter cases hold`
  : `${bad} mismatches`);
process.exit(bad === 0 ? 0 : 1);
