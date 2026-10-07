import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const start = html.indexOf('async function handleBigTaskDelegate()');
const end = html.indexOf('// Player-triggered publish.', start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);

function setup(result, { ok = true, failure, goal = 'Build a tool' } = {}) {
  const elements = {
    bigTaskInput: { value: goal, focus() { this.focused = true; } },
    bigTaskStatus: { textContent: '' },
    bigTaskBtn: { disabled: false },
  };
  let calls = 0;
  const context = vm.createContext({
    document: { getElementById: id => elements[id] },
    SIM_STATUS: { owner: 'server' },
    AGENTS: { ada: { name: 'Ada' } },
    apiFetch: async (url, options) => {
      calls++;
      assert.equal(url, '/api/intent/assign-big-task');
      assert.equal(JSON.parse(options.body).goal, goal.trim());
      if (failure) throw new Error(failure);
      return { ok, json: async () => result };
    },
  });
  vm.runInContext(source, context);
  return { elements, run: () => context.handleBigTaskDelegate(), calls: () => calls };
}

for (const kind of ['backlogged', 'newTeam', 'staffed']) {
  test(`successful ${kind} request is acknowledged and clears the input`, async () => {
    const ui = setup({ ok: true, [kind]: true, admin: 'ada', note: 'Request accepted.' });
    await ui.run();
    assert.equal(ui.elements.bigTaskStatus.textContent, 'Request accepted.');
    assert.equal(ui.elements.bigTaskInput.value, '');
    assert.equal(ui.elements.bigTaskBtn.disabled, false);
  });
}

test('queued subtasks still display their titles', async () => {
  const ui = setup({ admin: 'ada', subtasks: [{ title: 'Build it', room: 'workroom' }] });
  await ui.run();
  assert.match(ui.elements.bigTaskStatus.textContent, /Build it/);
});

test('server rejection preserves the task for retry', async () => {
  const ui = setup({ error: 'No free team.' }, { ok: false });
  await ui.run();
  assert.equal(ui.elements.bigTaskStatus.textContent, 'No free team.');
  assert.equal(ui.elements.bigTaskInput.value, 'Build a tool');
  assert.equal(ui.elements.bigTaskBtn.disabled, false);
});

test('network failure shows its reason and re-enables delegation', async () => {
  const ui = setup(null, { failure: 'Failed to fetch' });
  await ui.run();
  assert.match(ui.elements.bigTaskStatus.textContent, /Failed to fetch/);
  assert.equal(ui.elements.bigTaskBtn.disabled, false);
  assert.equal(ui.elements.bigTaskInput.value, 'Build a tool');
});

test('empty task prompts for input without sending a request', async () => {
  const ui = setup(null, { goal: '  ' });
  await ui.run();
  assert.equal(ui.calls(), 0);
  assert.equal(ui.elements.bigTaskInput.focused, true);
  assert.match(ui.elements.bigTaskStatus.textContent, /Enter a task/);
});
