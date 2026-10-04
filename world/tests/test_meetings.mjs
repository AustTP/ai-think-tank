// Tests for the Town Hall meeting manager (meetings.js): concurrent
// meetings, busy-drop semantics, the respawn-reposition contract of
// endMeeting, the order-independent DM key, and postMessage's contact
// signals (the one live input to morale.js's neglect penalty). Loads the
// REAL meetings.js into a vm context with the handful of globals it reads.
//
// Run: node tests/test_meetings.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'meetings.js'), 'utf8');
vm.runInContext(src, context, { filename: 'meetings.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function get(name) { vm.runInContext(`__extract_${name} = ${name};`, context); return context['__extract_' + name]; }

const startMeeting = (...a) => vm.runInContext('startMeeting', context)(...a);
const endMeeting = (...a) => vm.runInContext('endMeeting', context)(...a);
const isAgentBusy = (...a) => vm.runInContext('isAgentBusy', context)(...a);
const dmKey = (...a) => vm.runInContext('dmKey', context)(...a);
const postMessage = (...a) => vm.runInContext('postMessage', context)(...a);
const loadMeetingPresets = () => vm.runInContext('loadMeetingPresets', context)();

let passed = 0, failed = 0;
const queued = [];
function test(name, fn) {
  queued.push({ name, fn });
}

function agent(id, overrides = {}) {
  return { id, x: 100, y: 100, busy: false, visible: true, meetingId: null, ...overrides };
}
function reset(agents, locationKind = 'outside') {
  setGlobal('AGENTS', agents);
  setGlobal('state', { location: { kind: locationKind } });
  vm.runInContext('MEETINGS = {}; nextMeetingId = 1;', context);
  const taken = [];
  setGlobal('pickFreeSpot', (occupied) => {
    const spot = { x: 500 + taken.length * 10, y: 500 };
    taken.push(spot);
    return spot;
  });
  setGlobal('markContacted', () => {});
}

console.log('meetings.js meeting manager');

test('isAgentBusy checks the player via state.location and agents via their busy flag', () => {
  reset({ ada: agent('ada', { busy: true }), ben: agent('ben') }, 'meeting');
  assert.equal(isAgentBusy('player'), true);
  setGlobal('state', { location: { kind: 'outside' } });
  assert.equal(isAgentBusy('player'), false);
  assert.equal(isAgentBusy('ada'), true);
  assert.equal(isAgentBusy('ben'), false);
  assert.equal(isAgentBusy('nobody'), false);
});

test('startMeeting returns null when the initiator is already busy', () => {
  reset({ ada: agent('ada', { busy: true }), ben: agent('ben') });
  assert.equal(startMeeting('ada', ['ben']), null);
});

test('startMeeting silently drops busy participants', () => {
  reset({ ada: agent('ada'), ben: agent('ben', { busy: true }), cora: agent('cora') });
  const m = startMeeting('ada', ['ben', 'cora', 'ada']);
  assert.ok(m);
  assert.deepEqual([...m.participants], ['ada', 'cora']);
  assert.equal(m.initiator, 'ada');
  assert.ok(m.startedAt);
  assert.equal(m.groupLog.length, 0);
  assert.equal(Object.keys(m.dmLogs).length, 0);
});

test('startMeeting returns null when no one eligible is left', () => {
  reset({ ada: agent('ada'), ben: agent('ben', { busy: true }) });
  assert.equal(startMeeting('ada', ['ben']), null);
});

test('startMeeting marks all agent participants busy, hidden, and meeting-bound', () => {
  reset({ ada: agent('ada'), ben: agent('ben'), cora: agent('cora') });
  const m = startMeeting('ada', ['ben', 'cora']);
  for (const pid of ['ada', 'ben', 'cora']) {
    const a = vm.runInContext('AGENTS', context)[pid];
    assert.equal(a.busy, true);
    assert.equal(a.visible, false);
    assert.equal(a.meetingId, m.id);
  }
});

test('independent meetings can run concurrently', () => {
  reset({ ada: agent('ada'), ben: agent('ben'), cora: agent('cora'), dev: agent('dev') });
  const m1 = startMeeting('ada', ['ben']);
  const m2 = startMeeting('cora', ['dev']);
  assert.ok(m1 && m2);
  assert.notEqual(m1.id, m2.id);
  assert.equal(vm.runInContext('Object.keys(MEETINGS).length', context), 2);
});

test('endMeeting returns [] for an unknown meeting', () => {
  reset({ ada: agent('ada') });
  assert.deepEqual([...endMeeting('nope')], []);
});

test('endMeeting repositions participants, clears busy, and returns occupied points', () => {
  reset({ ada: agent('ada', { x: 10, y: 10 }), ben: agent('ben', { x: 20, y: 20 }) });
  startMeeting('ada', ['ben']);
  const occupied = endMeeting(vm.runInContext('Object.keys(MEETINGS)[0]', context));
  const AGENTS = vm.runInContext('AGENTS', context);
  for (const pid of ['ada', 'ben']) {
    assert.equal(AGENTS[pid].busy, false);
    assert.equal(AGENTS[pid].visible, true);
    assert.equal(AGENTS[pid].meetingId, null);
    assert.notEqual(AGENTS[pid].x, pid === 'ada' ? 10 : 20, 'must not respawn onto pre-call spot');
  }
  // Returned occupied list includes both pre-call spots plus both fresh
  // respawn spots (occupied begins empty -- no visible agents outside the
  // meeting -- then gains pre-call spots and respawn spots).
  assert.equal(occupied.length, 4);
  assert.equal(Object.keys(vm.runInContext('MEETINGS', context)).length, 0);
});

test('dmKey is order-independent', () => {
  assert.equal(dmKey('player', 'ada'), dmKey('ada', 'player'));
  assert.equal(dmKey('player', 'ada'), 'ada|player');
});

test('postMessage ignores empty text and unknown meetings', () => {
  reset({ ada: agent('ada'), ben: agent('ben') });
  const m = startMeeting('ada', ['ben']);
  postMessage(m.id, 'ada', '   ');
  postMessage('nope', 'ada', 'hi');
  assert.equal(m.groupLog.length, 0);
});

test('a group post lands in groupLog and contacts every other participant', () => {
  reset({ ada: agent('ada'), ben: agent('ben'), cora: agent('cora') });
  const contacts = [];
  setGlobal('markContacted', (id, ts) => contacts.push(id));
  const m = startMeeting('ada', ['ben', 'cora']);
  postMessage(m.id, 'ada', '  hello world  ');
  assert.equal(m.groupLog.length, 1);
  assert.equal(m.groupLog[0].fromId, 'ada');
  assert.equal(m.groupLog[0].text, 'hello world');
  assert.ok(m.groupLog[0].ts);
  assert.deepEqual(contacts.sort(), ['ben', 'cora']);
});

test('a DM lands in the order-independent dmLogs thread and contacts only its recipient', () => {
  reset({ ada: agent('ada'), ben: agent('ben'), cora: agent('cora') });
  const contacts = [];
  setGlobal('markContacted', (id) => contacts.push(id));
  const m = startMeeting('ada', ['ben', 'cora']);
  postMessage(m.id, 'ben', 'psst', 'ada');
  assert.equal(m.groupLog.length, 0);
  assert.equal(m.dmLogs['ada|ben'].length, 1);
  assert.equal(m.dmLogs['ada|ben'][0].fromId, 'ben');
  assert.equal(m.dmLogs['ada|ben'][0].toId, 'ada');
  assert.equal(m.dmLogs['ada|ben'][0].text, 'psst');
  assert.deepEqual(contacts, ['ada']);
});

test('loadMeetingPresets reads the presets file through fetch', async () => {
  context.fetch = async () => ({ json: async () => ({ townhall: ['ada', 'ben'] }) });
  await loadMeetingPresets();
  assert.deepEqual(vm.runInContext('MEETING_PRESETS', context).townhall, ['ada', 'ben']);
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
