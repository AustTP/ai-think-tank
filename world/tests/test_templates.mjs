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

// --- Fake DOM -------------------------------------------------------------
// templates.js's render/edit/delete paths touch document.getElementById,
// createElement, createTextNode, querySelectorAll, classList, addEventListener,
// value, innerHTML, style, disabled, textContent, and the boardModal maxHeight
// style. Build a small fake element tree that records the interactions the
// tests assert on instead of doing real layout.
const elements = {}; // id -> fake element

function fakeEl(id, tag = 'div') {
  const classes = new Set();
  const el = {
    tagName: tag,
    innerHTML: '',
    textContent: '',
    value: '',
    className: '',
    disabled: false,
    rows: 0,
    placeholder: '',
    type: 'text',
    _attr: {},
    _handlers: {},
    style: {},
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); },
    setAttribute(k, v) { this._attr[k] = v; },
    getAttribute(k) { return this._attr[k]; },
    appendChild() {},
    querySelectorAll() { return []; },
    _classes: classes,
  };
  // Assigning an id (e.g. `btn.id = 'templateCreateBtn'` in buildNewTemplateForm)
  // must make the element findable via getElementById, like a real DOM.
  Object.defineProperty(el, 'id', {
    get: () => id,
    set: (v) => {
      if (id && elements[id] === el) delete elements[id];
      id = v;
      if (v) elements[v] = el;
    },
    configurable: true,
  });
  return el;
}

context.document = {
  getElementById(id) {
    if (!elements[id]) elements[id] = fakeEl(id);
    return elements[id];
  },
  createElement(tag) { return fakeEl('', tag); },
  createTextNode(t) { return { nodeType: 3, textContent: t }; },
};
context.alert = () => {};
context.confirm = () => true;
context.renderBoard = () => {};
context.state = { ui: 'board' };
context.setTimeout = () => {};
context.escapeHtml = (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

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

const fetchTemplates = (...a) => vm.runInContext('fetchTemplates', context)(...a);
const fetchTemplate = (...a) => vm.runInContext('fetchTemplate', context)(...a);
const saveTemplate = (...a) => vm.runInContext('saveTemplate', context)(...a);
const deleteTemplate = (...a) => vm.runInContext('deleteTemplate', context)(...a);
const renderTemplatesEditor = () => vm.runInContext('renderTemplatesEditor', context)();
const buildNewTemplateForm = (...a) => vm.runInContext('buildNewTemplateForm', context)(...a);
const handleTemplateCreate = (...a) => vm.runInContext('handleTemplateCreate', context)(...a);
const openTemplateEditor = (...a) => vm.runInContext('openTemplateEditor', context)(...a);
const handleTemplateSave = (...a) => vm.runInContext('handleTemplateSave', context)(...a);
const handleTemplateDelete = (...a) => vm.runInContext('handleTemplateDelete', context)(...a);
const escapeHtml = (...a) => vm.runInContext('escapeHtml', context)(...a);
const bindTemplateListButtons = (...a) => vm.runInContext('bindTemplateListButtons', context)(...a);

console.log('director-owned template fetch/save/render paths (templates.js)');

await test('escapeHtml escapes the HTML-special characters', () => {
  assert.equal(escapeHtml('<b a="1">&'), '&lt;b a=&quot;1&quot;&gt;&amp;');
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml(''), '');
  assert.equal(escapeHtml('plain'), 'plain');
});

await test('fetchTemplates returns the templates map', async () => {
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: { Orchard: { mission: 'x' } } }) });
  const t = await fetchTemplates();
  assert.equal(t.Orchard.mission, 'x');
});

await test('fetchTemplates handles a payload with no templates key', async () => {
  context.apiFetch = async () => ({ ok: true, json: async () => ({}) });
  assert.deepEqual(Object.keys(await fetchTemplates()), []);
});

await test('fetchTemplate fetches and returns one role', async () => {
  context.apiFetch = async (url) => {
    assert.equal(url, '/api/templates/Orchard');
    return { ok: true, json: async () => ({ role: 'Orchard', profile: { mission: 'm' } }) };
  };
  const d = await fetchTemplate('Orchard');
  assert.equal(d.role, 'Orchard');
  assert.equal(d.profile.mission, 'm');
});

await test('fetchTemplate URL-encodes the role', async () => {
  context.apiFetch = async (url) => {
    assert.equal(url, '/api/templates/My%20Role');
    return { ok: true, json: async () => ({ role: 'My Role' }) };
  };
  const d = await fetchTemplate('My Role');
  assert.equal(d.role, 'My Role');
});

await test('saveTemplate posts with the acting authority and returns the response', async () => {
  setRoster(FULL_ROSTER);
  context.agentFetch = async (url, author, opts) => {
    assert.equal(url, '/api/templates?requesterId=faye');
    assert.equal(author, 'faye');
    assert.equal(opts.method, 'POST');
    const body = JSON.parse(opts.body);
    assert.equal(body.role, 'Orchard');
    assert.equal(body.mission, 'Tend.');
    assert.deepEqual(body.instructions, ['R1']);
    return { ok: true, json: async () => ({ ok: true, appliedTo: ['dev'] }) };
  };
  const r = await saveTemplate('Orchard', '  Tend.  ', ['R1', '   '], []);
  assert.equal(r.ok, true);
});

await test('saveTemplate blocks when no director or admin is available', async () => {
  setRoster([{ id: 'dev', name: 'Dev', role: 'Code', director: 'nora' }]);
  context.agentFetch = async () => { throw new Error('should not be called'); };
  const r = await saveTemplate('Orchard', 'm', ['r'], []);
  assert.equal(r.error, 'No director or admin available to author templates.');
});

await test('deleteTemplate sends a DELETE with the acting authority', async () => {
  setRoster(FULL_ROSTER);
  context.agentFetch = async (url, author, opts) => {
    assert.equal(url, '/api/templates/Orchard?requesterId=faye');
    assert.equal(author, 'faye');
    assert.equal(opts.method, 'DELETE');
    return { ok: true, json: async () => ({ ok: true }) };
  };
  const r = await deleteTemplate('Orchard');
  assert.equal(r.ok, true);
});

await test('deleteTemplate blocks when no authority is available', async () => {
  setRoster([{ id: 'dev', name: 'Dev', role: 'Code', director: 'nora' }]);
  const r = await deleteTemplate('Orchard');
  assert.equal(r.error, 'No director or admin available.');
});

await test('renderTemplatesEditor does nothing outside the board UI', async () => {
  setRoster(FULL_ROSTER);
  context.state = { ui: 'outside' };
  const body = context.document.getElementById('boardTemplatesBody');
  body.innerHTML = 'sentinel';
  await renderTemplatesEditor();
  assert.equal(body.innerHTML, 'sentinel', 'must not touch the container when the board is closed');
  context.state = { ui: 'board' };
});

await test('renderTemplatesEditor renders the no-authority message when no director is present', async () => {
  setRoster([{ id: 'dev', name: 'Dev', role: 'Code', director: 'nora' }]);
  const body = context.document.getElementById('boardTemplatesBody');
  await renderTemplatesEditor();
  assert.ok(body.innerHTML.includes('Only directors and the admin'), body.innerHTML);
});

await test('renderTemplatesEditor lists every template with edit/delete wiring', async () => {
  setRoster([...FULL_ROSTER, { id: 'jack', name: 'Jack', role: 'Orchard', director: 'dev' }]);
  context.apiFetch = async () => ({ ok: true, json: async () => ({
    templates: {
      Orchard: { mission: 'Tend trees', instructions: ['R1', 'R2'], updatedBy: 'faye', updatedAt: 1700000000000 },
      Banker: { mission: 'Count', instructions: ['R3'], updatedBy: 'nora', updatedAt: null },
    },
  }) });
  const body = context.document.getElementById('boardTemplatesBody');
  await renderTemplatesEditor();
  assert.ok(body.innerHTML.includes('Orchard'), 'role name rendered');
  assert.ok(body.innerHTML.includes('Banker'), 'second role rendered');
  assert.ok(body.innerHTML.includes('2 rule'), 'plural rule count');
  assert.ok(body.innerHTML.includes('1 rule'), 'singular rule count');
  assert.ok(body.innerHTML.includes('in use'), 'live-holder label');
  assert.ok(body.innerHTML.includes('no live holders'), 'no-holder label');
  assert.ok(body.innerHTML.includes('&mdash; Faye'), 'updated-by name resolved via roster');
});

await test('renderTemplatesEditor falls back to the updatedBy id when not on the roster', async () => {
  setRoster(FULL_ROSTER);
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: { X: { mission: 'm', updatedBy: 'ghost' } } }) });
  const body = context.document.getElementById('boardTemplatesBody');
  await renderTemplatesEditor();
  assert.ok(body.innerHTML.includes('ghost'), 'unknown author id shown verbatim');
});

await test('renderTemplatesEditor shows the empty state when no templates exist', async () => {
  setRoster(FULL_ROSTER);
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: {} }) });
  const body = context.document.getElementById('boardTemplatesBody');
  await renderTemplatesEditor();
  assert.ok(body.innerHTML.includes('No templates yet'), body.innerHTML);
});

await test('renderTemplatesEditor shows an error on a failed fetch', async () => {
  setRoster(FULL_ROSTER);
  context.apiFetch = async () => { throw new Error('network'); };
  const body = context.document.getElementById('boardTemplatesBody');
  await renderTemplatesEditor();
  assert.ok(body.innerHTML.includes('Could not load templates'), body.innerHTML);
});

await test('buildNewTemplateForm wires the create button to handleTemplateCreate', async () => {
  const box = buildNewTemplateForm({});
  const btn = context.document.getElementById('templateCreateBtn');
  assert.equal(btn.textContent, 'Create template');
  assert.equal(typeof btn._handlers.click[0], 'function', 'click handler registered');
});

await test('handleTemplateCreate validates a missing role and an existing role', async () => {
  setRoster(FULL_ROSTER);
  const alerts = [];
  context.alert = (m) => alerts.push(m);
  context.document.getElementById('templateNewRole').value = '   ';
  context.document.getElementById('templateNewMission').value = 'm';
  await handleTemplateCreate({});
  assert.ok(alerts[0].includes('name'), 'missing name alerts');

  context.document.getElementById('templateNewRole').value = 'Orchard';
  await handleTemplateCreate({ Orchard: { mission: 'x' } });
  assert.ok(alerts[1].includes('already exists'), 'existing role alerts');
  assert.equal(alerts.length, 2);
});

await test('handleTemplateCreate creates and refreshes on success', async () => {
  setRoster(FULL_ROSTER);
  let saved = null, renders = 0;
  context.agentFetch = async (url, author, opts) => { saved = JSON.parse(opts.body); return { ok: true, json: async () => ({ ok: true }) }; };
  context.renderBoard = () => renders++;
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: {} }) });
  context.document.getElementById('templateNewRole').value = 'FreshRole';
  context.document.getElementById('templateNewMission').value = 'Fresh mission';
  context.document.getElementById('templateNewInstructions').value = 'R1\nR2\n';
  await handleTemplateCreate({});
  assert.equal(saved.role, 'FreshRole');
  assert.equal(saved.mission, 'Fresh mission');
  assert.deepEqual(saved.instructions, ['R1', 'R2']);
  assert.equal(renders, 1, 'board refreshed after creation');
});

await test('handleTemplateCreate alerts on a save failure', async () => {
  setRoster(FULL_ROSTER);
  const alerts = [];
  context.alert = (m) => alerts.push(m);
  context.agentFetch = async () => ({ ok: true, json: async () => ({ ok: false, error: 'boom' }) });
  context.document.getElementById('templateNewRole').value = 'FreshRole';
  await handleTemplateCreate({});
  assert.ok(alerts[0].includes('boom'));
});

await test('openTemplateEditor hides the list and loads the template into the editor', async () => {
  setRoster(FULL_ROSTER);
  context.apiFetch = async (url) => ({ ok: true, json: async () => ({ role: 'Orchard', profile: { mission: 'Tend', instructions: ['R1'], notes: ['N1'] } }) });
  const list = context.document.getElementById('boardTemplatesBody');
  const editor = context.document.getElementById('boardTemplateEditor');
  const filler = context.document.getElementById('boardTemplateEditorBody');
  openTemplateEditor('Orchard');
  await new Promise(r => setTimeout(r, 10));
  assert.equal(editor._attr['data-role'], 'Orchard');
  assert.equal(context.document.getElementById('boardModal').style.maxHeight, '90vh');
  assert.ok(filler.innerHTML.includes('Edit template: Orchard'));
  assert.ok(filler.innerHTML.includes('Tend'));
  const saveBtn = context.document.getElementById('tplSaveBtn');
  assert.equal(typeof saveBtn._handlers.click[0], 'function');
  // Cancel returns to the list.
  const cancelBtn = context.document.getElementById('tplCancelBtn');
  cancelBtn._handlers.click[0]();
  assert.ok(editor.classList.contains('hidden'));
});

await test('openTemplateEditor shows an error when the fetch fails', async () => {
  setRoster(FULL_ROSTER);
  context.apiFetch = async () => { throw new Error('network'); };
  const filler = context.document.getElementById('boardTemplateEditorBody');
  openTemplateEditor('Orchard');
  await new Promise(r => setTimeout(r, 10));
  assert.ok(filler.innerHTML.includes('Could not load this template'), filler.innerHTML);
});

await test('handleTemplateSave saves and reports the applied count', async () => {
  setRoster(FULL_ROSTER);
  let renders = 0, reRenderEditor = 0;
  context.agentFetch = async () => ({ ok: true, json: async () => ({ ok: true, appliedTo: ['dev', 'maya'] }) });
  context.renderBoard = () => renders++;
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: {} }) });
  context.setTimeout = (fn) => { fn(); };
  // Render the editor so the tpl* elements exist and are populated.
  openTemplateEditor('Orchard');
  await new Promise(r => setTimeout(r, 10));
  context.document.getElementById('tplMission').value = 'New mission';
  context.document.getElementById('tplInstructions').value = 'A\nB';
  context.document.getElementById('tplNotes').value = 'note';
  const statusEl = context.document.getElementById('tplStatus');
  const btn = context.document.getElementById('tplSaveBtn');
  await handleTemplateSave('Orchard');
  assert.equal(statusEl.textContent, 'Saved & applied to 2 live Orchard(s).');
  assert.equal(btn.textContent, 'Saved');
  assert.equal(renders, 1);
  delete context.setTimeout;
});

await test('handleTemplateSave reports a failure without disabling the button forever', async () => {
  setRoster(FULL_ROSTER);
  context.agentFetch = async () => ({ ok: true, json: async () => ({ ok: false, error: 'no such role' }) });
  const statusEl = context.document.getElementById('tplStatus');
  const btn = context.document.getElementById('tplSaveBtn');
  btn.disabled = true;
  await handleTemplateSave('Orchard');
  assert.equal(btn.disabled, false);
  assert.ok(statusEl.textContent.includes('no such role'));
});

await test('handleTemplateDelete deletes and refreshes on success', async () => {
  setRoster(FULL_ROSTER);
  let deleted = null, renders = 0;
  context.agentFetch = async (url, author, opts) => {
    deleted = { url, method: opts.method };
    return { ok: true, json: async () => ({ ok: true }) };
  };
  context.renderBoard = () => renders++;
  context.apiFetch = async () => ({ ok: true, json: async () => ({ templates: {} }) });
  await handleTemplateDelete('Orchard');
  assert.equal(deleted.method, 'DELETE');
  assert.equal(renders, 1);
});

await test('handleTemplateDelete alerts on failure', async () => {
  setRoster(FULL_ROSTER);
  const alerts = [];
  context.alert = (m) => alerts.push(m);
  context.agentFetch = async () => ({ ok: true, json: async () => ({ ok: false, error: 'gone' }) });
  await handleTemplateDelete('Orchard');
  assert.ok(alerts[0].includes('gone'));
});

await test('bindTemplateListButtons wires edit/delete clicks with confirmation', async () => {
  setRoster(FULL_ROSTER);
  const handlers = { edit: [], del: [] };
  const makeBtn = (cls, role) => ({
    _handlers: {},
    _attr: { role },
    getAttribute: (k) => role,
    addEventListener: (type, fn) => { handlers[cls].push(fn); },
  });
  const container = {
    querySelectorAll: (sel) => sel === '.template-edit' ? [makeBtn('edit', 'Orchard')] : [makeBtn('del', 'Banker')],
  };
  let confirmed = true;
  context.confirm = () => confirmed;
  let opened = null;
  // Stub openTemplateEditor + handleTemplateDelete to observe what the buttons call.
  vm.runInContext('var __origOpen = openTemplateEditor;', context);
  vm.runInContext('openTemplateEditor = (r) => { __obsOpen = r; };', context);
  vm.runInContext('handleTemplateDelete = (r) => { __obsDel = r; };', context);
  bindTemplateListButtons(container);
  handlers.edit[0]();
  assert.equal(vm.runInContext('__obsOpen', context), 'Orchard');
  handlers.del[0]();
  assert.equal(vm.runInContext('__obsDel', context), 'Banker');
  // A declined confirmation does not delete.
  confirmed = false;
  handlers.del[0]();
  assert.equal(vm.runInContext('__obsDel', context), 'Banker', 'unchanged when not confirmed');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);