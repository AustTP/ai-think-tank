// agents.js state + presentation helpers: saveState/loadPersistedState (the
// whole-state autosave with server-ownership stripping), initAgents' fresh-
// seed scatter branch, markContacted, and the speech-bubble helpers
// (saySpeech/_wrapSpeechLines).
//
// Run: node tests/test_agents_flow.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'agents.js'), 'utf8');
vm.runInContext(src, context, { filename: 'agents.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const loadPersistedState = () => vm.runInContext('loadPersistedState', context)();
const saveState = () => vm.runInContext('saveState', context)();
const initAgents = () => vm.runInContext('initAgents', context)();
const markContacted = (...a) => vm.runInContext('markContacted', context)(...a);
const saySpeech = (...a) => vm.runInContext('saySpeech', context)(...a);
const wrapSpeech = (...a) => vm.runInContext('_wrapSpeechLines', context)(...a);

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

function seedGlobals() {
  setGlobal('AGENT_ROSTER', []);
  setGlobal('AGENTS', {});
  setGlobal('REPORTS', []);
  setGlobal('nextReportId', 1);
  setGlobal('WORK_QUEUE', []);
  setGlobal('RESEARCH_TOPICS', []);
  setGlobal('AGENT_KEYS', []);
}

console.log('loadPersistedState (agents.js)');

await test('returns null when the backend is unreachable', async () => {
  setGlobal('apiFetch', async () => { throw new Error('network'); });
  const data = await loadPersistedState();
  assert.equal(data, null);
});

await test('returns the saved payload and restores AGENT_KEYS', async () => {
  setGlobal('AGENT_KEYS', []);
  setGlobal('apiFetch', async () => ({ json: async () => ({ agentKeys: ['k1'], agents: {} }) }));
  const data = await loadPersistedState();
  assert.equal(data.agentKeys.length, 1);
  assert.equal(vm.runInContext('AGENT_KEYS', context)[0], 'k1');
});

console.log('saveState (agents.js)');

await test('posts the full payload when the client owns positions', async () => {
  seedGlobals();
  setGlobal('AGENTS', { dev: { id: 'dev', x: 5, y: 6, dir: 'south', busy: false } });
  setGlobal('AGENT_ROSTER', [{ id: 'dev' }]);
  let body = null;
  setGlobal('apiFetch', async (url, opts) => { body = JSON.parse(opts.body); return {}; });
  await saveState();
  assert.equal(body.agents.dev.x, 5, 'client-owned positions included unchanged');
  assert.equal(body.agents.dev.busy, false);
  assert.equal(body.agentRoster.length, 1);
  assert.ok('workQueue' in body && 'researchTopics' in body);
  assert.ok('lastSkillReviewAt' in body === false, 'server-owned cadence stamp is never saved');
});

await test('strips server-owned spatial fields when SIM_STATUS owns positions', async () => {
  seedGlobals();
  setGlobal('SIM_STATUS', { owner: 'server' });
  setGlobal('AGENTS', { dev: { id: 'dev', x: 5, y: 6, dir: 'south', busy: true, path: [{ x: 1 }], pathIndex: 0, stuckTimer: 1, task: 't' } });
  let body = null;
  setGlobal('apiFetch', async (url, opts) => { body = JSON.parse(opts.body); return {}; });
  await saveState();
  const dev = body.agents.dev;
  assert.equal(dev.x, undefined, 'x is server-owned and stripped');
  assert.equal(dev.dir, undefined, 'dir stripped');
  assert.equal(dev.path, undefined, 'path stripped');
  assert.equal(dev.stuckTimer, undefined, 'stuckTimer stripped');
  assert.equal(dev.busy, true, 'non-spatial state still synced');
  assert.equal(dev.task, 't', 'non-spatial state still synced');
  delete context.SIM_STATUS;
});

await test('swallows save failures without throwing', async () => {
  seedGlobals();
  setGlobal('apiFetch', async () => { throw new Error('down'); });
  await saveState(); // must not throw
  assert.ok(true);
});

console.log('initAgents fresh-seed scatter (agents.js)');

await test('scatters agents stacked at one origin onto free spots', async () => {
  seedGlobals();
  setGlobal('apiFetch', async () => ({
    json: async () => ({
      agentRoster: [{ id: 'a' }, { id: 'b' }],
      agents: {
        a: { id: 'a', x: 0, y: 0, visible: true, offDuty: false, busy: false, mailbox: [], hiredAt: 1, meetingId: null, inRoom: null, task: null, handoff: null, pairWith: null, pairTaskId: null, path: null },
        b: { id: 'b', x: 0, y: 0, visible: true, offDuty: false, busy: false, mailbox: [], hiredAt: 1, meetingId: null, inRoom: null, task: null, handoff: null, pairWith: null, pairTaskId: null, path: null },
      },
      reports: [], nextReportId: 1, workQueue: [], researchTopics: [],
    }),
  }));
  setGlobal('state', { player: { x: 300, y: 200 } });
  const spots = [];
  setGlobal('pickFreeSpot', (avoid) => { const s = { x: 100 + spots.length * 70, y: 100 }; spots.push(s); return s; });
  setGlobal('spawnAgentAtFreeSpot', () => {});
  await initAgents();
  assert.equal(spots.length, 2, 'each stacked agent re-scattered');
  assert.equal(vm.runInContext('AGENT_ROSTER', context).length, 2);
});

console.log('markContacted (agents.js)');

await test('stamps lastContactedAt on the agent', () => {
  setGlobal('AGENTS', { dev: { id: 'dev' } });
  markContacted('dev', 12345);
  assert.equal(vm.runInContext('AGENTS.dev.lastContactedAt', context), 12345);
});

await test('no-ops for an unknown agent', () => {
  markContacted('ghost', 1);
  assert.equal(vm.runInContext('typeof AGENTS.ghost', context), 'undefined');
});

console.log('saySpeech (agents.js)');

await test('short speech is passed through verbatim', () => {
  setGlobal('AGENTS', { dev: { id: 'dev' } });
  saySpeech('dev', '  Hi there.  ');
  const a = vm.runInContext('AGENTS.dev', context);
  assert.equal(a.speechText, 'Hi there.');
  assert.ok(a.speechUntil > Date.now());
});

await test('long speech is truncated at a word boundary with an ellipsis', () => {
  setGlobal('AGENTS', { dev: { id: 'dev' } });
  const words = 'word '.repeat(30); // 150 chars
  saySpeech('dev', words);
  const a = vm.runInContext('AGENTS.dev', context);
  assert.equal(a.speechText.length <= 81, true, `truncated to ${a.speechText.length} chars`);
  assert.ok(a.speechText.endsWith('…'));
  assert.ok(a.speechText.endsWith('word…'), `cut at a word boundary, got "${a.speechText}"`);
});

await test('long speech with no word boundary is cut hard at the limit', () => {
  setGlobal('AGENTS', { dev: { id: 'dev' } });
  saySpeech('dev', 'a'.repeat(120));
  const a = vm.runInContext('AGENTS.dev', context);
  assert.equal(a.speechText.length, 81, '80 chars + ellipsis');
  assert.equal(a.speechText.endsWith('…'), true);
});

await test('whitespace-only text yields an empty speech bubble', () => {
  setGlobal('AGENTS', { dev: { id: 'dev' } });
  saySpeech('dev', '   ');
  assert.equal(vm.runInContext('AGENTS.dev.speechText', context), '');
});

console.log('_wrapSpeechLines (agents.js)');

const mkCtx = (charW) => ({ measureText: (text) => ({ width: text.length * charW }) });

await test('wraps long lines to fit maxWidth', () => {
  // 6px/char, 80px max -> roughly 13 chars per line; "aaa bbb ccc ddd" (15
  // chars) must wrap.
  const lines = [...wrapSpeech(mkCtx(6), 'aaa bbb ccc ddd', 80)];
  assert.equal(lines.length > 1, true);
  assert.ok(lines.every(l => l.length > 0));
  assert.equal(lines.join(' '), 'aaa bbb ccc ddd', 'wrapping loses no words');
});

await test('single line fits untouched', () => {
  const lines = [...wrapSpeech(mkCtx(6), 'hello world', 1000)];
  assert.deepEqual(lines, ['hello world']);
});

await test('an empty string wraps to no lines', () => {
  const lines = [...wrapSpeech(mkCtx(6), '', 100)];
  assert.deepEqual(lines, []);
});

console.log('agent drawing (agents.js)');

const drawAgentAt = (...a) => vm.runInContext('drawAgentAt', context)(...a);
const agentIsDrawn = (...a) => vm.runInContext('agentIsDrawn', context)(...a);
const renderAgents = (...a) => vm.runInContext('renderAgents', context)(...a);
const renderAgentsInRoom = (...a) => vm.runInContext('renderAgentsInRoom', context)(...a);
const drawSpeechBubble = (...a) => vm.runInContext('drawSpeechBubble', context)(...a);

function mkCtx2() {
  return {
    save() {}, restore() {},
    fill() {}, stroke() {}, closePath() {}, beginPath() {},
    moveTo() {}, lineTo() {}, arcTo() {}, fillText() {}, strokeText() {},
    drawImage() {},
    fillStyle: '', strokeStyle: '', lineWidth: 1, textAlign: 'left', font: '',
    measureText: (t) => ({ width: t.length * 6 }),
  };
}

function mkSprite() { return { width: 32, height: 32 }; }
const toScreen = (x, y) => [x * 2, y * 2];

await test('agentIsDrawn requires visible and on-duty', () => {
  assert.equal(agentIsDrawn({ visible: true, offDuty: false }), true);
  assert.equal(agentIsDrawn({ visible: false, offDuty: false }), false);
  assert.equal(agentIsDrawn({ visible: true, offDuty: true }), false);
});

await test('renderAgents draws every on-duty visible agent with its sprite', () => {
  const drawn = [];
  const ctx = mkCtx2();
  const sprites = { south: mkSprite() };
  setGlobal('AGENTS', {
    dev: { id: 'dev', name: 'Dev', color: '#f00', x: 10, y: 20, dir: 'south', visible: true, offDuty: false },
    rest: { id: 'rest', name: 'Rest', color: '#0f0', x: 1, y: 2, dir: 'south', visible: false, offDuty: true },
  });
  const origDraw = vm.runInContext('drawAgentAt', context);
  setGlobal('drawAgentAt', (c, s, z, sp, a, x, y) => { drawn.push(a.id); });
  renderAgents(ctx, toScreen, 2, sprites);
  assert.deepEqual(drawn, ['dev'], 'only the on-duty visible agent drawn');
  setGlobal('drawAgentAt', origDraw);
});

await test('renderAgentsInRoom draws only agents present in that room', () => {
  const drawn = [];
  setGlobal('AGENTS', {
    dev: { id: 'dev', name: 'Dev', color: '#f00', inRoom: 'commandcenter', roomX: 10, roomY: 20, visible: true, offDuty: false },
    idle: { id: 'idle', name: 'Idle', color: '#0f0', inRoom: null, visible: true, offDuty: false },
  });
  const origDraw = vm.runInContext('drawAgentAt', context);
  setGlobal('drawAgentAt', (c, s, z, sp, a, x, y) => { drawn.push({ id: a.id, x, y }); });
  renderAgentsInRoom(mkCtx2(), toScreen, 2, { south: mkSprite() }, 'commandcenter');
  assert.deepEqual(drawn, [{ id: 'dev', x: 10, y: 20 }], 'only the agent actually in the room');
  setGlobal('drawAgentAt', origDraw);
});

await test('drawSpeechBubble draws nothing for an expired or absent bubble', () => {
  const ctx = mkCtx2();
  let saved = 0;
  ctx.save = () => saved++;
  drawSpeechBubble(ctx, toScreen, 1, { speechText: 'hi' }, 0, 0); // no speechUntil -> expired
  assert.equal(saved, 0);
  drawSpeechBubble(ctx, toScreen, 1, { speechText: 'hi', speechUntil: Date.now() - 100 }, 0, 0);
  assert.equal(saved, 0, 'expired bubble not drawn');
});

await test('drawSpeechBubble draws an active bubble with wrapped text', () => {
  const ctx = mkCtx2();
  let saved = 0, filled = 0;
  ctx.save = () => saved++;
  ctx.fill = () => filled++;
  drawSpeechBubble(ctx, toScreen, 1, { speechText: 'hello world how are you', speechUntil: Date.now() + 10000 }, 0, 0);
  assert.equal(saved, 1);
  assert.ok(filled > 0, 'bubble shape + tail filled');
});

await test('drawAgentAt renders sprite, nameplate, and bubble', () => {
  const ctx = mkCtx2();
  let imgs = 0, strokes = 0;
  ctx.drawImage = () => imgs++;
  ctx.strokeText = () => strokes++;
  const a = { name: 'Dev', color: '#f00', dir: 'south', speechText: null, speechUntil: null };
  drawAgentAt(ctx, toScreen, 1, { south: mkSprite() }, a, 5, 5);
  assert.equal(imgs, 1, 'sprite drawn');
  assert.ok(strokes > 0, 'nameplate stroke drawn');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);