// Real regression test (2026-09-21): scheduled research topics, the
// synthesis step that turns collected pages into a real skill file, and
// the incremental "only what's new since last time" mechanism. Follows
// the idle-quiet contract this project already locks in for WORK_QUEUE
// (test_idle_quiet.mjs) -- an empty or not-yet-due RESEARCH_TOPICS must
// cost exactly nothing.
//
// Run: node tests/test_research_schedule.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise, setTimeout };
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
setGlobal('MAX_ACTIVE_AGENTS', 1000);
const checkResearchSchedule = () => vm.runInContext('checkResearchSchedule', context)();
const defineResearchTopic = (...a) => vm.runInContext('defineResearchTopic', context)(...a);
const assignTask = (...a) => vm.runInContext('assignTask', context)(...a);
const runResearchTask = (...a) => vm.runInContext('runResearchTask', context)(...a);

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

console.log('checkResearchSchedule: idle-cost contract');

await test('an empty RESEARCH_TOPICS makes zero calls and queues nothing', () => {
  setGlobal('RESEARCH_TOPICS', []);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
});

await test('a topic not yet due queues nothing and leaves lastRunAt untouched', () => {
  const now = Date.now();
  setGlobal('RESEARCH_TOPICS', [{ id: 'topic-1', topic: 'x', startUrl: 'https://example.com', cadenceMs: 999999999, lastRunAt: now, seenUrls: [] }]);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  assert.equal(getGlobal('WORK_QUEUE').length, 0);
  assert.equal(getGlobal('RESEARCH_TOPICS')[0].lastRunAt, now);
});

await test('a due topic queues a real WORK_QUEUE item carrying its topicId, and stamps lastRunAt immediately', () => {
  setGlobal('RESEARCH_TOPICS', [{ id: 'topic-2', topic: 'think tank governance', startUrl: 'https://example.com', cadenceMs: 1000, lastRunAt: 0, seenUrls: [] }]);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  const queue = getGlobal('WORK_QUEUE');
  assert.equal(queue.length, 1);
  assert.equal(queue[0].research.topicId, 'topic-2');
  assert.equal(queue[0].room, 'observatory');
  const topics = getGlobal('RESEARCH_TOPICS');
  assert.ok(topics[0].lastRunAt > 0, 'lastRunAt should be stamped the moment it becomes due, before any task is even assigned');
});

await test('a topic that has run before carries its REAL previous lastRunAt as "since", not the freshly-stamped now', () => {
  const previousRunAt = Date.now() - 5000;
  setGlobal('RESEARCH_TOPICS', [{ id: 'topic-2b', topic: 'x', startUrl: 'https://example.com', cadenceMs: 1000, lastRunAt: previousRunAt, seenUrls: ['https://example.com/old'] }]);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  const queue = getGlobal('WORK_QUEUE');
  assert.equal(queue[0].research.since, previousRunAt, '"since" must be the run that JUST ended, not the new timestamp this call itself just stamped');
  assert.ok(getGlobal('RESEARCH_TOPICS')[0].lastRunAt > previousRunAt, 'lastRunAt itself should still advance to now');
});

await test('a genuinely first-ever run has since: 0 -- nothing to compare against yet', () => {
  setGlobal('RESEARCH_TOPICS', [{ id: 'topic-2c', topic: 'x', startUrl: 'https://example.com', cadenceMs: 1000, lastRunAt: 0, seenUrls: [] }]);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  assert.equal(getGlobal('WORK_QUEUE')[0].research.since, 0);
});

await test('a due topic checked twice in the same tick is only ever queued once', () => {
  setGlobal('RESEARCH_TOPICS', [{ id: 'topic-3', topic: 'x', startUrl: 'https://example.com', cadenceMs: 1000, lastRunAt: 0, seenUrls: [] }]);
  setGlobal('WORK_QUEUE', []);
  checkResearchSchedule();
  checkResearchSchedule();
  assert.equal(getGlobal('WORK_QUEUE').length, 1, 'the second call should see lastRunAt already stamped and skip it');
});

console.log('\ndefineResearchTopic: creates a real, immediately-due-by-default entry');

await test('a freshly defined topic is due right away, not after waiting a full cadence', () => {
  setGlobal('RESEARCH_TOPICS', []);
  const entry = defineResearchTopic({ topic: 'x', startUrl: 'https://example.com', cadenceMs: 999999999 });
  assert.equal(entry.lastRunAt, 0);
  assert.equal(entry.seenUrls.length, 0); // deepEqual against a host [] literal would fail on prototype identity alone -- this array was built inside the vm context
});

console.log('\nassignTask: the research field survives all the way to TASKS[id]');

await test('TASKS[id].research matches what was passed through', () => {
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('ROOM_DOOR_TRIGGERS', { observatory: { x: 100, y: 100, w: 16, h: 16 } });
  setGlobal('findPath', () => [{ x: 90, y: 108 }]);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('AGENTS', { dev: { id: 'dev', busy: false, task: null, x: 0, y: 0 } });
  const task = assignTask('dev', 'Scheduled research: x', 'observatory', 'instructions', 'x', { research: { topicId: 'topic-9' } });
  assert.deepEqual(task.research, { topicId: 'topic-9' });
});

console.log('\nrunResearchTask: the scheduled crawl + skill-file synthesis branch (2026-09-21)');

await test('collects pages, updates seenUrls, and writes an externally-sourced skill file', async () => {
  const topic = { id: 'topic-4', topic: 'think tank governance', startUrl: 'https://example.com', linkKeyword: '', pageKeyword: '', seenUrls: ['https://example.com/old'] };
  setGlobal('RESEARCH_TOPICS', [topic]);
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', profile: { notes: [] } } });
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('SKILL_FILE_FORMAT_GUIDE', 'FORMAT GUIDE');
  // skillSlug lives in world.js, not loaded into this tasks.js-only
  // context -- mirrors the real implementation closely enough to matter.
  setGlobal('skillSlug', (name) => name.replace(/[^a-z0-9_-]/gi, '-').toLowerCase());
  let crawlArgs = null;
  setGlobal('crawlAndCollect', async (agentId, sandboxId, startUrl, purpose, opts) => {
    // Snapshot skipUrls at call-time -- it's the SAME array as
    // topic.seenUrls by reference, which runResearchTask mutates (push)
    // right after this call returns, so reading opts.skipUrls later
    // would see the post-mutation state instead of what was actually
    // passed in.
    crawlArgs = { agentId, sandboxId, startUrl, opts: { ...opts, skipUrls: [...opts.skipUrls] } };
    return { ok: true, pagesVisited: 2, pagesKept: 1, pages: [{ url: 'https://example.com/new', text: 'Real new content about think tank governance.' }], manifestPath: 'downloads/manifest.json' };
  });
  setGlobal('readLibraryFile', async () => 'EXISTING SKILL CONTENT');
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/research-model', label: 'test' }));
  let skillCall = null;
  setGlobal('writeSkillFile', async (agentId, name, content, source) => { skillCall = { agentId, name, content, source }; });
  setGlobal('agentFetch', async (p) => {
    if (p === '/api/chat') return { ok: true, json: async () => ({ reply: 'Updated skill file content.' }) };
    return { ok: true, json: async () => ({}) };
  });

  const task = { id: 'task-research-4', title: 'Scheduled research: think tank governance', research: { topicId: 'topic-4', since: 1758000000000 } };
  await runResearchTask('dev', task);

  assert.ok(crawlArgs, 'expected a real crawlAndCollect call');
  assert.deepEqual(crawlArgs.opts.skipUrls, ['https://example.com/old'], 'expected the topic\'s existing seenUrls to be passed through so already-collected pages are not re-kept');
  assert.equal(crawlArgs.opts.since, 1758000000000, 'expected the task\'s own research.since to be threaded through to crawlAndCollect, not silently dropped');
  assert.ok(skillCall, 'expected a real writeSkillFile call');
  assert.equal(skillCall.source, 'external', 'content built from crawled pages must be quarantined like every other externally-sourced Library write');
  assert.equal(skillCall.content, 'Updated skill file content.');
  const savedTopic = getGlobal('RESEARCH_TOPICS')[0];
  assert.ok(savedTopic.seenUrls.includes('https://example.com/new'), 'the newly kept page should be added to seenUrls so a future run does not re-collect it');
});

await test('nothing new collected -- does not write a low-content skill-file update', async () => {
  const topic = { id: 'topic-5', topic: 'x', startUrl: 'https://example.com', linkKeyword: '', pageKeyword: '', seenUrls: [] };
  setGlobal('RESEARCH_TOPICS', [topic]);
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', profile: { notes: [] } } });
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('crawlAndCollect', async () => ({ ok: true, pagesVisited: 3, pagesKept: 0, pages: [], manifestPath: 'downloads/manifest.json' }));
  setGlobal('writeSkillFile', async () => { throw new Error('should never be called when nothing new was collected'); });
  setGlobal('pickModelTierForAction', async () => { throw new Error('should never be called when nothing new was collected'); });
  setGlobal('agentFetch', async () => { throw new Error('should never be called when nothing new was collected'); });

  const task = { id: 'task-research-5', title: 'Scheduled research: x', research: { topicId: 'topic-5' } };
  await runResearchTask('dev', task);
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.ok(notes[0].includes('nothing new'), `expected a plain "nothing new" note, got: ${notes[0]}`);
});

await test('a missing topic record does not throw', async () => {
  setGlobal('RESEARCH_TOPICS', []);
  setGlobal('AGENTS', { dev: { id: 'dev', name: 'Dev', profile: { notes: [] } } });
  const task = { id: 'task-research-6', title: 'Scheduled research: gone', research: { topicId: 'topic-does-not-exist' } };
  await runResearchTask('dev', task);
  const notes = getGlobal('AGENTS').dev.profile.notes;
  assert.ok(notes[0].includes('no matching topic'));
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
