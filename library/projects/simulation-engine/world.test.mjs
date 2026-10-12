import { test } from 'node:test';
import assert from 'node:assert/strict';
import { World } from './world.js';

function makeSpySystem() {
  const calls = [];
  return {
    calls,
    update(dt, world) { calls.push([dt, world]); },
  };
}

test('constructor validates dt and maxSubSteps', () => {
  assert.throws(() => new World({ dt: 0 }), TypeError);
  assert.throws(() => new World({ dt: -1 }), TypeError);
  assert.throws(() => new World({ dt: NaN }), TypeError);
  assert.throws(() => new World({ maxSubSteps: 0 }), TypeError);
  const w = new World();
  assert.equal(w.dt, 1 / 60);
  assert.equal(w.time, 0);
  assert.equal(w.ticks, 0);
});

test('addSystem validates, init runs once, duplicates ignored', () => {
  const w = new World();
  assert.throws(() => w.addSystem({}), TypeError);
  assert.throws(() => w.addSystem(null), TypeError);
  let inits = 0;
  const sys = { init() { inits += 1; }, update() {} };
  w.addSystem(sys);
  w.addSystem(sys);
  assert.equal(inits, 1);
  assert.ok(w.hasSystem(sys));
});

test('removeSystem calls deinit and is safe when absent', () => {
  const w = new World();
  let deinits = 0;
  const sys = { update() {}, deinit() { deinits += 1; } };
  w.removeSystem(sys);
  assert.equal(deinits, 0);
  w.addSystem(sys);
  w.removeSystem(sys);
  assert.equal(deinits, 1);
  assert.ok(!w.hasSystem(sys));
});

test('step calls each system with fixed dt and the world', () => {
  const w = new World({ dt: 0.5 });
  const a = makeSpySystem();
  const b = makeSpySystem();
  w.addSystem(a).addSystem(b);
  w.step();
  assert.deepEqual(a.calls, [[0.5, w]]);
  assert.deepEqual(b.calls, [[0.5, w]]);
  assert.equal(w.time, 0.5);
  assert.equal(w.ticks, 1);
});

test('advance runs only whole steps and keeps remainder', () => {
  const w = new World({ dt: 0.1 });
  const sys = makeSpySystem();
  w.addSystem(sys);
  assert.equal(w.advance(0.25), 2);
  assert.equal(sys.calls.length, 2);
  assert.equal(w.time, 0.2);
  assert.equal(w.advance(0.05), 0);
  assert.equal(w.advance(0.05), 1);
  assert.ok(Math.abs(w.time - 0.3) < 1e-9, `time=${w.time}`);
});

test('advance rejects bad deltas', () => {
  const w = new World();
  assert.throws(() => w.advance(-1), TypeError);
  assert.throws(() => w.advance(NaN), TypeError);
});

test('advance caps at maxSubSteps to avoid spiral of death', () => {
  const w = new World({ dt: 0.1, maxSubSteps: 3 });
  const sys = makeSpySystem();
  w.addSystem(sys);
  assert.equal(w.advance(10), 3);
  assert.equal(sys.calls.length, 3);
  assert.ok(Math.abs(w.time - 0.3) < 1e-9, `time=${w.time}`);
});

test('update order follows registration order', () => {
  const w = new World();
  const order = [];
  w.addSystem({ update() { order.push('a'); } });
  w.addSystem({ update() { order.push('b'); } });
  w.step();
  assert.deepEqual(order, ['a', 'b']);
});
