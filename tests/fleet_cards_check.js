// Exercises the SHIPPED fleetCards out of dashboard.html: the strip that claims
// to be the worker fleet.
const fs = require('fs');
const src = fs.readFileSync('casebroker/static/dashboard.html', 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const i = src.indexOf('function fleetCards(');
let d = 0, end = i;
for (let k = src.indexOf('{', i); k < src.length; k++) {
  if (src[k] === '{') d++; else if (src[k] === '}') { d--; if (d === 0) { end = k + 1; break; } }
}
eval(src.slice(i, end));

const NOW = 1_000_000;
let bad = 0;
const check = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) { bad++; console.log(`MISMATCH ${label}: ${JSON.stringify(got)} want ${JSON.stringify(want)}`); }
};

// A workstation solving a case had no card at all before this.
let cards = fleetCards(
  [{ worker_id: 'foam', cluster: null, current_case: 'v2-1', last_seen: NOW - 10 }],
  [], NOW);
check('workstation appears', cards.map(c => [c.name, c.working, c.idle, c.queued]),
      [['Workstations', 1, 0, null]]);
check('workstation is not stale', cards[0].stale, false);

// Two workstations, one idle: counted together, not one card each.
cards = fleetCards([
  { worker_id: 'foam', cluster: null, current_case: 'v2-1', last_seen: NOW - 10 },
  { worker_id: 'ws2', cluster: null, current_case: null, last_seen: NOW - 20 },
], [], NOW);
check('workstations aggregate', [cards.length, cards[0].working, cards[0].idle], [1, 1, 1]);

// A cluster that reports a queue keeps it, and its live workers are observed.
cards = fleetCards(
  [{ worker_id: 'phoenix-1-2', cluster: 'Phoenix', current_case: 'v2-9', last_seen: NOW - 30 }],
  [{ cluster: 'Phoenix', running: 4, queued: 3, detail: '', age_seconds: 60 }], NOW);
check('cluster keeps its queue', cards.map(c => [c.name, c.working, c.queued, c.stale]),
      [['Phoenix', 1, 3, false]]);

// A cluster whose report is 11 days old is stale, and still listed.
cards = fleetCards([], [{ cluster: 'ICE', running: 4, queued: 3, detail: '', age_seconds: 273 * 3600 }], NOW);
check('stale cluster stays visible', [cards[0].name, cards[0].stale, cards[0].queued], ['ICE', true, 3]);
// Nobody has leased anything, but the scheduler says 4 are running: only it knows.
check('reported running survives with no workers', cards[0].working, 4);

// Both kinds at once, busiest first.
cards = fleetCards([
  { worker_id: 'foam', cluster: null, current_case: null, last_seen: NOW - 10 },
  { worker_id: 'p1', cluster: 'Phoenix', current_case: 'v2-1', last_seen: NOW - 10 },
  { worker_id: 'p2', cluster: 'Phoenix', current_case: 'v2-2', last_seen: NOW - 10 },
], [{ cluster: 'Phoenix', running: 2, queued: 7, detail: '', age_seconds: 30 }], NOW);
check('both kinds, busiest first', cards.map(c => c.name), ['Phoenix', 'Workstations']);
check('idle workstation still counted', cards[1].idle, 1);

// A workstation nobody has heard from in an hour reads stale, by the same 300 s
// rule the Workers table's Offline badge uses -- two panels must not disagree.
cards = fleetCards([{ worker_id: 'foam', cluster: null, current_case: null, last_seen: NOW - 3600 }], [], NOW);
check('quiet workstation is stale', cards[0].stale, true);

// Nine working and two gone for 16 h: the gone ones are OFFLINE, not idle -- the
// Workers table's Offline badge (300 s) and this card must say the same thing.
cards = fleetCards([
  { worker_id: 'foam', cluster: null, current_case: 'v2-1', last_seen: NOW - 20 },
  { worker_id: 'ws2', cluster: null, current_case: null, last_seen: NOW - 30 },
  { worker_id: 'gone1', cluster: null, current_case: null, last_seen: NOW - 16 * 3600 },
  { worker_id: 'gone2', cluster: null, current_case: null, last_seen: NOW - 17 * 3600 },
], [], NOW);
check('offline is not idle', [cards[0].working, cards[0].idle, cards[0].offline], [1, 1, 2]);
check('a live worker keeps the card fresh', cards[0].stale, false);
// The boundary is the table's: < 300 s is Active, 300 s is Offline.
cards = fleetCards([{ worker_id: 'a', cluster: null, current_case: null, last_seen: NOW - 299 },
                    { worker_id: 'b', cluster: null, current_case: null, last_seen: NOW - 300 }], [], NOW);
check('offline boundary matches the table', [cards[0].idle, cards[0].offline], [1, 1]);

console.log(bad === 0 ? 'fleet strip: all cases agree' : `${bad} mismatches`);
process.exit(bad === 0 ? 0 : 1);
