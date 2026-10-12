import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFlock } from './boids.js';

test('creates the requested number of boids in bounds', () => {
  const flock = createFlock({ width: 50, height: 80, count: 5, rng: () => 0.5 });
  assert.equal(flock.boids().length, 5);
  for (const b of flock.boids()) {
    assert.ok(b.x >= 0 && b.x < 50);
    assert.ok(b.y >= 0 && b.y < 80);
  }
});

test('step keeps positions inside bounds and finite', () => {
  const flock = createFlock({ width: 100, height: 100, count: 10 });
  for (let i = 0; i < 100; i++) flock.step();
  for (const b of flock.boids()) {
    assert.ok(Number.isFinite(b.x) && Number.isFinite(b.y));
    assert.ok(b.x >= 0 && b.x < 100 && b.y >= 0 && b.y < 100);
  }
});

test('cohesion pulls isolated pair together', () => {
  // Deterministic but staggered spawn so the pair starts apart.
  const seq = [0.1, 0.1, 0.9, 0.9, 0.5, 0.5];
  let i = 0;
  const rng = () => seq[i++ % seq.length];
  const flock = createFlock({ width: 100, height: 100, count: 2, cohesion: 2, alignment: 0, separation: 0, vision: 100, maxSpeed: 2, rng });
  const dist = () => Math.hypot(flock.boids()[0].x - flock.boids()[1].x, flock.boids()[0].y - flock.boids()[1].y);
  const before = dist();
  assert.ok(before > 1, `expected pair to start apart, got ${before}`);
  for (let s = 0; s < 30; s++) flock.step();
  const after = dist();
  assert.ok(after < before, `after=${after} before=${before}`);
});

test('speed never exceeds maxSpeed', () => {
  const flock = createFlock({ count: 8, maxSpeed: 2, vision: 50 });
  for (let i = 0; i < 50; i++) flock.step();
  for (const b of flock.boids()) {
    assert.ok(Math.hypot(b.vx, b.vy) <= 2 + 1e-9);
  }
});

test('deterministic with a seeded rng', () => {
  const mk = () => { let s = 42; return () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648; };
  const a = createFlock({ count: 6, rng: mk() });
  const b = createFlock({ count: 6, rng: mk() });
  for (let i = 0; i < 10; i++) { a.step(); b.step(); }
  assert.deepEqual(a.boids(), b.boids());
});

test('handles empty flock and zero boid count', () => {
  const flock = createFlock({ count: 0 });
  flock.step();
  assert.equal(flock.boids().length, 0);
});
