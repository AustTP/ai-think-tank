// The autonomous hiring cycle end to end (hiring.js): whoNeedsHelp consults
// Jev (falling back to lowest morale on outage), attemptAutoHire claims the
// cooldown slot, parks the admin in Control Room, and finishHire creates the
// new agent -- standing her up or keeping her dormant per the active cap.
//
// Run: node tests/test_hire_flow.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'hiring.js'), 'utf8');
vm.runInContext(src, context, { filename: 'hiring.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const whoNeedsHelp = (...a) => vm.runInContext('whoNeedsHelp', context)(...a);
const attemptAutoHire = (...a) => vm.runInContext('attemptAutoHire', context)(...a);
const finishHire = (...a) => vm.runInContext('finishHire', context)(...a);
const pickFallbackHireName = (...a) => vm.runInContext('pickFallbackHireName', context)(...a);

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

// Roster: faye the admin/hirer, dev a struggling worker, ben/nadia peers.
const ROSTER = [
  { id: 'faye', name: 'Faye', isAdmin: true, isDirector: true },
  { id: 'dev', name: 'Dev', role: 'Code' },
  { id: 'ben', name: 'Ben', role: 'Ops' },
  { id: 'nadia', name: 'Nadia', role: 'Research' },
];

function baseSetup({ morale = {}, reports = {}, jevChoice = null } = {}) {
  const roster = ROSTER.map(d => ({ ...d }));
  const AGENTS = {
    faye: { id: 'faye', name: 'Faye', busy: false, offDuty: false, approvedCount: 5, droppedCount: 1 },
    dev: { id: 'dev', name: 'Dev', busy: false, offDuty: false, approvedCount: 5, droppedCount: 3 },
    ben: { id: 'ben', name: 'Ben', busy: false, offDuty: false, approvedCount: 8, droppedCount: 0 },
    nadia: { id: 'nadia', name: 'Nadia', busy: false, offDuty: false, approvedCount: 2, droppedCount: 0 },
  };
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', AGENTS);
  setGlobal('thinkTankHasWork', () => true);
  setGlobal('moraleFor', (id) => morale[id] ?? 50);
  setGlobal('reportsAbout', (id) => reports[id] || []);
  setGlobal('requestJevChoice', async () => (jevChoice === null ? null : { choice: jevChoice }));
  setGlobal('logThinkTankAction', () => {});
  setGlobal('showToast', () => {});
  setGlobal('lastHireAt', 0);
  setGlobal('lastHireCapNoticeAt', 0);
  setGlobal('availableAuthority', () => roster[0]); // faye
  return { roster, AGENTS };
}

console.log('whoNeedsHelp (hiring.js)');

await test('lets Jev pick whoever most needs help, excluding the hirer', async () => {
  const { roster } = baseSetup({ jevChoice: 'dev' });
  const picked = await whoNeedsHelp('faye');
  assert.equal(picked.id, 'dev');
  assert.equal(roster.length, 4, 'roster untouched');
});

await test('falls back to the lowest-morale agent when Jev is unavailable', async () => {
  baseSetup({ morale: { dev: 20, ben: 55, nadia: 40 }, jevChoice: null });
  const picked = await whoNeedsHelp('faye');
  assert.equal(picked.id, 'dev', 'lowest morale wins in the fallback');
});

await test('returns null when the hirer is the only agent on the roster', async () => {
  setGlobal('AGENT_ROSTER', [{ id: 'faye', name: 'Faye', isAdmin: true }]);
  setGlobal('AGENTS', { faye: { id: 'faye', approvedCount: 5, droppedCount: 1 } });
  setGlobal('requestJevChoice', async () => { throw new Error('must not be called'); });
  assert.equal(await whoNeedsHelp('faye'), null);
});

console.log('attemptAutoHire (hiring.js)');

await test('no-ops when the think tank has no work', async () => {
  baseSetup();
  setGlobal('thinkTankHasWork', () => false);
  assert.equal(await attemptAutoHire(), false);
});

await test('no-ops inside the hire cooldown', async () => {
  baseSetup();
  setGlobal('lastHireAt', Date.now() - 1000);
  assert.equal(await attemptAutoHire(), false);
});

await test('releases the claimed cooldown slot when nobody needs help', async () => {
  baseSetup();
  setGlobal('whoNeedsHelp', async () => null);
  assert.equal(await attemptAutoHire(), false);
  assert.equal(vm.runInContext('lastHireAt', context), 0, 'cooldown released for a real retry');
});

await test('bails if the admin took on work while Jev deliberated', async () => {
  baseSetup();
  setGlobal('whoNeedsHelp', async () => { vm.runInContext('AGENTS.faye.busy = true;', context); return ROSTER[1]; });
  const started = await attemptAutoHire();
  assert.equal(started, false, 'no hire starts when the admin got busy mid-await');
});

await test('starts a hire: parks the admin, hides her, schedules the finish', async () => {
  baseSetup();
  setGlobal('whoNeedsHelp', async () => ROSTER[1]); // dev
  let scheduled = null;
  setGlobal('setTimeout', (fn) => { scheduled = fn; });
  const started = await attemptAutoHire();
  assert.equal(started, true);
  const faye = vm.runInContext('AGENTS.faye', context);
  assert.equal(faye.busy, true);
  assert.equal(faye.visible, false);
  assert.equal(faye.inRoom, 'commandcenter');
  assert.equal(faye.roomX, 315);
  assert.equal(faye.roomY, 155);
  assert.equal(faye.dir, 'south');
  assert.equal(typeof scheduled, 'function', 'finishHire scheduled after the hire duration');
  delete context.setTimeout;
});

console.log('finishHire (hiring.js)');

function setupFinish({ profileReply, canActivate = true, namePoolTaken = false }) {
  const out = baseSetup();
  setGlobal('pickFreeSpot', (occupied) => ({ x: 7, y: 7 }));
  setGlobal('canActivateAnother', () => canActivate);
  const spawned = [];
  setGlobal('spawnAgentAtFreeSpot', (id) => spawned.push(id));
  const toasted = [];
  setGlobal('showToast', (m) => toasted.push(m));
  const actions = [];
  setGlobal('logThinkTankAction', (a, k, d) => actions.push({ a, k, d }));
  if (profileReply !== undefined) {
    setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ reply: profileReply }) }));
  } else {
    setGlobal('agentFetch', async () => { throw new Error('network down'); });
  }
  return { ...out, spawned, toasted, actions };
}

await test('creates the assistant from the generated profile and walks her out', async () => {
  const { spawned, toasted, actions } = setupFinish({ profileReply: JSON.stringify({ name: 'Priya', mission: 'Help Dev.', instructions: ['Report to Dev.'], notes: ['Hired for overflow.'] }) });
  await finishHire(ROSTER[0], ROSTER[1]);
  const id = 'priya';
  assert.equal(vm.runInContext('typeof AGENTS.' + id, context), 'object', 'agent created');
  assert.equal(vm.runInContext('AGENTS.' + id + '.name', context), 'Priya');
  assert.equal(vm.runInContext('AGENTS.' + id + '.profile.mission', context), 'Help Dev.');
  assert.equal(vm.runInContext('AGENT_ROSTER', context).length, 5);
  assert.deepEqual(spawned, [id], 'walked out when the active cap has room');
  assert.ok(toasted[0].includes('hired Priya to help Dev'), toasted[0]);
  assert.equal(actions[0].k, 'hire');
  assert.equal(actions[0].d.hired, id);
});

await test('joins the inventory dormant when the active cap is full', async () => {
  const { spawned } = setupFinish({ profileReply: JSON.stringify({ name: 'Marcus', mission: 'm', instructions: ['i'], notes: ['n'] }), canActivate: false });
  await finishHire(ROSTER[0], ROSTER[1]);
  assert.equal(spawned.length, 0, 'no walk-out when the active roster is full');
  assert.equal(vm.runInContext('AGENTS.marcus.offDuty', context), true);
  assert.equal(vm.runInContext('AGENTS.marcus.visible', context), false);
});

await test('falls back to the template profile + pool name when the model call fails', async () => {
  const { spawned } = setupFinish({ profileReply: undefined });
  await finishHire(ROSTER[0], ROSTER[1]);
  const rosterNames = vm.runInContext('AGENT_ROSTER', context).map(d => d.name);
  const id = vm.runInContext('AGENT_ROSTER[4].id', context);
  assert.ok(vm.runInContext('HIRE_NAME_POOL', context).some(n => n.toLowerCase() === id), 'a pool name used');
  assert.equal(vm.runInContext('AGENTS.' + id + '.profile.mission', context).includes('Support Dev'), true, 'templated mission');
  assert.equal(rosterNames.length, 5);
  assert.equal(spawned[0], id);
});

await test('comes back empty-handed and hires nothing when every name is taken', async () => {
  // Exhaust BOTH the pool and all numbered variants so pickFallbackHireName
  // returns null -- finishHire must bail with a toast, not crash or push a
  // nameless agent.
  const pool = vm.runInContext('HIRE_NAME_POOL', context);
  const taken = [];
  for (const n of pool) { taken.push({ id: n.toLowerCase(), name: n }); }
  for (const n of pool) { for (let i = 2; i < 1000; i++) taken.push({ id: n.toLowerCase() + i, name: n + i }); }
  const { toasted, actions } = setupFinish({ profileReply: undefined, canActivate: true });
  setGlobal('AGENT_ROSTER', taken);
  setGlobal('AGENTS', { faye: { id: 'faye', name: 'Faye', busy: false, offDuty: false } });
  await finishHire(ROSTER[0], ROSTER[1]);
  assert.ok(toasted[0].includes('came back empty-handed'), toasted[0]);
  assert.equal(actions.length, 0, 'no hire logged');
  assert.equal(vm.runInContext('AGENT_ROSTER', context).length, taken.length, 'roster unchanged');
});

console.log('pickFallbackHireName (hiring.js)');

await test('picks an unused pool name when some are free', () => {
  setGlobal('AGENT_ROSTER', [{ id: 'marcus', name: 'Marcus' }, { id: 'priya', name: 'Priya' }]);
  const name = pickFallbackHireName();
  assert.ok(name, 'a real name');
  assert.notEqual(name, 'Marcus');
  assert.notEqual(name, 'Priya');
  assert.ok(!/^[A-Za-z]+[0-9]+$/.test(name), 'plain pool name preferred over numbered variant');
});

console.log('hireSpecialist successful path (hiring.js)');

const hireSpecialist = (...a) => vm.runInContext('hireSpecialist', context)(...a);

await test('uses the model-generated name and profile when the call succeeds', async () => {
  const { spawned } = setupFinish({ profileReply: JSON.stringify({ name: 'Yuki', mission: 'Build the finger-drumming UI.', instructions: ['Write the drum pad code.', 'Wire the click handlers.'], notes: ['Hired for the drum project.'] }) });
  const id = await hireSpecialist('faye', 'Coder', 'coding', 'build the finger-drumming pad');
  assert.equal(id, 'yuki', 'the model-provided name becomes the id');
  assert.equal(vm.runInContext('AGENTS.yuki.model', context), 'coding', 'the requested model tier is kept');
  assert.equal(vm.runInContext('AGENTS.yuki.elevatedAccess', context), false, 'specialists are not elevated');
  assert.equal(vm.runInContext('AGENTS.yuki.profile.mission', context), 'Build the finger-drumming UI.');
  assert.equal(vm.runInContext('AGENTS.yuki.role', context), 'Coder');
  assert.deepEqual(spawned, [id]);
  assert.equal(vm.runInContext('AGENT_ROSTER', context).length, 5);
});

await test('keeps the templated profile but still uses the pool name when the model reply is malformed', async () => {
  const { spawned } = setupFinish({ profileReply: '{ not json' });
  const id = await hireSpecialist('faye', 'Tester', 'small', 'test the drum pad');
  assert.ok(id, 'fallback name used');
  assert.equal(vm.runInContext('AGENTS.' + id + '.profile.mission', context), 'test the drum pad', 'missionHint templated in');
  assert.equal(spawned.length, 1);
});

await test('returns null when a hire is attempted by an unknown admin', async () => {
  setupFinish({ profileReply: undefined });
  const id = await hireSpecialist('ghost', 'Tester', 'small', 'whatever');
  assert.equal(id, null);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);