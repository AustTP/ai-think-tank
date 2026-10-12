import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PredatorPreyEcosystem, EMPTY, GRASS, PREY, PREDATOR } from './predatorprey.js';

test('ecosystem constructs with initial populations', () => {
  const sim = new PredatorPreyEcosystem({ seed: 1 });
  const c = sim.counts();
  assert.equal(c.prey, 40);
  assert.equal(c.predator, 8);
  assert.equal(c.step, 0);
  assert.ok(c.grass > 0);
});

test('step advances and returns counts', () => {
  const sim = new PredatorPreyEcosystem({ seed: 2 });
  const before = sim.stepCount;
  const c = sim.step();
  assert.equal(sim.stepCount, before + 1);
  assert.equal(c.step, before + 1);
  assert.ok(c.prey >= 0 && c.predator >= 0 && c.grass >= 0);
});

test('simulation is deterministic for a fixed seed', () => {
  const a = new PredatorPreyEcosystem({ seed: 5 });
  const b = new PredatorPreyEcosystem({ seed: 5 });
  for (let i = 0; i < 30; i++) {
    assert.deepEqual(a.step(), b.step());
  }
});

test('predators eventually starve without prey', () => {
  const sim = new PredatorPreyEcosystem({ seed: 3, initialPrey: 0, initialPredators: 6, initialGrass: 0 });
  for (let i = 0; i < 60; i++) sim.step();
  assert.equal(sim.counts().predator, 0);
});

test('inBounds and neighbors respect grid edges', () => {
  const sim = new PredatorPreyEcosystem({ seed: 4, width: 3, height: 3 });
  assert.ok(sim.inBounds(0, 0));
  assert.ok(sim.inBounds(2, 2));
  assert.ok(!sim.inBounds(-1, 0));
  assert.ok(!sim.inBounds(0, 3));
  const nbrs = sim._neighbors(0);
  assert.equal(nbrs.length, 4);
});

test('cell type constants are stable', () => {
  assert.equal(EMPTY, 0);
  assert.equal(GRASS, 1);
  assert.equal(PREY, 2);
  assert.equal(PREDATOR, 3);
});
