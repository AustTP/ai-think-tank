// Real regression test for two mechanical sandbox-integrity checks in
// runCodingTask() (tasks.js), added after a rewrite produced a "clean"
// result that was actually still broken in two ways neither review nor QA
// caught:
//
// 1. settings.html referenced <script src="settings.js">, but
//    settings.js was never written -- invisible to the ORIGINAL
//    _findPhantomScriptRefs, which only ever checked index.html.
// 2. app.js built and positioned elements against a selector that
//    styles.css styled and app.js queried, but no HTML file ever actually
//    placed those elements in the page. Nothing existed to catch a
//    selector that's referenced everywhere except the one place that
//    would make it real.
//
// These tests lock in the fixes: _findPhantomScriptRefs/_findUnlinkedJsFiles
// now check every *.html file, not just index.html, and a new
// _findDanglingSelectorRefs catches a queried selector that's neither in
// markup nor created dynamically.
//
// Run: node tests/test_sandbox_integrity_checks.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Map, RegExp, Promise };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'tasks.js'), 'utf8'), context, { filename: 'tasks.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const findPhantomScriptRefs = (...a) => vm.runInContext('_findPhantomScriptRefs', context)(...a);
const findUnlinkedJsFiles = (...a) => vm.runInContext('_findUnlinkedJsFiles', context)(...a);
const findDanglingSelectorRefs = (...a) => vm.runInContext('_findDanglingSelectorRefs', context)(...a);

// Each stub just returns the stdout a real /api/execute call would have
// produced for the shell command these functions send -- the tests don't
// re-derive the shell logic, they pin the CONTRACT: given this real
// sandbox content, what should the function conclude.
function stubExecute(stdout) {
  setGlobal('agentFetch', async () => ({ json: async () => ({ allowed: true, stdout }) }));
}

let passed = 0, failed = 0;
function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`  ok - ${name}`); passed++; })
    .catch(e => { console.log(`  FAIL - ${name}`); console.log(`         ${e.message}`); failed++; });
}

console.log('_findPhantomScriptRefs (all *.html files, not just index.html)');

await test('catches a phantom script reference in a NON-index.html page', async () => {
  stubExecute('EXISTS:index.html:app.js\nPHANTOM:settings.html:settings.js\n');
  const refs = await findPhantomScriptRefs('sam', 'test-project');
  assert.equal(JSON.stringify(refs), JSON.stringify([{ html: 'settings.html', file: 'settings.js' }]));
});

await test('reports refs from multiple html files independently', async () => {
  stubExecute('PHANTOM:index.html:missing.js\nPHANTOM:settings.html:settings.js\n');
  const refs = await findPhantomScriptRefs('sam', 'test-project');
  assert.equal(JSON.stringify(refs), JSON.stringify([
    { html: 'index.html', file: 'missing.js' },
    { html: 'settings.html', file: 'settings.js' },
  ]));
});

await test('a fully linked sandbox reports nothing', async () => {
  stubExecute('EXISTS:index.html:app.js\nEXISTS:settings.html:settings.js\n');
  const refs = await findPhantomScriptRefs('sam', 'test-project');
  assert.equal(JSON.stringify(refs), JSON.stringify([]));
});

console.log('\n_findUnlinkedJsFiles (checks every *.html file)');

await test('a file linked only from a non-index.html page is not a false positive', async () => {
  stubExecute('LINKED:settings.js\n');
  const unlinked = await findUnlinkedJsFiles('sam', 'test-project', ['settings.js']);
  assert.equal(JSON.stringify(unlinked), JSON.stringify([]));
});

await test('a truly unlinked file is still reported', async () => {
  stubExecute('UNLINKED:orphan.js\n');
  const unlinked = await findUnlinkedJsFiles('sam', 'test-project', ['orphan.js']);
  assert.equal(JSON.stringify(unlinked), JSON.stringify(['orphan.js']));
});

console.log('\n_findDanglingSelectorRefs (queried selector with no real element anywhere)');

await test('a class queried in JS but never in markup or created dynamically is flagged', async () => {
  const blob = [
    '--- index.html ---',
    '<div class="pad-grid"></div><div id="score-display"></div>',
    '--- app.js ---',
    "document.querySelector('.highway'); document.querySelector('.hit-line');",
  ].join('\n');
  stubExecute(blob);
  const dangling = await findDanglingSelectorRefs('sam', 'test-project');
  const selectors = [...dangling].map(d => d.selector).sort();
  assert.equal(JSON.stringify(selectors), JSON.stringify(['.highway', '.hit-line']));
});

await test('a class present in static markup is not flagged', async () => {
  const blob = [
    '--- index.html ---',
    '<div class="pad-grid"></div>',
    '--- app.js ---',
    "document.querySelector('.pad-grid');",
  ].join('\n');
  stubExecute(blob);
  const dangling = await findDanglingSelectorRefs('sam', 'test-project');
  assert.equal(JSON.stringify(dangling), JSON.stringify([]));
});

await test('a class created dynamically via classList.add is not flagged', async () => {
  const blob = [
    '--- index.html ---',
    '<div id="root"></div>',
    '--- app.js ---',
    "const el = document.createElement('div'); el.classList.add('note');",
    "document.querySelector('.note');",
  ].join('\n');
  stubExecute(blob);
  const dangling = await findDanglingSelectorRefs('sam', 'test-project');
  assert.equal(JSON.stringify(dangling), JSON.stringify([]));
});

await test('an id created dynamically via setAttribute is not flagged', async () => {
  const blob = [
    '--- index.html ---',
    '<div id="root"></div>',
    '--- app.js ---',
    "const el = document.createElement('div'); el.setAttribute('id', 'dynamic-thing');",
    "document.getElementById('dynamic-thing');",
  ].join('\n');
  stubExecute(blob);
  const dangling = await findDanglingSelectorRefs('sam', 'test-project');
  assert.equal(JSON.stringify(dangling), JSON.stringify([]));
});

await test('a read failure is treated as unconfirmed, not flagged', async () => {
  setGlobal('agentFetch', async () => ({ json: async () => ({ allowed: false }) }));
  const dangling = await findDanglingSelectorRefs('sam', 'test-project');
  assert.equal(JSON.stringify(dangling), JSON.stringify([]));
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
