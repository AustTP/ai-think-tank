// Real regression test for the single most expensive bug found in this
// project: an idle think tank that never stopped spending. TASK_POOL is a
// fixed list that ALWAYS has entries, so the old pickNextTask() always
// found "work," every idle agent was always assigned something, and the
// 6-second cycle burned 2-3 real Jev calls per agent indefinitely --
// confirmed live at 247 firing reviews and a continuous stream of
// `decide` calls with nobody waiting on any of it.
//
// The contract these tests lock in: an empty WORK_QUEUE means runTaskCycle
// makes ZERO calls -- not fewer, not cheaper, none -- and the shared
// thinkTankHasWork() gate that hiring/firing also use agrees.
//
// Run: node tests/test_idle_quiet.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise, setTimeout, clearTimeout };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'tasks.js'), 'utf8'), context, { filename: 'tasks.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}

// canActivateAnother() (tasks.js) references MAX_ACTIVE_AGENTS, a real
// const defined in hiring.js -- not loaded into this tasks.js-only
// context. A generous default here keeps every existing test (which
// isn't about the active cap at all) from throwing a ReferenceError;
// tests specifically about the cap override this to a small number.
setGlobal('MAX_ACTIVE_AGENTS', 1000);
const runTaskCycle = () => vm.runInContext('runTaskCycle', context)();
const thinkTankHasWork = () => vm.runInContext('thinkTankHasWork', context)();
const queueWork = (...a) => vm.runInContext('queueWork', context)(...a);
const spawnAgentAtFreeSpot = (...a) => vm.runInContext('spawnAgentAtFreeSpot', context)(...a);
const sendAgentOffDuty = (...a) => vm.runInContext('sendAgentOffDuty', context)(...a);
const appearFromOutskirts = (...a) => vm.runInContext('appearFromOutskirts', context)(...a);
const activeAgentCount = () => vm.runInContext('activeAgentCount', context)();
const canActivateAnother = () => vm.runInContext('canActivateAnother', context)();
const runWorkroomTask = (...a) => vm.runInContext('runWorkroomTask', context)(...a);
const assignTask = (...a) => vm.runInContext('assignTask', context)(...a);
const assignBigTask = (...a) => vm.runInContext('assignBigTask', context)(...a);
const runResearchTask = (...a) => vm.runInContext('runResearchTask', context)(...a);
const finishTask = (...a) => vm.runInContext('finishTask', context)(...a);
const runReviewTask = (...a) => vm.runInContext('runReviewTask', context)(...a);
const arriveAtTask = (...a) => vm.runInContext('arriveAtTask', context)(...a);
const runSkillReviewTask = (...a) => vm.runInContext('runSkillReviewTask', context)(...a);
const checkSkillReviewSchedule = () => vm.runInContext('checkSkillReviewSchedule', context)();
// Captured as a direct function reference, NOT a per-call name lookup like
// the bindings above -- installCallCounters() (below) permanently
// overwrites the GLOBAL `assignTaskViaJev` with a mock for the rest of
// this file's run, and nothing ever restores it. A wrapper that re-
// resolves the name on every call would silently start calling whichever
// mock happened to be installed last by the time a later test runs this.
const assignTaskViaJevReal = vm.runInContext('assignTaskViaJev', context);
// Same direct-reference reasoning as assignTaskViaJevReal above --
// runReviewTask's own tests further down mock the GLOBAL `queueWork` to
// inspect what it's called with, and never restore it. Anything that
// calls the real queueWork (checkResearchSchedule, checkSkillReview-
// Schedule) after those tests have run needs this real reference back.
const queueWorkReal = vm.runInContext('queueWork', context);

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

// Every outbound call path the cycle could take, counted. If any of these
// fires while the think tank is idle, the test fails -- which is the whole
// point, since "idle but cheap" was never the ask.
let calls;
function installCallCounters() {
  calls = { jev: 0, assignSolo: 0, assignPair: 0 };
  setGlobal('requestJevChoice', async () => { calls.jev++; return null; });
  setGlobal('assignTaskViaJev', async () => { calls.assignSolo++; return { assignedTo: 'someone' }; });
  setGlobal('assignPairTask', async () => { calls.assignPair++; return { assignedTo: 'someone' }; });
  setGlobal('logThinkTankAction', () => {});
}

function setRoster(idleWorkerCount) {
  const roster = [], agents = {};
  for (let i = 0; i < idleWorkerCount; i++) {
    roster.push({ id: 'w' + i, isAdmin: false });
    agents['w' + i] = { id: 'w' + i, busy: false, task: null, pairWith: null, offDuty: false, handoff: null };
  }
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
}

console.log('idle think tank makes zero API calls (tasks.js)');

await test('an empty queue means the cycle makes NO calls at all, however many agents are idle', async () => {
  installCallCounters();
  setRoster(9);
  setGlobal('WORK_QUEUE', []);
  await runTaskCycle();
  assert.equal(calls.jev, 0, 'expected zero Jev calls while idle');
  assert.equal(calls.assignSolo, 0, 'expected zero solo assignments while idle');
  assert.equal(calls.assignPair, 0, 'expected zero pair assignments while idle');
});

await test('thinkTankHasWork() is false when nothing is queued and nobody is working', async () => {
  setRoster(5);
  setGlobal('WORK_QUEUE', []);
  assert.equal(thinkTankHasWork(), false);
});

await test('thinkTankHasWork() is true as soon as real work is queued', async () => {
  setRoster(5);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'a real requested task', room: 'library' }]);
  assert.equal(thinkTankHasWork(), true);
});

await test('thinkTankHasWork() is true while an agent is still mid-task, even with an empty queue', async () => {
  setRoster(2);
  setGlobal('WORK_QUEUE', []);
  const agents = getGlobal('AGENTS');
  agents.w0.task = { id: 'something-in-flight' };
  setGlobal('AGENTS', agents);
  assert.equal(thinkTankHasWork(), true);
});

await test('queued work IS assigned -- the gate stops idle spend, it does not stop real work', async () => {
  installCallCounters();
  setRoster(3);
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'task one', room: 'library' },
    { title: 'task two', room: 'bank', pair: true },
  ]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1, 'expected the non-pair item to be assigned solo');
  assert.equal(calls.assignPair, 1, 'expected the pair-flagged item to be assigned as a pair');
  assert.equal(getGlobal('WORK_QUEUE').length, 0, 'expected the queue to drain');
});

await test('the cycle never assigns more items than there are idle agents', async () => {
  installCallCounters();
  setRoster(2);
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'one', room: 'library' }, { title: 'two', room: 'bank' },
    { title: 'three', room: 'media' }, { title: 'four', room: 'postoffice' },
  ]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 2, 'expected exactly one assignment per idle agent');
  assert.equal(getGlobal('WORK_QUEUE').length, 2, 'expected the rest to stay queued for a later tick');
});

await test('a failing item is only attempted ONCE per cycle, even with a large roster (2026-09-21)', async () => {
  // Real bug caught live: without excluding an already-attempted item
  // from _pickNextDueIndex, a failed-and-requeued item could be pulled
  // AGAIN within the same synchronous loop (bounded by rosterSize, often
  // 15-20 in the real think tank) -- exhausting all WORK_ITEM_MAX_ATTEMPTS
  // retries in one burst with zero real wall-clock time for genuine
  // transient congestion (agents actually walking toward a contested
  // door) to clear. Confirmed live: 3 real subtasks all abandoned within
  // about a second of each other, not spread across separate ticks.
  installCallCounters();
  let attemptCount = 0;
  setGlobal('assignTaskViaJev', async () => { attemptCount++; calls.assignSolo++; return null; }); // always fails
  setRoster(20); // large enough that the old bug could exhaust every retry within one call
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'congested for now', room: 'pressoffice' }]);
  await runTaskCycle(); // just ONE tick
  assert.equal(attemptCount, 1, 'a single cycle should only attempt a due item once, not exhaust all retries in one synchronous burst');
  assert.equal(getGlobal('WORK_QUEUE').length, 1, 'the item should still be queued for a later, real tick -- not abandoned yet');
});

await test('a permanently-unassignable item is abandoned rather than retried forever', async () => {
  installCallCounters();
  setGlobal('assignTaskViaJev', async () => { calls.assignSolo++; return null; }); // always fails
  setRoster(4);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'impossible', room: 'library' }]);
  for (let tick = 0; tick < 6; tick++) await runTaskCycle();
  assert.equal(getGlobal('WORK_QUEUE').length, 0, 'expected the doomed item to be dropped, not retried forever');
});

console.log('\nscheduled work (notBefore) -- an agent that cannot start until a specific time');

await test('a queued item scheduled for the future makes NO calls yet, even with idle agents', async () => {
  installCallCounters();
  setRoster(3);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'do this later', room: 'library', notBefore: Date.now() + 3600000 }]);
  await runTaskCycle();
  assert.equal(calls.jev, 0);
  assert.equal(calls.assignSolo, 0);
  assert.equal(getGlobal('WORK_QUEUE').length, 1, 'the scheduled item must still be sitting in the queue, untouched');
});

await test('thinkTankHasWork() is false for a queue that only has future-scheduled items', async () => {
  setRoster(5);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'do this later', room: 'library', notBefore: Date.now() + 3600000 }]);
  assert.equal(thinkTankHasWork(), false, 'nothing is actually workable yet, so this must read as idle');
});

await test('an item scheduled for the past (its time has arrived) is assigned normally', async () => {
  installCallCounters();
  setRoster(2);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'do this now', room: 'library', notBefore: Date.now() - 1000 }]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1);
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
});

await test('a due item is not blocked by a scheduled-for-later item ahead of it in the queue', async () => {
  installCallCounters();
  setRoster(1);
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'future item, queued first', room: 'library', notBefore: Date.now() + 3600000 },
    { title: 'due item, queued second', room: 'bank' },
  ]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1, 'the due item should be picked even though it is not at the front of the queue');
  const remaining = getGlobal('WORK_QUEUE');
  assert.equal(remaining.length, 1);
  assert.equal(remaining[0].title, 'future item, queued first', 'the future item must be left alone, not consumed or reordered');
});

await test('waiting for its scheduled time does not count as a failed attempt', async () => {
  installCallCounters();
  setGlobal('assignTaskViaJev', async () => { calls.assignSolo++; return null; }); // would fail if ever tried
  setRoster(3);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'not yet', room: 'library', notBefore: Date.now() + 3600000 }]);
  for (let tick = 0; tick < 10; tick++) await runTaskCycle();
  assert.equal(calls.assignSolo, 0, 'it should never even have been attempted');
  const remaining = getGlobal('WORK_QUEUE');
  assert.equal(remaining.length, 1, 'ten idle ticks must not abandon an item whose time simply has not come yet');
  assert.equal(remaining[0].attempts || 0, 0, 'waiting is not a failed attempt');
});

console.log('\ntask priority -- more important queued work is picked first');

await test('a higher-priority item is picked before an earlier-queued normal one', async () => {
  const assignedTitles = [];
  setGlobal('assignTaskViaJev', async (title) => { assignedTitles.push(title); return { assignedTo: 'someone' }; });
  setGlobal('assignPairTask', async () => ({ assignedTo: 'someone' }));
  setGlobal('logThinkTankAction', () => {});
  setRoster(1); // one idle agent per tick, so order is unambiguous
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'queued first, normal', room: 'library' },
    { title: 'queued second, urgent', room: 'library', priority: 'urgent' },
  ]);
  await runTaskCycle();
  assert.equal(assignedTitles[0], 'queued second, urgent', 'urgent work must be picked even though it was queued later');
});

await test('equal-priority items still resolve in arrival order', async () => {
  const assignedTitles = [];
  setGlobal('assignTaskViaJev', async (title) => { assignedTitles.push(title); return { assignedTo: 'someone' }; });
  setGlobal('logThinkTankAction', () => {});
  setRoster(1);
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'first', room: 'library' },
    { title: 'second', room: 'library' },
  ]);
  await runTaskCycle();
  assert.equal(assignedTitles[0], 'first');
});

await test('an unrecognized priority string is treated as normal, not rejected or crashed on', () => {
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'x', room: 'library', priority: 'super-mega-urgent' }]);
  assert.equal(getGlobal('WORK_QUEUE')[0].priority, 1); // WORK_PRIORITY.normal
});

await test('a low-priority item never jumps ahead of a due normal item, only the reverse', async () => {
  const assignedTitles = [];
  setGlobal('assignTaskViaJev', async (title) => { assignedTitles.push(title); return { assignedTo: 'someone' }; });
  setGlobal('logThinkTankAction', () => {});
  setRoster(1);
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'queued first, low', room: 'library', priority: 'low' },
    { title: 'queued second, normal', room: 'library' },
  ]);
  await runTaskCycle();
  assert.equal(assignedTitles[0], 'queued second, normal');
});

console.log('\nwaking an off-duty agent: always for a scheduled item, only as a fallback for ordinary work');

function setAllOffDutyRoster(count) {
  const roster = [], agents = {};
  for (let i = 0; i < count; i++) {
    roster.push({ id: 'w' + i, isAdmin: false });
    agents['w' + i] = { id: 'w' + i, busy: false, task: null, pairWith: null, offDuty: true, visible: false, handoff: null };
  }
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
}

await test('a scheduled item whose time has come wakes a sleeping agent to take it', async () => {
  installCallCounters();
  setGlobal('assignTaskViaJev', async (title, room, instructions, includeOffDuty) => {
    calls.assignSolo++;
    if (!includeOffDuty) return null; // mirrors the real function's own candidate filter
    return { assignedTo: 'w0' };
  });
  setAllOffDutyRoster(3);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'scheduled wake-up task', room: 'library', notBefore: Date.now() - 1000 }]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1, 'a genuinely scheduled, due item must be attempted even with nobody awake');
  assert.equal(getGlobal('WORK_QUEUE').length, 0, 'it should have been successfully assigned');
});

await test('ordinary (unscheduled) work wakes someone once every on-duty agent is off duty (the "call someone in" fallback)', async () => {
  // Real ask (2026-09-20): a larger hireable "inventory" with only some
  // agents active only works as auto-scaling if ordinary backlog can
  // pull someone in when nobody active is free -- not just a genuinely
  // scheduled item. This is the reversal of the OLD policy this same
  // test file used to lock in ("ordinary due work never wakes anyone");
  // the fallback-only guard (next test) is what keeps this from waking
  // someone who isn't actually needed.
  installCallCounters();
  setGlobal('assignTaskViaJev', async (title, room, instructions, includeOffDuty) => {
    calls.assignSolo++;
    if (!includeOffDuty) return null;
    return { assignedTo: 'w0' };
  });
  setAllOffDutyRoster(3);
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'ordinary ambient work', room: 'library' }]); // no notBefore -- not a deliberate schedule
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1, 'ordinary work should call someone in when every on-duty agent is off duty');
  assert.equal(getGlobal('WORK_QUEUE').length, 0, 'the item should have been picked up, not left waiting');
});

await test('ordinary work does NOT wake anyone while a real on-duty idle agent is still available', async () => {
  installCallCounters();
  setGlobal('assignTaskViaJev', async (title, room, instructions, includeOffDuty) => {
    calls.assignSolo++;
    assert.equal(includeOffDuty, false, 'an idle on-duty agent exists -- ordinary work must not even be allowed to consider off-duty candidates yet');
    return { assignedTo: 'awake' };
  });
  const roster = [{ id: 'awake', isAdmin: false }, { id: 'asleep', isAdmin: false }];
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', {
    awake: { id: 'awake', busy: false, task: null, pairWith: null, offDuty: false, visible: true, handoff: null },
    asleep: { id: 'asleep', busy: false, task: null, pairWith: null, offDuty: true, visible: false, handoff: null },
  });
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'ordinary ambient work', room: 'library' }]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1);
});

await test('one awake agent still only gets one item per tick even with a scheduled item also due', async () => {
  installCallCounters();
  const roster = [{ id: 'awake', isAdmin: false }, { id: 'asleep', isAdmin: false }];
  const agents = {
    awake: { id: 'awake', busy: false, task: null, pairWith: null, offDuty: false, visible: true, handoff: null },
    asleep: { id: 'asleep', busy: false, task: null, pairWith: null, offDuty: true, visible: false, handoff: null },
  };
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
  setGlobal('assignTaskViaJev', async (title, room, instructions, includeOffDuty) => {
    calls.assignSolo++;
    const id = includeOffDuty ? 'asleep' : 'awake';
    agents[id].busy = true; // a real assignment would flip this -- needed so the second iteration sees her unavailable
    return { assignedTo: id };
  });
  setGlobal('WORK_QUEUE', []);
  queueWork([
    { title: 'ordinary work for the awake one', room: 'library' },
    { title: 'scheduled work that could wake the sleeper', room: 'library', notBefore: Date.now() - 1000 },
  ]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 2, 'both the awake agent and the woken sleeper should have been used this tick');
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
});

console.log('\nspawnAgentAtFreeSpot places an agent at a free spot, on duty and visible');

await test('it marks her on-duty/visible at a free think tank spot', () => {
  // The outskirts trailhead doors are gone (2026-09-22): a woken agent /
  // new hire appears at a free spot in the main think tank, not at a door.
  setGlobal('pickFreeSpot', () => ({ x: 500, y: 500 }));
  const agents = { walker: { id: 'walker', visible: false, offDuty: false, inRoom: 'pressoffice' } };
  setGlobal('AGENTS', agents);
  const ok = spawnAgentAtFreeSpot('walker', []);
  const after = getGlobal('AGENTS').walker;
  assert.equal(ok, true);
  assert.equal(after.visible, true);
  assert.equal(after.inRoom, null);
  assert.equal(after.x, 500);
  assert.equal(after.y, 500);
});

console.log('\nsendAgentOffDuty refuses to hijack an agent who is not actually idle');

await test('a busy agent is left completely untouched', () => {
  // Mirror of the server's send_agent_off_duty (2026-09-22): off-duty is an
  // in-place vanish, refused for anyone busy/mid-task so a bulk "send everyone
  // home" call can never hijack an agent who's genuinely still working.
  const original = { id: 'busyone', visible: true, busy: true, task: 'task-1', x: 10, y: 10, path: null, inRoom: null, offDuty: false };
  setGlobal('AGENTS', { busyone: { ...original } });
  sendAgentOffDuty('busyone');
  assert.deepEqual(getGlobal('AGENTS').busyone, original, 'a busy agent should come out completely unchanged');
});

await test('an agent mid-task-walk (task set, not yet busy) is also left alone', () => {
  const original = { id: 'walker', visible: true, busy: false, task: 'task-2', x: 10, y: 10, path: [{ x: 1, y: 1 }], inRoom: null, offDuty: false };
  setGlobal('AGENTS', { walker: { ...original } });
  sendAgentOffDuty('walker');
  assert.deepEqual(getGlobal('AGENTS').walker, original, 'her real task walk should be left completely intact');
});

await test('a pair navigator (pairWith set, busy still false) is also left alone', () => {
  const original = { id: 'nav', visible: true, busy: false, task: null, pairWith: 'driver1', x: 10, y: 10, path: [{ x: 1, y: 1 }], inRoom: null, offDuty: false };
  setGlobal('AGENTS', { nav: { ...original } });
  sendAgentOffDuty('nav');
  assert.deepEqual(getGlobal('AGENTS').nav, original);
});

await test('a genuinely idle agent is vanished in place, not walked anywhere', () => {
  // No trailhead door, no path -- she rests exactly where she stands.
  setGlobal('pickFreeSpot', () => { throw new Error('sendAgentOffDuty must not spawn anywhere'); });
  const original = { id: 'idle', visible: true, busy: false, task: null, pairWith: null, handoff: null, x: 10, y: 10, path: [{ x: 1, y: 1 }], inRoom: 'pressoffice', offDuty: false };
  setGlobal('AGENTS', { idle: { ...original } });
  sendAgentOffDuty('idle');
  const after = getGlobal('AGENTS').idle;
  assert.equal(after.offDuty, true, 'she rests now');
  assert.equal(after.visible, false, 'she is gone from the map');
  assert.equal(after.x, 10, 'her position is unchanged -- she vanished in place');
  assert.equal(after.y, 10);
  assert.equal(after.path, null, 'no walk-home path is issued');
  assert.equal(after.inRoom, null, 'she is absent indoors too');
});

console.log('\nfinishTask makes her visible again (arriveAtTask made her invisible to work "inside")');

function baseFinishTaskSetup(extraAgentFields = {}) {
  setGlobal('TASKS', { 't1': { id: 't1', title: 'Test task', room: 'pressoffice', status: 'working', entryX: 50, entryY: 60 } });
  setGlobal('AGENTS', {
    worker: { id: 'worker', name: 'Worker', visible: false, busy: true, task: 't1', inRoom: 'pressoffice',
              approvedCount: 0, profile: { notes: [] }, x: 0, y: 0, ...extraAgentFields },
  });
  setGlobal('writeLibraryFile', () => {});
  setGlobal('showToast', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('lastTaskCompletedAt', {});
}

await test('with no handoff dependency, she becomes visible again immediately', () => {
  baseFinishTaskSetup();
  setGlobal('attemptHandoff', async () => false); // nothing depends on this room right now
  finishTask('worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(a.visible, true, 'real bug caught live: arriveAtTask() sets visible=false and nothing ever set it back, freezing her walk home forever');
  assert.equal(a.busy, false);
  assert.equal(a.task, null);
});

await test('two agents finishing the same room land on genuinely different spots, not the identical pixel', () => {
  // Real deadlock caught live: two agents finishing the SAME task.room
  // reset to the identical entryX/entryY -- tickAgentMovement's
  // co-located exemption only ever buys the FIRST one a single step of
  // separation, nowhere near enough to clear AGENT_W (20px) at real
  // per-frame movement speed, so a subsequent walk from that shared
  // point (off duty, a handoff) genuinely deadlocked: stuckTimer cycled
  // 0->TASK_STUCK_TIMEOUT->0 forever, real position never moving.
  setGlobal('AGENT_W', 20);
  const AGENT_W = getGlobal('AGENT_W');
  baseFinishTaskSetup({ x: 50, y: 60 }); // matches task.entryX/entryY exactly -- as if she'd already have landed there
  setGlobal('attemptHandoff', async () => false);
  const agents = getGlobal('AGENTS');
  agents.other = { id: 'other', visible: true, x: 50, y: 60 }; // already sitting exactly on the shared entry point
  setGlobal('AGENTS', agents);
  finishTask('worker');
  const a = getGlobal('AGENTS').worker;
  assert.ok(a.x !== 50 || a.y !== 60, 'should not land on the exact same pixel as a real occupant');
  const apart = Math.hypot(a.x - 50, a.y - 60);
  assert.ok(apart >= AGENT_W, `should step aside by at least a full agent-width, only ${apart.toFixed(1)}px apart`);
});

await test('with no one else there, she lands on the exact entry point as always (no unnecessary jitter)', () => {
  baseFinishTaskSetup({ x: 50, y: 60 });
  setGlobal('attemptHandoff', async () => false);
  finishTask('worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(a.x, 50);
  assert.equal(a.y, 60);
});

await test('even when a handoff DOES trigger, she is visible for that walk too, not still hidden from the room', () => {
  // The exact scenario that surfaced this live: attemptHandoff's own
  // success path (handoffs.js) never touched a.visible either, so if
  // finishTask itself didn't fix it first, she'd get a real path to the
  // handoff and then never actually move -- frozen with a path that just
  // sits there, invisible, forever (tickAgentMovement skips invisible
  // agents outright).
  baseFinishTaskSetup();
  let handoffCalledWith = null;
  setGlobal('attemptHandoff', async (fromId) => { handoffCalledWith = fromId; return true; });
  finishTask('worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(a.visible, true, 'she must be visible the instant finishTask runs, regardless of what attemptHandoff later decides');
});

console.log('\nappearFromOutskirts places a called-in agent at a free spot, on duty/visible');

await test('she is placed at a free think tank spot and marked on-duty/visible, not at a trailhead door', () => {
  // Real ask (2026-09-20): calling someone in should be a visible event --
  // she appears on the map and walks from there, not an instant pop-in
  // wherever her stale x/y happened to be left. With the outskirts trailhead
  // doors gone (2026-09-22), the free spot IS the arrival point.
  setGlobal('pickFreeSpot', () => ({ x: 300, y: 320 }));
  setGlobal('AGENTS', {});
  const agent = { id: 'w0', offDuty: true, visible: false, inRoom: 'somewhere-stale', x: 9999, y: 9999 };
  appearFromOutskirts(agent);
  assert.equal(agent.offDuty, false);
  assert.equal(agent.visible, true);
  assert.equal(agent.inRoom, null);
  assert.equal(agent.x, 300, 'she should land on the free spot pickFreeSpot returned');
  assert.equal(agent.y, 320);
});

console.log('\nactiveAgentCount/canActivateAnother -- the active headcount ceiling');

await test('counts everyone currently on duty, admins included, ignores who is off duty', () => {
  setGlobal('MAX_ACTIVE_AGENTS', 1000);
  setGlobal('AGENT_ROSTER', [
    { id: 'a', isAdmin: true }, { id: 'b', isAdmin: false }, { id: 'c', isAdmin: false },
  ]);
  setGlobal('AGENTS', {
    a: { id: 'a', offDuty: false }, b: { id: 'b', offDuty: true }, c: { id: 'c', offDuty: false },
  });
  assert.equal(activeAgentCount(), 2);
});

await test('canActivateAnother is false once active count reaches the cap', () => {
  setGlobal('MAX_ACTIVE_AGENTS', 2);
  setGlobal('AGENT_ROSTER', [{ id: 'a', isAdmin: false }, { id: 'b', isAdmin: false }]);
  setGlobal('AGENTS', { a: { id: 'a', offDuty: false }, b: { id: 'b', offDuty: false } });
  assert.equal(canActivateAnother(), false);
});

console.log('\nMAX_ACTIVE_AGENTS is a hard ceiling on waking anyone, scheduled or not');

await test('a due SCHEDULED item still waits if waking someone would exceed the active cap', async () => {
  // Real ask (2026-09-20): 25 (here, 1 for the test) is a hard ceiling on
  // who may be online or freshly called in for a scheduled item -- not a
  // preference. Even a genuinely scheduled item (which can normally
  // always wake someone) must wait if the think tank is already at its
  // active ceiling, rather than pushing past it.
  installCallCounters();
  setGlobal('MAX_ACTIVE_AGENTS', 1);
  setGlobal('AGENT_ROSTER', [
    { id: 'active1', isAdmin: false }, { id: 'asleep1', isAdmin: false },
  ]);
  setGlobal('AGENTS', {
    active1: { id: 'active1', busy: true, task: 'somewhere-else', pairWith: null, offDuty: false, handoff: null }, // already active, but busy -- not a candidate itself
    asleep1: { id: 'asleep1', busy: false, task: null, pairWith: null, offDuty: true, visible: false, handoff: null },
  });
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'scheduled item, cap already full', room: 'library', notBefore: Date.now() - 1000 }]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 0, 'nobody should even have been attempted -- the active cap is already full');
  assert.equal(getGlobal('WORK_QUEUE').length, 1, 'the item should still be waiting, not lost');
});

await test('the same scheduled item IS picked up once a slot frees (active count drops below the cap)', async () => {
  installCallCounters();
  setGlobal('assignTaskViaJev', async (title, room, instructions, includeOffDuty) => {
    calls.assignSolo++;
    if (!includeOffDuty) return null;
    return { assignedTo: 'asleep1' };
  });
  setGlobal('MAX_ACTIVE_AGENTS', 1);
  setGlobal('AGENT_ROSTER', [{ id: 'asleep1', isAdmin: false }]);
  setGlobal('AGENTS', {
    asleep1: { id: 'asleep1', busy: false, task: null, pairWith: null, offDuty: true, visible: false, handoff: null },
  });
  setGlobal('WORK_QUEUE', []);
  queueWork([{ title: 'scheduled item, room now available', room: 'library', notBefore: Date.now() - 1000 }]);
  await runTaskCycle();
  assert.equal(calls.assignSolo, 1, 'active count is 0 (nobody currently on duty), so there is real room under the cap of 1');
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
});

console.log('\nassignTask stores the REAL walk-in resting spot, not the pre-relaxation door target (2026-09-21)');

await test('entryX/entryY match the path\'s own actual last waypoint, not the raw door target', () => {
  // Real bug caught live: this used to store the RAW, pre-relaxation
  // door target (door.x+w/2, door.y+h+4) as entryX/entryY. findPath
  // itself already relaxes a contested/trap target cell to the nearest
  // one that actually passes cellFitsAgent -- but finishTask() later
  // teleports her EXACTLY to entryX/entryY with no such check, so if
  // the raw target's own cell happened to fail cellFitsAgent (confirmed
  // live on a real door), every agent finishing a task there was
  // teleported straight into the same trap findPath had correctly
  // routed AROUND on the way in.
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('ROOM_DOOR_TRIGGERS', { library: { x: 100, y: 100, w: 16, h: 16 } });
  // The raw target would be (108, 124) -- deliberately return a path
  // whose LAST waypoint is somewhere else, simulating a relaxed route.
  setGlobal('findPath', () => [{ x: 50, y: 50 }, { x: 90, y: 108 }]);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, x: 0, y: 0 } });
  const task = assignTask('dev', 'Digest a feed', 'library');
  assert.equal(task.entryX, 90, 'expected entryX to match the path\'s real last waypoint, not the raw 108');
  assert.equal(task.entryY, 108, 'expected entryY to match the path\'s real last waypoint, not the raw 124');
});

console.log('\nassignTask carries the real task content through, not just title/room');

await test('TASKS[id] stores instructions and projectLabel', () => {
  // Real fix (2026-09-21, hiring-to-shutdown audit): a subtask's own
  // instructions used to be dropped after the Jev "who should do this"
  // call, and the broader goal it came from was never attached at all --
  // neither ever reached arriveAtTask's real-work dispatch.
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('ROOM_DOOR_TRIGGERS', { library: { x: 100, y: 100, w: 16, h: 16 } });
  setGlobal('findPath', () => [{ x: 110, y: 120 }]);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, x: 0, y: 0 } });
  const task = assignTask('dev', 'Digest a feed', 'library', 'Summarize the latest post for the player.', 'Build a small daily news digest');
  assert.ok(task);
  assert.equal(task.instructions, 'Summarize the latest post for the player.');
  assert.equal(task.projectLabel, 'Build a small daily news digest');
});

console.log('\nrunWorkroomTask acts on the real task content when it has real project lineage');

await test('calls the real coding pipeline when the task has a projectLabel', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  let calledWith = null;
  setGlobal('runCodingTask', async (agentId, sandboxId, backlogItem, contextSummary, projectLabel) => {
    calledWith = { agentId, sandboxId, backlogItem, contextSummary, projectLabel };
    return { ok: true };
  });
  setGlobal('agentFetch', async () => ({ json: async () => ({ allowed: true, stdout: 'index.html\n' }) }));
  setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
  const task = { title: 'Write the page skeleton', instructions: 'Start with a basic layout.', projectLabel: 'Build a simple to-do list app' };
  await runWorkroomTask('dev', task);
  assert.ok(calledWith, 'expected runCodingTask to actually be called');
  assert.equal(calledWith.sandboxId, 'workroom-shared');
  assert.equal(calledWith.projectLabel, 'Build a simple to-do list app');
  assert.equal(calledWith.backlogItem, 'Write the page skeleton -- Start with a basic layout.');
  assert.ok(calledWith.contextSummary.includes('index.html'), 'expected the real sandbox listing to be included as context');
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.equal(notes[0], 'Worked in the shared Work Room sandbox on: Write the page skeleton.');
});

await test('falls back to the fixed health-check when there is no real project lineage', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('runCodingTask', async () => { throw new Error('should never be called without a projectLabel'); });
  setGlobal('agentFetch', async () => ({ json: async () => ({ failedStep: null }) }));
  await runWorkroomTask('dev', { title: 'Ambient tooling check' }); // no projectLabel
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.equal(notes[0], 'Checked and ran the shared tooling in the Work Room -- all good.');
});

console.log('\nassignBigTask attaches the real goal to every subtask it queues');

await test('every queued subtask carries the real goal it came from', async () => {
  setGlobal('AGENT_ROSTER', [{ id: 'faye', isAdmin: true }]);
  setGlobal('AGENTS', { faye: { id: 'faye', name: 'Faye', busy: false } });
  setGlobal('MODEL_TIERS', { planning: { slug: 'test/planning-model' } }); // real const lives in hiring.js, not loaded into this tasks.js-only context
  setGlobal('agentFetch', async () => ({
    ok: true,
    json: async () => ({ reply: JSON.stringify({ subtasks: [
      { title: 'Write the page skeleton', room: 'pressoffice', instructions: 'Start with a basic layout.' },
    ] }) }),
  }));
  setGlobal('logThinkTankAction', () => {});
  setGlobal('WORK_QUEUE', []);
  const result = await assignBigTask('Build a simple to-do list app');
  assert.ok(!result.error, `expected a real plan, got error: ${result.error}`);
  const queued = getGlobal('WORK_QUEUE');
  assert.equal(queued.length, 1);
  assert.equal(queued[0].goal, 'Build a simple to-do list app');
});

console.log('\nassignBigTask tells the planner what each room actually does (2026-09-21)');

await test('the planning prompt names pressoffice as the real coding room, not a bare key', async () => {
  // Real bug caught live: the planner used to get nothing but bare room
  // keys ("pressoffice, observatory, library, ...") with no indication
  // of what any of them actually do -- confirmed live, it repeatedly
  // assigned real coding work to library/observatory instead of
  // pressoffice, apparently guessing from the room name alone.
  setGlobal('AGENT_ROSTER', [{ id: 'faye', isAdmin: true }]);
  setGlobal('AGENTS', { faye: { id: 'faye', name: 'Faye', busy: false } });
  setGlobal('MODEL_TIERS', { planning: { slug: 'test/planning-model' } });
  let capturedPrompt = null;
  setGlobal('agentFetch', async (path, agentId, opts) => {
    const body = JSON.parse(opts.body);
    capturedPrompt = body.messages[0].content;
    return { ok: true, json: async () => ({ reply: JSON.stringify({ subtasks: [{ title: 'x', room: 'pressoffice', instructions: 'y' }] }) }) };
  });
  setGlobal('logThinkTankAction', () => {});
  setGlobal('WORK_QUEUE', []);
  await assignBigTask('Build a simple to-do list app');
  assert.ok(capturedPrompt, 'expected the planning call to actually happen');
  assert.ok(capturedPrompt.includes('sandboxed software development'), 'expected pressoffice to be described as the real coding room, not just named');
  assert.ok(capturedPrompt.includes('No automated work happens here at all'), 'expected the rooms with no real dispatch to say so honestly');
});

console.log('\nrunResearchTask acts on the real task content when it has real project lineage (2026-09-21)');

await test('makes a real model call about the specific subtask and logs a genuine finding', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/research-model', label: 'test' }));
  let chatCall = null, pipelineCall = null, libraryCall = null;
  setGlobal('agentFetch', async (path, agentId, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (path === '/api/chat') {
      chatCall = body;
      return { ok: true, json: async () => ({ reply: 'Vanilla JS with a single global counter variable is simplest for a page this small.' }) };
    }
    if (path === '/api/pipeline') {
      pipelineCall = body;
      return { json: async () => ({ failedStep: null }) };
    }
    if (path === '/api/library/file') { libraryCall = body; return { json: async () => ({}) }; }
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('writeLibraryFile', (agentId, path, content) => { libraryCall = { agentId, path, content }; });
  const task = { id: 'task-9', title: 'Research counter implementation approaches', instructions: 'Compare a few simple ways to track state.', projectLabel: 'Build a simple to-do list app' };
  await runResearchTask('dev', task);
  assert.ok(chatCall, 'expected a real model call about the specific subtask');
  assert.ok(chatCall.messages[0].content.includes('Research counter implementation approaches'), 'expected the actual task title in the prompt, not a canned one');
  assert.ok(pipelineCall, 'expected the finding to be logged into the real sandbox');
  assert.ok(pipelineCall.steps[0].command.includes('Vanilla JS with a single global counter'), 'expected the real finding text in the logged command');
  assert.ok(libraryCall, 'expected a real, findable Library record, not just a private note');
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.equal(notes[0], 'Researched "Research counter implementation approaches" for real and logged the finding.');
});

await test('falls back to the fixed log-and-tally when there is no real project lineage', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('pickModelTierForAction', async () => { throw new Error('should never be called without a projectLabel'); });
  setGlobal('agentFetch', async () => ({ json: async () => ({ failedStep: null }) }));
  await runResearchTask('dev', { title: 'Ambient research check' }); // no projectLabel
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.equal(notes[0], 'Reviewed and logged this week\'s findings in the Research Center.');
});

console.log('\nDEDICATED_PROJECT_ROLES: review/QA/UI-research roles are now generally delegatable (2026-09-21)');

function baseAssignTaskViaJevSetup(roster, agents) {
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('ROOM_DOOR_TRIGGERS', { pressoffice: { x: 100, y: 100, w: 16, h: 16 } });
  setGlobal('findPath', () => [{ x: 90, y: 108 }]);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('appearFromOutskirts', () => {});
  setGlobal('requestJevChoice', async () => null); // forces the "fall back to candidates[0]" path -- proves who WAS a candidate
}

await test('a Code Reviewer is a real candidate for general work, not silently excluded', async () => {
  baseAssignTaskViaJevSetup(
    [{ id: 'rev', name: 'Rev', role: 'Code Reviewer', isAdmin: false, profile: { mission: 'review things' } }],
    { rev: { id: 'rev', busy: false, task: null, pairWith: null, offDuty: false, x: 0, y: 0, profile: { mission: 'review things' } } }
  );
  const task = await assignTaskViaJevReal('Do something', 'pressoffice', 'instructions');
  assert.ok(task, 'expected a Code Reviewer to be assignable to general delegated work');
  assert.equal(task.assignedTo, 'rev');
});


console.log('\ntaskType threading: assignTask stores it, defaults to "code"');

await test('TASKS[id].taskType matches what was passed through', () => {
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('ROOM_DOOR_TRIGGERS', { pressoffice: { x: 100, y: 100, w: 16, h: 16 } });
  setGlobal('findPath', () => [{ x: 90, y: 108 }]);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, x: 0, y: 0 } });
  const task = assignTask('dev', 'Review the app', 'pressoffice', 'instructions', 'Build a to-do app', { taskType: 'review' });
  assert.equal(task.taskType, 'review');
});

await test('taskType defaults to "code" when omitted', () => {
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, x: 0, y: 0 } });
  const task = assignTask('dev', 'Write the app', 'pressoffice', 'instructions', 'Build a to-do app');
  assert.equal(task.taskType, 'code');
});

console.log('\nrunReviewTask: the generalized review/QA pass (2026-09-21)');

function baseReviewTaskSetup() {
  setGlobal('AGENTS', { rev: { id: 'rev', name: 'Rev', profile: { notes: [] } } });
  setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
  setGlobal('gatherUnifiedContext', async () => 'CURRENT CODE CONTEXT');
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/review-model', label: 'test' }));
  setGlobal('reviewScreenshot', async () => ({ ok: false, note: 'no screenshot in this test' }));
}

await test('a "review" task writes a firsthand (not external) Library record and reflects the actual verdict', async () => {
  baseReviewTaskSetup();
  let chatPrompt = null, libraryCall = null, queuedFollowUp = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      chatPrompt = JSON.parse(opts.body).messages[0].content;
      return { ok: true, json: async () => ({ reply: 'Found a real bug: the submit handler never checks for an empty input.' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('writeLibraryFile', async (agentId, path, content, source) => { libraryCall = { agentId, path, content, source }; });
  setGlobal('requestJevChoice', async () => ({ choice: 'actionable' }));
  setGlobal('queueWork', (items) => { queuedFollowUp = items; return 1; });

  const task = { id: 'task-review-1', title: 'Review the to-do app', taskType: 'review' };
  const result = await runReviewTask('rev', task, 'Build a to-do app');

  assert.ok(chatPrompt.includes('reviewing a teammate\'s real code'), 'expected the review-flavored system prompt, not the QA one');
  assert.ok(libraryCall, 'expected a real Library record');
  assert.equal(libraryCall.source, 'firsthand', 'this is the reviewer\'s own first-hand assessment of code she looked at directly, not content sourced from an external page -- it must not land in pending_review/');
  assert.ok(queuedFollowUp, 'an actionable review should queue a real follow-up fix');
  assert.equal(queuedFollowUp[0].room, 'pressoffice');
  assert.ok(result.ok && result.note.includes('queued a fix'));
});

await test('a "qa" task uses the QA-flavored prompt and does not queue a fix when nothing is actionable', async () => {
  baseReviewTaskSetup();
  let chatPrompt = null, queuedFollowUp = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      chatPrompt = JSON.parse(opts.body).messages[0].content;
      return { ok: true, json: async () => ({ reply: 'Played through it end to end -- menu, one challenge, and remap all worked as expected.' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('requestJevChoice', async () => ({ choice: 'clean' }));
  setGlobal('queueWork', (items) => { queuedFollowUp = items; return 1; });

  const task = { id: 'task-qa-1', title: 'QA the to-do app', taskType: 'qa' };
  const result = await runReviewTask('rev', task, 'Build a to-do app');

  assert.ok(chatPrompt.includes('QA-testing'), 'expected the QA-flavored system prompt, not the review one');
  assert.equal(queuedFollowUp, null, 'nothing actionable was found -- no fix should be queued');
  assert.ok(result.ok && result.note.includes('nothing actionable found'));
});

await test('a review can request a real runtime probe before giving its final assessment (2026-09-21)', async () => {
  baseReviewTaskSetup();
  let chatCallCount = 0;
  let probeArgs = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      chatCallCount++;
      if (chatCallCount === 1) {
        // First reply: a real probeRequest, not a final assessment.
        return { ok: true, json: async () => ({ reply: JSON.stringify({ probeRequest: { path: 'index.html', actions: [{ type: 'click', selector: 'text=Play' }], probes: ['document.body.className'] } }) }) };
      }
      return { ok: true, json: async () => ({ reply: 'After actually clicking Play, the game genuinely starts -- no real bugs found.' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('requestPageProbe', async (agentId, sandboxId, path, actions, probes) => {
    probeArgs = { path, actions, probes };
    return { ok: true, results: { 'document.body.className': 'in-game' } };
  });
  setGlobal('formatPageProbeResult', (data) => 'PROBE RESULT: ' + JSON.stringify(data));
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('requestJevChoice', async () => ({ choice: 'clean' }));
  setGlobal('queueWork', () => 1);

  const task = { id: 'task-review-probe', title: 'Review the game', taskType: 'review' };
  const result = await runReviewTask('rev', task, 'Build a game');

  assert.equal(chatCallCount, 2, 'expected one probe round then a real final answer, not a single one-shot call');
  assert.ok(probeArgs, 'expected a real requestPageProbe call, not just an offer to make one');
  assert.equal(probeArgs.path, 'index.html');
  assert.ok(result.ok);
});

console.log('\nrunWorkroomTask dispatches review/qa taskTypes to runReviewTask, not runCodingTask');

await test('taskType "review" calls runReviewTask, never runCodingTask', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('runReviewTask', async () => ({ ok: true, note: 'reviewed it' }));
  setGlobal('runCodingTask', async () => { throw new Error('should never be called for a review taskType'); });
  await runWorkroomTask('dev', { title: 'Review something', projectLabel: 'x', taskType: 'review' });
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.equal(notes[0], 'reviewed it');
});

await test('taskType "code" (the default) still calls runCodingTask, never runReviewTask', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => ({ json: async () => ({ allowed: true, stdout: '' }) }));
  setGlobal('runReviewTask', async () => { throw new Error('should never be called for a code taskType'); });
  setGlobal('runCodingTask', async () => ({ ok: true, note: 'coded it' }));
  await runWorkroomTask('dev', { title: 'Build something', projectLabel: 'x', taskType: 'code' });
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.ok(notes[0].includes('Build something'));
});

console.log('\narriveAtTask: finishTask now waits for the real dispatch, not a fixed timer (2026-09-21)');

await test('finishTask does not run until the real dispatched work actually resolves', async () => {
  let resolveDispatch;
  setGlobal('MIN_TASK_VISUAL_MS', 0); // isolate this assertion from the visual floor
  setGlobal('ROOMS', { pressoffice: { collision: 'pressoffice' } });
  setGlobal('ROOM_INTERACTABLES', {});
  setGlobal('ROOM_NATIVE_W', 100);
  setGlobal('ROOM_NATIVE_H', 100);
  setGlobal('runWorkroomTask', () => new Promise((resolve) => { resolveDispatch = resolve; }));
  setGlobal('attemptHandoff', async () => false);
  setGlobal('sendAgentOffDuty', () => {});
  setGlobal('writeLibraryFile', () => {});
  setGlobal('showToast', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('lastTaskCompletedAt', {});
  setGlobal('TASKS', { 't1': { id: 't1', title: 'Slow real task', room: 'pressoffice', entryX: 50, entryY: 60 } });
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: 't1', x: 0, y: 0, profile: { notes: [] }, approvedCount: 0 } });

  arriveAtTask('worker');
  assert.equal(getGlobal('AGENTS').worker.task, 't1', 'the real dispatch has not resolved yet -- she should not be finished');

  // A real wait, not just "immediately after" -- with MIN_TASK_VISUAL_MS
  // at 0, the OLD fixed-timer bug (setTimeout(finishTask, 0)) would have
  // already fired well within this window regardless of the still-pending
  // dispatch, so this is what actually distinguishes "waits for the real
  // work" from "waits a fixed amount of time no matter what."
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(getGlobal('AGENTS').worker.task, 't1', 'still not finished after a real wait -- the dispatch is still pending');

  resolveDispatch();
  await new Promise((r) => setTimeout(r, 20)); // let the promise chain actually flush
  assert.equal(getGlobal('AGENTS').worker.task, null, 'finishTask should have run once the real dispatch resolved');
});

await test('a minimum visual floor still applies even when the real work resolves instantly', async () => {
  setGlobal('MIN_TASK_VISUAL_MS', 40);
  setGlobal('ROOMS', { pressoffice: { collision: 'pressoffice' } });
  setGlobal('ROOM_INTERACTABLES', {});
  setGlobal('ROOM_NATIVE_W', 100);
  setGlobal('ROOM_NATIVE_H', 100);
  setGlobal('runWorkroomTask', async () => ({ ok: true, note: 'instant' })); // already resolved by the time arriveAtTask returns
  setGlobal('attemptHandoff', async () => false);
  setGlobal('sendAgentOffDuty', () => {});
  setGlobal('writeLibraryFile', () => {});
  setGlobal('showToast', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('lastTaskCompletedAt', {});
  setGlobal('TASKS', { 't2': { id: 't2', title: 'Instant real task', room: 'pressoffice', entryX: 50, entryY: 60 } });
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: 't2', x: 0, y: 0, profile: { notes: [] }, approvedCount: 0 } });

  arriveAtTask('worker');
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(getGlobal('AGENTS').worker.task, 't2', 'the minimum visual floor has not elapsed yet -- she should still look like she is working');

  await new Promise((r) => setTimeout(r, 60));
  assert.equal(getGlobal('AGENTS').worker.task, null, 'the floor has now elapsed -- she should be finished');
});

await test('a genuinely hung dispatch is still cut off by TASK_DISPATCH_TIMEOUT_MS, not left busy forever', async () => {
  setGlobal('MIN_TASK_VISUAL_MS', 0);
  setGlobal('TASK_DISPATCH_TIMEOUT_MS', 30); // tiny for the test -- the mechanism is the same regardless of the real value
  setGlobal('ROOMS', { pressoffice: { collision: 'pressoffice' } });
  setGlobal('ROOM_INTERACTABLES', {});
  setGlobal('ROOM_NATIVE_W', 100);
  setGlobal('ROOM_NATIVE_H', 100);
  setGlobal('runWorkroomTask', () => new Promise(() => {})); // never resolves -- a genuinely hung call
  setGlobal('attemptHandoff', async () => false);
  setGlobal('sendAgentOffDuty', () => {});
  setGlobal('writeLibraryFile', () => {});
  setGlobal('showToast', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('lastTaskCompletedAt', {});
  setGlobal('TASKS', { 't3': { id: 't3', title: 'Hung real task', room: 'pressoffice', entryX: 50, entryY: 60 } });
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: 't3', x: 0, y: 0, profile: { notes: [] }, approvedCount: 0 } });

  arriveAtTask('worker');
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(getGlobal('AGENTS').worker.task, 't3', 'well under the timeout -- should still look busy');

  await new Promise((r) => setTimeout(r, 60));
  assert.equal(getGlobal('AGENTS').worker.task, null, 'past the timeout -- she must be freed even though the dispatch itself never resolved');
  setGlobal('TASK_DISPATCH_TIMEOUT_MS', 180000); // restore the real default for any test that runs after this one
});

console.log('\ncheckSkillReviewSchedule: idle-cost contract, same shape as checkResearchSchedule');

await test('nothing pending, not due yet -- makes no calls and queues nothing', () => {
  setGlobal('queueWork', queueWorkReal); // restore -- runReviewTask's own tests above mock this and never restore it
  setGlobal('lastSkillReviewAt', Date.now());
  setGlobal('WORK_QUEUE', []);
  checkSkillReviewSchedule();
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
});

await test('due -- queues a real skillReview WORK_QUEUE item and stamps lastSkillReviewAt', () => {
  setGlobal('queueWork', queueWorkReal);
  setGlobal('lastSkillReviewAt', 0);
  setGlobal('WORK_QUEUE', []);
  checkSkillReviewSchedule();
  const queue = getGlobal('WORK_QUEUE');
  assert.equal(queue.length, 1);
  assert.equal(queue[0].skillReview, true);
  assert.equal(queue[0].room, 'observatory');
  assert.ok(getGlobal('lastSkillReviewAt') > 0);
});

console.log('\nrunSkillReviewTask: keeps or rejects real pending skill files (2026-09-21)');

await test('nothing pending -- says so plainly, makes no judgment calls', async () => {
  setGlobal('listLibraryFiles', async () => [{ path: 'archive/2026-09-20-task-1.md' }]); // nothing under pending_review/skills/
  setGlobal('requestJevChoice', async () => { throw new Error('should never be called with nothing pending'); });
  const note = await runSkillReviewTask('rev');
  assert.ok(note.includes('nothing waiting'));
});

await test('a genuinely useful file is promoted, not rejected', async () => {
  setGlobal('listLibraryFiles', async () => [{ path: 'pending_review/skills/think tank-governance.md' }]);
  setGlobal('readLibraryFile', async () => '# Skill: Think Tank Governance\n\nReal, specific, accurate content.');
  setGlobal("requestJevChoice", async () => ({ choice: "keep" }));
  let promoted = null, rejected = null;
  setGlobal('promoteLibraryFile', async (agentId, path) => { promoted = path; return true; });
  setGlobal('rejectLibraryFile', async (agentId, path) => { rejected = path; return true; });
  setGlobal('writeLibraryFile', async () => { throw new Error('a KEPT file should not be rewritten with rejection reasoning'); });
  const note = await runSkillReviewTask('rev');
  assert.equal(promoted, 'pending_review/skills/think tank-governance.md');
  assert.equal(rejected, null);
  assert.ok(note.includes('1 promoted, 0 rejected'));
});

await test('an inaccurate/vague file is rejected with reasoning appended, not silently deleted', async () => {
  setGlobal('listLibraryFiles', async () => [{ path: 'pending_review/skills/some-junk.md' }]);
  setGlobal('readLibraryFile', async () => 'vague, generic, not actually useful content');
  setGlobal("requestJevChoice", async () => ({ choice: "reject" }));
  let rejectedPath = null, rewrittenContent = null;
  setGlobal('promoteLibraryFile', async () => { throw new Error('a REJECTED file should never be promoted'); });
  setGlobal('rejectLibraryFile', async (agentId, path) => { rejectedPath = path; return true; });
  setGlobal('writeLibraryFile', async (agentId, path, content, source) => { rewrittenContent = { path, content, source }; });
  const note = await runSkillReviewTask('rev');
  assert.equal(rejectedPath, 'pending_review/skills/some-junk.md');
  assert.equal(rewrittenContent.path, 'pending_review/skills/some-junk.md', 'the rejection reasoning must be written to the SAME pending path before it moves, not a new one');
  assert.equal(rewrittenContent.source, 'firsthand', 'this is the reviewer\'s own judgment, not new external content -- must not get re-quarantined');
  assert.ok(rewrittenContent.content.includes('Rejected'));
  assert.ok(note.includes('0 promoted, 1 rejected'));
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
