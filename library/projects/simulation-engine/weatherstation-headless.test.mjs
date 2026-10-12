import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runHeadless, runHeadlessFinal, toJSON } from './weatherstation-headless.js';

// Tiny deterministic model for testing: a counter that doubles a value.
const counterModel = {
  init(config) {
    return { n: 0, value: config.start ?? 1 };
  },
  step(state) {
    return { n: state.n + 1, value: state.value * 2 };
  }
};

test('runHeadless captures initial frame before any step', () => {
  const result = runHeadless(counterModel, { steps: 0, config: { start: 3 } });
  assert.equal(result.steps, 0);
  assert.deepEqual(result.frames, [{ n: 0, value: 3 }]);
});

test('runHeadless produces one frame per step plus initial', () => {
  const result = runHeadless(counterModel, { steps: 3, config: { start: 1 } });
  assert.equal(result.frames.length, 4);
  assert.deepEqual(result.frames, [
    { n: 0, value: 1 },
    { n: 1, value: 2 },
    { n: 2, value: 4 },
    { n: 3, value: 8 }
  ]);
});

test('runHeadless output is JSON round-trippable', () => {
  const result = runHeadless(counterModel, { steps: 2, config: { start: 1 } });
  const roundTrip = JSON.parse(JSON.stringify(result));
  assert.deepEqual(roundTrip, result);
});

test('runHeadlessFinal returns last frame', () => {
  const last = runHeadlessFinal(counterModel, { steps: 2, config: { start: 5 } });
  assert.deepEqual(last, { n: 2, value: 20 });
});

test('runHeadless validates arguments', () => {
  assert.throws(() => runHeadless(null), TypeError);
  assert.throws(() => runHeadless({}), TypeError);
  assert.throws(() => runHeadless(counterModel, { steps: -1 }), RangeError);
});

test('runHeadless rejects a model that returns undefined from step', () => {
  const bad = { init: () => ({}), step: () => undefined };
  assert.throws(() => runHeadless(bad, { steps: 1 }), /returned undefined/);
});

test('toJSON strips functions and copies nested arrays/objects', () => {
  const state = { a: 1, b: [1, { c: 2 }], hidden: () => 'x' };
  const out = toJSON(state);
  assert.deepEqual(out, { a: 1, b: [1, { c: 2 }] });
  assert.notEqual(out.b[1], state.b[1]);
  assert.equal(toJSON(42), 42);
  assert.equal(toJSON(null), null);
});
