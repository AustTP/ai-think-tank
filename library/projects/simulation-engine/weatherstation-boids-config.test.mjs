import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('./weatherstation-boids-config.js', import.meta.url), 'utf8');

function load(stubWindow) {
  new Function('window', src)(stubWindow);
  return globalThis.WeatherStationBoidsConfig;
}

test('config exposes name, defaultParams, and factory', () => {
  const cfg = load({});
  assert.equal(cfg.name, 'boids');
  assert.equal(typeof cfg.createBoidsSim, 'function');
  assert.equal(cfg.defaultParams.count, 60);
});

test('createBoidsSim produces count boids that move and wrap', () => {
  const cfg = load({});
  const sim = cfg.createBoidsSim({ count: 5, width: 100, height: 100 });
  const before = sim.getBoids().map((b) => b.x);
  const out = sim.step();
  assert.equal(out.length, 5);
  for (const b of out) {
    assert.ok(b.x >= 0 && b.x < 100 && b.y >= 0 && b.y < 100);
  }
  assert.deepEqual(sim.getBoids().map((b) => b.x), out.map((b) => b.x));
  assert.notDeepEqual(before, out.map((b) => b.x));
});

test('registers boids into a live WeatherStation hub when present', () => {
  const recorded = [];
  load({ WeatherStation: { record: (name, sim) => recorded.push(name) } });
  assert.deepEqual(recorded, ['boids']);
});

test('does not throw when WeatherStation hub is absent', () => {
  assert.doesNotThrow(() => load({}));
});
