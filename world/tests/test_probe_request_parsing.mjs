// Real regression test for the agentic probe-request protocol added to
// runCodingTask() (tasks.js) -- the single biggest gap found this
// session: the core coding loop had no way for the model to ask for a
// real fact mid-generation, so every fact any fix ever needed had to be
// pre-guessed and stuffed into context by a human, one at a time, across
// eight real rounds. _parseProbeRequest is the pure half of that: telling
// a genuine probe request apart from an ordinary shell command reply,
// deliberately narrow so nothing else is ever misread as one.
//
// Run: node tests/test_probe_request_parsing.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'tasks.js'), 'utf8');
vm.runInContext(src, context, { filename: 'tasks.js' });

const parseProbeRequest = (...args) => vm.runInContext('_parseProbeRequest', context)(...args);

let passed = 0, failed = 0;
function test(name, fn) {
  try {
    fn();
    console.log(`  ok - ${name}`);
    passed++;
  } catch (e) {
    console.log(`  FAIL - ${name}`);
    console.log(`         ${e.message}`);
    failed++;
  }
}

console.log('_parseProbeRequest (tasks.js)');

test('a well-formed probe request parses, with defaults filled in', () => {
  const reply = JSON.stringify({ probeRequest: { actions: [{ type: 'click', selector: 'text=Play' }], probes: ['1+1'] } });
  const parsed = parseProbeRequest(reply);
  assert.equal(parsed.path, 'index.html');
  assert.equal(parsed.actions.length, 1);
  assert.equal(parsed.probes.length, 1);
});

test('an explicit path is preserved, not overridden by the default', () => {
  const reply = JSON.stringify({ probeRequest: { path: 'other.html', probes: ['1+1'] } });
  assert.equal(parseProbeRequest(reply).path, 'other.html');
});

test('probes-only (no actions) is a valid request', () => {
  const reply = JSON.stringify({ probeRequest: { probes: ['typeof window.Foo'] } });
  const parsed = parseProbeRequest(reply);
  assert.deepEqual(JSON.parse(JSON.stringify(parsed.actions)), []);
  assert.equal(parsed.probes.length, 1);
});

test('actions-only (no probes) is a valid request', () => {
  const reply = JSON.stringify({ probeRequest: { actions: [{ type: 'wait', ms: 100 }] } });
  const parsed = parseProbeRequest(reply);
  assert.equal(parsed.actions.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(parsed.probes)), []);
});

test('markdown-fenced JSON is still recognized', () => {
  const reply = '```json\n' + JSON.stringify({ probeRequest: { probes: ['1+1'] } }) + '\n```';
  assert.ok(parseProbeRequest(reply), 'expected a fenced probeRequest block to still parse');
});

test('an ordinary shell command is never mistaken for a probe request', () => {
  assert.equal(parseProbeRequest("cat > fix.js << 'EOF'\nconsole.log('hi');\nEOF"), null);
});

test('valid JSON that is not a probeRequest is not mistaken for one', () => {
  assert.equal(parseProbeRequest(JSON.stringify({ somethingElse: true })), null);
});

test('a probeRequest with neither actions nor probes is rejected as malformed', () => {
  assert.equal(parseProbeRequest(JSON.stringify({ probeRequest: { path: 'index.html' } })), null);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
