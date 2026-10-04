// Tests for the agent-to-agent handoff mechanic (handoffs.js): the
// dependency-triggered walk-over that lets a finishing agent tell whoever
// depends on their work. Exercises attemptHandoff's candidate filtering,
// Jev fallbacks, mail fallback, walk-offset targeting and race re-check;
// arriveAtHandoff's clock-off/talk/notes contract; and cancelHandoff's
// clean-abort shape. Loads the REAL handoffs.js into a vm context with
// stubbed dependencies.
//
// Run: node tests/test_handoffs.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, setTimeout };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'handoffs.js'), 'utf8');
vm.runInContext(src, context, { filename: 'handoffs.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function get(name) { vm.runInContext(`__extract_${name} = ${name};`, context); return context['__extract_' + name]; }

const attemptHandoff = (...a) => vm.runInContext('attemptHandoff', context)(...a);
const arriveAtHandoff = (...a) => vm.runInContext('arriveAtHandoff', context)(...a);
const cancelHandoff = (...a) => vm.runInContext('cancelHandoff', context)(...a);

let passed = 0, failed = 0;
const queued = [];
function test(name, fn) {
  queued.push({ name, fn });
}

function agent(id, overrides = {}) {
  return {
    id, name: id, role: 'dev', model: 'gpt-4o-mini', x: 100, y: 100, dir: 'north',
    visible: true, busy: false, task: null, pairWith: null, offDuty: false, handoff: null,
    path: null, pathIndex: 0, pathTarget: null, stuckTimer: 0, replanCount: 0, respawnedForTask: false,
    profile: { mission: `mission of ${id}`, notes: [] },
    ...overrides,
  };
}

function reset({ evicted = [], mailFallback = false } = {}) {
  const ada = agent('ada', { x: 50, y: 50 });
  const ben = agent('ben', { x: 200, y: 200 });
  const cora = agent('cora', { x: 300, y: 300 });
  for (const id of evicted) {
    // evict one of the roster members to test the "no idle candidates" path
    const victims = { ada, ben, cora };
    victims[id].busy = true;
    victims[id].task = 'some task';
  }
  const AGENTS = { ada, ben, cora };
  const AGENT_ROSTER = [
    { id: 'ada', name: 'ada', role: 'dev', isAdmin: false, profile: { mission: 'm-ada' } },
    { id: 'ben', name: 'ben', role: 'dev', isAdmin: false, profile: { mission: 'm-ben' } },
    { id: 'cora', name: 'cora', role: 'dev', isAdmin: false, profile: { mission: 'm-cora' } },
  ];
  const admin = { id: 'admin', name: 'Admin', role: 'admin', isAdmin: true, profile: { mission: 'm-admin' } };
  if (mailFallback) {
    // every eligible agent busy => mail fallback path
    for (const a of Object.values(AGENTS)) { a.busy = true; a.task = 't'; }
  }
  setGlobal('AGENTS', AGENTS);
  setGlobal('AGENT_ROSTER', [...AGENT_ROSTER, admin]);
  setGlobal('TASK_POOL', [{ id: 't1', dependsOn: 'pressoffice', title: 'The report' }]);
  setGlobal('DEDICATED_PROJECT_ROLES', new Set());
  setGlobal('MODEL_TIERS', { small: { slug: 'small-model' }, 'gpt-4o-mini': { slug: 's4' } });
  setGlobal('AGENT_W', 20);
  setGlobal('AGENT_H', 16);
  vm.runInContext('HANDOFFS = {}; nextHandoffId = 1;', context);

  // findPath: return a path unless (x,y) is exactly the "unreachable" marker.
  setGlobal('findPath', (sx, sy, tx, ty) => {
    if (tx === 9999) return null;
    return [{ x: sx, y: sy }, { x: tx, y: ty }];
  });
  setGlobal('sendMail', () => {});
  setGlobal('saySpeech', () => {});
  setGlobal('showToast', () => {});
  setGlobal('logThinkTankAction', () => {});
  setGlobal('markContacted', () => {});
  setGlobal('agentFetch', async () => ({ ok: true, json: async () => ({ reply: 'nice work' }) }));
  setGlobal('requestJevChoice', async () => ({ choice: 'ben' }));
  // handoffs.js schedules the recipient's reply speech 2200ms after the
  // handoff line lands -- run it immediately so tests don't stall for 2s.
  setGlobal('setTimeout', (fn) => { fn(); });
  return { ada, ben, cora };
}

console.log('handoffs.js agent handoffs');

test('attemptHandoff returns false when nothing depends on the finished room', async () => {
  const { ada } = reset();
  setGlobal('TASK_POOL', [{ id: 't1', dependsOn: 'otherroom' }]);
  assert.equal(await attemptHandoff('ada', 'pressoffice', 'The report'), false);
});

test('attemptHandoff returns false when there are no candidates at all', async () => {
  const { ada, ben, cora } = reset();
  ben.busy = true; ben.task = 't'; cora.busy = true; cora.task = 't';
  setGlobal('AGENT_ROSTER', [
    { id: 'ada', name: 'ada', role: 'dev', isAdmin: false, profile: { mission: 'm' } },
    { id: 'admin', name: 'Admin', role: 'admin', isAdmin: true, profile: { mission: 'm' } },
  ]);
  assert.equal(await attemptHandoff('ada', 'pressoffice', 'The report'), false);
});

test('a Jev non-choice aborts the handoff without side effects', async () => {
  const { ada } = reset();
  setGlobal('requestJevChoice', async () => ({ choice: null }));
  assert.equal(await attemptHandoff('ada', 'pressoffice', 'The report'), false);
  assert.equal(ada.handoff, null);
  assert.equal(ada.offDuty, false);
});

test('with everyone busy, it mails the line and returns false (no walk-over)', async () => {
  const mails = [];
  const { ada } = reset({ mailFallback: true });
  setGlobal('sendMail', (from, to, line) => mails.push({ from, to, line }));
  const result = await attemptHandoff('ada', 'pressoffice', 'The report');
  assert.equal(result, false);
  assert.equal(ada.handoff, null);
  assert.equal(mails.length, 1);
  assert.equal(mails[0].from, 'ada');
  assert.equal(mails[0].to, 'ben');
});

test('a happy path starts a walk to an offset beside the recipient', async () => {
  const { ada, ben } = reset();
  const started = await attemptHandoff('ada', 'pressoffice', 'The report');
  assert.equal(started, true);
  assert.equal(ada.handoff, 'handoff-1');
  assert.ok(Array.isArray(ada.path) && ada.path.length > 0);
  assert.ok(ada.pathTarget, 'pathTarget set to the offset beside ben');
  assert.equal(ben.handoff, null, 'recipient does not get a handoff marker');
  const h = vm.runInContext('HANDOFFS', context)['handoff-1'];
  assert.equal(h.fromId, 'ada');
  assert.equal(h.toId, 'ben');
  assert.equal(h.title, 'The report');
  assert.equal(h.status, 'walking');
});

test('DEDICATED_PROJECT_ROLES members are not handoff candidates', async () => {
  const { ada, ben } = reset();
  setGlobal('DEDICATED_PROJECT_ROLES', new Set(['dev']));
  // Only the two 'dev' roster members are eligible; both are filtered out.
  const started = await attemptHandoff('ada', 'pressoffice', 'The report');
  assert.equal(started, false);
  assert.equal(ada.handoff, null);
});

test('if the only path is empty (already-there), the handoff is aborted', async () => {
  const { ada } = reset();
  setGlobal('findPath', () => []);
  assert.equal(await attemptHandoff('ada', 'pressoffice', 'The report'), false);
  assert.equal(ada.handoff, null);
});

test('the post-Jev race re-check aborts when the recipient vanished into a task', async () => {
  const { ada, ben } = reset();
  // Jev resolves and picks ben, but between the filter and the re-check ben
  // becomes busy (simulating the async gap).
  setGlobal('requestJevChoice', async () => {
    ben.busy = true; ben.task = 'other';
    return { choice: 'ben' };
  });
  assert.equal(await attemptHandoff('ada', 'pressoffice', 'The report'), false);
  assert.equal(ada.handoff, null);
});

test('arriveAtHandoff clocks the walker off, hides her, and runs the two-way exchange', async () => {
  const mails = [], speeches = [], actions = [], notes = [];
  const { ada, ben } = reset();
  setGlobal('saySpeech', (id, text) => speeches.push({ id, text }));
  setGlobal('logThinkTankAction', (id, action, details) => actions.push({ id, action, details }));
  await attemptHandoff('ada', 'pressoffice', 'The report');

  const before = get('HANDOFFS');
  const h = before['handoff-1'];
  h.status = 'walking';
  ada.handoff = 'handoff-1';

  arriveAtHandoff('ada');
  // Synchronous effects:
  assert.equal(ada.handoff, null);
  assert.equal(ada.path, null);
  assert.equal(ada.offDuty, true);
  assert.equal(ada.visible, false);
  assert.equal(ada.dir, 'south');
  assert.equal(h.status, 'talking');

  // Wait for the async exchange (two await points + a setTimeout).
  await new Promise(r => setTimeout(r, 30));
  assert.equal(h.status, 'done');
  assert.equal(speeches.length, 1, 'ben (still visible) gets a real speech bubble');
  assert.equal(speeches[0].id, 'ben');
  assert.ok(speeches[0].text.length > 0);
  assert.equal(actions.length, 1);
  assert.equal(actions[0].action, 'handoff');
  assert.equal(actions[0].details.to, 'ben');
  assert.ok(actions[0].details.line && actions[0].details.reply);
});

test('arriveAtHandoff works even if the recipient has wandered off', async () => {
  const { ada, ben } = reset();
  await attemptHandoff('ada', 'pressoffice', 'The report');
  const h = get('HANDOFFS')['handoff-1'];
  ada.handoff = 'handoff-1';
  delete vm.runInContext('AGENTS', context).ben; // recipient gone
  arriveAtHandoff('ada');
  assert.equal(ada.offDuty, true);
  // Recipient missing -> arriveAtHandoff returns before the talk status;
  // the walker still clocks off (finishTask deferred that to this point).
  assert.equal(h.status, 'walking');
});

test('a failed model call still completes the exchange with a fallback line', async () => {
  const speeches = [];
  const { ada, ben } = reset();
  setGlobal('agentFetch', async () => { throw new Error('network down'); });
  setGlobal('saySpeech', (id, text) => speeches.push({ id, text }));
  await attemptHandoff('ada', 'pressoffice', 'The report');
  const h = get('HANDOFFS')['handoff-1'];
  ada.handoff = 'handoff-1';
  arriveAtHandoff('ada');
  await new Promise(r => setTimeout(r, 30));
  assert.equal(h.status, 'done');
  assert.ok(speeches.some(s => s.text.includes('acknowledges')), 'fallback ack reply used');
});

test('cancelHandoff aborts cleanly and clocks the agent off', () => {
  const { ada } = reset();
  vm.runInContext('HANDOFFS["handoff-9"] = { id: "handoff-9", fromId: "ada", toId: "ben", status: "walking" };', context);
  ada.handoff = 'handoff-9';
  cancelHandoff('ada');
  assert.equal(get('HANDOFFS')['handoff-9'].status, 'cancelled');
  assert.equal(ada.handoff, null);
  assert.equal(ada.path, null);
  assert.equal(ada.offDuty, true);
  assert.equal(ada.visible, false);
});

test('cancelHandoff tolerates a missing handoff record', () => {
  const { ada } = reset();
  ada.handoff = 'handoff-missing';
  cancelHandoff('ada'); // must not throw
  assert.equal(ada.handoff, null);
  assert.equal(ada.offDuty, true);
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
