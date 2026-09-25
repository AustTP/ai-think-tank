// Real regression test for room-capacity overflow (tasks.js), added
// after a direct ask: "if the work room is full, they can use the
// observatory to do the same work... I don't want to limit if we have
// tasks ongoing." Work Room (pressoffice) and Research Center
// (observatory) share the exact same six-desk 'workstations' layout
// (ROOM_COLLISIONS/ROOM_INTERACTABLES, rooms.js/terminals.js), so
// overflow from one into the other is physically sensible.
//
// The contract these tests lock in: pressoffice only redirects to
// observatory once it's genuinely full, never redirects if observatory
// is ALSO full (stays put rather than pretending a second full room is
// the answer), and every other room is left alone entirely -- this is
// one specific, named pair, not a general room-load-balancer.
//
// Run: node tests/test_room_overflow.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, Promise };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'tasks.js'), 'utf8'), context, { filename: 'tasks.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
const roomDeskCapacity = (...a) => vm.runInContext('_roomDeskCapacity', context)(...a);
const roomOccupancy = (...a) => vm.runInContext('_roomOccupancy', context)(...a);
const resolveRoomWithOverflow = (...a) => vm.runInContext('_resolveRoomWithOverflow', context)(...a);

setGlobal('ROOMS', {
  pressoffice: { collision: 'workstations' },
  observatory: { collision: 'workstations' },
  library: { collision: 'library' },
  bank: { collision: 'bank_but_undeclared' }, // deliberately not in ROOM_INTERACTABLES below
});
setGlobal('ROOM_INTERACTABLES', {
  workstations: [0, 1, 2, 3, 4, 5].map(() => ({ type: 'terminal' })), // 6 real desks
  library: [0, 1, 2].map(() => ({ type: 'terminal' })), // 3, arbitrary but real
});

function makeAgents(busyInRoom) {
  // busyInRoom: array of room names, one busy agent per entry, all
  // otherwise idle/irrelevant fields omitted since occupancy only checks
  // busy + inRoom.
  const agents = {};
  busyInRoom.forEach((room, i) => { agents['a' + i] = { busy: true, inRoom: room }; });
  agents['idle-somewhere'] = { busy: false, inRoom: 'pressoffice' }; // must NOT count -- not busy
  return agents;
}

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

console.log('room-capacity overflow (tasks.js)');

test('_roomDeskCapacity reads the real desk count for a workstations-layout room', () => {
  assert.equal(roomDeskCapacity('pressoffice'), 6);
  assert.equal(roomDeskCapacity('observatory'), 6);
});

test('_roomDeskCapacity reads a different room\'s own real desk count, not a shared constant', () => {
  assert.equal(roomDeskCapacity('library'), 3);
});

test('_roomDeskCapacity does not invent a limit for a room with no known desk layout', () => {
  assert.equal(roomDeskCapacity('bank'), Infinity);
});

test('_roomOccupancy only counts busy agents actually in that room', () => {
  setGlobal('AGENTS', makeAgents(['pressoffice', 'pressoffice', 'observatory']));
  assert.equal(roomOccupancy('pressoffice'), 2);
  assert.equal(roomOccupancy('observatory'), 1);
  assert.equal(roomOccupancy('library'), 0);
});

test('an under-capacity pressoffice is left alone', () => {
  setGlobal('AGENTS', makeAgents(['pressoffice', 'pressoffice']));
  assert.equal(resolveRoomWithOverflow('pressoffice'), 'pressoffice');
});

test('a full pressoffice overflows to observatory when observatory has room', () => {
  setGlobal('AGENTS', makeAgents(Array(6).fill('pressoffice')));
  assert.equal(resolveRoomWithOverflow('pressoffice'), 'observatory');
});

test('a full pressoffice stays put if observatory is ALSO full', () => {
  setGlobal('AGENTS', makeAgents([...Array(6).fill('pressoffice'), ...Array(6).fill('observatory')]));
  assert.equal(resolveRoomWithOverflow('pressoffice'), 'pressoffice');
});

test('a room with no configured overflow target is never redirected, however full', () => {
  setGlobal('AGENTS', makeAgents(Array(3).fill('library')));
  assert.equal(resolveRoomWithOverflow('library'), 'library');
});

test('resolving an already-resolved room (observatory) is a safe no-op even when full', () => {
  // Guards the double-resolution case: assignPairTask resolves once,
  // then assignTask resolves again internally -- observatory has no
  // overflow target of its own, so this must never redirect further.
  setGlobal('AGENTS', makeAgents(Array(6).fill('observatory')));
  assert.equal(resolveRoomWithOverflow('observatory'), 'observatory');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
