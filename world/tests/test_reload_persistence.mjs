// Real regression test (2026-09-21): a page reload used to force every
// agent back on duty (offDuty = false, visible = true) and, for anyone who
// was genuinely resting, walk them out of the outskirts door too -- per
// your explicit call, resting across a reload should now behave like every
// other persisted field: restored as saved, not reset. Meanwhile anything
// that genuinely CAN'T be resumed after a reload (a busy call, a mid-task
// walk, a mid-handoff) still needs to be abandoned exactly as before --
// this isn't a blanket "restore everything untouched," just "stop treating
// rest as abandonment."
//
// Run: node tests/test_reload_persistence.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise, setTimeout };
vm.createContext(context);
// agents.js references REPORTS/nextReportId (reports.js) and WORK_QUEUE
// (tasks.js) inside initAgents()'s restore branch -- loaded for real
// rather than stubbed, so this stays honest about what actually runs.
vm.runInContext(fs.readFileSync(path.join(worldDir, 'reports.js'), 'utf8'), context, { filename: 'reports.js' });
vm.runInContext('const WORK_QUEUE = [];', context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'agents.js'), 'utf8'), context, { filename: 'agents.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}
const initAgents = () => vm.runInContext('initAgents', context)();

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

function mockSavedState(agents) {
  setGlobal('apiFetch', async (url) => {
    if (url === '/api/state') {
      return { json: async () => ({ agentRoster: [], agents, reports: [], nextReportId: 1, workQueue: [] }) };
    }
    return { json: async () => null };
  });
}

await test('initAgents: a genuinely resting agent stays off duty and invisible across a reload', async () => {
  mockSavedState({
    resting: {
      id: 'resting', offDuty: true, visible: false, x: 123, y: 456,
      busy: false, task: null, headingOffDuty: null, mailbox: [], hiredAt: Date.now(),
    },
  });
  await initAgents();
  const a = getGlobal('AGENTS').resting;
  assert.equal(a.offDuty, true, 'should still be off duty after reload');
  assert.equal(a.visible, false, 'should still be invisible after reload');
  assert.equal(a.x, 123, 'should not have been moved');
  assert.equal(a.y, 456, 'should not have been moved');
});

await test('initAgents: an agent genuinely mid-task when the page closed still has that abandoned on reload', async () => {
  mockSavedState({
    working: {
      id: 'working', offDuty: false, visible: false, busy: true, x: 10, y: 20,
      task: { id: 't1' }, meetingId: 'm1', inRoom: 'observatory', handoff: { to: 'x' },
      pairWith: 'other', pairTaskId: 't1', path: [{ x: 1, y: 1 }], pathIndex: 2,
      headingOffDuty: null, mailbox: [], hiredAt: Date.now(),
    },
  });
  await initAgents();
  const a = getGlobal('AGENTS').working;
  assert.equal(a.busy, false);
  assert.equal(a.visible, true, 'not resting -- should be forced visible again like before');
  assert.equal(a.task, null);
  assert.equal(a.meetingId, null);
  assert.equal(a.inRoom, null);
  assert.equal(a.handoff, null);
  assert.equal(a.pairWith, null);
  assert.equal(a.pairTaskId, null);
  assert.equal(a.path, null);
});

await test('initAgents: a stale legacy headingOffDuty field is cleared on reload', async () => {
  // headingOffDuty is a retired field from the old walk-to-the-trailhead
  // off-duty mechanic (removed 2026-09-22). An agent saved under that old
  // regime carries it forward; a reload must clear it so it can't linger,
  // and must leave her resting state (offDuty) untouched.
  mockSavedState({
    walking: {
      id: 'walking', offDuty: false, visible: true, busy: false, x: 5, y: 5,
      task: null, headingOffDuty: 'outskirts_east', path: [{ x: 6, y: 6 }], pathIndex: 0,
      mailbox: [], hiredAt: Date.now(),
    },
  });
  await initAgents();
  const a = getGlobal('AGENTS').walking;
  assert.equal(a.offDuty, false);
  assert.equal(a.headingOffDuty, null, 'the retired field must be cleared');
  assert.equal(a.path, null);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
