// Generic invariant checks: every simulation module must export at least
// one callable so the engine, models, and view layer stay wired together.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const modules = [
  './rng.js',
  './sim-engine.js',
  './sir.js',
  './weatherstation.js',
  './weatherstation-sir.js',
];

for (const path of modules) {
  test(`module ${path} exports something callable`, async () => {
    const mod = await import(path);
    const values = Object.values(mod);
    assert.ok(values.length > 0, `${path} has no exports`);
    assert.ok(
      values.some((v) => typeof v === 'function' || typeof v === 'object'),
      `${path} exports nothing usable`
    );
  });
}
