// world.js client helpers: the api/agent fetch wrappers (with the 401 session
// bounce and per-agent key header), model-tier loading + per-action tier
// picking, screenshot review, the Library file/skill helpers, unified context
// assembly, smart page fetching with the render fallback, curl/sandbox-save,
// the crawl frontier, page-probe wrapper + formatter, and collision-grid
// loading/blockedAt. Loads the real sources (world.js + hiring.js for
// MODEL_TIERS) into a vm.
//
// Run: node tests/test_world_helpers.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise, encodeURIComponent };
vm.createContext(context);

for (const f of ['hiring.js', 'world.js']) {
  const src = fs.readFileSync(path.join(worldDir, f), 'utf8');
  vm.runInContext(src, context, { filename: f });
}

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const call = (name, ...a) => vm.runInContext(name, context)(...a);

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

const okJson = (obj) => ({ ok: true, status: 200, json: async () => obj });

console.log('apiFetch / agentFetch (world.js)');

await test('apiFetch forwards the request and returns the response', async () => {
  let seen = null;
  context.fetch = async (url, opts) => { seen = { url, opts }; return { status: 200 }; };
  const res = await call('apiFetch', '/api/x', { method: 'POST' });
  assert.equal(res.status, 200);
  assert.equal(seen.url, '/api/x');
  assert.equal(seen.opts.method, 'POST');
  delete context.fetch;
});

await test('apiFetch bounces to the login page on a 401', async () => {
  let redirected = null;
  context.window = { location: { href: null } };
  context.fetch = async () => ({ status: 401 });
  context.window.location = { href: (redirected = null, '') };
  await assert.rejects(() => call('apiFetch', '/api/x'), /Session expired/);
  assert.equal(context.window.location.href, '/');
  delete context.fetch;
  delete context.window;
});

await test('agentFetch adds the X-Agent-Key header', async () => {
  setGlobal('AGENT_KEYS', { dev: 'k123' });
  let seenHeaders = null;
  context.fetch = async (url, opts) => { seenHeaders = opts.headers; return { status: 200 }; };
  const res = await call('agentFetch', '/api/x', 'dev', { method: 'GET' });
  assert.equal(seenHeaders['X-Agent-Key'], 'k123');
  assert.equal(res.status, 200);
  delete context.fetch;
});

console.log('logThinkTankAction / loadModelTiers (world.js)');

await test('logThinkTankAction posts to the activity log, fire-and-forget', async () => {
  let body = null;
  context.fetch = async (url, opts) => { body = JSON.parse(opts.body); return { ok: true }; };
  call('logThinkTankAction', 'dev', 'hire', { hired: 'x' });
  assert.equal(body.agentId, 'dev');
  assert.equal(body.action, 'hire');
  delete context.fetch;
});

await test('logThinkTankAction swallows a failed log post', () => {
  context.fetch = async () => { throw new Error('down'); };
  call('logThinkTankAction', 'dev', 'hire', {});
  delete context.fetch;
  assert.ok(true);
});

await test('loadModelTiers maps the server bands onto MODEL_TIERS', async () => {
  context.fetch = async () => okJson({
    low: { slug: 's1', name: 'Small!' },
    mid: { slug: 's2', name: 'Mid!' },
    coding: { slug: 's3', name: 'Coding!' },
    high: { slug: 's4', name: 'Plan!' },
    vision: { slug: 's5', name: 'Vision!' },
  });
  await call('loadModelTiers');
  assert.equal(vm.runInContext('MODEL_TIERS.small.slug', context), 's1');
  assert.equal(vm.runInContext('MODEL_TIERS.small.label', context), 'Small!');
  assert.equal(vm.runInContext('MODEL_TIERS.mid.slug', context), 's2');
  assert.equal(vm.runInContext('MODEL_TIERS.coding.slug', context), 's3');
  assert.equal(vm.runInContext('MODEL_TIERS.planning.slug', context), 's4');
  assert.equal(vm.runInContext('MODEL_TIERS.vision.slug', context), 's5');
  delete context.fetch;
});

await test('loadModelTiers ignores a missing band without touching the tier', async () => {
  context.fetch = async () => okJson({ low: { slug: 's1', name: 'S' } });
  vm.runInContext('MODEL_TIERS.vision.slug = "keep";', context);
  await call('loadModelTiers');
  assert.equal(vm.runInContext('MODEL_TIERS.vision.slug', context), 'keep');
  delete context.fetch;
});

await test('loadModelTiers handles an error response and a network failure', async () => {
  context.fetch = async () => okJson({ error: 'boom' });
  await call('loadModelTiers'); // console.error path, no throw
  context.fetch = async () => { throw new Error('net'); };
  await call('loadModelTiers');
  delete context.fetch;
  assert.ok(true);
});

await test('pickModelTierForAction maps a Jev choice to a tier', async () => {
  setGlobal('requestJevChoice', async () => ({ choice: 'coding' }));
  const t = await call('pickModelTierForAction', { name: 'Dev', role: 'Code', model: 'small' }, 'writes index.html');
  assert.equal(vm.runInContext('MODEL_TIERS.coding', context), t);
});

await test('pickModelTierForAction falls back to the agent model tier on no decision', async () => {
  setGlobal('requestJevChoice', async () => null);
  const t = await call('pickModelTierForAction', { name: 'Dev', role: 'Code', model: 'mid' }, 'summary');
  assert.equal(vm.runInContext('MODEL_TIERS.mid', context), t);
});

await test('pickModelTierForAction falls back to small for an unknown agent model', async () => {
  setGlobal('requestJevChoice', async () => ({ choice: 'bogus' }));
  const t = await call('pickModelTierForAction', { name: 'Dev', role: 'Code', model: 'nope' }, 'x');
  assert.equal(vm.runInContext('MODEL_TIERS.small', context), t);
});

console.log('reviewScreenshot (world.js)');

await test('reviewScreenshot returns ok:false when the screenshot fails', async () => {
  context.fetch = async () => okJson({ error: 'no chrome' });
  const r = await call('reviewScreenshot', 'dev', 'sb', 'file.png', 'is this ok?');
  assert.equal(r.ok, false);
  assert.ok(r.note.includes('no chrome'));
  delete context.fetch;
});

await test('reviewScreenshot sends the image to vision and returns the review', async () => {
  const seen = [];
  context.fetch = async (url, opts) => {
    if (url === '/api/screenshot') return okJson({ imageBase64: 'AAAA' });
    if (url === '/api/chat') {
      const b = JSON.parse(opts.body);
      seen.push(b.messages[0].content[1].image_url.url);
      return okJson({ reply: '  It looks fine.  ' });
    }
    return { ok: false, json: async () => ({}) };
  };
  const r = await call('reviewScreenshot', 'dev', 'sb', 'a.png', 'ok?');
  assert.equal(r.ok, true);
  assert.equal(r.review, 'It looks fine.');
  assert.ok(seen[0].includes('data:image/png;base64,AAAA'));
  delete context.fetch;
});

await test('reviewScreenshot reports a vision failure without throwing', async () => {
  context.fetch = async (url) => url === '/api/screenshot' ? okJson({ imageBase64: 'A' }) : { ok: false, json: async () => ({}) };
  const r = await call('reviewScreenshot', 'dev', 'sb', 'a.png', 'ok?');
  assert.equal(r.ok, false);
  assert.ok(r.note.includes('vision call failed'));
  context.fetch = async (url) => {
    if (url === '/api/screenshot') return okJson({ imageBase64: 'A' });
    throw new Error('boom'); // the vision call itself blows up
  };
  const r2 = await call('reviewScreenshot', 'dev', 'sb', 'a.png', 'ok?');
  assert.equal(r2.ok, false);
  assert.ok(r2.note.includes('boom'));
  delete context.fetch;
});

console.log('Library helpers (world.js)');

await test('writeLibraryFile posts the file, fire-and-forget', async () => {
  let body = null, headers = null;
  context.fetch = async (url, opts) => { body = JSON.parse(opts.body); headers = opts.headers; return { ok: true }; };
  setGlobal('AGENT_KEYS', { dev: 'k' });
  await call('writeLibraryFile', 'dev', 'skills/x.md', 'content');
  assert.equal(body.path, 'skills/x.md');
  assert.equal(body.source, 'firsthand');
  assert.equal(headers['X-Agent-Key'], 'k');
  delete context.fetch;
});

await test('writeLibraryFile swallows a failed write', async () => {
  context.fetch = async () => { throw new Error('down'); };
  await call('writeLibraryFile', 'dev', 'p', 'c');
  delete context.fetch;
  assert.ok(true);
});

await test('readLibraryFile returns content or null', async () => {
  context.fetch = async (url) => okJson({ content: 'hello' });
  assert.equal(await call('readLibraryFile', 'a/b.md'), 'hello');
  context.fetch = async () => ({ ok: false, status: 404 });
  assert.equal(await call('readLibraryFile', 'a/b.md'), null);
  context.fetch = async () => { throw new Error('x'); };
  assert.equal(await call('readLibraryFile', 'a/b.md'), null);
  delete context.fetch;
});

await test('listLibraryFiles returns the file list or []', async () => {
  context.fetch = async () => okJson({ files: ['f1'] });
  assert.deepEqual([...(await call('listLibraryFiles'))], ['f1']);
  context.fetch = async () => ({ ok: false });
  assert.deepEqual([...(await call('listLibraryFiles'))], []);
  context.fetch = async () => { throw new Error('x'); };
  assert.deepEqual([...(await call('listLibraryFiles'))], []);
  delete context.fetch;
});

await test('promoteLibraryFile promotes and returns success', async () => {
  let path = null;
  context.fetch = async (url, opts) => { path = JSON.parse(opts.body).path; return { ok: true }; };
  assert.equal(await call('promoteLibraryFile', 'dev', 'pending_review/x.md'), true);
  assert.equal(path, 'pending_review/x.md');
  context.fetch = async () => { throw new Error('x'); };
  assert.equal(await call('promoteLibraryFile', 'dev', 'p'), false);
  delete context.fetch;
});

await test('rejectLibraryFile rejects and returns success', async () => {
  context.fetch = async () => ({ ok: true });
  assert.equal(await call('rejectLibraryFile', 'dev', 'pending_review/x.md'), true);
  context.fetch = async () => { throw new Error('x'); };
  assert.equal(await call('rejectLibraryFile', 'dev', 'p'), false);
  delete context.fetch;
});

await test('skillSlug slugs a name and writeSkillFile targets skills/', async () => {
  assert.equal(call('skillSlug', 'React Hooks!'), 'react-hooks-');
  assert.equal(call('skillSlug', 'D3 Charts'), 'd3-charts');
  let path = null;
  context.fetch = async (url, opts) => { path = JSON.parse(opts.body).path; return { ok: true }; };
  await call('writeSkillFile', 'dev', 'React Hooks!', 'content', 'external');
  assert.equal(path, 'skills/react-hooks-.md');
  delete context.fetch;
});

await test('searchLibraryFiles returns matches or []', async () => {
  let url = null;
  context.fetch = async (u) => { url = u; return okJson({ matches: [{ path: 'p' }] }); };
  const m = await call('searchLibraryFiles', 'dev', 'some query');
  assert.equal(m.length, 1);
  assert.ok(url.includes('some%20query'));
  context.fetch = async () => { throw new Error('x'); };
  assert.deepEqual([...(await call('searchLibraryFiles', 'dev', 'q'))], []);
  delete context.fetch;
});

console.log('gatherUnifiedContext (world.js)');

await test('assembles sandbox, library matches, and mail into one context', async () => {
  setGlobal('AGENTS', { dev: { mailbox: [{ text: 'old', read: true }, { text: 'new mail', read: false }] } });
  setGlobal('getSandboxContext', async () => 'sandbox: a.txt, b.txt');
  setGlobal('searchLibraryFiles', async () => [{ path: 'skills/react.md', snippet: 'hooks' }]);
  setGlobal('markMailRead', () => {});
  const ctx = await call('gatherUnifiedContext', 'dev', 'sb', 'react');
  assert.ok(ctx.includes('sandbox: a.txt, b.txt'));
  assert.ok(ctx.includes('skills/react.md: hooks'));
  assert.ok(ctx.includes('[NEW] new mail'));
  assert.ok(ctx.includes('old'));
});

await test('gatherUnifiedContext handles an agent with no mailbox and no topic', async () => {
  setGlobal('AGENTS', {});
  setGlobal('getSandboxContext', async () => 'empty');
  setGlobal('searchLibraryFiles', async () => []);
  setGlobal('markMailRead', () => {});
  const ctx = await call('gatherUnifiedContext', 'ghost', 'sb', null);
  assert.ok(ctx.includes('nothing in your mailbox'));
  assert.ok(ctx.includes('no relevant Library files'));
});

console.log('fetchPageSmart (world.js)');

await test('returns the plain fetch when the page is rich enough', async () => {
  context.fetch = async () => okJson({ allowed: true, text: 'x'.repeat(500) });
  const r = await call('fetchPageSmart', 'dev', 'https://x.test', 'research');
  assert.equal(r.rendered, false);
  delete context.fetch;
});

await test('re-renders a thin page through a real browser when it adds content', async () => {
  const calls = [];
  context.fetch = async (url, opts) => {
    calls.push(JSON.parse(opts.body).render ? 'render' : 'plain');
    if (calls.length === 1) return okJson({ allowed: true, text: 'shell only' });
    return okJson({ allowed: true, text: 'real content after JS runs' });
  };
  const r = await call('fetchPageSmart', 'dev', 'https://spa.test', 'research');
  assert.equal(r.rendered, true);
  assert.deepEqual(calls, ['plain', 'render']);
  delete context.fetch;
});

await test('keeps the plain fetch when the render comes back thinner', async () => {
  context.fetch = async (url, opts) => {
    if (JSON.parse(opts.body).render) return okJson({ allowed: true, text: 'shorter' });
    return okJson({ allowed: true, text: 'shell only' });
  };
  const r = await call('fetchPageSmart', 'dev', 'https://x.test', 'r');
  assert.equal(r.rendered, false);
  delete context.fetch;
});

await test('returns disallowed/failed plain results without re-rendering', async () => {
  context.fetch = async () => okJson({ allowed: false, reason: 'nope' });
  const r = await call('fetchPageSmart', 'dev', 'https://x.test', 'r');
  assert.equal(r.allowed, false);
  assert.equal(r.rendered, false);
  delete context.fetch;
});

console.log('curl / sandbox-save (world.js)');

await test('curlRequest posts with the live in-room attached', async () => {
  setGlobal('AGENTS', { dev: { inRoom: 'commandcenter' } });
  let body = null;
  context.fetch = async (url, opts) => { body = JSON.parse(opts.body); return okJson({ allowed: true }); };
  const r = await call('curlRequest', 'dev', 'GET', 'https://x.test', 'p', {}, null);
  assert.equal(body.inRoom, 'commandcenter');
  assert.equal(r.allowed, true);
  context.fetch = async () => { throw new Error('down'); };
  const r2 = await call('curlRequest', 'dev', 'GET', 'https://x.test', 'p', {}, null);
  assert.equal(r2.allowed, false);
  delete context.fetch;
});

await test('savePageIntoSandbox posts the page and returns the result', async () => {
  setGlobal('AGENTS', { dev: { inRoom: null } });
  context.fetch = async () => okJson({ allowed: true, ok: true, path: 'p' });
  const r = await call('savePageIntoSandbox', 'dev', 'sb', 'https://x.test', 'p', 'f.txt', 'text');
  assert.equal(r.ok, true);
  context.fetch = async () => { throw new Error('down'); };
  const r2 = await call('savePageIntoSandbox', 'dev', 'sb', 'u', 'p', 'f', 't');
  assert.equal(r2.allowed, false);
  delete context.fetch;
});

console.log('crawlAndCollect (world.js)');

await test('crawls a frontier, keeps matching pages, writes the manifest', async () => {
  let saved = [];
  setGlobal('savePageIntoSandbox', async (a, sb, url, purpose, filename, content) => {
    saved.push({ url, filename });
    return { allowed: true, ok: true, path: 'sb/' + filename, bytes: content.length };
  });
  context.fetch = async (url, opts) => {
    const b = JSON.parse(opts.body);
    if (b.url === 'https://hub.test') return okJson({ allowed: true, text: 'hub page with links', links: [{ url: 'https://page.test' }] });
    return okJson({ allowed: true, text: 'detailed page content' });
  };
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://hub.test', 'research');
  assert.equal(r.ok, true);
  assert.equal(r.pagesVisited, 2);
  assert.equal(r.pagesKept, 2);
  assert.equal(saved.length, 3, 'two pages + manifest');
  assert.equal(saved[0].filename, 'crawl-1.txt');
  assert.equal(r.manifestPath, 'sb/manifest.json');
  delete context.fetch;
});

await test('crawl respects skipUrls + pageKeyword and does not revisit', async () => {
  setGlobal('savePageIntoSandbox', async () => ({ allowed: true, ok: true, path: 'p', bytes: 1 }));
  context.fetch = async () => okJson({ allowed: true, text: 'content', links: [] });
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://skip.test', 'r', { maxPages: 3, skipUrls: ['https://skip.test'], pageKeyword: 'match', linkKeyword: '' });
  assert.equal(r.pagesKept, 0, 'already collected page not kept');
  delete context.fetch;
});

await test('crawl records failed and blocked fetches in the manifest', async () => {
  setGlobal('savePageIntoSandbox', async () => ({ allowed: true, ok: true, path: 'p', bytes: 1 }));
  context.fetch = async (url, opts) => {
    if (JSON.parse(opts.body).url.includes('blocked')) return okJson({ allowed: false, reason: 'blocked' });
    return okJson({ allowed: true, text: 'ok content', links: [{ url: 'https://blocked.test' }, { url: 'https://kept.test' }] });
  };
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://hub.test', 'r');
  assert.equal(r.pagesVisited, 3);
  assert.equal(r.pagesKept, 2);
  delete context.fetch;
});

await test('crawl notes a fetch that throws as a request failure', async () => {
  setGlobal('savePageIntoSandbox', async () => ({ allowed: true, ok: true, path: 'p', bytes: 1 }));
  context.fetch = async (url, opts) => {
    if (JSON.parse(opts.body).url.includes('boom')) throw new Error('conn reset');
    return okJson({ allowed: true, text: 'fine', links: [{ url: 'https://boom.test' }] });
  };
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://hub.test', 'r');
  assert.equal(r.pagesVisited, 2, 'hub + the link that then failed');
  assert.equal(r.pagesKept, 1);
  delete context.fetch;
});

await test('crawl marks a page whose sandbox save fails', async () => {
  setGlobal('savePageIntoSandbox', async () => ({ allowed: false, ok: false, reason: 'sandbox full' }));
  context.fetch = async () => okJson({ allowed: true, text: 'content', links: [] });
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://hub.test', 'r');
  assert.equal(r.pagesKept, 0);
  delete context.fetch;
});

await test('crawl skips pages that do not match the pageKeyword', async () => {
  setGlobal('savePageIntoSandbox', async () => ({ allowed: true, ok: true, path: 'p', bytes: 1 }));
  context.fetch = async () => okJson({ allowed: true, text: 'irrelevant text', links: [] });
  const r = await call('crawlAndCollect', 'dev', 'sb', 'https://hub.test', 'r', { pageKeyword: 'needle' });
  assert.equal(r.pagesKept, 0);
  delete context.fetch;
});

console.log('page probe (world.js)');

await test('requestPageProbe posts actions/probes and returns the result', async () => {
  let body = null;
  context.fetch = async (url, opts) => { body = JSON.parse(opts.body); return okJson({ results: {} }); };
  const r = await call('requestPageProbe', 'dev', 'sb', null, ['click'], []);
  assert.equal(body.path, 'index.html');
  assert.deepEqual(body.actions, ['click']);
  context.fetch = async () => { throw new Error('down'); };
  const r2 = await call('requestPageProbe', 'dev', 'sb', 'a.html', [], []);
  assert.equal(r2.error, 'request failed: down');
  delete context.fetch;
});

await test('formatPageProbeResult renders a readable fact sheet', () => {
  const fmt = (d) => call('formatPageProbeResult', d);
  assert.ok(fmt({ error: 'x' }).includes('probe could not run'));
  const out = fmt({
    actionLog: ['click #btn'],
    console: ['hello'],
    pageErrors: [],
    customGlobals: [
      { name: 'state', type: 'object', keys: ['ui', 'player'] },
      { name: 'list', type: 'array', length: 3 },
      { name: 'x', type: 'number', value: 42 },
      { name: 'fn', type: 'function' },
    ],
    results: { 'typeof state': '"object"' },
  });
  assert.ok(out.includes('click #btn'));
  assert.ok(out.includes('hello'));
  assert.ok(out.includes('window.state (object) -- real keys: ui, player'));
  assert.ok(out.includes('window.list (array, length 3)'));
  assert.ok(out.includes('window.x (number) = 42'));
  assert.ok(out.includes('window.fn (function)'));
  assert.ok(out.includes('typeof state =>'), out);
});

await test('formatPageProbeResult handles empty inventories and no results', () => {
  const fmt = (d) => call('formatPageProbeResult', d);
  const out = fmt({ actionLog: [], console: [], pageErrors: [], customGlobals: [], results: {} });
  assert.ok(out.includes('(none)'));
  assert.ok(out.includes('(none found)'));
});

console.log('collision grid (world.js)');

await test('loadCollisionGrid fetches and stores the grid', async () => {
  const grid = { cols: 4, rows: 2, cell: 8, grid: [[0, 0, 0, 0], [0, 1, 0, 0]] };
  context.fetch = async (url) => { assert.ok(url.startsWith('collision_grid.json')); return okJson(grid); };
  await call('loadCollisionGrid');
  assert.equal(vm.runInContext('COLLISION_GRID.cols', context), 4);
  delete context.fetch;
});

await test('blockedAt reports blocked only where the grid says so', () => {
  vm.runInContext('COLLISION_GRID = { cols: 4, rows: 2, cell: 8, grid: [[0, 0, 0, 0], [0, 1, 0, 0]] };', context);
  // cell (1,1) is blocked: world coords x=8..16, y=8..16 at SCALE=2
  assert.equal(call('blockedAt', { x: 9 * 2, y: 9 * 2, w: 1, h: 1 }), true);
  assert.equal(call('blockedAt', { x: 1 * 2, y: 1 * 2, w: 1, h: 1 }), false);
});

await test('blockedAt treats a missing grid as fully open', () => {
  vm.runInContext('COLLISION_GRID = null;', context);
  assert.equal(call('blockedAt', { x: 0, y: 0, w: 10, h: 10 }), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);