import { test } from 'node:test';
import assert from 'node:assert/strict';
import { predatorPreyStation } from './weatherstation-predatorprey.js';

test('station exposes id, label and fields', () => {
  assert.equal(predatorPreyStation.id, 'predatorprey');
  assert.equal(predatorPreyStation.label, 'Predator-Prey Ecosystem');
  assert.deepEqual(predatorPreyStation.fields, ['prey', 'predators']);
});

test('create/tick/series work end to end', () => {
  const sim = predatorPreyStation.create(11);
  const before = sim.history.length;
  const snap = predatorPreyStation.tick(sim);
  assert.ok(Number.isFinite(snap.prey) && Number.isFinite(snap.predators));
  assert.equal(predatorPreyStation.series(sim).length, before + 1);
});
