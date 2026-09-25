// Regression tests for the Phase G graded, bounded revision loop
// (runGradedReviewLoop in tasks.js). Drives the loop's orchestration with
// injected production + stubbed grading so we can assert the create ->
// evaluate -> revise -> evaluate-again shape without a browser.
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';

const base = path.resolve(import.meta.dirname, '..');
const context = vm.createContext();
vm.runInContext(fs.readFileSync(path.join(base, 'grading.js'), 'utf8'), context, { filename: 'grading.js' });
vm.runInContext(fs.readFileSync(path.join(base, 'tasks.js'), 'utf8'), context, { filename: 'tasks.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

// Stub the loop's heavy dependencies.
setGlobal('AGENTS', { agent1: { name: 'Agent One' } });
setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
setGlobal('WORK_QUEUE', []);
vm.runInContext(`
__stub_wlf = () => {};
__stub_fetch = async () => ({ json: async () => ({ allowed: true, stdout: 'f' }) });
__stub_coding = async () => { coding_budget_n++; };
__stub_esc = async (a, k, q) => esc_log.push(q);
coding_budget_n = 0;
esc_log = [];
`, context);
setGlobal('writeLibraryFile', vm.runInContext('__stub_wlf', context));
setGlobal('agentFetch', vm.runInContext('__stub_fetch', context));
setGlobal('runCodingTask', vm.runInContext('__stub_coding', context));
setGlobal('escalateReviewRequirement', vm.runInContext('__stub_esc', context));

const loop = (task, firstReview, produce) =>
  vm.runInContext('runGradedReviewLoop', context)('agent1', task, 'proj', false, {
    produce, firstReview,
  });

const req = (id, type) => ({ id, section: 'sec-' + id, question: 'question ' + id, type });

let passed = 0, failed = 0;
const ok = (name, cond) => { if (cond) { passed++; console.log('  ok - ' + name); } else { failed++; console.log('  FAIL - ' + name); } };
const readGlobal = (n) => {
  vm.runInContext(`__extract_${n} = ${n};`, context);
  return context['__extract_' + n];
};

console.log('runGradedReviewLoop (Phase G)');

// 1. All requirements met in round 1 -> clean, no revisions, no escalation.
const t1 = { title: 'T', id: 't1', revisions: 0, checklist: [req('a', 'code'), req('b', 'code')] };
vm.runInContext(`gradeCodeRequirement = (r, rev) => ({ requirementId: r.id, section: r.section, verdict: "meets_requirement", confidence: 0.95 });`, context);
const r1 = await loop(t1, 'solid', async () => 'should not be called');
ok('clean round 1 -> all requirements met', r1.note.includes('all 2 checklist requirements met'));
ok('no revisions when clean', t1.revisions === 0);
ok('no escalation when clean', r1.escalated.length === 0);

// 2. A failing code requirement with budget -> one inline revision + produce, then re-grade.
const t2 = { title: 'T', id: 't2', revisions: 0, checklist: [req('c', 'code')] };
// Grade stub fails on the FIRST check only, then passes -- so round 1 fails,
// triggers one revision, and round 2 meets. Stub via a context-visible counter.
vm.runInContext(`__grade_fail_once = (() => { let n = 0; return (r) => { n++; return { requirementId: r.id, section: r.section, verdict: n === 1 ? "fails_requirement" : "meets_requirement", confidence: 0.99 }; }; })();`, context);
vm.runInContext(`gradeCodeRequirement = __grade_fail_once;`, context);
vm.runInContext('coding_budget_n = 0;', context);
let produced = 0;
const r2 = await loop(t2, 'has a bug', async (brief) => { produced++; ok('revision produce got a targeted brief', brief.includes('question c')); return 'now fixed'; });
ok('failing requirement triggers one revision', t2.revisions === 1);
ok('revision prompted a fresh review (produce called)', produced === 1);
ok('revision invoked one inline coding pass on the sandbox', readGlobal('coding_budget_n') === 1);
ok('grading re-ran after revision and now meets', r2.note.includes('met in round 2'));

// 3. Revision budget exhausted with a requirement STILL failing -> escalate to player, no infinite loop.
//    (grade stub always fails; task already at the cap.)
vm.runInContext(`gradeCodeRequirement = (r, rev) => ({ requirementId: r.id, section: r.section, verdict: "fails_requirement", confidence: 0.99 });`, context);
vm.runInContext('coding_budget_n = 0;', context);
const t3 = { title: 'T', id: 't3', revisions: 2, checklist: [req('d', 'code')] };
const r3 = await loop(t3, 'still broken', async () => { produced++; return 'still broken'; });
ok('at cap -> notes the cap, does not auto-decide', r3.note.includes('revision cap'));
ok('at cap -> no coding pass or produce re-run', readGlobal('coding_budget_n') === 0 && produced === 1);
ok('at cap -> failing requirement surfaces in grades', r3.grades[0].verdict === 'fails_requirement');

// 4. Uncertain grade (insufficient evidence) -> escalate to player, no auto-pass, no revision.
const t4 = { title: 'T', id: 't4', revisions: 0, checklist: [req('e', 'jev')] };
vm.runInContext(`gradeJevRequirements = async () => [{ requirementId: "e", section: "sec-e", verdict: "insufficient_evidence", confidence: 0.4 }];`, context);
const r4 = await loop(t4, 'unclear', async () => 'unclear');
ok('insufficient evidence -> escalated to player', r4.escalated.includes('e'));
ok('insufficient evidence -> not auto-passed nor revised', t4.revisions === 0);
ok('escalation carries a resolvable question', readGlobal('esc_log').some(q => q.includes('judge requirement "question e"')));

// 5. Human-type requirement -> always surfaced to player, never auto-decided.
const t5 = { title: 'T', id: 't5', revisions: 0, checklist: [req('h1', 'human')] };
const r5 = await loop(t5, 'needs a human', async () => 'needs a human');
ok('human requirement -> escalated, never auto-decided', r5.escalated.includes('h1'));
ok('human requirement -> no revision attempted', t5.revisions === 0);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);