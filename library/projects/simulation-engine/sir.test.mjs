import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSIR } from './sir.js';
import { runEnsemble, bulletin } from './weatherstation-sir.js';

const cfg = { population: 1000, beta: 0.5, gamma: 0.1, days: 50, seed: 7 };

test('population is conserved every day', () => {
  const sim = createSIR(cfg);
  for (const row of sim.history) {
    assert.equal(row.susceptible + row.infected + row.recovered, 1000);
  }
});

test('counts never go negative and history length matches days', () => {
  const sim = createSIR(cfg);
  assert.equal(sim.history.length, cfg.days + 1);
  for (const row of sim.history) {
    for (const v of [row.susceptible, row.infected, row.recovered]) {
      assert.ok(v >= 0);
      assert.ok(Number.isFinite(v));
    }
  }
});

test('deterministic for same seed', () => {
  const a = createSIR(cfg);
  const b = createSIR(cfg);
  assert.deepEqual(a.history, b.history);
});

test('different seeds diverge', () => {
  const a = createSIR(cfg);
  const b = createSIR({ ...cfg, seed: 8 });
  assert.notDeepEqual(a.history, b.history);
});

test('no epidemic when beta is zero', () => {
  const sim = createSIR({ ...cfg, beta: 0, infected: 3 });
  const t = sim.totals();
  assert.equal(t.recovered, 3);
  assert.equal(t.susceptible, 997);
});

test('peak is the max infected row', () => {
  const sim = createSIR(cfg);
  const peak = sim.peak();
  for (const row of sim.history) {
    assert.ok(peak.infected >= row.infected);
  }
});

test('bad config throws', () => {
  assert.throws(() => createSIR({ ...cfg, population: -1 }));
  assert.throws(() => createSIR({ ...cfg, beta: -1 }));
  assert.throws(() => createSIR({ ...cfg, gamma: -1 }));
  assert.throws(() => createSIR({ ...cfg, days: 1.5 }));
  assert.throws(() => createSIR({ ...cfg, infected: 2000 }));
});

test('ensemble means lie in [0, population] and sum correctly', () => {
  const ens = runEnsemble(cfg, 5);
  assert.equal(ens.runs, 5);
  assert.ok(ens.meanSusceptible >= 0 && ens.meanSusceptible <= 1000);
  assert.ok(ens.attackRate >= 0 && ens.attackRate <= 1);
  const sum = ens.meanSusceptible + ens.meanInfected + ens.meanRecovered;
  assert.ok(Math.abs(sum - 1000) < 1e-9);
});

test('bulletin is a string with attack rate', () => {
  const text = bulletin(cfg, 3);
  assert.ok(typeof text === 'string');
  assert.ok(text.includes('EPIDEMIC BULLETIN'));
  assert.ok(text.includes('%'));
});
