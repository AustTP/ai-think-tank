// Director-owned role templates (templates.js): the House (directors'/admin's
// workspace) is where role templates are authored. Only directors + the admin
// may create/edit/delete; every write acts AS the available authority. Tests
// the client-side helpers: payload shaping, the director/admin author gate, and
// how the acting authority resolves.
//
// Run: node tests/test_templates.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set };
vm.createContext(context);

for (const f of ['agents.js', 'templates.js']) {
  const src = fs.readFileSync(path.join(worldDir, f), 'utf8');
  vm.runInContext(src, context, { filename: f });
}
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const templateAuthorId = (...a) => vm.runInContext('templateAuthorId', context)(...a);
const isTemplateAuthor = (...a) => vm.runInContext('isTemplateAuthor', context)(...a);
const buildTemplatePayload = (...a) => vm.runInContext('buildTemplatePayload', context)(...a);

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

function setRoster(roster) {
  setGlobal('AGENT_ROSTER', roster);
}
// availableAuthority is defined in agents.js and reads AGENT_ROSTER directly.

const FULL_ROSTER = [
  { id: 'faye', name: 'Faye', isAdmin: true, isDirector: true },
  { id: 'nora', name: 'Nora', isDirector: true },
  { id: 'dev', name: 'Dev', role: 'Code', director: 'faye' },
  { id: 'nadia', name: 'Nadia', role: 'Research', director: 'dev' },
];

console.log('director-owned templates (templates.js + agents.js)');

await test('admin is the acting authority when present', () => {
  setRoster(FULL_ROSTER);
  assert.equal(templateAuthorId(), 'faye', 'availableAuthority prefers the admin');
  assert.equal(isTemplateAuthor(), true);
});

await test('senior-most director stands in when no admin', () => {
  setRoster([
    { id: 'nora', name: 'Nora', isDirector: true },
    { id: 'dev', name: 'Dev', role: 'Code', director: 'nora' },
  ]);
  assert.equal(templateAuthorId(), 'nora', 'senior-most director when admin absent');
  assert.equal(isTemplateAuthor(), true);
});

await test('no authority means authoring is blocked', () => {
  setRoster([{ id: 'dev', name: 'Dev', role: 'Code', director: 'nora' }]);
  assert.equal(templateAuthorId(), null, 'a lone worker is not an author');
  assert.equal(isTemplateAuthor(), false);
});

await test('payload shaping trims and drops empty instructions', () => {
  const p = buildTemplatePayload('Orchard', '  Tend the orchard.  ', ['First rule', '   ', 'Second rule'], ['a note']);
  // Field-by-field across the VM boundary -- deepEqual across realms fails on
  // prototype identity, not on content.
  assert.equal(p.role, 'Orchard');
  assert.equal(p.mission, 'Tend the orchard.');
  assert.deepEqual([...p.instructions], ['First rule', 'Second rule']);
  assert.deepEqual([...p.notes], ['a note']);
});

await test('payload allows a brand-new role (create)', () => {
  const p = buildTemplatePayload('', 'Fresh role mission', ['Rule'], []);
  assert.equal(p.role, '');
  assert.equal(p.mission, 'Fresh role mission');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);