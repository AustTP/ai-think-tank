// Tests for the Jev decision API client (jev.js): the generic "pick one of
// N labeled candidates" call that tasks.js and hiring.js both use. Covers
// the happy path, empty candidates, a server error, a Jev answer that
// matches no real candidate, and a network failure -- the confidence/cost
// extraction and the callers' fallback contract are exactly what these
// lock in. Loads the REAL jev.js into a vm context.
//
// Run: node tests/test_jev.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'jev.js'), 'utf8');
vm.runInContext(src, context, { filename: 'jev.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

const requestJevChoice = (...a) => vm.runInContext('requestJevChoice', context)(...a);

let passed = 0, failed = 0;
const queued = [];
function test(name, fn) {
  queued.push({ name, fn });
}

const candidates = [
  { id: 'ada', description: 'Ada, dev. mission-x' },
  { id: 'ben', description: 'Ben, dev. mission-y' },
];

console.log('jev.js Jev decision client');

test('requestJevChoice returns null immediately for no candidates', async () => {
  assert.equal(await requestJevChoice('pick', []), null);
  assert.equal(await requestJevChoice('pick', null), null);
});

test('a happy path returns the chosen candidate with confidence and cost', async () => {
  const seen = [];
  setGlobal('apiFetch', async (url, opts) => {
    const body = JSON.parse(opts.body);
    seen.push(body);
    assert.equal(url, '/api/decide');
    assert.equal(body.questions.choice.type, 'choice');
    assert.equal(body.questions.choice.instructions, 'pick one');
    assert.deepEqual(body.questions.choice.criteria.ada, 'Ada, dev. mission-x');
    return { ok: true, json: async () => ({ answers: { choice: { choice: 'ben', confidence: 0.9 } }, usage: { cost: 0.00012 } }) };
  });
  const r = await requestJevChoice('pick one', candidates);
  assert.equal(r.choice, 'ben');
  assert.equal(r.confidence, 0.9);
  assert.equal(r.cost, 0.00012);
});

test('an answer matching no real candidate comes back as choice null', async () => {
  setGlobal('apiFetch', async () => ({ ok: true, json: async () => ({ answers: { choice: { choice: 'nobody' } } }) }));
  const r = await requestJevChoice('pick', candidates);
  assert.equal(r.choice, null);
  assert.equal(r.confidence, 1.0, 'missing confidence defaults to 1.0');
  assert.equal(r.cost, 0.0, 'missing cost defaults to 0.0');
});

test('a non-OK response with an error is reported, not raised', async () => {
  setGlobal('apiFetch', async () => ({ ok: false, json: async () => ({ error: 'boom' }) }));
  const r = await requestJevChoice('pick', candidates);
  assert.equal(r.choice, null);
  assert.equal(r.confidence, 1.0);
  assert.equal(r.cost, 0.0);
});

test('a thrown network error is swallowed into a null choice', async () => {
  setGlobal('apiFetch', async () => { throw new Error('network down'); });
  const r = await requestJevChoice('pick', candidates);
  assert.equal(r.choice, null);
  assert.equal(r.confidence, 1.0);
});

(async () => {
  for (const { name, fn } of queued) {
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
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
