// The firing-review cycle end to end (firing.js): attemptAutoFiringReview
// decides who comes up for review and parks both reviewers in Control Room;
// finishFiringReview consults Jev, applies the anti-premature-firing
// consultation guard, and either removes the agent (fire) or records a
// "keep" so reviewIsStale skips re-litigating the same evidence.
//
// Run: node tests/test_firing_review.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'firing.js'), 'utf8');
vm.runInContext(src, context, { filename: 'firing.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

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

// Full roster fixture: faye admin/director, nora senior director (director with
// no director of their own), dev a worker under review, ben/nadia peers.
const ROSTER = [
  { id: 'faye', name: 'Faye', isAdmin: true, isDirector: true, director: undefined },
  { id: 'nora', name: 'Nora', isDirector: true, director: undefined },
  { id: 'dev', name: 'Dev', role: 'Code', director: 'faye' },
  { id: 'ben', name: 'Ben', role: 'Ops', director: 'faye' },
];

function setupWorld({ reports, devOverrides = {}, jevChoice = null }) {
  const dev = Object.assign(
    { id: 'dev', name: 'Dev', role: 'Code', droppedCount: 0, approvedCount: 5, busy: false, task: null, pairWith: null, handoff: null },
    devOverrides,
  );
  // Fresh roster per test: a fire verdict splices AGENT_ROSTER, and a shared
  // const would corrupt every later test's candidate slot.
  const roster = ROSTER.map(d => ({ ...d }));
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye' },
    nora: { id: 'nora', name: 'Nora' },
    dev,
    ben: { id: 'ben', name: 'Ben' },
  });
  setGlobal('REPORTS', reports);
  setGlobal('reportsAbout', (id) => (reports || []).filter(r => r.aboutId === id));
  setGlobal('moraleFor', () => 50);
  setGlobal('requestJevChoice', async () => (jevChoice === null ? null : { choice: jevChoice }));
  setGlobal('logThinkTankAction', () => {});
  setGlobal('showToast', () => {});
  setGlobal('thinkTankHasWork', () => true);
  setGlobal('lastFiringReviewAt', 0);
  return roster;
}

console.log('firingReviewers (firing.js)');

await test('returns the admin and senior-most director in roster order', () => {
  const roster = setupWorld({ reports: [] });
  const reviewers = vm.runInContext('firingReviewers', context)();
  assert.equal(reviewers.length, 2);
  assert.equal(reviewers[0].id, 'faye');
  assert.equal(reviewers[1].id, 'nora');
});

await test('returns [] when there is no admin', () => {
  setupWorld({ reports: [] });
  setGlobal('AGENT_ROSTER', [
    { id: 'nora', name: 'Nora', isDirector: true, director: undefined },
    { id: 'dev', name: 'Dev', role: 'Code', director: 'faye' },
  ]);
  assert.deepEqual([...vm.runInContext('firingReviewers', context)()], []);
});

await test('returns [] when there is no senior-most director', () => {
  setupWorld({ reports: [] });
  setGlobal('AGENT_ROSTER', [
    { id: 'faye', name: 'Faye', isAdmin: true, isDirector: true, director: undefined },
    { id: 'dev', name: 'Dev', role: 'Code', director: 'faye' },
  ]);
  assert.deepEqual([...vm.runInContext('firingReviewers', context)()], []);
});

console.log('attemptAutoFiringReview (firing.js)');

await test('no-ops without think-tank work', async () => {
  setupWorld({ reports: [] });
  setGlobal('thinkTankHasWork', () => false);
  assert.equal(await vm.runInContext('attemptAutoFiringReview', context)(), false);
});

await test('no-ops during the cooldown window', async () => {
  setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'bad', severity: 'severe' }] });
  setGlobal('lastFiringReviewAt', Date.now() - 1000); // 1s ago < 60s cooldown
  assert.equal(await vm.runInContext('attemptAutoFiringReview', context)(), false);
});

await test('no-ops when the admin is busy', async () => {
  setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'bad', severity: 'severe' }] });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye', busy: true, offDuty: false },
    nora: { id: 'nora', name: 'Nora', busy: false, offDuty: false },
    dev: { id: 'dev', name: 'Dev', busy: false, task: null, pairWith: null, handoff: null, droppedCount: 0, approvedCount: 5 },
  });
  assert.equal(await vm.runInContext('attemptAutoFiringReview', context)(), false);
});

await test('no-ops when the senior director is off duty', async () => {
  setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'bad', severity: 'severe' }] });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye', busy: false, offDuty: false },
    nora: { id: 'nora', name: 'Nora', busy: false, offDuty: true },
    dev: { id: 'dev', name: 'Dev', busy: false, task: null, pairWith: null, handoff: null, droppedCount: 0, approvedCount: 5 },
  });
  assert.equal(await vm.runInContext('attemptAutoFiringReview', context)(), false);
});

await test('no-ops when nobody needs review', async () => {
  setupWorld({ reports: [] }); // no signal -> no candidate
  assert.equal(await vm.runInContext('attemptAutoFiringReview', context)(), false);
});

await test('starts a review: parks reviewers, hides them, schedules the finish', async () => {
  setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'bad', severity: 'severe' }] });
  let scheduled = null;
  setGlobal('setTimeout', (fn) => { scheduled = fn; });
  const started = await vm.runInContext('attemptAutoFiringReview', context)();
  assert.equal(started, true);
  const faye = vm.runInContext('AGENTS.faye', context);
  const nora = vm.runInContext('AGENTS.nora', context);
  assert.equal(faye.busy, true);
  assert.equal(faye.visible, false);
  assert.equal(faye.inRoom, 'commandcenter');
  assert.equal(faye.dir, 'south');
  assert.equal(faye.roomX, 315);
  assert.equal(faye.roomY, 155);
  assert.equal(nora.roomX, 485);
  assert.equal(nora.roomY, 155);
  assert.equal(typeof scheduled, 'function', 'finishFiringReview scheduled after the review duration');
  delete context.setTimeout;
});

console.log('finishFiringReview (firing.js)');

const finish = (...a) => vm.runInContext('finishFiringReview', context)(...a);

await test('a review that lands after the candidate left resolves nothing', async () => {
  const roster = setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'bad', severity: 'severe' }] });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye' },
    nora: { id: 'nora', name: 'Nora' },
    ben: { id: 'ben', name: 'Ben' },
  }); // no dev anymore
  await finish(roster[0], roster[1], roster[2]);
  const faye = vm.runInContext('AGENTS.faye', context);
  assert.equal(faye.busy, false, 'reviewers still released');
  assert.equal(faye.visible, true);
  assert.equal(faye.inRoom, null);
});

await test('Jev fire + clear evidence removes the agent and fires the toast', async () => {
  const roster = setupWorld({ reports: [
    { aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' },
    { aboutId: 'dev', fromId: 'nora', quote: 'q2', severity: 'serious' },
  ], jevChoice: 'fire' });
  const toasts = [];
  let actions = [];
  setGlobal('showToast', (t) => toasts.push(t));
  setGlobal('logThinkTankAction', (a, k, d) => actions.push({ a, k, d }));
  await finish(roster[0], roster[1], roster[2]);
  assert.equal(vm.runInContext('typeof AGENTS.dev', context), 'undefined', 'candidate removed');
  assert.equal(vm.runInContext('AGENT_ROSTER', context).length, 3, 'candidate spliced from roster');
  assert.ok(toasts[0].includes('let Dev go'), toasts[0]);
  assert.equal(actions[0].k, 'firing_review');
  assert.equal(actions[0].d.decision, 'fire');
});

await test('Jev keep records a verdict so the same evidence is not re-litigated', async () => {
  const roster = setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' }], jevChoice: 'keep' });
  const toasts = [];
  setGlobal('showToast', (t) => toasts.push(t));
  await finish(roster[0], roster[1], roster[2]);
  const last = vm.runInContext('AGENTS.dev.lastFiringReview', context);
  assert.ok(last, 'verdict recorded');
  assert.equal(last.verdict, 'keep');
  assert.equal(last.morale, 50);
  assert.equal(last.reportCount, 1);
  assert.ok(toasts[0].includes('keep them on'), toasts[0]);
});

await test('Jev outage fallback fires when the drop-off is real', async () => {
  const roster = setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' }], jevChoice: null });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye' },
    nora: { id: 'nora', name: 'Nora' },
    dev: { id: 'dev', name: 'Dev', role: 'Code', droppedCount: 6, approvedCount: 5, busy: false, task: null, pairWith: null, handoff: null },
    ben: { id: 'ben', name: 'Ben' },
  });
  await finish(roster[0], roster[1], roster[2]);
  assert.equal(vm.runInContext('typeof AGENTS.dev', context), 'undefined', 'report + real drop-off -> fire in fallback');
});

await test('Jev outage fallback keeps when only one signal holds', async () => {
  const roster = setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' }], jevChoice: null });
  // droppedCount 0 -> no drop-off, report alone -> keep
  await finish(roster[0], roster[1], roster[2]);
  const last = vm.runInContext('AGENTS.dev.lastFiringReview', context);
  assert.equal(last.verdict, 'keep');
});

await test('a fire verdict is deferred when consultation blocks it', async () => {
  // ben is currently paired with dev -> firing would strand the collab.
  const roster = setupWorld({ reports: [{ aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' }], jevChoice: 'fire' });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye' },
    nora: { id: 'nora', name: 'Nora' },
    dev: { id: 'dev', name: 'Dev', role: 'Code', busy: false, task: null, pairWith: null, handoff: null, droppedCount: 0, approvedCount: 5 },
    ben: { id: 'ben', name: 'Ben', pairWith: 'dev' },
  });
  let action = null;
  setGlobal('logThinkTankAction', (a, k, d) => { action = d; });
  await finish(roster[0], roster[1], roster[2]);
  assert.equal(vm.runInContext('typeof AGENTS.dev', context), 'object', 'candidate kept when consultation blocks');
  assert.equal(action.decision, 'deferred_for_consultation');
  assert.equal(vm.runInContext('AGENTS.dev.lastFiringReview', context), undefined, 'not marked reviewed');
});

await test('a fire verdict is deferred if the candidate got busy mid-review', async () => {
  const roster = setupWorld({ reports: [
    { aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' },
    { aboutId: 'dev', fromId: 'nora', quote: 'q2', severity: 'serious' },
  ], jevChoice: 'fire' });
  setGlobal('AGENTS', {
    faye: { id: 'faye', name: 'Faye' },
    nora: { id: 'nora', name: 'Nora' },
    dev: { id: 'dev', name: 'Dev', role: 'Code', busy: true, task: null, pairWith: null, handoff: null, droppedCount: 0, approvedCount: 5 },
    ben: { id: 'ben', name: 'Ben' },
  });
  let action = null;
  setGlobal('logThinkTankAction', (a, k, d) => { action = d; });
  await finish(roster[0], roster[1], roster[2]);
  assert.equal(vm.runInContext('typeof AGENTS.dev', context), 'object');
  assert.equal(action.decision, 'deferred');
  assert.equal(action.reason, 'candidate became busy mid-review');
});

await test('firing closes an open conversation window with the candidate', async () => {
  const roster = setupWorld({ reports: [
    { aboutId: 'dev', fromId: 'ben', quote: 'q1', severity: 'severe' },
    { aboutId: 'dev', fromId: 'nora', quote: 'q2', severity: 'serious' },
  ], jevChoice: 'fire' });
  let closed = 0;
  setGlobal('activeConversationAgent', 'dev');
  setGlobal('closeConversation', () => closed++);
  await finish(roster[0], roster[1], roster[2]);
  assert.equal(closed, 1, 'conversation closed before the agent is deleted');
  delete context.activeConversationAgent;
  delete context.closeConversation;
});

console.log('isNegativeSeverity (firing.js)');

const negSev = (...a) => vm.runInContext('isNegativeSeverity', context)(...a);

await test('recognizes the negative severity vocabulary', () => {
  assert.equal(negSev('SEVERE'), true);
  assert.equal(negSev('serious'), true);
  assert.equal(negSev('major'), true);
  assert.equal(negSev('minor'), false);
  assert.equal(negSev(''), false);
  assert.equal(negSev(null), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);