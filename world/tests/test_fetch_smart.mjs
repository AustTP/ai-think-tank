// Real regression test for fetchPageSmart's fallback decision logic
// (world.js) -- the fix for a real, confirmed gap: plain /api/browse
// only ever sees a page's initial HTML, so a JS-rendered site (Reddit,
// Twitter/X, any single-page app) comes back as a near-empty shell. This
// locks in WHEN the real (slower, costlier) rendered retry actually
// fires: only when the plain fetch looks thin, never for an ordinary
// page, and never if the "render" retry didn't actually help.
//
// Run: node tests/test_fetch_smart.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Promise };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'world.js'), 'utf8'), context, { filename: 'world.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const fetchPageSmart = (...a) => vm.runInContext('fetchPageSmart', context)(...a);

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

// One counter per call in a scripted sequence -- lets each test dictate
// exactly what the plain fetch and the render retry each return, and
// confirms whether the retry was even attempted.
function installFetchSequence(responses) {
  let call = 0;
  setGlobal('agentFetch', async (_url, _agentId, opts) => {
    const body = JSON.parse(opts.body);
    const wasRender = !!body.render;
    const resp = responses[call++];
    return { json: async () => resp, wasRender };
  });
  return () => call;
}

console.log('fetchPageSmart render-fallback decision (world.js)');

await test('a normal-length page never triggers the render retry', async () => {
  const callCount = installFetchSequence([
    { allowed: true, text: 'x'.repeat(500) },
  ]);
  const result = await fetchPageSmart('sam', 'https://example.com', 'test');
  assert.equal(result.rendered, false);
  assert.equal(callCount(), 1, 'expected only the plain fetch, no retry');
});

await test('a thin/empty-shell result triggers exactly one render retry', async () => {
  const callCount = installFetchSequence([
    { allowed: true, text: '' }, // the JS-shell case: nearly nothing came back
    { allowed: true, text: 'x'.repeat(5000) }, // the real content, once rendered
  ]);
  const result = await fetchPageSmart('sam', 'https://example.com', 'test');
  assert.equal(result.rendered, true);
  assert.equal(result.text.length, 5000);
  assert.equal(callCount(), 2, 'expected the plain fetch, then exactly one render retry');
});

await test('a blocked plain fetch never attempts a render retry', async () => {
  const callCount = installFetchSequence([
    { allowed: false, reason: 'not approved' },
  ]);
  const result = await fetchPageSmart('sam', 'https://example.com', 'test');
  assert.equal(result.rendered, false);
  assert.equal(result.allowed, false);
  assert.equal(callCount(), 1, 'a blocked URL should never trigger a second, rendered attempt at the same URL');
});

await test('if the render retry comes back even thinner, the original plain result wins', async () => {
  const callCount = installFetchSequence([
    { allowed: true, text: '' },
    { allowed: true, text: '' }, // render also failed to find real content (e.g. timed out)
  ]);
  const result = await fetchPageSmart('sam', 'https://example.com', 'test');
  assert.equal(result.rendered, false, 'a render that did not actually help should not be reported as the winning read');
  assert.equal(callCount(), 2, 'the retry should still have been attempted even though it did not help');
});

await test('a render retry that itself gets blocked falls back to the plain result', async () => {
  const callCount = installFetchSequence([
    { allowed: true, text: '' },
    { allowed: false, reason: 'blocked on the retry somehow' },
  ]);
  const result = await fetchPageSmart('sam', 'https://example.com', 'test');
  assert.equal(result.rendered, false);
  assert.equal(result.allowed, true, 'should still report the original (thin but allowed) plain result, not the retry\'s block');
  assert.equal(callCount(), 2);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
