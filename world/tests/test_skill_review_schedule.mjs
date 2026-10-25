// Skill-review scheduling and promotion tests, split out of
// test_idle_quiet.mjs so every script stays under 1,000 lines.
//
// Run: node tests/test_skill_review_schedule.mjs
import { assert, vm, context, setGlobal, getGlobal, test, summary,
         installBaselineGlobals } from './_task_env.mjs';

installBaselineGlobals();

const checkSkillReviewSchedule = () => vm.runInContext('checkSkillReviewSchedule', context)();
const runSkillReviewTask = (...a) => vm.runInContext('runSkillReviewTask', context)(...a);
const queueWorkReal = vm.runInContext('queueWork', context);

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

summary();
