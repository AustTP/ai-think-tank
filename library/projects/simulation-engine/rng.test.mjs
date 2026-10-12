// Node smoke test: node rng.test.mjs
import assert from 'node:assert';
import { Rng, mulberry32, hashSeed } from './rng.js';

const a = new Rng(42);
const b = new Rng(42);
assert.deepStrictEqual(
  Array.from({ length: 100 }, () => a.next()),
  Array.from({ length: 100 }, () => b.next())
);
assert.strictEqual(new Rng('weather').next(), new Rng('weather').next());
assert.notStrictEqual(new Rng(1).next(), new Rng(2).next());
assert.strictEqual(hashSeed('abc'), hashSeed('abc'));
const r = new Rng(7);
assert.ok(r.range(5, 10) >= 5 && r.range(5, 10) < 10);
assert.ok(r.int(0, 3) >= 0 && r.int(0, 3) <= 3);
assert.deepStrictEqual(r.shuffle([1, 2, 3, 4]).sort(), [1, 2, 3, 4]);
assert.strictEqual(mulberry32(0)(), mulberry32(0)());
console.log('rng tests passed');
