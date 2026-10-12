import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBoidsStation } from './weatherstation-boids.js';

test('station steps and reports state', () => {
  const station = createBoidsStation({ count: 4, width: 50, height: 50 });
  assert.equal(station.name, 'boids');
  station.step();
  station.step();
  const state = station.state();
  assert.equal(state.steps, 2);
  assert.equal(state.count, 4);
  assert.ok(state.meanSpeed >= 0);
});

test('empty flock yields zero mean speed', () => {
  const station = createBoidsStation({ count: 0 });
  station.step();
  assert.equal(station.state().meanSpeed, 0);
});

test('params are exposed', () => {
  const station = createBoidsStation({ separation: 2 });
  assert.equal(station.params.separation, 2);
});
