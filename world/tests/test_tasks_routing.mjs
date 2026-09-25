// Regression tests for tasks.js's smallest pure routing predicates that are
// otherwise only exercised transitively through runTaskCycleBody (which is
// heavily mocked and async): _isWorkItemDue, _normalizePriority, and
// _pickNextDueIndex's determinism / exclusion logic (the attemptedThisCycle
// fix from the 2026-09-21 audit). Room-capacity overflow and the full queue
// cycle are covered by test_room_overflow.mjs and test_idle_quiet.mjs
// respectively, so those aren't re-tested here.
//
// Run: node tests/test_tasks_routing.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

// _isWorkItemDue uses Date.now(); _normalizePriority reads WORK_PRIORITY and
// _pickNextDueIndex reads WORK_QUEUE, which live at module scope in tasks.js.
const context = { console, Math, Date, JSON, Array, Object, Set };
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

const isWorkItemDue = (...a) => vm.runInContext('_isWorkItemDue', context)(...a);
const normalizePriority = (...a) => vm.runInContext('_normalizePriority', context)(...a);
const pickNextDueIndex = (...a) => vm.runInContext('_pickNextDueIndex', context)(...a);
const WORK_PRIORITY = getGlobal('WORK_PRIORITY');

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

console.log('tasks.js pure routing predicates');

await test('_isWorkItemDue: an item with no notBefore is due immediately', () => {
  assert.equal(isWorkItemDue({ title: 'x' }), true);
  assert.equal(isWorkItemDue({ title: 'x', notBefore: null }), true);
});

await test('_isWorkItemDue: a scheduled item is not due until its time passes', () => {
  const past = Date.now() - 10;
  const future = Date.now() + 60_000;
  assert.equal(isWorkItemDue({ notBefore: past }), true, 'already-past notBefore must be due');
  assert.equal(isWorkItemDue({ notBefore: future }), false, 'future notBefore must NOT be due yet');
  assert.equal(isWorkItemDue({ notBefore: Date.now() }), true, 'the boundary instant is due');
});

await test('_normalizePriority: numeric value passes straight through', () => {
  assert.equal(normalizePriority(3), 3); // urgent
  assert.equal(normalizePriority(0), 0); // low
  assert.equal(normalizePriority(1), 1); // normal
});

await test('_normalizePriority: named levels, case-insensitive', () => {
  assert.equal(normalizePriority('urgent'), WORK_PRIORITY.urgent);
  assert.equal(normalizePriority('HIGH'), WORK_PRIORITY.high);
  assert.equal(normalizePriority('Normal'), WORK_PRIORITY.normal);
  assert.equal(normalizePriority('low'), WORK_PRIORITY.low);
});

await test('_normalizePriority: unknown value falls back to normal, never crashes', () => {
  assert.equal(normalizePriority('critical'), WORK_PRIORITY.normal);
  assert.equal(normalizePriority(99), WORK_PRIORITY.normal);
  assert.equal(normalizePriority(undefined), WORK_PRIORITY.normal);
  assert.equal(normalizePriority(null), WORK_PRIORITY.normal);
  assert.equal(normalizePriority(true), WORK_PRIORITY.normal);
});

await test('_pickNextDueIndex: empty queue yields no pick', () => {
  setGlobal('WORK_QUEUE', []);
  assert.equal(pickNextDueIndex(), -1);
});

await test('_pickNextDueIndex: due item wins; future-scheduled item is skipped', () => {
  setGlobal('WORK_QUEUE', [
    { title: 'future', notBefore: Date.now() + 60_000, priority: WORK_PRIORITY.urgent },
    { title: 'now', priority: WORK_PRIORITY.normal },
  ]);
  assert.equal(pickNextDueIndex(), 1, 'must pick the DUE normal item, not the future urgent one');
});

await test('_pickNextDueIndex: highest priority among due items wins', () => {
  setGlobal('WORK_QUEUE', [
    { title: 'low', priority: WORK_PRIORITY.low },
    { title: 'high', priority: WORK_PRIORITY.high },
    { title: 'urgent', priority: WORK_PRIORITY.urgent },
  ]);
  assert.equal(pickNextDueIndex(), 2);
});

await test('_pickNextDueIndex: equal priority keeps first-queued position (strict >, not >=)', () => {
  setGlobal('WORK_QUEUE', [
    { title: 'first', priority: WORK_PRIORITY.high },
    { title: 'second', priority: WORK_PRIORITY.high },
  ]);
  assert.equal(pickNextDueIndex(), 0, 'first-queued must win a tie, or every tick would reshuffle order');
});

await test('_pickNextDueIndex: excludeItems prevents re-picking the same item within one cycle', () => {
  const item = { title: 'only', priority: WORK_PRIORITY.normal };
  setGlobal('WORK_QUEUE', [item]);
  const exclude = new Set([item]);
  assert.equal(pickNextDueIndex(exclude), -1, 'an excluded item must not be selected again this cycle');
  assert.equal(pickNextDueIndex(new Set()), 0, 'without exclusion it IS still selectable');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);