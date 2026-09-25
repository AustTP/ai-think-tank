// Real regression test for the villager headcount cap (hiring.js) --
// per your call on 2026-09-20: admin should know the current count and
// visibly stop trying to hire once the village is at its max (25),
// rather than silently no-op forever. Covers both hiring entry points
// (attemptAutoHire's autonomous path, hireSpecialist's direct path) and
// the notice cooldown that keeps this from log-spamming every 5s poll.
//
// Run: node tests/test_hire_cap.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Promise };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'hiring.js'), 'utf8');
vm.runInContext(src, context, { filename: 'hiring.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function getGlobal(name) {
  return vm.runInContext(name, context);
}
const attemptAutoHire = (...args) => vm.runInContext('attemptAutoHire', context)(...args);
const hireSpecialist = (...args) => vm.runInContext('hireSpecialist', context)(...args);
const generateHireProfile = (...args) => vm.runInContext('generateHireProfile', context)(...args);
const pickFallbackHireName = (...args) => vm.runInContext('pickFallbackHireName', context)(...args);

let passed = 0, failed = 0;
function test(name, fn) {
  return (async () => {
    try {
      await fn();
      console.log(`  ok - ${name}`);
      passed++;
    } catch (e) {
      console.log(`  FAIL - ${name}`);
      console.log(`         ${e.stack || e.message}`);
      failed++;
    }
  })();
}

function rosterOfSize(n) {
  const roster = [];
  for (let i = 0; i < n; i++) roster.push({ id: `agent${i}`, isAdmin: i === 0 });
  return roster;
}

function baseSetup(rosterSize) {
  const roster = rosterOfSize(rosterSize);
  const agents = {};
  for (const def of roster) agents[def.id] = { id: def.id, busy: false, offDuty: false, approvedCount: 0, droppedCount: 0 };
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
  setGlobal('villageHasWork', () => true);
  // availableAuthority lives in agents.js (which this test, loading only
  // hiring.js, doesn't pull in) -- stub it to return the roster's admin, the
  // same authority figure the production function resolves.
  setGlobal('availableAuthority', () => roster.find(d => d.isAdmin) || null);
  const logged = [];
  const toasted = [];
  setGlobal('logVillageAction', (agentId, action, details) => logged.push({ agentId, action, details }));
  setGlobal('showToast', (msg) => toasted.push(msg));
  setGlobal('lastHireAt', 0);
  setGlobal('lastHireCapNoticeAt', 0);
  return { roster, agents, logged, toasted };
}

console.log('villager headcount cap (hiring.js)');

await test('attemptAutoHire refuses once the roster is at MAX_TOTAL_AGENTS', async () => {
  const max = getGlobal('MAX_TOTAL_AGENTS');
  const { logged, toasted } = baseSetup(max);
  const started = await attemptAutoHire();
  assert.equal(started, false, 'should not start a hire at the cap');
  assert.equal(logged.length, 1, 'should log the cap-blocked fact exactly once');
  assert.equal(logged[0].action, 'hire_blocked_at_cap');
  assert.equal(logged[0].details.rosterSize, max);
  assert.equal(toasted.length, 1, 'should surface a real toast, not just a silent log line');
});

await test('a second call within the notice cooldown does not log/toast again', async () => {
  const max = getGlobal('MAX_TOTAL_AGENTS');
  const { logged, toasted } = baseSetup(max);
  await attemptAutoHire();
  await attemptAutoHire();
  assert.equal(logged.length, 1, 'the 30-minute notice cooldown should suppress the repeat');
  assert.equal(toasted.length, 1);
});

await test('a roster one under the cap is not blocked by the cap check', async () => {
  const max = getGlobal('MAX_TOTAL_AGENTS');
  const { logged, agents, roster } = baseSetup(max - 1);
  // Admin (agent0) must be free for attemptAutoHire to proceed past the
  // cap check into the real (mocked-out) hiring flow.
  setGlobal('whoNeedsHelp', async () => null); // no one needs help -- stop right after the cap check, before any real hire machinery runs
  const started = await attemptAutoHire();
  assert.equal(started, false); // whoNeedsHelp returned null, but for a REAL reason, not the cap
  assert.equal(logged.length, 0, 'a roster under the cap should never log the cap-blocked notice');
});

await test('hireSpecialist also refuses and notes the cap, without calling any hiring machinery', async () => {
  const max = getGlobal('MAX_TOTAL_AGENTS');
  const { logged, toasted } = baseSetup(max);
  const result = await hireSpecialist('agent0', 'Tester', 'small', 'testing the cap');
  assert.equal(result, null);
  assert.equal(logged.length, 1);
  assert.equal(logged[0].action, 'hire_blocked_at_cap');
  assert.equal(toasted.length, 1);
});

console.log('\nan off-duty admin is not treated as available');

await test('attemptAutoHire does not start a hire for an off-duty admin, even though she is not busy', async () => {
  // Real bug caught live: this check only looked at .busy, so an
  // off-duty (resting) admin -- not busy, just asleep -- was treated as
  // available. finishHire() then unconditionally sets visible=true on
  // completion without ever restoring offDuty, leaving her stuck
  // offDuty=true/visible=true simultaneously, an impossible combination
  // that also (depending on her stale x/y) can leave her visible
  // somewhere she should never be seen.
  const { roster } = baseSetup(5);
  const agents = getGlobal('AGENTS');
  agents[roster[0].id].offDuty = true; // the one admin (i===0), resting
  setGlobal('AGENTS', agents);
  setGlobal('whoNeedsHelp', async () => { throw new Error('should never be reached -- the admin check must fail first'); });
  const started = await attemptAutoHire();
  assert.equal(started, false);
});

console.log('\na fresh hire starts off-duty (dormant) when the active cap is already full');

await test('hireSpecialist creates the agent but does not walk her out when canActivateAnother() is false', async () => {
  // Real ask (2026-09-20): the total inventory (MAX_TOTAL_AGENTS) can be
  // much larger than the active ceiling (MAX_ACTIVE_AGENTS, tasks.js).
  // Hiring someone new for a fresh skill set while the village is
  // already at its active ceiling should still succeed -- she joins the
  // inventory -- but she must NOT walk out into an already-full active
  // roster. canActivateAnother() lives in tasks.js, not loaded into this
  // hiring.js-only vm context, so it's mocked here the same way
  // pickFreeSpot/spawnAgentAtFreeSpot already have to be.
  const { agents } = baseSetup(5);
  setGlobal('pickFreeSpot', () => ({ x: 0, y: 0 }));
  setGlobal('canActivateAnother', () => false);
  setGlobal('spawnAgentAtFreeSpot', () => { throw new Error('should never be called -- the active cap is full'); });
  setGlobal('agentFetch', async () => ({ ok: false, json: async () => ({ error: true }) }));
  const id = await hireSpecialist('agent0', 'Tester', 'small', 'testing the active cap');
  assert.ok(id, 'the hire itself should still succeed');
  const created = getGlobal('AGENTS')[id];
  assert.equal(created.offDuty, true, 'should join the inventory dormant, not walk out');
  assert.equal(created.visible, false);
});

await test('hireSpecialist walks her out normally when there is room to be active', async () => {
  const { agents } = baseSetup(5);
  setGlobal('pickFreeSpot', () => ({ x: 0, y: 0 }));
  setGlobal('canActivateAnother', () => true);
  let spawned = null;
  setGlobal('spawnAgentAtFreeSpot', (id) => { spawned = id; });
  setGlobal('agentFetch', async () => ({ ok: false, json: async () => ({ error: true }) }));
  const id = await hireSpecialist('agent0', 'Tester', 'small', 'testing the active cap');
  assert.ok(id);
  assert.equal(spawned, id, 'should appear on the map for real when there is room to be active');
});

console.log('\nname generation replaces the exhausted fixed pool (2026-09-21)');

await test('pickFallbackHireName finds a real numbered variant once the whole pool is taken', () => {
  // Real bug this whole feature exists to fix: the old fixed 10-name
  // pool was already exhausted at a real 17-agent roster, silently
  // failing every hire from then on regardless of MAX_TOTAL_AGENTS.
  const pool = getGlobal('HIRE_NAME_POOL');
  const roster = pool.map(n => ({ id: n.toLowerCase(), name: n }));
  setGlobal('AGENT_ROSTER', roster);
  const name = pickFallbackHireName();
  assert.ok(name, 'should still produce a real name, not give up');
  assert.ok(/^[A-Za-z]+2$/.test(name), `expected a numbered variant like "Marcus2", got "${name}"`);
});

await test('generateHireProfile uses the model-provided name when it is genuinely new', async () => {
  setGlobal('AGENT_ROSTER', [{ id: 'faye', name: 'Faye', isAdmin: true }, { id: 'dev', name: 'Dev' }]);
  setGlobal('agentFetch', async () => ({
    ok: true,
    json: async () => ({ reply: JSON.stringify({ name: 'Priya', mission: 'Help Dev.', instructions: ['Report to Dev.'], notes: ['Hired for overflow.'] }) }),
  }));
  const result = await generateHireProfile({ id: 'faye', name: 'Faye' }, 'Assistant to Dev', { id: 'dev', name: 'Dev', role: 'Studio' });
  assert.equal(result.name, 'Priya');
  assert.equal(result.mission, 'Help Dev.');
});

await test('generateHireProfile rejects a name that collides with the real roster, but keeps the profile', async () => {
  // Real risk this guards against: the model was TOLD the used-names
  // list, but "asked nicely" isn't "verified" -- a hallucinated or
  // ignored constraint here would otherwise silently collide with an
  // existing agent's own id the moment finishHire() writes AGENTS[id].
  setGlobal('AGENT_ROSTER', [{ id: 'faye', name: 'Faye', isAdmin: true }, { id: 'dev', name: 'Dev' }, { id: 'priya', name: 'Priya' }]);
  setGlobal('agentFetch', async () => ({
    ok: true,
    json: async () => ({ reply: JSON.stringify({ name: 'Priya', mission: 'Help Dev.', instructions: ['Report to Dev.'], notes: ['Hired for overflow.'] }) }),
  }));
  const result = await generateHireProfile({ id: 'faye', name: 'Faye' }, 'Assistant to Dev', { id: 'dev', name: 'Dev', role: 'Studio' });
  assert.equal(result.name, null, 'a colliding name must never be trusted, even though the model produced it');
  assert.equal(result.mission, 'Help Dev.', 'the rest of the generated profile should still be usable');
});

await test('hireSpecialist falls back to a pool name when the model call fails outright', async () => {
  const { agents } = baseSetup(5);
  setGlobal('pickFreeSpot', () => ({ x: 0, y: 0 }));
  setGlobal('canActivateAnother', () => true);
  setGlobal('spawnAgentAtFreeSpot', () => {});
  setGlobal('agentFetch', async () => { throw new Error('network down'); });
  const id = await hireSpecialist('agent0', 'Tester', 'small', 'testing the name fallback');
  assert.ok(id, 'the hire should still succeed via the fallback name, not fail outright');
  assert.ok(getGlobal('HIRE_NAME_POOL').some(n => n.toLowerCase() === id), 'expected a real pool name as the fallback id');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
