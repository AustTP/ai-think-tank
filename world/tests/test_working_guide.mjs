// Regression tests for Phase G's working-guide memory (readWorkingGuide,
// prependWorkingGuide, appendWorkingGuide in tasks.js). readLibraryFile and
// writeLibraryFile live in world.js (not loaded here), so they're stubbed
// to a small in-memory store -- enough to prove the guide is read at task
// start, wrapped into the prompt, and appended-to (never clobbered).
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';

const base = path.resolve(import.meta.dirname, '..');
const context = vm.createContext();
vm.runInContext(fs.readFileSync(path.join(base, 'tasks.js'), 'utf8'), context, { filename: 'tasks.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const readGlobal = (n) => {
  vm.runInContext(`__extract_${n} = ${n};`, context);
  return context['__extract_' + n];
};

// In-memory library stub: only working-guide.md, all other paths read null.
vm.runInContext(`
__lib_store = { 'working-guide.md': null };
__stub_read = async (p) => __lib_store[p] || null;
__stub_write = async (a, p, content) => { __lib_store[p] = content; };
`, context);
setGlobal('readLibraryFile', vm.runInContext('__stub_read', context));
setGlobal('writeLibraryFile', vm.runInContext('__stub_write', context));

const readWorkingGuide = (...a) => vm.runInContext('readWorkingGuide', context)(...a);
const prependWorkingGuide = (...a) => vm.runInContext('prependWorkingGuide', context)(...a);
const appendWorkingGuide = (...a) => vm.runInContext('appendWorkingGuide', context)(...a);

let passed = 0, failed = 0;
const ok = (name, cond) => { if (cond) { passed++; console.log('  ok - ' + name); } else { failed++; console.log('  FAIL - ' + name); } };
const store = (p) => { vm.runInContext(`__g = __lib_store[${JSON.stringify(p)}];`, context); return vm.runInContext('__g', context); };

console.log('working-guide memory (Phase G)');

// 1. No guide yet -> readWorkingGuide returns null; prepend is a no-op.
ok('readWorkingGuide -> null when no guide', (await readWorkingGuide()) === null);
const plain = prependWorkingGuide('TASK PROMPT', null);
ok('prependWorkingGuide leaves prompt unchanged with no guide', plain === 'TASK PROMPT');

// 2. A guide exists -> it is read and wrapped above the task prompt.
vm.runInContext('__lib_store["working-guide.md"] = "Lead with a concrete reader outcome.";', context);
ok('readWorkingGuide returns the guide when present', (await readWorkingGuide()) === 'Lead with a concrete reader outcome.');
const wrapped = prependWorkingGuide('TASK PROMPT', await readWorkingGuide());
ok('prependWorkingGuide places the guide above the task', wrapped.startsWith('You carry the village\'s working guide'));
ok('prependWorkingGuide preserves the task prompt at the end', wrapped.endsWith('TASK PROMPT'));

// 3. appendWorkingGuide creates the file on the first lesson.
vm.runInContext('__lib_store["working-guide.md"] = null;', context);
await appendWorkingGuide('agent1', 'Verify real behavior with a page probe.');
ok('appendWorkingGuide seeds the file on a first lesson', (await readWorkingGuide()).includes('Verify real behavior with a page probe.'));

// 4. appendWorkingGuide APPENDS a second lesson, never clobbers the first.
await appendWorkingGuide('agent1', 'Rerun the page probe after writing a new file.');
const both = await readWorkingGuide();
ok('appendWorkingGuide appends (keeps the first lesson)', both.includes('Verify real behavior with a page probe.'));
ok('appendWorkingGuide adds the second lesson', both.includes('Rerun the page probe'));
ok('appendWorkingGuide marks each as a Lesson block', (both.match(/## Lesson/g) || []).length === 2);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);