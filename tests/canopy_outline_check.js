// Runs the SHIPPED canopy outline -- sliced out of dashboard.html, not copied -- on synthetic
// canopy grids. Exits non-zero on the first disagreement. Driven by tests/test_field_view_trees.py.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('casebroker/static/dashboard.html', 'utf8');
const slice = (name) => {
  const from = html.indexOf(`  function ${name}(`);
  if (from < 0) throw new Error(`${name} not found in dashboard.html`);
  return html.slice(from, html.indexOf('\n  }\n', from) + 4);
};
const ctx = {};
vm.createContext(ctx);
vm.runInContext(slice('wfCanopyOutline') + slice('wfCanopyAt')
  + '\nthis.outline = wfCanopyOutline; this.at = wfCanopyAt;', ctx);

const n = 6, half = 60, step = 2 * half / n;          // 20 m cells over +-60 m
const grid = (cells) => { const g = new Array(n * n).fill(0); for (const [r, c, h] of cells) g[r * n + c] = h; return g; };
const can = (cells) => ({ n, half_m: half, min_canopy_m: 2, grid: grid(cells) });
const centre = (r, c) => [-half + (c + 0.5) * step, half - (r + 0.5) * step];
const near = (a, b) => Math.abs(a - b) < 1e-9;

// Nothing to outline: no trees, a 1 m shrub under the model's own 2 m cut, no payload at all.
// (Lengths, not deepStrictEqual: the arrays come from another vm realm.)
assert.strictEqual(ctx.outline(can([])).length, 0);
assert.strictEqual(ctx.outline(can([[2, 2, 1]])).length, 0);
assert.strictEqual(ctx.outline(null).length, 0);

// One crown cell: a closed diamond around its centre, each corner midway to its bare
// neighbour -- half a cell out, however tall the crown.
const one = ctx.outline(can([[2, 3, 4]]));
assert.strictEqual(one.length, 4, 'four segments');
const [cx, cy] = centre(2, 3);
const ends = one.flat();
for (const [x, y] of ends)
  assert.ok((near(x, cx) && near(Math.abs(y - cy), step / 2)) || (near(y, cy) && near(Math.abs(x - cx), step / 2)),
    `corner ${x},${y} is half a cell from the crown, on an axis`);
// Closed: every end point is shared by exactly two segments.
const key = ([x, y]) => `${x.toFixed(6)},${y.toFixed(6)}`;
const count = new Map();
for (const p of ends) count.set(key(p), (count.get(key(p)) || 0) + 1);
assert.ok([...count.values()].every((k) => k === 2), 'the outline is closed');

// North is up: a crown in the NORTH-WEST corner cell is outlined at negative x, positive y --
// and, sitting on the grid's edge, still closes against the bare-ground padding.
const corner = ctx.outline(can([[0, 0, 12]]));
assert.strictEqual(corner.length, 4);
assert.ok(corner.flat().every(([x, y]) => x <= -40 + 1e-9 && y >= 40 - 1e-9), 'drawn in the north-west, within its cell');

// A 2 x 2 block is one outline around the block, not four around its cells.
const block = ctx.outline(can([[1, 1, 10], [1, 2, 10], [2, 1, 10], [2, 2, 10]]));
const bc = new Map();
for (const p of block.flat()) bc.set(key(p), (bc.get(key(p)) || 0) + 1);
assert.ok([...bc.values()].every((k) => k === 2), 'closed');
assert.ok(block.flat().every(([x, y]) => x > -60 && x < 20 && y > -20 && y < 60), 'only around the block');

// The tooltip's lookup: the crown's own cell, and bare ground next to it.
assert.strictEqual(ctx.at(can([[2, 3, 4]]), cx, cy), 4);
assert.strictEqual(ctx.at(can([[2, 3, 4]]), cx + step, cy), 0);
assert.strictEqual(ctx.at(can([[2, 3, 4]]), 500, 0), 0, 'outside the grid is no tree');

console.log('canopy outlines are closed, north up, and at the 2 m cut');
