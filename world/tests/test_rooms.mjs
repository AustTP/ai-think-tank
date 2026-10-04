// Tests for the interior-room definitions (rooms.js): the collision rects,
// the room registry, and the door-trigger loader that scales JSON-authored
// outdoor triggers up by SCALE. These tests load the REAL rooms.js into a
// vm context -- previously this module was never loaded by any test (0%
// coverage), which is exactly the class of "data file with zero
// exercises" that eventually drifts out of sync with the art it describes.
//
// Run: node tests/test_rooms.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
context.SCALE = 2;
vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'rooms.js'), 'utf8');
vm.runInContext(src, context, { filename: 'rooms.js' });

let passed = 0, failed = 0;
const queued = [];
function test(name, fn) {
  queued.push({ name, fn });
}

// Async tests run one at a time, in definition order -- they share mutable
// vm-context globals (here context.fetch and ROOM_DOOR_TRIGGERS), so running
// them concurrently would race (one test's payload clobbering another's).
// Sequential + awaited also guarantees the post-`await` assertions actually
// execute before the process exits, which the old fire-and-forget test()
// helper silently skipped. The runner lives at the bottom of the file, after
// every test() call has registered.

function get(name) { vm.runInContext(`__extract_${name} = ${name};`, context); return context['__extract_' + name]; }
const ROOM_NATIVE_W = get('ROOM_NATIVE_W');
const ROOM_NATIVE_H = get('ROOM_NATIVE_H');
const ROOM_EXIT_TRIGGER = get('ROOM_EXIT_TRIGGER');
const ROOM_COLLISIONS = get('ROOM_COLLISIONS');
const ROOMS = get('ROOMS');
const loadDoorTriggers = (...a) => vm.runInContext('loadDoorTriggers', context)(...a);

console.log('rooms.js room definitions and door triggers');

test('native room size and exit trigger conventions', () => {
  assert.equal(ROOM_NATIVE_W, 632);
  assert.equal(ROOM_NATIVE_H, 424);
  // Open-bottom-edge convention: a centered strip along the bottom.
  assert.equal(ROOM_EXIT_TRIGGER.x, 632 / 2 - 20);
  assert.equal(ROOM_EXIT_TRIGGER.y + ROOM_EXIT_TRIGGER.h, 424);
  assert.equal(ROOM_EXIT_TRIGGER.w, 40);
  assert.equal(ROOM_EXIT_TRIGGER.h, 24);
});

test('every collision set has matching room registrations', () => {
  const collisionKeys = Object.keys(ROOM_COLLISIONS);
  const roomCollisions = new Set(Object.values(ROOMS).map(r => r.collision));
  assert.deepEqual([...roomCollisions].sort(), collisionKeys.sort());
});

test('workstations collision set has six desks in two rows', () => {
  const ws = ROOM_COLLISIONS.workstations;
  assert.equal(ws.length, 6);
  // Three desks per row, same y per row.
  assert.equal(new Set(ws.slice(0, 3).map(d => d.y)).size, 1);
  assert.equal(new Set(ws.slice(3, 6).map(d => d.y)).size, 1);
  // X positions are distinct per row.
  assert.equal(new Set(ws.slice(0, 3).map(d => d.x)).size, 3);
});

test('bank collision set is only the counters (walk-through dividers)', () => {
  assert.equal(ROOM_COLLISIONS.bank.length, 3);
  for (const c of ROOM_COLLISIONS.bank) assert.equal(c.h, 65);
});

test('postoffice is one wide counter band, hangout is a top wall band', () => {
  assert.equal(ROOM_COLLISIONS.postoffice.length, 1);
  assert.equal(ROOM_COLLISIONS.postoffice[0].w, 620);
  assert.equal(ROOM_COLLISIONS.hangout[0].h, 136);
  assert.equal(ROOM_COLLISIONS.hangout[0].x, 0);
  assert.equal(ROOM_COLLISIONS.hangout[0].y, 0);
});

test('rooms that share art share the same collision key', () => {
  // Press Office, Media, Weather Station, Observatory + Command Center all
  // use the shared workstations image -> ONE collision definition.
  for (const name of ['pressoffice', 'media', 'weatherstation', 'observatory', 'commandcenter']) {
    assert.equal(ROOMS[name].collision, 'workstations');
  }
  assert.equal(ROOMS.pressoffice.image, ROOMS.weatherstation.image);
  assert.equal(ROOMS.commandcenter.image, ROOMS.weatherstation.image);
});

test('loadDoorTriggers scales native coords by SCALE and aliases the hangout to townhall', async () => {
  context.fetch = async () => ({
    json: async () => ({
      townhall: { x: 100, y: 50, w: 40, h: 30 },
      bank: { x: 10, y: 20, w: 12, h: 14 },
    }),
  });
  await loadDoorTriggers();
  const triggers = get('ROOM_DOOR_TRIGGERS');
  assert.equal(triggers.townhall.x, 200);
  assert.equal(triggers.townhall.y, 100);
  assert.equal(triggers.townhall.w, 80);
  assert.equal(triggers.townhall.h, 60);
  assert.equal(triggers.bank.x, 20);
  assert.equal(triggers.bank.y, 40);
  // Hangout shares the Town Hall front door.
  assert.deepEqual(triggers.hangout, triggers.townhall);
});

test('loadDoorTriggers tolerates a payload with no townhall (no hangout alias)', async () => {
  context.fetch = async () => ({ json: async () => ({ bank: { x: 1, y: 2, w: 3, h: 4 } }) });
  await loadDoorTriggers();
  const triggers = get('ROOM_DOOR_TRIGGERS');
  assert.equal(triggers.bank.x, 2);
  assert.equal(triggers.hangout, undefined);
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
