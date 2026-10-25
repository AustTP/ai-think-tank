// Delivery-lane tests for the task system: feed digests and task closeout.
// Split from test_tasks_flow.mjs so every script stays under 1,000 lines.
//
// Run: node tests/test_tasks_delivery_flow.mjs
import { assert, setGlobal, getGlobal, call, test, summary,
         installBaselineGlobals } from './_task_env.mjs';

installBaselineGlobals();

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

summary();
