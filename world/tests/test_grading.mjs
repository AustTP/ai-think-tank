// Regression tests for Phase G's grading machinery:
//   - grading.js pure predicates (gradeCodeRequirement, confidence gating,
//     the meets/fails/insufficient vocabulary, MAX_REVISION_ROUNDS).
//   - the checklist threading from a parsed big-task plan through
//     queueWork -> assignTask -> TASKS[id] (the field survives the same
//     whitelist-drop risks that goal/research/taskType already guard).
//
// Run: node tests/test_grading.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, String, Promise, encodeURIComponent };
vm.createContext(context);

function load(script) {
  vm.runInContext(fs.readFileSync(path.join(worldDir, script), 'utf8'), context, { filename: script });
}

// grading.js needs requestJevChoice defined (jev.js). jev.js needs apiFetch.
context.__apiFetch = async () => ({ ok: true, json: async () => ({ answers: { choice: { choice: 'ben', confidence: 0.9 } }, usage: { cost: 0.001 } }) });
vm.runInContext('apiFetch = __apiFetch;', context);
load('jev.js');
load('grading.js');

function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

const gradeCodeReq = getGlobal('gradeCodeRequirement');
const GRADE_MEETS = getGlobal('GRADE_MEETS');
const GRADE_FAILS = getGlobal('GRADE_FAILS');
const GRADE_UNSURE = getGlobal('GRADE_UNSURE');
const MAX_REVISION_ROUNDS = getGlobal('MAX_REVISION_ROUNDS');
const JEV_GRADE_CONFIDENCE = getGlobal('JEV_GRADE_CONFIDENCE');

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve().then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

console.log('grading.js + checklist threading');

await test('gradeCodeRequirement: a passing predicate yields meets', () => {
  const req = { id: 'no-forbidden-char', question: 'no forbidden char', section: 'draft', type: 'code', code: (txt) => !txt.includes('~') };
  const g = gradeCodeReq(req, 'this is fine');
  assert.equal(g.verdict, GRADE_MEETS);
  assert.equal(g.confidence, 1.0);
});

await test('gradeCodeRequirement: a failing predicate yields fails', () => {
  const req = { id: 'no-forbidden-char', question: 'no ~', section: 'draft', type: 'code', code: (txt) => !txt.includes('~') };
  assert.equal(gradeCodeReq(req, 'bad ~ char').verdict, GRADE_FAILS);
});

await test('gradeCodeRequirement: a throwing predicate degrades to unsure, never guess', () => {
  const req = { id: 'bad-pred', question: 'x', section: 'draft', type: 'code', code: () => { throw new Error('boom'); } };
  assert.equal(gradeCodeReq(req, 'anything').verdict, GRADE_UNSURE);
});

await test('gradeCodeRequirement: empty/missing predicate means no requirement to fail', () => {
  // A req without a `code` fn shouldn't be graded as code at all; caller
  // (gradeJevRequirements) only routes type==='code' here anyway. If one
  // slips through with no predicate, treat as fails-safe-unsure rather
  // than crashing.
  const req = { id: 'nopred', question: 'x', section: 's', type: 'code' };
  assert.equal(gradeCodeReq(req, 'x').verdict, GRADE_UNSURE);
});

await test('gradeAgainstRequirements: meets at high confidence -> meets', async () => {
  // Stub requestJevChoice to return high-confidence meets.
  const real = getGlobal('requestJevChoice');
  setGlobal('requestJevChoice', async () => ({ choice: 'meets_requirement', confidence: 0.9, cost: 0.001 }));
  const g = await getGlobal('gradeAgainstRequirements')({ id: 'r1', question: 'does it?', section: 'opening' }, 'sample', 'ada');
  setGlobal('requestJevChoice', real);
  assert.equal(g.verdict, GRADE_MEETS);
  assert.equal(g.confidence, 0.9);
  assert.equal(g.section, 'opening');
});

await test('gradeAgainstRequirements: fails at high confidence -> fails', async () => {
  const real = getGlobal('requestJevChoice');
  setGlobal('requestJevChoice', async () => ({ choice: 'fails_requirement', confidence: 0.8, cost: 0.001 }));
  const g = await getGlobal('gradeAgainstRequirements')({ id: 'r1', question: 'q', section: 's' }, 'x', 'ada');
  setGlobal('requestJevChoice', real);
  assert.equal(g.verdict, GRADE_FAILS);
});

await test('gradeAgainstRequirements: low-confidence meets escalates to insufficient (never auto-passes)', async () => {
  const real = getGlobal('requestJevChoice');
  setGlobal('requestJevChoice', async () => ({ choice: 'meets_requirement', confidence: 0.3, cost: 0.001 }));
  const g = await getGlobal('gradeAgainstRequirements')({ id: 'r1', question: 'q', section: 's' }, 'x', 'ada');
  setGlobal('requestJevChoice', real);
  assert.equal(g.verdict, GRADE_UNSURE, 'a weak-signal pass must not auto-pass -- surface to player');
  assert.equal(g.confidence, 0.3);
});

await test('gradeAgainstRequirements: a failed/absent Jev call -> insufficient_evidence', async () => {
  const real = getGlobal('requestJevChoice');
  setGlobal('requestJevChoice', async () => null); // Jev outage
  const g = await getGlobal('gradeAgainstRequirements')({ id: 'r1', question: 'q', section: 's' }, 'x', 'ada');
  setGlobal('requestJevChoice', real);
  assert.equal(g.verdict, GRADE_UNSURE, 'an unreachable grader must fail toward the human');
});

await test('gradeJevRequirements: only grades jev-type, skips code/human', async () => {
  const real = getGlobal('requestJevChoice');
  setGlobal('requestJevChoice', async () => ({ choice: 'meets_requirement', confidence: 0.9, cost: 0.001 }));
  const checklist = [
    { id: 'jev1', question: 'q1', section: 's1', type: 'jev' },
    { id: 'code1', question: 'q2', section: 's2', type: 'code', code: (t) => true },
    { id: 'human1', question: 'q3', section: 's3', type: 'human' },
  ];
  const grades = await getGlobal('gradeJevRequirements')(checklist, 'review text', 'ada');
  setGlobal('requestJevChoice', real);
  // only the single jev-type yields a grade; code/human are skipped here.
  assert.equal(grades.length, 1);
  assert.equal(grades[0].requirementId, 'jev1');
});

await test('gradeJevRequirements: an empty checklist grades nothing (no spurious Jev calls)', async () => {
  const real = getGlobal('requestJevChoice');
  let calls = 0;
  setGlobal('requestJevChoice', async () => { calls++; return ({ choice: 'meets_requirement', confidence: 0.9, cost: 0.001 }); });
  const grades = await getGlobal('gradeJevRequirements')([], 'review', 'ada');
  setGlobal('requestJevChoice', real);
  assert.equal(calls, 0, 'ambient task with no checklist must make zero grader calls');
  assert.equal(grades.length, 0);
});

await test('MAX_REVISION_ROUNDS and confidence bar are exported + sane', () => {
  assert.equal(MAX_REVISION_ROUNDS, 2);
  assert.equal(typeof JEV_GRADE_CONFIDENCE, 'number');
  assert.equal(JEV_GRADE_CONFIDENCE, 0.6);
});

// ---- checklist threading through tasks.js (queueWork + assignTask) ----
await test('queueWork whitelist carries checklist through (no silent drop)', () => {
  load('tasks.js'); // needs AGENT_ROSTER etc.? queueWork itself does not, but module top-level might.
  const queueWorkFn = getGlobal('queueWork');
  setGlobal('WORK_QUEUE', []);
  const checklist = [{ id: 'c1', question: 'q', section: 's', type: 'jev' }];
  queueWorkFn([{ title: 't', room: 'pressoffice', checklist }]);
  const q = getGlobal('WORK_QUEUE');
  assert.ok(q.length === 1, 'one item queued');
  assert.deepEqual(q[0].checklist, checklist, 'checklist must survive queueWork whitelisting');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);