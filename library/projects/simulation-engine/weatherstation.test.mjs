import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WeatherStation, createWeatherSystem } from './weatherstation.js';

test('same seed produces identical sequences', () => {
  const a = new WeatherStation(42);
  const b = new WeatherStation(42);
  for (let i = 0; i < 100; i++) {
    assert.deepEqual(a.step(60), b.step(60));
  }
});

test('different seeds diverge', () => {
  const a = new WeatherStation(1);
  const b = new WeatherStation(2);
  a.step(60);
  b.step(60);
  assert.notDeepEqual(a.snapshot(), b.snapshot());
});

test('fixed dt accumulation is equivalent to one big step modulo dt effects', () => {
  const a = new WeatherStation(7);
  let snap = null;
  for (let i = 0; i < 1440; i++) snap = a.step(60); // one day in minutes
  assert.ok(snap.temperature > -30 && snap.temperature < 45);
  assert.ok(snap.humidity >= 0 && snap.humidity <= 100);
  assert.ok(snap.pressure >= 950 && snap.pressure <= 1050);
  assert.ok(snap.rain >= 0 && snap.rain <= 1);
});

test('non-positive dt is a no-op snapshot', () => {
  const a = new WeatherStation(3);
  const before = a.snapshot();
  assert.deepEqual(a.step(0), before);
  assert.equal(a.time, 0);
});

test('system adapter works with both call conventions', () => {
  const sys = createWeatherSystem(5);
  assert.equal(sys.name, 'weather');
  const worldA = {};
  sys.update(worldA, 10);
  assert.ok(worldA.weather);
  const sys2 = createWeatherSystem(5);
  const worldB = {};
  sys2(worldB, 10); // called directly as a function
  assert.deepEqual(worldB.weather, worldA.weather);
});

test('reset restores exact initial state', () => {
  const a = new WeatherStation(9);
  const initial = a.snapshot();
  for (let i = 0; i < 50; i++) a.step(120);
  a.reset();
  assert.deepEqual(a.snapshot(), initial);
});
