import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createCA, RULES } from './ca-engine.js';

test('block pattern is stable under Life', () => {
  const ca = createCA({ width: 6, height: 6 });
  ca.paint([[1, 1], [2, 1], [1, 2], [2, 2]]);
  for (let i = 0; i < 5; i++) ca.step();
  assert.equal(ca.population, 4);
});

test('blinker oscillates with period 2', () => {
  const ca = createCA({ width: 5, height: 5 });
  ca.paint([[1, 2], [2, 2], [3, 2]]);
  ca.step();
  assert.equal(ca.population, 3);
  ca.step();
  assert.equal(ca.population, 3);
  const after = [[1, 2], [2, 2], [3, 2]].every(([x, y]) => ca.getCell(x, y) === 1);
  assert.equal(after, true);
});

test('torus wraps neighbors across edges', () => {
  const ca = createCA({ width: 3, height: 3, torus: true });
  ca.paint([[0, 0], [2, 0], [0, 2]]);
  // Corner (2,2) has all three cells as torus neighbors.
  assert.equal(ca.countNeighbors(2, 2), 3);
  // Center has 3 neighbors too (corners of the grid).
  assert.equal(ca.countNeighbors(1, 1), 3);
});

test('bounded grid treats out-of-bounds as dead', () => {
  const ca = createCA({ width: 3, height: 3, torus: false });
  ca.paint([[0, 0], [2, 0], [0, 2]]);
  // (2,2) sees only in-bounds cells; the three painted corners are not its
  // Moore neighbors, so the count is 0 (no wrap, no phantom out-of-bounds).
  assert.equal(ca.countNeighbors(2, 2), 0);
  // Counting FROM out of bounds still sees live in-bounds neighbors: (0,0)
  // is a real diagonal neighbor of (-1,-1). Out-of-bounds cells themselves
  // are dead; that is what setCell/getCell enforce, not this count.
  assert.equal(ca.countNeighbors(-1, -1), 1);
  assert.equal(ca.getCell(-1, -1), 0);
});

test('setCell out of bounds on bounded grid is a no-op', () => {
  const ca = createCA({ width: 2, height: 2, torus: false });
  ca.setCell(5, 5, 1);
  assert.equal(ca.population, 0);
});

test('custom rule: birth-on-1 explodes from a single cell', () => {
  // Seeds (B2/S[]) does NOT explode from one cell: each ring-1 cell has
  // exactly one neighbor, which is not enough for a B2 birth. A custom
  // birth-on-1 rule does: the center dies (empty survive) and all eight
  // neighbors are born.
  const ca = createCA({ width: 7, height: 7, birth: [1], survive: [] });
  ca.paint([[3, 3]]);
  assert.equal(ca.getCell(3, 3), 1);
  ca.step();
  assert.equal(ca.population, 8);
  assert.equal(ca.generation, 1);
});

test('Seeds (B2/S[]) kills a lone cell and births nothing', () => {
  const ca = createCA({ width: 7, height: 7, ...RULES.seeds });
  ca.paint([[3, 3]]);
  ca.step();
  assert.equal(ca.population, 0);
  assert.equal(ca.generation, 1);
});

test('reset clears the grid and generation', () => {
  const ca = createCA({ width: 4, height: 4 });
  ca.paint([[0, 0]]);
  ca.step();
  ca.reset();
  assert.equal(ca.population, 0);
  assert.equal(ca.generation, 0);
});

test('reset accepts a pattern', () => {
  const ca = createCA({ width: 4, height: 4 });
  ca.reset([[1, 1]]);
  assert.equal(ca.population, 1);
  assert.equal(ca.getCell(1, 1), 1);
});

test('randomize honors density bounds and a seeded rng', () => {
  let s = 42;
  const rng = () => {
    s = (s * 1103515245 + 12345) % 2147483648;
    return s / 2147483648;
  };
  const ca = createCA({ width: 10, height: 10 });
  ca.randomize(0.5, rng);
  assert.ok(ca.population > 0 && ca.population < 100);
});

test('snapshot returns a serializable copy', () => {
  const ca = createCA({ width: 4, height: 4 });
  ca.paint([[0, 0]]);
  const snap = ca.snapshot();
  assert.equal(snap.width, 4);
  assert.equal(snap.population, 1);
  assert.equal(snap.cells.length, 16);
  assert.deepEqual(snap.cells, ca.snapshot().cells);
});

test('getCell wraps negative coordinates on torus', () => {
  const ca = createCA({ width: 3, height: 3 });
  ca.paint([[1, 1]]);
  assert.equal(ca.getCell(-2, -2), 1);
  assert.equal(ca.getCell(4, 4), 1);
  assert.equal(ca.getCell(0, 0), 0);
});
