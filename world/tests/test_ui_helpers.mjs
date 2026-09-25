// Regression tests for small pure functions that live inline in
// index.html, mixed in with a large, DOM/canvas-dependent game-loop
// script that can't reasonably be loaded whole in Node without stubbing
// out the entire rendering stack. Instead of doing that (or re-typing the
// logic here, which could silently drift from the real code), this pulls
// the exact source text of just these two self-contained functions out
// of the real file and evals them -- still the real shipped code, just
// extracted rather than the whole file executed.
//
// Run: node tests/test_ui_helpers.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(worldDir, 'index.html'), 'utf8');

// Brace-matched extraction starting at `function <name>(` -- robust to
// nested braces inside the function body (both of these have some),
// unlike a naive regex up to the first `}`.
function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`function ${name} not found in index.html`);
  const bodyStart = src.indexOf('{', start);
  let depth = 0, i = bodyStart;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

function extractConst(src, name) {
  // Matches both `const NAME = N` and a second declarator in the same
  // statement (`const VIEW_W = 960, VIEW_H = 680;`), which is how these
  // two are actually declared.
  const m = src.match(new RegExp(`(?:const\\s+)?\\b${name}\\s*=\\s*(\\d+)`));
  if (!m) throw new Error(`const ${name} not found in index.html`);
  return Number(m[1]);
}

const VIEW_W = extractConst(html, 'VIEW_W');
const VIEW_H = extractConst(html, 'VIEW_H');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);
// camXY closes over VIEW_W/VIEW_H (index.html module-level consts) --
// provide the same real values so the extracted function sees what it
// would in the browser, not `undefined`.
vm.runInContext(`var VIEW_W = ${VIEW_W}, VIEW_H = ${VIEW_H};`, context);
vm.runInContext(extractFunction(html, 'camXY'), context, { filename: 'index.html:camXY' });
vm.runInContext(extractFunction(html, 'dedupActivity'), context, { filename: 'index.html:dedupActivity' });
const camXY = (...args) => vm.runInContext('camXY', context)(...args);
const dedupActivity = (...args) => vm.runInContext('dedupActivity', context)(...args);

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

console.log('camXY / dedupActivity (index.html inline script, extracted)');

test('camXY: centers when the world is smaller than the viewport', () => {
  const [camX, camY] = camXY(1, 100, 100, 50, 50);
  assert.equal(camX, -(VIEW_W / 1 - 100) / 2);
  assert.equal(camY, -(VIEW_H / 1 - 100) / 2);
});

test('camXY: clamps to the world edge, never shows past it', () => {
  const worldW = 2000, worldH = 2000;
  const [camX, camY] = camXY(1, worldW, worldH, -500, -500); // focus way off the top-left edge
  assert.equal(camX, 0);
  assert.equal(camY, 0);
});

test('camXY: clamps at the far edge too', () => {
  const worldW = 2000, worldH = 2000;
  const [camX] = camXY(1, worldW, worldH, worldW + 500, worldH + 500);
  assert.equal(camX, worldW - VIEW_W / 1);
});

test('dedupActivity: collapses consecutive identical rows with a count', () => {
  const entries = [
    { agentId: 'a', action: 'decide', details: { x: 1 }, ts: 3 },
    { agentId: 'a', action: 'decide', details: { x: 1 }, ts: 2 },
    { agentId: 'a', action: 'decide', details: { x: 1 }, ts: 1 },
  ];
  const out = dedupActivity(entries);
  assert.equal(out.length, 1);
  assert.equal(out[0].count, 3);
  assert.equal(out[0].oldestTs, 1);
});

test('dedupActivity: does NOT merge the same action separated by a different one', () => {
  const entries = [
    { agentId: 'a', action: 'decide', details: {}, ts: 3 },
    { agentId: 'b', action: 'hire', details: {}, ts: 2 },
    { agentId: 'a', action: 'decide', details: {}, ts: 1 },
  ];
  const out = dedupActivity(entries);
  assert.equal(out.length, 3, 'a non-consecutive repeat should stay its own row, not get merged across the row between them');
});

test('dedupActivity: different details on the same action/agent do not merge', () => {
  const entries = [
    { agentId: 'a', action: 'task_assigned', details: { room: 'bank' }, ts: 2 },
    { agentId: 'a', action: 'task_assigned', details: { room: 'library' }, ts: 1 },
  ];
  const out = dedupActivity(entries);
  assert.equal(out.length, 2);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
