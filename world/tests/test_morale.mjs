// Real regression test for morale.js's dropped-work decay -- found
// empirically, not by code review: droppedCount is only ever set once
// (seed data at hire time, or 0 for a real hire) and nothing in the
// running think tank ever incremented OR decayed it. For Dev (seeded with 6
// drops), that made the dropped-work penalty a permanent, un-earnable-
// back ceiling: 236 real firing reviews in one session, morale stuck at
// 45-49 the whole time, because the one input actually holding the score
// down could never move. moraleFor() now decays that penalty toward 0
// over MORALE_DROPPED_DECAY_DAYS since hire.
//
// Run: node tests/test_morale.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'morale.js'), 'utf8');
vm.runInContext(src, context, { filename: 'morale.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const moraleFor = (...args) => vm.runInContext('moraleFor', context)(...args);
const DAY_MS = 86400000;

let passed = 0, failed = 0;
function test(name, fn) {
  try {
    fn();
    console.log(`  ok - ${name}`);
    passed++;
  } catch (e) {
    console.log(`  FAIL - ${name}`);
    console.log(`         ${e.message}`);
    failed++;
  }
}

function agent(overrides) {
  return { id: 'dev', approvedCount: 0, droppedCount: 0, lastContactedAt: Date.now(), hiredAt: Date.now(), ...overrides };
}

console.log('moraleFor dropped-work decay (morale.js)');

test('a freshly-hired agent with drops pays the full penalty', () => {
  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, hiredAt: Date.now() }) });
  setGlobal('reportsAbout', () => []);
  // 100 - (6 * 6 * 1.0 decay) - 0 neglect (just contacted) = 64
  assert.equal(moraleFor('dev'), 64);
});

test('the same agent, 14+ days after hire with no new drops, pays none of it', () => {
  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, hiredAt: Date.now() - 20 * DAY_MS }) });
  setGlobal('reportsAbout', () => []);
  assert.equal(moraleFor('dev'), 100);
});

test('halfway through the decay window, the penalty is roughly halved', () => {
  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, hiredAt: Date.now() - 7 * DAY_MS }) });
  setGlobal('reportsAbout', () => []);
  // 100 - (6 * 6 * 0.5) = 82
  assert.equal(moraleFor('dev'), 82);
});

test('a real regression case: 6 drops + never contacted, stuck below 50 with no decay, recovers once decayed', () => {
  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, approvedCount: 20, lastContactedAt: null, hiredAt: Date.now() }) });
  setGlobal('reportsAbout', () => []);
  const stuck = moraleFor('dev');
  assert.ok(stuck < 50, `expected the undecayed case to reproduce the real stuck-below-50 bug, got ${stuck}`);

  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, approvedCount: 20, lastContactedAt: null, hiredAt: Date.now() - 30 * DAY_MS }) });
  const recovered = moraleFor('dev');
  assert.ok(recovered > stuck, `expected morale to recover once the dropped-work penalty decayed, got ${recovered} (was ${stuck})`);
});

test('an agent with no hiredAt at all (pre-migration data) is NOT treated as fully decayed', () => {
  setGlobal('AGENTS', { dev: agent({ droppedCount: 6, hiredAt: undefined }) });
  setGlobal('reportsAbout', () => []);
  // hiredAt missing -> daysSinceHire treated as 0 -> full penalty, same as
  // a freshly-hired agent -- NOT silently forgiven just because the field
  // is absent. (agents.js's own restore-path default is what actually
  // backfills hiredAt for real persisted state; this only guards moraleFor
  // itself against a missing field.)
  assert.equal(moraleFor('dev'), 64);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
