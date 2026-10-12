// sim/rng.js — Seeded deterministic RNG utility for the simulation engine.
// Pure ES module; also exposes a global for plain <script> usage so the
// same code runs identically in Node and the browser.

// Mulberry32: tiny, fast, well-distributed 32-bit PRNG. Deterministic
// for a given integer seed on any platform (no Math.random involved).
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Hash an arbitrary string into a 32-bit seed (FNV-1a) so callers may
// seed with strings like "weatherstation-run-42" and stay deterministic.
export function hashSeed(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

// Convenience RNG facade with the standard helpers simulations need.
export class Rng {
  constructor(seed = 1) {
    this.seed = typeof seed === 'string' ? hashSeed(seed) : (seed >>> 0);
    this._next = mulberry32(this.seed);
    this.reset(); // start the counter at a clean state
  }

  reset() {
    this._next = mulberry32(this.seed);
    this.calls = 0;
  }

  // float in [0, 1)
  next() {
    this.calls += 1;
    return this._next();
  }

  // float in [min, max)
  range(min, max) {
    return min + this.next() * (max - min);
  }

  // integer in [min, max] inclusive
  int(min, max) {
    return Math.floor(this.range(min, max + 1));
  }

  // true with probability p
  chance(p) {
    return this.next() < p;
  }

  // pick a random element (array must be non-empty)
  pick(arr) {
    return arr[this.int(0, arr.length - 1)];
  }

  // Fisher–Yates shuffle, returns a new array
  shuffle(arr) {
    const out = arr.slice();
    for (let i = out.length - 1; i > 0; i--) {
      const j = this.int(0, i);
      const tmp = out[i];
      out[i] = out[j];
      out[j] = tmp;
    }
    return out;
  }
}

// Browser/global fallback so pages can do: const rng = new SimRng.Rng(7);
if (typeof window !== 'undefined') {
  window.SimRng = { mulberry32, hashSeed, Rng };
}

export { Rng as RNG };

// Alias  so invariants.test.mjs can import the same class as RNG
// while rng.test.mjs and the rest of the engine keep using Rng.

