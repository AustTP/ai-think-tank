import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';

test('README documents the public API', () => {
  assert.ok(existsSync('README.md'), 'README.md must exist');
  const readme = readFileSync('README.md', 'utf8');
  for (const name of [
    'weatherstation.js', 'record', 'history', 'latest', 'names',
    'onChange', 'reset', 'runHeadless', 'JSON',
    'predatorprey-core.js', 'boids.js', 'rng.js',
  ]) {
    assert.ok(readme.includes(name), `README must mention ${name}`);
  }
});

test('README documents weatherstation observed globals accurately', () => {
  const readme = readFileSync('README.md', 'utf8');
  assert.ok(readme.includes('window.WeatherStation'));
  assert.ok(readme.includes('window.ppStats'));
  assert.ok(readme.includes('PredatorPreyEcosystem'));
});
