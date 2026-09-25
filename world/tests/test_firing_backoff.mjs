// Real regression test for the firing-review backoff (firing.js) --
// empirically caught live: one long session ran 236 real firing reviews,
// all about the same agent, all landing on "keep," because nothing
// stopped the exact same unchanged evidence from being re-litigated every
// cooldown period forever. reviewIsStale()/whoNeedsReview() should skip a
// candidate whose last verdict was "keep" until something real changes
// (morale score or report count).
//
// Run: node tests/test_firing_backoff.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'firing.js'), 'utf8');
vm.runInContext(src, context, { filename: 'firing.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const whoNeedsReview = () => vm.runInContext('whoNeedsReview', context)();
const reviewIsStale = (...args) => vm.runInContext('reviewIsStale', context)(...args);
const attemptAutoFiringReview = (...args) => vm.runInContext('attemptAutoFiringReview', context)(...args);

let passed = 0, failed = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log(`  ok - ${name}`);
    passed++;
  } catch (e) {
    console.log(`  FAIL - ${name}`);
    console.log(`         ${e.message}`);
    failed++;
  }
}

// 2026-09-23: firing keys off the firing SIGNAL (a negative report or a real
// drop-off), not the raw morale score. `setup` models the agent + its signal;
// `hasFiringSignal` reads reportsAbout(def.id) (negative severity) and
// droppedCount > approvedCount*0.3.
function setup({ dropped, approved, reportSeverity = [], reportCount, lastFiringReview, morale }) {
  reportCount = reportCount !== undefined ? reportCount : reportSeverity.length;
  const reports = [];
  for (let i = 0; i < reportCount; i++) {
    const sev = reportSeverity[i] || 'low';
    reports.push({ quote: 'x', severity: sev });
  }
  setGlobal('AGENT_ROSTER', [{ id: 'dev', isAdmin: false }]);
  setGlobal('AGENTS', { dev: { id: 'dev', droppedCount: dropped, approvedCount: approved, lastFiringReview } });
  setGlobal('reportsAbout', () => reports);
  // reviewIsStale still compares morale for its staleness check (morale is kept
  // as context/feedback, just no longer the firing GATE). Default to matching a
  // lastFiringReview whose morale is 47 (the common fixture below).
  setGlobal('moraleFor', () => morale !== undefined ? morale : 47);
}

console.log('firing-review backoff (firing.js)');

await test('a candidate with a NEGATIVE report and never reviewed is eligible', () => {
  setup({ dropped: 0, approved: 5, reportSeverity: ['severe'], lastFiringReview: undefined });
  assert.equal(reviewIsStale({ id: 'dev' }), false);
  const worst = whoNeedsReview();
  assert.ok(worst && worst.id === 'dev', 'expected the reported, never-reviewed agent to be picked');
});

await test('a candidate with ONLY a drop-off (no report) is eligible', () => {
  setup({ dropped: 6, approved: 5, reportSeverity: [], lastFiringReview: undefined });
  const worst = whoNeedsReview();
  assert.ok(worst && worst.id === 'dev', 'a real drop-off is a firing signal on its own');
});

await test('a LOW-morale but unreported, non-overloaded agent is NOT a firing candidate', () => {
  // Morale decoupled from firing: low morale alone (no negative report, no
  // real drop-off) makes an agent a help/hiring target, not a firing one.
  setup({ dropped: 0, approved: 5, reportSeverity: [], lastFiringReview: undefined });
  assert.equal(whoNeedsReview(), null, 'neglect/low morale alone must NOT trigger a firing review');
});

await test('a "keep" verdict with nothing changed makes the candidate stale (skipped)', () => {
  setup({ dropped: 0, approved: 5, reportSeverity: [], reportCount: 0, lastFiringReview: { morale: 47, reportCount: 0, verdict: 'keep', at: 0 } });
  assert.equal(reviewIsStale({ id: 'dev' }), true);
  assert.equal(whoNeedsReview(), null, 'the only candidate is stale, so nobody should come up for review');
});

await test('a NEW negative report since the last "keep" makes them eligible again', () => {
  setup({ dropped: 0, approved: 5, reportSeverity: ['severe'], lastFiringReview: { morale: 47, reportCount: 0, verdict: 'keep', at: 0 } });
  assert.equal(reviewIsStale({ id: 'dev' }), false);
  const worst = whoNeedsReview();
  assert.ok(worst && worst.id === 'dev', 'a new negative report is a real change -> eligible again');
});

console.log('\nnever fire someone mid-task/pair/handoff (dangling-reference guard)');

// Give the agent a real firing signal (negative report) so the idle guard is
// what excludes them -- we're testing the dangling-reference guard, not the
// signal logic.
function setupSignaled(busyFields) {
  setGlobal('AGENT_ROSTER', [{ id: 'dev', isAdmin: false }]);
  setGlobal('AGENTS', { dev: Object.assign({ id: 'dev', droppedCount: 6, approvedCount: 5, lastFiringReview: undefined }, busyFields) });
  setGlobal('reportsAbout', () => [{ quote: 'x', severity: 'severe' }]);
}

await test('a candidate mid-task is skipped even though they have a real firing signal', () => {
  setupSignaled({ busy: true });
  assert.equal(whoNeedsReview(), null, 'a busy candidate should not come up for review at all');
});

await test('a pair navigator (pairWith set, busy still false) is also skipped', () => {
  setupSignaled({ busy: false, pairWith: 'ben' });
  assert.equal(whoNeedsReview(), null, 'pairWith alone (no .busy) must still count as "doing something"');
});

await test('a candidate mid-handoff is also skipped', () => {
  setupSignaled({ busy: false, handoff: { with: 'ben' } });
  assert.equal(whoNeedsReview(), null);
});

await test('once genuinely idle again, a candidate WITH a signal becomes eligible', () => {
  setupSignaled({ busy: false, task: null, pairWith: null, handoff: null });
  const worst = whoNeedsReview();
  assert.ok(worst && worst.id === 'dev', 'idle + a real report/drop signal -> eligible for review');
});

await test('a candidate with NO signal stays ineligible even when idle', () => {
  setGlobal('AGENT_ROSTER', [{ id: 'dev', isAdmin: false }]);
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, pairWith: null, handoff: null, droppedCount: 0, approvedCount: 5, lastFiringReview: undefined } });
  setGlobal('reportsAbout', () => []);
  assert.equal(whoNeedsReview(), null, 'idle alone, with no report and no drop-off, is not a firing target');
});

console.log('\nan off-duty admin is not treated as available for a firing review');

await test('attemptAutoFiringReview does not start while one admin is off duty, even though she is not busy', async () => {
  // Real bug caught live: this only checked .busy, so an off-duty admin
  // (resting, not busy) was treated as available. finishFiringReview()
  // unconditionally sets visible=true on completion without ever
  // restoring offDuty, leaving her stuck offDuty=true/visible=true
  // simultaneously -- an impossible combination.
  setGlobal('villageHasWork', () => true);
  setGlobal('AGENT_ROSTER', [
    { id: 'admin1', isAdmin: true }, { id: 'admin2', isAdmin: true }, { id: 'dev', isAdmin: false },
  ]);
  setGlobal('AGENTS', {
    admin1: { id: 'admin1', busy: false, offDuty: true },
    admin2: { id: 'admin2', busy: false, offDuty: false },
    dev: { id: 'dev', busy: false, task: null, pairWith: null, handoff: null },
  });
  setGlobal('moraleFor', () => 10);
  setGlobal('reportsAbout', () => []);
  setGlobal('lastFiringReviewAt', 0);
  setGlobal('whoNeedsReview', () => { throw new Error('should never be reached -- the admin check must fail first'); });
  const started = await attemptAutoFiringReview();
  assert.equal(started, false);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
