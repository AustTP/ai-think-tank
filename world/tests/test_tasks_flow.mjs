// Deep coverage for tasks.js's real work orchestration -- everything the
// routing/queue tests only reach transitively or mock away: the room-purpose
// fetch, per-frame movement with the slide/stuck/respawn branches,
// cancelTask, pair programming (assignPairTask -> arriveAtPair ->
// requestPairLine -> runPairProgrammingSession), the sandbox context reader,
// the full runCodingTask pipeline (heredoc balance/continuation, probe
// rounds, orphaned/phantom/dangling file checks), the graded review loop,
// escalation, runWorkroomTask/runResearchTask/runMediaDigestTask dispatch
// paths, checkWeatherReference, and finishTask's edge branches.
//
// Run: node tests/test_tasks_flow.mjs
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
const call = (name, ...a) => vm.runInContext(name, context)(...a);

// setGlobal can overwrite a real module-level function (function
// declarations are live globals), so snapshot the implementations we
// intentionally stub in one section and want the real ones back in
// another.
const realFns = {};
for (const name of [
  'runPairProgrammingSession', 'requestPairLine', 'runCodingTask',
  'runGradedReviewLoop', '_produceReviewText', 'escalateReviewRequirement',
  '_extractWrittenJsFiles', '_findUnlinkedJsFiles', '_guessLinkTargetHtml',
  '_autoLinkJsFiles', '_findPhantomScriptRefs', '_removePhantomScriptRefs',
  '_findDanglingSelectorRefs',
]) {
  realFns[name] = getGlobal(name);
}
function restoreReal(...names) {
  for (const n of names) setGlobal(n, realFns[n]);
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

// ---- baseline globals every area needs (overridden per-test as needed) ----
setGlobal('DEDICATED_PROJECT_ROLES', new Set());
setGlobal('MAX_ACTIVE_AGENTS', 1000);
setGlobal('AGENT_W', 20);
setGlobal('AGENT_H', 16);
setGlobal('ROOMS', { pressoffice: { collision: 'pressoffice' } });
setGlobal('ROOM_INTERACTABLES', {});
setGlobal('ROOM_NATIVE_W', 200);
setGlobal('ROOM_NATIVE_H', 200);
setGlobal('ROOM_DOOR_TRIGGERS', {
  pressoffice: { x: 100, y: 100, w: 16, h: 16 },
  observatory: { x: 200, y: 200, w: 16, h: 16 },
  library: { x: 300, y: 300, w: 16, h: 16 },
  media: { x: 400, y: 400, w: 16, h: 16 },
});
setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
setGlobal('MODEL_TIERS', { small: { slug: 'test/small' }, mid: { slug: 'test/mid' }, planning: { slug: 'test/planning' } });
setGlobal('SKILL_FILE_FORMAT_GUIDE', 'Format guide.');
setGlobal('overlaps', () => false);
setGlobal('blockedAt', () => false);
setGlobal('agentBlockedAt', () => false);
setGlobal('pickFreeSpot', (occupied) => ({ x: 500, y: 500 }));
setGlobal('_resolveRoomWithOverflow', (room) => room);
setGlobal('logThinkTankAction', () => {});
setGlobal('showToast', () => {});
setGlobal('writeLibraryFile', async () => {});
setGlobal('readLibraryFile', async () => null);
setGlobal('listLibraryFiles', async () => []);
setGlobal('promoteLibraryFile', async () => true);
setGlobal('rejectLibraryFile', async () => true);
setGlobal('writeSkillFile', async () => {});
setGlobal('attemptHandoff', async () => false);
setGlobal('sendAgentOffDuty', () => {});
setGlobal('pickModelTierForAction', async () => ({ slug: 'test/model', label: 'test' }));
setGlobal('requestPageProbe', async () => ({ ok: true, results: {} }));
setGlobal('formatPageProbeResult', (data) => 'PROBE: ' + JSON.stringify(data));
setGlobal('reviewScreenshot', async () => ({ ok: true, review: 'looks fine' }));
setGlobal('gatherUnifiedContext', async () => 'CURRENT CONTEXT');
setGlobal('fetchPageSmart', async () => ({ allowed: true, text: 'feed page text' }));
setGlobal('readWorkingGuide', async () => null);
setGlobal('GRADE_MEETS', 'meets_requirement');
setGlobal('GRADE_FAILS', 'fails_requirement');
setGlobal('GRADE_UNSURE', 'insufficient_evidence');
setGlobal('MAX_REVISION_ROUNDS', 2);

console.log('loadRoomPurposes (tasks.js)');

await test('loads server room purposes into ROOM_PURPOSES', async () => {
  setGlobal('apiFetch', async () => ({ json: async () => ({ rooms: { pressoffice: { purpose: 'Real coding room!' }, library: { purpose: 'Books' } } }) }));
  await call('loadRoomPurposes');
  assert.equal(vm.runInContext('ROOM_PURPOSES.pressoffice', context), 'Real coding room!');
  assert.equal(vm.runInContext('ROOM_PURPOSES.library', context), 'Books');
});

await test('keeps the fallback purposes when the server is unreachable', async () => {
  vm.runInContext('ROOM_PURPOSES.pressoffice = "fallback-for-test";', context);
  setGlobal('apiFetch', async () => { throw new Error('down'); });
  await call('loadRoomPurposes');
  assert.equal(vm.runInContext('ROOM_PURPOSES.pressoffice', context), 'fallback-for-test');
});

console.log('tickAgentMovement: slide/stuck/respawn branches (tasks.js)');

function movementAgent(overrides = {}) {
  const a = {
    id: 'walker', name: 'Walker', visible: true, x: 0, y: 0, dir: 'south',
    path: [{ x: 1, y: 50 }], pathIndex: 0, pathTarget: { x: 1, y: 50 },
    stuckTimer: 0, replanCount: 0, respawnedForTask: false, ...overrides,
  };
  setGlobal('AGENTS', { walker: a });
  return a;
}

await test('falls through to the Y-only slide when both diagonal and X are blocked', () => {
  setGlobal('overlaps', () => false);
  setGlobal('agentBlockedAt', () => false);
  setGlobal('blockedAt', (box) => box.y < 5 || (box.x > 0 && box.y < 60));
  setGlobal('findPath', () => null);
  movementAgent();
  call('tickAgentMovement', 1);
  const a = getGlobal('AGENTS').walker;
  assert.equal(a.x, 0, 'x must stay put when only the y-slide is free');
  assert.ok(a.y > 59 && a.y < 60.01, `y should have slid ~60px, got ${a.y}`);
});

await test('replans once, then respawns at a free spot and retries successfully', () => {
  setGlobal('overlaps', () => false);
  setGlobal('agentBlockedAt', () => false);
  setGlobal('blockedAt', () => true); // permanently wedged where she stands
  setGlobal('pickFreeSpot', () => ({ x: 30, y: 30 }));
  let findCalls = 0;
  setGlobal('findPath', () => { findCalls++; return findCalls === 1 ? null : [{ x: 30, y: 30 }]; });
  movementAgent({ path: [{ x: 20, y: 0 }], pathTarget: { x: 20, y: 0 } });
  call('tickAgentMovement', 2.0); // one frame, dt exceeds TASK_STUCK_TIMEOUT
  const a = getGlobal('AGENTS').walker;
  assert.equal(a.respawnedForTask, true, 'the one-shot respawn should have happened');
  assert.equal(a.x, 30, 'dropped at the fresh free spot');
  assert.equal(a.y, 30);
  assert.equal(a.path[0].x, 30, 'picked up the fresh retry path');
  assert.equal(findCalls, 2, 'one failed replan + one retry from the fresh spot');
});

await test('gives up and cancels the task when even a fresh spot cannot reach the target', () => {
  setGlobal('overlaps', () => false);
  setGlobal('agentBlockedAt', () => false);
  setGlobal('blockedAt', () => true);
  setGlobal('pickFreeSpot', () => ({ x: 30, y: 30 }));
  setGlobal('findPath', () => null); // no route from anywhere
  setGlobal('TASKS', { 't1': { id: 't1', title: 'Unreachable', room: 'library', status: 'working' } });
  movementAgent({ path: [{ x: 20, y: 0 }], pathTarget: { x: 20, y: 0 }, task: 't1' });
  call('tickAgentMovement', 2.0);
  const a = getGlobal('AGENTS').walker;
  assert.equal(a.respawnedForTask, false, 'cancelTask resets the one-shot respawn flag');
  assert.equal(getGlobal('TASKS').t1.status, 'cancelled', 'the real cancelTask must run');
  assert.equal(a.task, null);
  assert.equal(a.path, null);
});

console.log('cancelTask (tasks.js)');

await test('marks a real task cancelled and clears the walk state', () => {
  setGlobal('TASKS', { 't1': { id: 't1', title: 'Mail sorting', room: 'postoffice', status: 'walking' } });
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: 't1', path: [{ x: 1, y: 1 }], pathIndex: 1, pathTarget: { x: 9, y: 9 }, stuckTimer: 0.5, replanCount: 2, respawnedForTask: true } });
  call('cancelTask', 'worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(getGlobal('TASKS').t1.status, 'cancelled');
  assert.equal(a.task, null);
  assert.equal(a.path, null);
  assert.equal(a.pathIndex, 0);
  assert.equal(a.pathTarget, null);
  assert.equal(a.respawnedForTask, false);
});

await test('handles a worker with no real task (toast says "a task")', () => {
  setGlobal('TASKS', {});
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: null, path: [{ x: 1, y: 1 }], pathIndex: 0, pathTarget: null } });
  call('cancelTask', 'worker');
  assert.equal(getGlobal('AGENTS').worker.task, null);
});

console.log('assignPairTask (tasks.js)');

function pairRoster() {
  const roster = [
    { id: 'driver', name: 'Ari', role: 'Code', isAdmin: false, profile: { mission: 'build things' } },
    { id: 'nav', name: 'Nim', role: 'Ops', isAdmin: false, profile: { mission: 'review things' } },
    { id: 'admin', name: 'Faye', role: 'Director', isAdmin: true, profile: { mission: 'lead' } },
  ];
  const agents = {
    driver: { id: 'driver', name: 'Ari', role: 'Code', busy: false, task: null, pairWith: null, path: null, offDuty: false, visible: true, x: 0, y: 0, profile: { mission: 'build things' } },
    nav: { id: 'nav', name: 'Nim', role: 'Ops', busy: false, task: null, pairWith: null, path: null, offDuty: false, visible: true, x: 10, y: 10, profile: { mission: 'review things' } },
    admin: { id: 'admin', name: 'Faye', role: 'Director', busy: false, task: null, pairWith: null, path: null, offDuty: false, visible: true, x: 20, y: 20, profile: { mission: 'lead' } },
  };
  setGlobal('AGENT_ROSTER', roster);
  setGlobal('AGENTS', agents);
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('findPath', () => [{ x: 110, y: 120 }]);
  setGlobal('pickFreeSpot', () => ({ x: 500, y: 500 }));
  return { roster, agents };
}

await test('assigns a real pair: driver drives, navigator gets a walk toward the desk', async () => {
  pairRoster();
  const picks = ['driver', 'nav'];
  let pickIdx = 0;
  setGlobal('requestJevChoice', async () => ({ choice: picks[pickIdx++] }));
  setGlobal('logThinkTankAction', () => {});
  const task = await call('assignPairTask', 'Write the module together', 'library', 'Pair up.', false, 'Build a to-do app');
  assert.ok(task, 'a pair session was created');
  assert.equal(task.assignedTo, 'driver');
  assert.equal(task.pairWith, 'nav', 'the task records who is navigating');
  const nav = getGlobal('AGENTS').nav;
  assert.equal(nav.pairWith, 'driver');
  assert.equal(nav.pairTaskId, task.id);
  assert.ok(nav.path && nav.path.length > 0, 'the navigator should have a real walk path');
  assert.equal(nav.visible, true, 'navigator stays visible walking in');
});

await test('falls back to the first pool member when Jev picks nobody', async () => {
  pairRoster();
  setGlobal('requestJevChoice', async () => null);
  const task = await call('assignPairTask', 'Write the module', 'library', 'Pair up.', false, 'Build a to-do app');
  assert.equal(task.assignedTo, 'driver', 'driver falls back to the first eligible candidate');
  assert.equal(task.pairWith, 'nav', 'navigator falls back to the remaining candidate');
});

await test('returns null when fewer than two hands are free', async () => {
  pairRoster();
  getGlobal('AGENTS').nav.busy = true;
  setGlobal('requestJevChoice', async () => { throw new Error('must not be called'); });
  const task = await call('assignPairTask', 'Write the module', 'library', 'Pair up.');
  assert.equal(task, null, 'pairing needs two free agents');
});

await test('wakes an off-duty driver before the pair starts', async () => {
  pairRoster();
  getGlobal('AGENTS').driver.offDuty = true;
  getGlobal('AGENTS').driver.visible = false;
  setGlobal('requestJevChoice', async () => ({ choice: 'driver' }));
  const task = await call('assignPairTask', 'Write the module', 'library', 'Pair up.', true, 'Build a to-do app');
  assert.ok(task);
  const driver = getGlobal('AGENTS').driver;
  assert.equal(driver.offDuty, false, 'appearFromOutskirts must wake the chosen driver');
  assert.equal(driver.visible, true);
});

await test('driver goes it alone when the navigator genuinely cannot reach the desk', async () => {
  pairRoster();
  // assignTask needs a route for the driver, but every nav-side offset fails.
  setGlobal('findPath', (x, y, tx, ty, id) => (id === 'nav' ? null : [{ x: 110, y: 120 }]));
  let pickIdx = 0;
  setGlobal('requestJevChoice', async () => ({ choice: pickIdx++ === 0 ? 'driver' : 'nav' }));
  const task = await call('assignPairTask', 'Write the module', 'library', 'Pair up.');
  assert.ok(task, 'the driver still gets her task');
  const nav = getGlobal('AGENTS').nav;
  assert.equal(nav.pairWith, null, 'the navigator is not committed to an unreachable session');
  assert.equal(nav.path, null);
});

console.log('arriveAtPair (tasks.js)');

await test('parks the navigator beside the driver and starts the pair session', () => {
  setGlobal('AGENTS', {
    nav: { id: 'nav', name: 'Nim', visible: true, busy: false, pairWith: 'driver', pairTaskId: 'task-9', path: [{ x: 1, y: 1 }], pathIndex: 0, pathTarget: { x: 2, y: 2 }, stuckTimer: 0, replanCount: 0, respawnedForTask: false },
    driver: { id: 'driver', name: 'Ari', roomX: 100, roomY: 200 },
  });
  setGlobal('TASKS', { 'task-9': { id: 'task-9', room: 'library' } });
  let sessionArgs = null;
  setGlobal('runPairProgrammingSession', (d, n, t) => { sessionArgs = { d, n, t }; });
  call('arriveAtPair', 'nav');
  const nav = getGlobal('AGENTS').nav;
  assert.equal(nav.visible, false, 'navigator works "inside" once seated');
  assert.equal(nav.busy, true);
  assert.equal(nav.inRoom, 'library');
  assert.equal(nav.roomX, 125, 'seated right beside the driver at the same desk');
  assert.equal(nav.roomY, 200);
  assert.equal(nav.dir, 'south');
  assert.deepEqual(sessionArgs, { d: 'driver', n: 'nav', t: { id: 'task-9', room: 'library' } });
});

await test('gives up cleanly when the driver or task has vanished', () => {
  setGlobal('AGENTS', {
    nav: { id: 'nav', name: 'Nim', visible: true, busy: false, pairWith: 'ghost', pairTaskId: 'task-x', path: [{ x: 1, y: 1 }], pathIndex: 0, pathTarget: null, stuckTimer: 0, replanCount: 0, respawnedForTask: false },
  });
  setGlobal('TASKS', {});
  let sessionStarted = false;
  setGlobal('runPairProgrammingSession', () => { sessionStarted = true; });
  call('arriveAtPair', 'nav');
  const nav = getGlobal('AGENTS').nav;
  assert.equal(sessionStarted, false, 'no session without a real driver');
  assert.equal(nav.pairWith, null);
  assert.equal(nav.pairTaskId, null);
  assert.equal(nav.visible, true);
});

console.log('requestPairLine (tasks.js)');

await test('returns the trimmed real reply on success', async () => {
  setGlobal('MODEL_TIERS', { small: { slug: 'test/small' }, mid: { slug: 'test/mid' } });
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ reply: '  a casual line  ' }) }));
  const line = await call('requestPairLine', { id: 'nav', name: 'Nim', role: 'Ops', model: 'mid' }, { id: 'driver', name: 'Ari' }, 'Build the app', 'last line', 'navigator');
  assert.equal(line, 'a casual line');
});

await test('uses the driver vs navigator wording in the system prompt', async () => {
  let seenPrompt = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    seenPrompt = JSON.parse(opts.body).messages[0].content;
    return { ok: true, json: async () => ({ reply: 'ok' }) };
  });
  await call('requestPairLine', { id: 'driver', name: 'Ari', role: 'Code' }, { id: 'nav', name: 'Nim' }, 'Build', 'hi', 'driver');
  assert.ok(seenPrompt.includes('DRIVING'), seenPrompt);
});

await test('nods along when the model call fails or is refused', async () => {
  setGlobal('agentFetch', async () => ({ ok: false, json: async () => ({ error: 'busy' }) }));
  const line = await call('requestPairLine', { id: 'nav', name: 'Nim', role: 'Ops', model: 'mid' }, { id: 'driver', name: 'Ari' }, 'Build', 'hi', 'navigator');
  assert.equal(line, '(Nim nods along.)');
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  const line2 = await call('requestPairLine', { id: 'nav', name: 'Nim', role: 'Ops', model: 'mid' }, { id: 'driver', name: 'Ari' }, 'Build', 'hi', 'navigator');
  assert.equal(line2, '(Nim nods along.)');
});

console.log('runPairProgrammingSession (tasks.js)');

restoreReal('runPairProgrammingSession');

function pairSessionSetup() {
  const driver = { id: 'driver', name: 'Ari', profile: { notes: [] } };
  const nav = { id: 'nav', name: 'Nim', profile: { notes: [] }, pairTaskId: 'task-1', pairWith: 'driver', busy: true, inRoom: 'pressoffice', visible: false, x: 1, y: 1 };
  setGlobal('AGENTS', { driver, nav });
  setGlobal('TASKS', { 'task-1': { id: 'task-1', title: 'Build the module', room: 'pressoffice', entryX: 110, entryY: 120 } });
  setGlobal('requestPairLine', async (speaker, other, title, lastLine, role) => `${speaker.name} replies as ${role}`);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('showToast', () => {});
  return { driver, nav };
}

await test('runs the real exchange rounds, logs it, and releases the navigator', async () => {
  pairSessionSetup();
  let pipelineBody = null;
  let libCall = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/pipeline') { pipelineBody = JSON.parse(opts.body); return { json: async () => ({ failedStep: null }) }; }
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('logThinkTankAction', (a, k, d) => { call('__noop'); });
  const actions = [];
  setGlobal('logThinkTankAction', (a, k, d) => actions.push({ a, k, d }));
  setGlobal('writeLibraryFile', (a, p, c) => { libCall = { a, p, c }; });
  await call('runPairProgrammingSession', 'driver', 'nav', getGlobal('TASKS')['task-1']);
  assert.ok(pipelineBody, 'the driver really executes in the shared sandbox');
  assert.equal(pipelineBody.agentId, 'driver');
  assert.equal(pipelineBody.sandboxId, 'workroom-shared');
  assert.equal(actions[0].k, 'pair_programming');
  assert.equal(actions[0].d.with, 'nav');
  assert.ok(libCall && libCall.p.includes('-pair-task-1.md'), libCall && libCall.p);
  const d = getGlobal('AGENTS').driver;
  assert.ok(d.profile.notes.some(n => n.includes('Paired with Nim')), JSON.stringify(d.profile.notes));
  const nav = getGlobal('AGENTS').nav;
  assert.equal(nav.pairWith, null, 'navigator is released');
  assert.equal(nav.pairTaskId, null);
  assert.equal(nav.busy, false);
  assert.equal(nav.visible, true);
  assert.equal(nav.x, 110, 'walks out beside the driver at entryX');
  assert.equal(nav.y, 140);
});

await test('reports a snag and still releases everyone when the run fails', async () => {
  pairSessionSetup();
  setGlobal('agentFetch', async () => ({ json: async () => ({ failedStep: 'run it' }) }));
  setGlobal('logThinkTankAction', () => {});
  setGlobal('writeLibraryFile', async () => {});
  await call('runPairProgrammingSession', 'driver', 'nav', getGlobal('TASKS')['task-1']);
  const d = getGlobal('AGENTS').driver;
  assert.ok(d.profile.notes.some(n => n.includes('hit a snag')), JSON.stringify(d.profile.notes));
  assert.equal(getGlobal('AGENTS').nav.busy, false);
});

await test('reports a hard pipeline failure and still releases everyone', async () => {
  pairSessionSetup();
  setGlobal('agentFetch', async (p) => {
    if (p === '/api/pipeline') throw new Error('pipeline down');
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('logThinkTankAction', () => {});
  setGlobal('writeLibraryFile', async () => {});
  await call('runPairProgrammingSession', 'driver', 'nav', getGlobal('TASKS')['task-1']);
  const d = getGlobal('AGENTS').driver;
  assert.ok(d.profile.notes.some(n => n.includes('the run itself failed')), JSON.stringify(d.profile.notes));
  assert.equal(getGlobal('AGENTS').nav.busy, false);
});

await test('no-ops when either half of the pair is missing', async () => {
  pairSessionSetup();
  setGlobal('requestPairLine', async () => { throw new Error('must not be called'); });
  await call('runPairProgrammingSession', 'ghost', 'nav', getGlobal('TASKS')['task-1']);
  assert.equal(getGlobal('AGENTS').nav.busy, true, 'nothing should have changed');
});

console.log('getSandboxContext / _heredocBalance (tasks.js)');

await test('getSandboxContext returns the real file dump', async () => {
  setGlobal('agentFetch', async (p, agentId, opts) => {
    assert.equal(p, '/api/execute');
    assert.ok(JSON.parse(opts.body).command.includes('budget=15000'));
    return { ok: true, json: async () => ({ allowed: true, stdout: '--- index.html ---\n...' }) };
  });
  const ctx = await call('getSandboxContext', 'dev', 'sandbox-1');
  assert.ok(ctx.includes('index.html'));
});

await test('getSandboxContext reports nothing written yet when empty or denied', async () => {
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: true, stdout: '' }) }));
  assert.equal(await call('getSandboxContext', 'dev', 's'), '(nothing written yet)');
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: false, stdout: 'x' }) }));
  assert.equal(await call('getSandboxContext', 'dev', 's'), '(nothing written yet)');
});

await test('getSandboxContext swallows a failed execute', async () => {
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  assert.equal(await call('getSandboxContext', 'dev', 's'), '(could not read current sandbox state)');
});

await test('_heredocBalance counts opens and closes', () => {
  const balanced = call('_heredocBalance', "cat > index.html << 'EOF'\n<html>\nEOF");
  assert.equal(balanced.opens, 1);
  assert.equal(balanced.closes, 1);
  assert.equal(balanced.balanced, true);
  const unbalanced = call('_heredocBalance', "cat > index.html << 'EOF'\n<html>");
  assert.equal(unbalanced.opens, 1);
  assert.equal(unbalanced.closes, 0);
  assert.equal(unbalanced.balanced, false);
  const none = call('_heredocBalance', 'echo hi');
  assert.equal(none.balanced, true);
});

console.log('runCodingTask (tasks.js)');

function codingSetup() {
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', model: 'mid', profile: { notes: [] } } });
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/model', label: 'test' }));
  setGlobal('readWorkingGuide', async () => null);
  setGlobal('_extractWrittenJsFiles', () => []);
  setGlobal('_findUnlinkedJsFiles', async () => []);
  setGlobal('_findPhantomScriptRefs', async () => []);
  setGlobal('_findDanglingSelectorRefs', async () => []);
}

await test('runs a clean heredoc command end to end', async () => {
  codingSetup();
  const chatReplies = ["cat > index.html << 'EOF'\n<html>hello</html>\nEOF"];
  let executeCount = 0;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: chatReplies.shift() }) };
    if (p === '/api/execute') { executeCount++; return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: 'written', stderr: '' }) }; }
    return { ok: true, json: async () => ({}) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'build the page', 'context here', 'To-do app');
  assert.equal(result.ok, true);
  assert.equal(executeCount, 1);
  assert.ok(result.command.includes('cat > index.html'));
  assert.equal(result.tier, 'test');
});

await test('reports an agent missing up front', async () => {
  setGlobal('AGENTS', {});
  const result = await call('runCodingTask', 'ghost', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.equal(result.note, 'agent missing');
});

await test('surfaces a model call that fails or returns nothing', async () => {
  codingSetup();
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ error: 'nope' }) }));
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.equal(result.note, 'model call failed or returned nothing');
});

await test('surfaces a thrown model call', async () => {
  codingSetup();
  setGlobal('agentFetch', async () => { throw new Error('boom'); });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.ok(result.note.includes('model call failed: boom'));
});

await test('rejects an empty command', async () => {
  codingSetup();
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ reply: '   ' }) }));
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.note, 'model returned an empty command');
});

await test('continues a truncated heredoc from exactly where it cut off', async () => {
  codingSetup();
  let chatCalls = 0;
  const replies = [
    "cat > index.html << 'EOF'\npartial content", // cut off mid-heredoc
    "more content\nEOF",
  ];
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') { chatCalls++; return { ok: true, json: async () => ({ reply: replies.shift() }) }; }
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, true);
  assert.equal(chatCalls, 2, 'one continuation after the truncation');
  assert.ok(result.command.includes('partial content'), 'first chunk kept');
  assert.ok(result.command.includes('more content'), 'continuation appended raw, not fence-stripped');
  assert.ok(result.command.endsWith('EOF'));
});

await test('gives up when the generation is still truncated after the continuation budget', async () => {
  codingSetup();
  let chatCalls = 0;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') { chatCalls++; return { ok: true, json: async () => ({ reply: "cat > index.html << 'EOF'\nnever closes" }) }; }
    return { ok: true, json: async () => ({}) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.ok(result.note.includes('generation still looked truncated'));
  assert.equal(chatCalls, 3, 'initial + CODE_CONTINUATION_ATTEMPTS continuations');
});

await test('runs a real page probe when the model asks for one, then executes', async () => {
  codingSetup();
  let probeArgs = null;
  setGlobal('requestPageProbe', async (agentId, sandboxId, pth, actions, probes) => { probeArgs = { pth, actions, probes }; return { ok: true, results: { 'typeof window.G': 'object' } }; });
  setGlobal('formatPageProbeResult', (data) => 'PROBE RESULT');
  let chatCalls = 0;
  const replies = [
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [{ type: 'click', selector: 'text=Go' }], probes: ['typeof window.G'] } }),
    "cat > index.js << 'EOF'\nconst x = 1;\nEOF",
  ];
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') { chatCalls++; return { ok: true, json: async () => ({ reply: replies.shift() }) }; }
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, true);
  assert.equal(chatCalls, 2, 'probe round then the real command');
  assert.ok(probeArgs && probeArgs.pth === 'index.html');
});

await test('tells the model when all probe rounds are exhausted', async () => {
  codingSetup();
  setGlobal('requestPageProbe', async () => ({ ok: true, results: {} }));
  setGlobal('formatPageProbeResult', (data) => 'PROBE FEEDBACK');
  let chatCalls = 0;
  let lastUserFeedback = null;
  const replies = [
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [], probes: ['a'] } }),
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [], probes: ['b'] } }),
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [], probes: ['c'] } }),
    "cat > index.html << 'EOF'\n<html>hi</html>\nEOF",
  ];
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      chatCalls++;
      const body = JSON.parse(opts.body);
      const last = body.messages[body.messages.length - 1];
      if (last && last.role === 'user' && last.content.startsWith('PROBE FEEDBACK')) lastUserFeedback = last.content;
      return { ok: true, json: async () => ({ reply: replies.shift() }) };
    }
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, true);
  assert.equal(chatCalls, 4, 'three probe rounds then the final command');
  assert.ok(lastUserFeedback && lastUserFeedback.includes('You have used all 3 probe rounds'), lastUserFeedback);
});

await test('reports a blocked execute with the server reason', async () => {
  codingSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat > index.html << 'EOF'\nhi\nEOF" }) };
    return { ok: true, json: async () => ({ allowed: false, reason: 'shell blocked' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.equal(result.note, 'blocked: shell blocked');
});

await test('surfaces a thrown execute', async () => {
  codingSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat > index.html << 'EOF'\nhi\nEOF" }) };
    throw new Error('exec down');
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, false);
  assert.ok(result.note.includes('execute failed: exec down'));
});

await test('auto-links a newly written .js file that was orphaned from the page', async () => {
  codingSetup();
  setGlobal('_extractWrittenJsFiles', () => ['settings.js']);
  setGlobal('_findUnlinkedJsFiles', async () => ['settings.js']);
  setGlobal('_guessLinkTargetHtml', async () => 'settings.html');
  let linkChatReply = "cat >> settings.html << 'EOF'\n<script src=\"settings.js\"></script>\nEOF";
  let linkExecOk = { allowed: true, exitCode: 0, timedOut: false };
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: linkChatReply }) };
    return { ok: true, json: async () => ({ ...linkExecOk, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, true);
  assert.ok(result.note.includes('auto-linked previously-orphaned file(s) into index.html: settings.js'), result.note);
});

await test('warns when the automatic link follow-up itself fails', async () => {
  codingSetup();
  setGlobal('_extractWrittenJsFiles', () => ['settings.js']);
  setGlobal('_findUnlinkedJsFiles', async () => ['settings.js']);
  setGlobal('_guessLinkTargetHtml', async () => 'index.html');
  let chatCalls = 0;
  const replies = [
    "cat > index.html << 'EOF'\n<html>hi</html>\nEOF",
    "cat >> index.html << 'EOF'\n<script src=\"settings.js\"></script>\nEOF",
  ];
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') { const reply = replies[chatCalls++]; return { ok: true, json: async () => ({ reply }) }; }
    const cmd = JSON.parse(opts.body).command;
    // The main write succeeds; the link follow-up (cat >>) is what gets blocked.
    if (cmd.includes('cat >>')) return { ok: true, json: async () => ({ allowed: false, reason: 'banned' }) };
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.equal(result.ok, true);
  assert.ok(result.note.includes('WARNING: created settings.js but it is not referenced'), result.note);
});

await test('removes phantom script references to files that were never written', async () => {
  codingSetup();
  setGlobal('_extractWrittenJsFiles', () => []);
  setGlobal('_findPhantomScriptRefs', async () => [{ html: 'index.html', file: 'ghost.js' }]);
  let cleanupOk = true;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat > index.html << 'EOF'\nhi\nEOF" }) };
    if (p === '/api/execute') {
      const cmd = JSON.parse(opts.body).command;
      if (cmd.includes('grep -v')) return { ok: true, json: async () => (cleanupOk ? { allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' } : { allowed: true, exitCode: 1, timedOut: false, stdout: '', stderr: '' }) };
    }
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.ok(result.note.includes('removed <script src> reference(s) to file(s) that don\'t actually exist'), result.note);
  cleanupOk = false;
  const result2 = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.ok(result2.note.includes('automatic cleanup failed'), result2.note);
});

await test('flags dangling selectors as an advisory warning', async () => {
  codingSetup();
  setGlobal('_extractWrittenJsFiles', () => []);
  setGlobal('_findPhantomScriptRefs', async () => []);
  setGlobal('_findDanglingSelectorRefs', async () => [{ selector: '.highway', files: ['index.js'] }]);
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat > index.html << 'EOF'\nhi\nEOF" }) };
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const result = await call('runCodingTask', 'dev', 'sandbox', 'x', '', 'p');
  assert.ok(result.note.includes('WARNING: code queries selector(s) that don\'t exist anywhere'), result.note);
});

console.log('runCodingTask helpers (tasks.js)');

restoreReal('_extractWrittenJsFiles', '_findUnlinkedJsFiles', '_guessLinkTargetHtml', '_autoLinkJsFiles', '_findPhantomScriptRefs', '_removePhantomScriptRefs', '_findDanglingSelectorRefs');

await test('_extractWrittenJsFiles pulls every .js a heredoc command wrote, deduped', () => {
  const files = call('_extractWrittenJsFiles', "cat > app.js << 'EOF'\ncat >> ui.js << 'EOF'\ncat > app.js << 'EOF'");
  assert.deepEqual([...files].sort(), ['app.js', 'ui.js']);
});

await test('_findUnlinkedJsFiles reports only files the real grep flagged', async () => {
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ stdout: 'LINKED:a.js\nUNLINKED:b.js' }) }));
  assert.deepEqual(await call('_findUnlinkedJsFiles', 'dev', 's', ['a.js', 'b.js']), ['b.js']);
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  assert.deepEqual(await call('_findUnlinkedJsFiles', 'dev', 's', ['a.js']), ['a.js'], 'unconfirmed files are treated as unlinked');
});

await test('_guessLinkTargetHtml prefers a same-named page, else index.html', async () => {
  setGlobal('agentFetch', async (p, agentId, opts) => ({ ok: true, json: async () => ({ stdout: JSON.parse(opts.body).command.includes('settings.html') ? 'yes' : 'no' }) }));
  assert.equal(await call('_guessLinkTargetHtml', 'dev', 's', 'settings.js'), 'settings.html');
  assert.equal(await call('_guessLinkTargetHtml', 'dev', 's', 'app.js'), 'index.html');
  assert.equal(await call('_guessLinkTargetHtml', 'dev', 's', 'index.js'), 'index.html', 'the entry page short-circuits');
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  assert.equal(await call('_guessLinkTargetHtml', 'dev', 's', 'app.js'), 'index.html');
});

await test('_autoLinkJsFiles groups by target page and reports a combined note', async () => {
  const execCalls = [];
  setGlobal('_guessLinkTargetHtml', async (a, s, f) => (f === 'settings.js' ? 'settings.html' : 'index.html'));
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      const body = JSON.parse(opts.body);
      const prompt = body.messages[body.messages.length - 1].content;
      const html = prompt.match(/in ([A-Za-z0-9_.\-]+\.html)/)[1];
      return { ok: true, json: async () => ({ reply: `cat >> ${html} << 'EOF'\n<script src="x.js"></script>\nEOF` }) };
    }
    execCalls.push(JSON.parse(opts.body).command);
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const tier = { slug: 'test/model' };
  const res = await call('_autoLinkJsFiles', 'dev', 's', ['settings.js', 'app.js'], [{ role: 'system', content: 'sys' }], tier);
  assert.equal(res.ok, true);
  assert.deepEqual([...execCalls], ['settings.html', 'index.html'].map(h => `cat >> ${h} << 'EOF'\n<script src="x.js"></script>\nEOF`));
});

await test('_autoLinkJsFiles reports which page failed and why', async () => {
  setGlobal('_guessLinkTargetHtml', async () => 'index.html');
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat >> index.html << 'EOF'\n<script src=\"x.js\"></script>\nEOF" }) };
    return { ok: true, json: async () => ({ allowed: false, reason: 'nope' }) };
  });
  const res = await call('_autoLinkJsFiles', 'dev', 's', ['app.js'], [], { slug: 'test/model' });
  assert.equal(res.ok, false);
  assert.ok(res.note.includes('index.html: blocked (nope)'));
});

await test('_autoLinkJsFiles surfaces a thrown execute', async () => {
  setGlobal('_guessLinkTargetHtml', async () => 'index.html');
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: "cat >> index.html << 'EOF'\n<script src=\"x.js\"></script>\nEOF" }) };
    throw new Error('link exec down');
  });
  const res = await call('_autoLinkJsFiles', 'dev', 's', ['app.js'], [], { slug: 'test/model' });
  assert.equal(res.ok, false);
  assert.ok(res.note.includes('follow-up execute failed: link exec down'), res.note);
});

await test('_findPhantomScriptRefs surfaces real phantom references', async () => {
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ stdout: 'EXISTS:index.html:app.js\nPHANTOM:index.html:ghost.js\nPHANTOM:settings.html:remap.js' }) }));
  const refs = await call('_findPhantomScriptRefs', 'dev', 's');
  assert.deepEqual([...refs].map(r => ({ html: r.html, file: r.file })), [{ html: 'index.html', file: 'ghost.js' }, { html: 'settings.html', file: 'remap.js' }]);
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  assert.equal((await call('_findPhantomScriptRefs', 'dev', 's')).length, 0, 'cannot confirm -- do not guess-remove');
});

await test('_removePhantomScriptRefs strips the offending lines per page', async () => {
  let cleanupCommand = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    cleanupCommand = JSON.parse(opts.body).command;
    return { ok: true, json: async () => ({ allowed: true, exitCode: 0, timedOut: false, stdout: '', stderr: '' }) };
  });
  const res = await call('_removePhantomScriptRefs', 'dev', 's', [{ html: 'index.html', file: 'ghost.js' }]);
  assert.equal(res.ok, true);
  assert.ok(cleanupCommand.includes('src="ghost\\.js"'), cleanupCommand);
});

await test('_removePhantomScriptRefs reports blocked and failed cleanups', async () => {
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: false, reason: 'denied' }) }));
  const res1 = await call('_removePhantomScriptRefs', 'dev', 's', [{ html: 'a.html', file: 'f.js' }]);
  assert.equal(res1.ok, false);
  assert.ok(res1.note.includes('a.html: blocked (denied)'));
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: true, exitCode: 1, timedOut: false, stdout: '', stderr: '' }) }));
  const res2 = await call('_removePhantomScriptRefs', 'dev', 's', [{ html: 'a.html', file: 'f.js' }]);
  assert.ok(res2.note.includes('exit code 1'));
  setGlobal('agentFetch', async () => { throw new Error('boom'); });
  const res3 = await call('_removePhantomScriptRefs', 'dev', 's', [{ html: 'a.html', file: 'f.js' }]);
  assert.ok(res3.note.includes('cleanup execute failed: boom'));
});

await test('_findDanglingSelectorRefs flags only selectors that exist nowhere', async () => {
  const blob = [
    '--- index.html ---',
    '<div class="hit-line"></div>',
    '--- index.js ---',
    "document.querySelector('.hit-line');",
    "document.querySelector('.highway');",
    "document.getElementById('play-btn');",
    "el.classList.add('created-later');",
    "document.querySelector('.created-later');",
  ].join('\n');
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: true, stdout: blob }) }));
  const dangling = await call('_findDanglingSelectorRefs', 'dev', 's');
  const keys = [...dangling.map(d => d.selector)].sort();
  assert.deepEqual(keys, ['#play-btn', '.highway'], JSON.stringify(dangling));
});

await test('_findDanglingSelectorRefs bails on a denied or failed read', async () => {
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: false, stdout: '' }) }));
  assert.equal((await call('_findDanglingSelectorRefs', 'dev', 's')).length, 0);
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  assert.equal((await call('_findDanglingSelectorRefs', 'dev', 's')).length, 0);
});

console.log('runGradedReviewLoop (tasks.js)');

function gradedSetup() {
  setGlobal('AGENTS', { rev: { id: 'rev', name: 'Rev', profile: { notes: [] } } });
  setGlobal('gradeJevRequirements', async () => []);
  setGlobal('gradeCodeRequirement', () => []);
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('escalateReviewRequirement', async () => {});
  setGlobal('runCodingTask', async () => ({ ok: true, note: 'revised' }));
}

await test('a review that meets every requirement passes on round 1', async () => {
  gradedSetup();
  const checklist = [{ id: 'r1', question: 'Q', section: 'S', type: 'jev' }];
  setGlobal('gradeJevRequirements', async () => [{ requirementId: 'r1', section: 'S', verdict: getGlobal('GRADE_MEETS'), confidence: 0.9 }]);
  const hooks = { firstReview: 'solid review', produce: async () => { throw new Error('round 1 reuses firstReview'); } };
  const task = { id: 'task-1', title: 'The deliverable', revisions: 0, checklist };
  const result = await call('runGradedReviewLoop', 'rev', task, 'Project', false, hooks);
  assert.equal(result.ok, true);
  assert.ok(result.note.includes('all 1 checklist requirements met in round 1'));
});

await test('a failing requirement drives an inline revision, then re-review', async () => {
  gradedSetup();
  let reviseRound = 0;
  setGlobal('gradeJevRequirements', async () => {
    if (reviseRound === 0) return [{ requirementId: 'r1', section: 'S', verdict: getGlobal('GRADE_FAILS'), confidence: 0.9 }];
    return [{ requirementId: 'r1', section: 'S', verdict: getGlobal('GRADE_MEETS'), confidence: 0.9 }];
  });
  let codingCalled = null;
  setGlobal('runCodingTask', async (...args) => { codingCalled = args; return { ok: true, note: 'revised' }; });
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: true, stdout: 'index.html\n' }) }));
  const checklist = [{ id: 'r1', question: 'Q', section: 'S', type: 'jev' }];
  const hooks = {
    firstReview: 'needs work',
    produce: async (brief) => { reviseRound++; assert.ok(brief.includes('Requirements that still need attention')); return 'fixed now'; },
  };
  const task = { id: 'task-1', title: 'The deliverable', revisions: 0 };
  const result = await call('runGradedReviewLoop', 'rev', task, 'Project', false, hooks);
  assert.equal(result.ok, true);
  assert.ok(result.note.includes('met in round 2'), result.note);
  assert.equal(task.revisions, 1);
  assert.ok(codingCalled, 'the revision ran the real coding pipeline');
  assert.equal(codingCalled[2].includes('FAILS'), false); // backlogItem is the brief text
});

await test('escalates unsure and human requirements to the player instead of guessing', async () => {
  gradedSetup();
  setGlobal('gradeJevRequirements', async () => [{ requirementId: 'u1', section: 'S', verdict: getGlobal('GRADE_UNSURE'), confidence: 0.3 }]);
  const escalated = [];
  setGlobal('escalateReviewRequirement', async (agentId, kind, question) => escalated.push({ kind, question }));
  const checklist = [
    { id: 'u1', question: 'Uncertain Q', section: 'S', type: 'jev' },
    { id: 'h1', question: 'Human Q', section: 'S', type: 'human' },
  ];
  const hooks = { firstReview: 'review', produce: async () => { throw new Error('no revision'); } };
  const task = { id: 'task-1', title: 'T', revisions: 0, checklist };
  const result = await call('runGradedReviewLoop', 'rev', task, 'Project', false, hooks);
  assert.equal(result.ok, true);
  assert.deepEqual(escalated.map(e => e.kind), ['uncertain review requirement', 'review decision for you']);
  assert.ok(result.note.includes('escalated to the player: u1, h1'));
});

await test('stops auto-revising at the revision cap and reports the still-failing count', async () => {
  gradedSetup();
  setGlobal('gradeJevRequirements', async () => [{ requirementId: 'r1', section: 'S', verdict: getGlobal('GRADE_FAILS'), confidence: 0.9 }]);
  let codingCalls = 0;
  setGlobal('runCodingTask', async () => { codingCalls++; return { ok: true, note: 'revised' }; });
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ allowed: true, stdout: '' }) }));
  const checklist = [{ id: 'r1', question: 'Q', section: 'S', type: 'jev' }];
  const hooks = { firstReview: 'still wrong', produce: async () => 'still wrong' };
  const task = { id: 'task-1', title: 'T', revisions: 2 }; // already at the cap
  const result = await call('runGradedReviewLoop', 'rev', task, 'Project', false, hooks);
  assert.equal(result.ok, true);
  assert.ok(result.note.includes('reached the 2-round revision cap, 1 requirement(s) still failing'), result.note);
  assert.equal(codingCalls, 0, 'no further revision at the cap');
});

await test('reports when the review call produces nothing usable', async () => {
  gradedSetup();
  const hooks = { firstReview: null, produce: async () => null };
  const task = { id: 'task-1', title: 'T', revisions: 0 };
  const result = await call('runGradedReviewLoop', 'rev', task, 'Project', false, hooks);
  assert.equal(result.ok, false);
  assert.ok(result.note.includes('model call didn\'t produce anything usable'));
});

console.log('runReviewTask checklist path / _produceReviewText probe exhaustion (tasks.js)');

await test('a checklist-bearing review runs the graded loop with a revision-aware producer', async () => {
  setGlobal('AGENTS', { rev: { id: 'rev', name: 'Rev', profile: { notes: [] } } });
  setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
  let producedBriefs = [];
  setGlobal('_produceReviewText', async (agentId, task, projectLabel, a, isQA, brief, context) => {
    producedBriefs.push(brief);
    return 'the review';
  });
  setGlobal('runGradedReviewLoop', async (agentId, task, projectLabel, isQA, hooks) => {
    await hooks.produce('round-2 brief');
    await hooks.produce(null);
    return { ok: true, note: 'graded to done' };
  });
  setGlobal('requestJevChoice', async () => null);
  const task = { id: 'task-1', title: 'Build the app', taskType: 'review', checklist: [{ id: 'r1', question: 'Q', section: 'S', type: 'jev' }], revisions: 0 };
  const result = await call('runReviewTask', 'rev', task, 'Project');
  assert.equal(result.note, 'graded to done');
  assert.equal(producedBriefs.length, 3, 'round 1 produces the initial review, then the loop re-produces after each revision');
  assert.equal(producedBriefs[0], 'Build the app', 'round 1 brief is the plain backlog item');
  assert.ok(producedBriefs[1].includes('round-2 brief'), 'revision brief folded into the review task');
  assert.equal(producedBriefs[2], 'Build the app', 'a fresh review without a brief uses the backlog item');
});

restoreReal('_produceReviewText');

await test('_produceReviewText tells the model when all probe rounds are spent', async () => {
  setGlobal('AGENTS', { rev: { id: 'rev', name: 'Rev', model: 'mid', profile: { notes: [] } } });
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/review', label: 'test' }));
  setGlobal('gatherUnifiedContext', async () => 'CONTEXT');
  setGlobal('requestPageProbe', async () => ({ ok: true, results: { a: 1 } }));
  setGlobal('formatPageProbeResult', (d) => 'PROBE DATA');
  setGlobal('reviewScreenshot', async () => ({ ok: false, note: 'no screenshot' }));
  let chatCalls = 0;
  const replies = [
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [], probes: ['a'] } }),
    JSON.stringify({ probeRequest: { path: 'index.html', actions: [], probes: ['b'] } }),
    'final assessment: looks okay',
  ];
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') {
      const body = JSON.parse(opts.body);
      chatCalls++;
      if (chatCalls === 3) {
        const lastUser = body.messages[body.messages.length - 1].content;
        assert.ok(lastUser.includes('You have used all 2 probe rounds'), lastUser);
      }
      return { ok: true, json: async () => ({ reply: replies[chatCalls - 1] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  const review = await call('_produceReviewText', 'rev', { title: 'T', taskType: 'review' }, 'Project', getGlobal('AGENTS').rev, false, 'a brief', null);
  assert.ok(review.includes('final assessment'));
  assert.ok(review.includes('Visual check'));
});

console.log('escalateReviewRequirement (tasks.js)');

restoreReal('escalateReviewRequirement');

await test('posts the escalation, and swallows a failed escalation', async () => {
  let body = null;
  setGlobal('agentFetch', async (p, agentId, opts) => { body = JSON.parse(opts.body); return { ok: true }; });
  await call('escalateReviewRequirement', 'dev', 'uncertain review requirement', 'Resolve it');
  assert.equal(body.kind, 'uncertain review requirement');
  assert.equal(body.question, 'Resolve it');
  setGlobal('agentFetch', async () => { throw new Error('mail down'); });
  await call('escalateReviewRequirement', 'dev', 'x', 'y'); // must not throw
});

console.log('runWorkroomTask failure paths (tasks.js)');

await test('reports a failed ambient sandbox request', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => { throw new Error('sandbox down'); });
  await call('runWorkroomTask', 'dev', { title: 'Ambient check' });
  assert.equal(getGlobal('AGENTS').dev.profile.notes[0], 'Tried to work in the Work Room sandbox, but the request failed.');
});

await test('names the failing pipeline step when one fails', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => ({ json: async () => ({ failedStep: 'run it' }) }));
  await call('runWorkroomTask', 'dev', { title: 'Ambient check' });
  assert.equal(getGlobal('AGENTS').dev.profile.notes[0], 'Worked in the shared sandbox -- hit a failure at "run it".');
});

console.log('runResearchTask dispatch paths (tasks.js)');

await test('a skillReview task delegates straight to the skill sweep', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('runSkillReviewTask', async () => 'Reviewed 1 pending skill file(s): 1 promoted, 0 rejected.');
  await call('runResearchTask', 'dev', { title: 'Skill sweep', skillReview: true });
  assert.equal(getGlobal('AGENTS').dev.profile.notes[0], 'Reviewed 1 pending skill file(s): 1 promoted, 0 rejected.');
});

await test('a scheduled topic with no matching record is a no-op, not a crash', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('RESEARCH_TOPICS', []);
  await call('runResearchTask', 'dev', { title: 'Scheduled', research: { topicId: 'ghost' } });
  assert.ok(getGlobal('AGENTS').dev.profile.notes[0].includes('no matching topic record'));
});

await test('reports when the skill-file synthesis call produces nothing', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', model: 'mid', profile: { notes: [] } } });
  setGlobal('RESEARCH_TOPICS', [{ id: 't1', topic: 'Grid Systems', startUrl: 'https://a.example', linkKeyword: 'grid', pageKeyword: 'grid', seenUrls: [] }]);
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('crawlAndCollect', async () => ({ pagesKept: 2, pagesVisited: 3, pages: [{ url: 'https://a.example/1', text: 'page one' }] }));
  setGlobal('readLibraryFile', async () => null);
  setGlobal('skillSlug', (t) => t.toLowerCase().replace(/\s+/g, '-'));
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/model', label: 'test' }));
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ error: 'nothing usable' }) };
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('writeSkillFile', async () => { throw new Error('must not be called'); });
  await call('runResearchTask', 'dev', { title: 'Scheduled', research: { topicId: 't1', since: 0 } });
  assert.ok(getGlobal('AGENTS').dev.profile.notes[0].includes('synthesis call didn\'t produce anything usable'));
});

await test('project research logs a "could not produce anything" note on a failed model call', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', profile: { notes: [] } } });
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/model', label: 'test' }));
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ error: 'nope' }) }));
  await call('runResearchTask', 'dev', { id: 'task-9', title: 'Research topics', instructions: 'Compare approaches.', projectLabel: 'Project' });
  assert.equal(getGlobal('AGENTS').dev.profile.notes[0], 'Tried to research "Research topics", but the model call didn\'t produce anything usable.');
});

await test('the ambient fallback reports a failed sandbox request', async () => {
  setGlobal('AGENTS', { dev: { id: 'dev', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => { throw new Error('sandbox down'); });
  await call('runResearchTask', 'dev', { title: 'Ambient research' });
  assert.equal(getGlobal('AGENTS').dev.profile.notes[0], 'Tried to work in the Research Center sandbox, but the request failed.');
});

console.log('checkWeatherReference (tasks.js)');

await test('logs the live reading for the configured location', async () => {
  setGlobal('AGENTS', { eli: { id: 'eli', profile: { notes: [] } } });
  setGlobal('agentFetch', async (p, agentId, opts) => ({ ok: true, json: async () => ({ location: 'Springfield', reading: 'Mostly sunny, 22C with a light breeze' }) }));
  await call('checkWeatherReference', 'eli');
  assert.ok(getGlobal('AGENTS').eli.profile.notes[0].includes('Logged live weather for Springfield'));
  assert.ok(getGlobal('AGENTS').eli.profile.notes[0].includes('Mostly sunny, 22C'));
});

await test('surfaces a server-side error reading', async () => {
  setGlobal('AGENTS', { eli: { id: 'eli', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ error: 'upstream down' }) }));
  await call('checkWeatherReference', 'eli');
  assert.ok(getGlobal('AGENTS').eli.profile.notes[0].includes('the server said: upstream down'));
});

await test('handles an empty reading and a failed request', async () => {
  setGlobal('AGENTS', { eli: { id: 'eli', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({}) }));
  await call('checkWeatherReference', 'eli');
  assert.ok(getGlobal('AGENTS').eli.profile.notes[0].includes('reading came back empty'));
  setGlobal('AGENTS', { eli: { id: 'eli', profile: { notes: [] } } });
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  await call('checkWeatherReference', 'eli');
  assert.ok(getGlobal('AGENTS').eli.profile.notes[0].includes('request failed'));
});

console.log('parseFeedUrls / runMediaDigestTask (tasks.js)');

await test('parseFeedUrls extracts one URL per non-comment line', () => {
  const urls = call('parseFeedUrls', '# a comment\n\nhttps://a.example/feed -- why it matters\nno url here\nhttp://b.org/rss\n# another comment');
  assert.deepEqual([...urls], ['https://a.example/feed', 'http://b.org/rss']);
});

function mediaSetup() {
  setGlobal('AGENTS', { studio: { id: 'studio', name: 'Studio', model: 'mid', profile: { notes: [] } } });
  setGlobal('logThinkTankAction', () => {});
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('MODEL_TIERS', { small: { slug: 'test/small' }, mid: { slug: 'test/mid' } });
}

await test('files a real digest on a subscribed feed end to end', async () => {
  mediaSetup();
  let digestPath = null, digestContent = null;
  let logged = null;
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p.startsWith('/api/library/file')) return { ok: true, json: async () => ({ content: 'https://news.example/rss -- the news\n' }) };
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: '  Two sentences of honest summary.  ' }) };
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('fetchPageSmart', async (agentId, url, purpose) => ({ allowed: true, text: 'real page content here' }));
  setGlobal('writeLibraryFile', async (a, p, c, s) => { digestPath = p; digestContent = c; });
  setGlobal('logThinkTankAction', (a, k, d) => { logged = { k, d }; });
  await call('runMediaDigestTask', 'studio');
  assert.ok(digestPath.startsWith('media/digests/'), digestPath);
  assert.ok(digestContent.includes('Source: https://news.example/rss'));
  assert.ok(digestContent.includes('Two sentences of honest summary.'));
  assert.equal(logged.k, 'media_digest_filed');
  assert.equal(logged.d.url, 'https://news.example/rss');
});

await test('waits for feeds to be configured when the file is empty or unreadable', async () => {
  mediaSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p.startsWith('/api/library/file')) return { ok: false, json: async () => ({}) };
    return { ok: true, json: async () => ({}) };
  });
  await call('runMediaDigestTask', 'studio');
  assert.equal(getGlobal('AGENTS').studio.profile.notes[0], 'No feeds configured yet -- waiting on media/feeds.md in the Library.');
});

await test('reports a feed fetch that was not approved', async () => {
  mediaSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p.startsWith('/api/library/file')) return { ok: true, json: async () => ({ content: 'https://news.example/rss\n' }) };
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('fetchPageSmart', async () => ({ allowed: false, reason: 'too expensive' }));
  await call('runMediaDigestTask', 'studio');
  assert.ok(getGlobal('AGENTS').studio.profile.notes[0].includes("wasn't approved: too expensive"));
});

await test('reports a feed page that came back empty', async () => {
  mediaSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p.startsWith('/api/library/file')) return { ok: true, json: async () => ({ content: 'https://news.example/rss\n' }) };
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('fetchPageSmart', async () => ({ allowed: true, text: '' }));
  await call('runMediaDigestTask', 'studio');
  assert.ok(getGlobal('AGENTS').studio.profile.notes[0].includes('page came back empty'));
});

await test('reports when the summarizer returns nothing', async () => {
  mediaSetup();
  setGlobal('agentFetch', async (p, agentId, opts) => {
    if (p.startsWith('/api/library/file')) return { ok: true, json: async () => ({ content: 'https://news.example/rss\n' }) };
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: '   ' }) };
    return { ok: true, json: async () => ({}) };
  });
  setGlobal('fetchPageSmart', async () => ({ allowed: true, text: 'page' }));
  await call('runMediaDigestTask', 'studio');
  assert.ok(getGlobal('AGENTS').studio.profile.notes[0].includes("couldn't summarize it this time"));
});

await test('reports a request failure gracefully', async () => {
  mediaSetup();
  setGlobal('agentFetch', async () => { throw new Error('down'); });
  await call('runMediaDigestTask', 'studio');
  assert.equal(getGlobal('AGENTS').studio.profile.notes[0], 'Tried to check a subscribed feed, but the request failed.');
});

console.log('finishTask edge branches (tasks.js)');

await test('falls back to a free spot when the task has no recorded entry point', () => {
  setGlobal('TASKS', { 't1': { id: 't1', title: 'Sort mail', room: 'postoffice' } }); // no entryX
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: 't1', visible: false, busy: true, inRoom: 'postoffice', approvedCount: 0, profile: { notes: [] }, x: 0, y: 0 } });
  setGlobal('pickFreeSpot', (occupied) => ({ x: 700, y: 700 }));
  setGlobal('attemptHandoff', async () => false);
  let offDuty = null;
  setGlobal('sendAgentOffDuty', (id) => { offDuty = id; });
  setGlobal('writeLibraryFile', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('lastTaskCompletedAt', {});
  call('finishTask', 'worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(a.x, 700);
  assert.equal(a.y, 700);
  assert.equal(offDuty, null, 'a real task exists -- she goes to handoff/log first');
});

await test('clocks off straight home when there is no task at all', () => {
  setGlobal('TASKS', {});
  setGlobal('AGENTS', { worker: { id: 'worker', name: 'Worker', task: null, visible: false, busy: true, inRoom: null, approvedCount: 0, profile: { notes: [] }, x: 0, y: 0 } });
  setGlobal('pickFreeSpot', (occupied) => ({ x: 800, y: 810 }));
  setGlobal('attemptHandoff', async () => { throw new Error('no task -- must not hand off'); });
  let offDuty = null;
  setGlobal('sendAgentOffDuty', (id) => { offDuty = id; });
  setGlobal('writeLibraryFile', () => {});
  setGlobal('logThinkTankAction', () => {});
  call('finishTask', 'worker');
  const a = getGlobal('AGENTS').worker;
  assert.equal(a.visible, true);
  assert.equal(a.busy, false);
  assert.equal(a.x, 800, 'free-spot fallback when there is no entry point');
  assert.equal(a.y, 810);
  assert.equal(offDuty, 'worker', 'with no task she is sent home directly');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);