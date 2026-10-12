import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PredatorPreyEcosystem,
  createEcosystem,
  runSimulation,
} from './predatorprey.js';

test('ecosystem entry point constructs and steps', () => {
  const sim = new PredatorPreyEcosystem({ width: 10, height: 10, seed: 1 });
  let last = sim.step();
  for (let i = 0; i < 5; i++) last = sim.step();
  assert.equal(last.step, 6);
  assert.ok(last.grass >= 0 && last.prey >= 0 && last.predator >= 0);
});

test('createEcosystem returns a stepping sim with history', () => {
  const sim = createEcosystem({ seed: 7, w: 8, h: 8 });
  const before = sim.history.length;
  sim.step();
  assert.equal(sim.history.length, before + 1);
});

test('runSimulation returns a deterministic series', () => {
  const a = runSimulation({ seed: 9, w: 10, h: 10 }, 10);
  const b = runSimulation({ seed: 9, w: 10, h: 10 }, 10);
  assert.deepEqual(a, b);
  assert.equal(a.length, 11);
});
