// Node test script asserting simulation invariants for the agent-lab engines.
// Run with: node sim.test.mjs   (exit 0 = all invariants hold)
import { strict as assert } from 'node:assert';

const ENGINE_FILES = ['rng.js', 'sim-engine.js', 'boids.js', 'ca-engine.js', 'sir.js'];

// ---------------------------------------------------------------- loading
const modules = {};
let skipped = [];
for (const file of ENGINE_FILES) {
  try {
    modules[file] = await import('./' + file);
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    // Browser-only engines (touch document/window at import time) may be
    // skipped; any other failure is a real problem and must fail the run.
    if (/document|window|navigator|canvas|HTMLElement/i.test(msg)) {
      skipped.push(file);
    } else {
      console.error(`FAIL: could not import ${file}: ${msg}`);
      process.exit(1);
    }
  }
}

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

// ---------------------------------------------------------------- helpers
// Find a seedable RNG surface among a module's exports: either a factory
// function seed -> generator, or a constructable class with next/random.
function findRng(mod) {
  for (const name of Object.keys(mod)) {
    const v = mod[name];
    if (typeof v !== 'function') continue;
    let gen = null;
    try {
      const r = v(12345);
      if (typeof r === 'function' || (r && typeof r.next === 'function')) gen = r;
    } catch { /* not a factory */ }
    if (!gen) {
      try {
        const r = new v(12345);
        if (r && (typeof r.next === 'function' || typeof r.random === 'function' ||
                  typeof r.uniform === 'function')) gen = r;
      } catch { /* not constructable with a seed */ }
    }
    if (gen) return { name, make: () => (typeof v === 'function' && v.length >= 0 ? null : null) ? null : null, gen, Ctor: v };
  }
  return null;
}

// Pull one uniform number out of whatever rng surface we found.
function nextNumber(r) {
  if (typeof r === 'function') return r();
  if (typeof r.next === 'function') {
    const x = r.next();
    return x && typeof x === 'object' ? x.value : x;
  }
  if (typeof r.random === 'function') return r.random();
  if (typeof r.uniform === 'function') return r.uniform();
  return null;
}

function freshRng(hit, seed) {
  try {
    return hit.Ctor(seed);
  } catch {
    return new hit.Ctor(seed);
  }
}

// ---------------------------------------------------------------- rng invariants
const rngHit = findRng(modules['rng.js'] || {});
ok(rngHit !== null, 'rng.js must expose a seedable rng (factory or class)');

const N = 200;
function draw(rng) {
  const out = [];
  for (let i = 0; i < N; i++) {
    const x = nextNumber(rng);
    ok(typeof x === 'number' && Number.isFinite(x), 'rng output must be a finite number');
    out.push(x);
  }
  return out;
}

const a = draw(freshRng(rngHit, 42));
const b = draw(freshRng(rngHit, 42));
const c = draw(freshRng(rngHit, 43));

ok(
  JSON.stringify(a) === JSON.stringify(b),
  'same seed must produce an identical sequence (determinism)'
);
ok(
  JSON.stringify(a) !== JSON.stringify(c),
  'different seeds must produce different sequences'
);
for (const x of a) {
  ok(x >= 0 && x < 1, `uniform output must lie in [0, 1), got ${x}`);
}
const mean = a.reduce((s, x) => s + x, 0) / N;
ok(Math.abs(mean - 0.5) < 0.15, `mean of ${N} draws should be near 0.5, got ${mean.toFixed(3)}`);

// ---------------------------------------------------------------- engine invariants
// Generic probe: any export that instantiates and exposes .step() is treated
// as a simulation engine and must satisfy: determinism and finiteness.
function findEngine(mod) {
  if (!mod) return null;
  for (const name of Object.keys(mod)) {
    const v = mod[name];
    if (typeof v !== 'function') continue;
    let inst = null;
    try { inst = new v(); } catch { try { inst = v(); } catch { continue; } }
    if (inst && typeof inst.step === 'function') return inst;
  }
  return null;
}

function isFiniteState(s) {
  const seen = new Set();
  function walk(v) {
    if (v === null || typeof v !== 'object') {
      return typeof v !== 'number' || Number.isFinite(v);
    }
    if (seen.has(v)) return true;
    seen.add(v);
    return Object.values(v).every(walk);
  }
  return walk(s);
}

const engineModules = ['sim-engine.js', 'boids.js', 'ca-engine.js', 'sir.js'];
for (const file of engineModules) {
  const inst = findEngine(modules[file]);
  if (!inst) continue;
  // Determinism: two identically-seeded instances (if seedable) or the same
  // instance reset-and-rerun must reach the same state.
  const runSteps = (sim) => {
    for (let i = 0; i < 50; i++) sim.step();
    return JSON.stringify(sim.state !== undefined ? sim.state : sim, (k, v) =>
      typeof v === 'number' ? (Number.isFinite(v) ? v : 'NaN') : v);
  };
  const s1 = runSteps(inst);
  ok(!s1.includes('"NaN"'), `${file}: 50 steps must not produce NaN/Infinity state`);
  ok(isFiniteState(inst.state !== undefined ? inst.state : inst),
    `${file}: engine state must be finite after stepping`);
  // If a second instance can be built the same way, it must agree.
  const second = findEngine(modules[file]);
  if (second && second !== inst) {
    ok(runSteps(second) === s1, `${file}: independent instances must evolve identically`);
  }
}

// Every engine module we successfully loaded must export at least one symbol.
for (const file of ENGINE_FILES) {
  if (modules[file]) {
    ok(Object.keys(modules[file]).length > 0, `${file} must export at least one symbol`);
  }
}

console.log(`sim invariants: ${checks} assertions passed` +
  (skipped.length ? ` (skipped browser-only: ${skipped.join(', ')})` : ''));
