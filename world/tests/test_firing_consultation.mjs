// Regression tests for the anti-premature-firing consultation (firing.js).
// Per your calls: a report must notify the candidate's OWN supervisor first
// (reports.js notifySupervisorOfReport), and reviewers should only fire
// after consulting anyone who's worked with the candidate + the reporter.
// Two guardrails: an ACTIVE collaborator blocks firing (would strand the
// collaboration), and thin evidence (one minor report, no corroboration)
// blocks firing too. Real severe + corroborated evidence DOESN'T block.
//
// Run: node tests/test_firing_consultation.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set };
vm.createContext(context);

for (const f of ['firing.js', 'reports.js']) {
  const src = fs.readFileSync(path.join(worldDir, f), 'utf8');
  vm.runInContext(src, context, { filename: f });
}
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const firingConsultation = (...a) => vm.runInContext('firingConsultation', context)(...a);
const consultationBlocksFiring = (...a) => vm.runInContext('consultationBlocksFiring', context)(...a);
const notifySupervisorOfReport = (...a) => vm.runInContext('notifySupervisorOfReport', context)(...a);
const fileReport_ = (...a) => vm.runInContext('fileReport', context)(...a);
const reportsAbout = (...a) => vm.runInContext('reportsAbout', context)(...a);
// fileReport fire-and-forgets classifyReportSeverity, which calls
// requestJevChoice (jev.js, not loaded here). Stub it to the fail-safe
// 'minor' so severity classification doesn't throw at exit.
setGlobal('requestJevChoice', async () => 'minor');

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

// Minimal roster: faye (admin/director), nora (senior director), dev (a worker),
// nadia (a worker who reports to dev), and ben (a coworker of dev).
const ROSTER = [
  { id: 'faye', name: 'Faye', isAdmin: true, isDirector: true, director: undefined },
  { id: 'nora', name: 'Nora', isDirector: true, director: undefined },
  { id: 'dev', name: 'Dev', role: 'Code', director: 'faye' },
  { id: 'nadia', name: 'Nadia', role: 'Research', director: 'dev' },
  { id: 'ben', name: 'Ben', role: 'Ops', director: 'faye' },
];

function setupWorld({ reports, pairWith, handoff }) {
  const AGENTS = {
    faye: { id: 'faye' }, nora: { id: 'nora' },
    dev: { id: 'dev', pairWith, handoff },
    nadia: { id: 'nadia' }, ben: { id: 'ben' },
  };
  setGlobal('AGENT_ROSTER', ROSTER);
  setGlobal('AGENTS', AGENTS);
  setGlobal('REPORTS', reports);
  setGlobal('nextReportId', 100);
}

console.log('firing consultation (firing.js + reports.js)');

await test('reports identify the reporter(s)', () => {
  setupWorld({ reports: [{ id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'missed a deadline', note: 'x', ts: 1, severity: 'minor' }] });
  const { reporters, coworkers } = firingConsultation({ id: 'nadia' });
  assert.equal(reporters.length, 1);
  assert.equal(reporters[0].id, 'dev', 'the reporter of the report about nadia should be dev');
  assert.equal(coworkers.length, 0);
});

await test('reports are de-duplicated by reporter', () => {
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'a', ts: 1, severity: 'minor' },
    { id: 'r2', aboutId: 'nadia', fromId: 'dev', quote: 'b', ts: 2, severity: 'serious' },
  ] });
  const { reporters } = firingConsultation({ id: 'nadia' });
  assert.equal(reporters.length, 1, 'same reporter twice should be one consulted party');
});

await test('an active pair/collaborator is treated as a coworker', () => {
  // ben is currently paired WITH dev -> ben has worked with dev.
  setupWorld({ reports: [{ id: 'r1', aboutId: 'dev', fromId: 'nadia', quote: 'q', note: 'n', ts: 1, severity: 'severe' }], pairWith: undefined });
  setGlobal('AGENTS', { faye: { id: 'faye' }, nora: { id: 'nora' }, dev: { id: 'dev' }, nadia: { id: 'nadia' }, ben: { id: 'ben', pairWith: 'dev' } });
  const { coworkers } = firingConsultation({ id: 'dev' });
  assert.equal(coworkers.length, 1);
  assert.equal(coworkers[0].id, 'ben', 'ben is paired with dev, so ben is a consulted coworker');
});

await test('an active collaborator blocks firing, even with a severe report', () => {
  // Candidate dev is currently paired with someone -- firing would strand it.
  setupWorld({ reports: [{ id: 'r1', aboutId: 'dev', fromId: 'nadia', quote: 'q', note: 'n', ts: 1, severity: 'severe' }] });
  setGlobal('AGENTS', { faye: { id: 'faye' }, nora: { id: 'nora' }, dev: { id: 'dev' }, nadia: { id: 'nadia' }, ben: { id: 'ben', pairWith: 'dev' } });
  assert.equal(consultationBlocksFiring({ id: 'dev' }, 30), true, 'do not fire someone with an active collaborator');
});

await test('thin evidence (single minor report) blocks firing', () => {
  setupWorld({ reports: [{ id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'q', note: 'n', ts: 1, severity: 'minor' }] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 40), true, 'one minor report from one person is premature');
});

await test('severe + corroborated evidence does NOT block firing', () => {
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'q1', note: 'n', ts: 1, severity: 'severe' },
    { id: 'r2', aboutId: 'nadia', fromId: 'ben', quote: 'q2', note: 'n', ts: 2, severity: 'serious' },
  ] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 25), false, 'real, corroborated, severe evidence justifies firing');
});

await test('a real severe report allows firing even uncorroborated', () => {
  // Corroboration is only required to distinguish a genuine signal from
  // noise. A SEVERE report is real evidence from the person who reported
  // them (someone the user explicitly wants consulted) -- firing on it is
  // NOT premature. Only a weak/minor report needs a second voice.
  setupWorld({ reports: [{ id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'q', note: 'n', ts: 1, severity: 'severe' }] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), false, 'a genuine severe report justifies a fire decision');
});

await test('a serious report alone is deferred (needs corroboration)', () => {
  // 'serious' is real but not the top bar; a single uncorroborated one is
  // worth holding for consultation.
  setupWorld({ reports: [{ id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'q', note: 'n', ts: 1, severity: 'serious' }] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), true, 'a single serious, uncorroborated report defers to consultation');
});

await test('no reports at all blocks firing (nothing to review)', () => {
  setupWorld({ reports: [] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), true, 'no evidence at all should never fire');
});

await test('positive/neutral peer-reviews never count toward firing', () => {
  // Server peer-review files "Standout" and "Nominal" reports too -- these
  // are not negative signals and must not satisfy the firing bar.
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'faye', quote: 'q', note: 'Standout performer -- doing the most real work this period.', ts: 1, severity: 'minor' },
  ] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), true, 'a positive peer-review is not grounds to fire');
});

await test('a single server "major" report is a real concern but defers without a second voice', () => {
  // serve.py peer-review uses its OWN scale: minor/major, where 'major'
  // means low output -- a real concern (recognition, not trivia), but a
  // single, uncorroborated one is exactly the "premature" case: defer.
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'faye', quote: 'q', note: 'Underperforming -- well below expected output this period.', ts: 1, severity: 'major' },
  ] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), true, 'a single server "major" underperformance report defers to consultation');
});

await test('corroborated server "major" concerns justify firing', () => {
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'faye', quote: 'q1', note: 'Underperforming.', ts: 1, severity: 'major' },
    { id: 'r2', aboutId: 'nadia', fromId: 'dev', quote: 'q2', note: 'Still underperforming.', ts: 2, severity: 'serious' },
  ] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), false, 'two independent negative reports justify a fire decision');
});

await test('a corroborated pair of minor reports does not fire (minor is not a concern)', () => {
  // Multiple MINOR reports still aren't severe/serious/major -- firing needs
  // a real concern, not just quantity of trivia. (The Jev classifier
  // reserves serious/severe for real failures.)
  setupWorld({ reports: [
    { id: 'r1', aboutId: 'nadia', fromId: 'dev', quote: 'q1', note: 'n', ts: 1, severity: 'minor' },
    { id: 'r2', aboutId: 'nadia', fromId: 'ben', quote: 'q2', note: 'n', ts: 2, severity: 'minor' },
  ] });
  assert.equal(consultationBlocksFiring({ id: 'nadia' }, 30), true, 'multiple minor reports are still trivial, not a firing concern');
});

await test('a report notifies the candidate\'s own supervisor, first', () => {
  // nadia reports to dev. Filing a report about nadia must land an unread
  // mail in dev's mailbox, before any firing review.
  setupWorld({ reports: [] });
  fileReport_('nadia', 'ben', 'missed a delivery', 'happened twice');
  const devMail = context.AGENTS.dev.mailbox;
  assert.ok(Array.isArray(devMail) && devMail.length === 1, 'dev (nadia\'s supervisor) should get exactly one unread report notification');
  assert.equal(devMail[0].read, false);
  assert.match(devMail[0].text, /nadia/i, 'dev\'s mail should name the reported subordinate');
  // And the report itself is recorded.
  assert.equal(reportsAbout('nadia').length, 1);
});

await test('a report about a top-of-chain manager (no supervisor) does not throw', () => {
  setupWorld({ reports: [] });
  fileReport_('faye', 'ben', 'overruled a call', 'n/a'); // faye has no director
  assert.ok(true, 'no exception when the subject has no supervisor');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);