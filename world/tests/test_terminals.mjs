// Tests for room interactable zones (terminals.js): the walk-in strips
// derived from ROOM_COLLISIONS, one per workstation desk, mailbox wall,
// bank teller, and library shelf. Loads the REAL rooms.js (for
// ROOM_COLLISIONS) and then terminals.js into the same vm context.
//
// Run: node tests/test_terminals.mjs
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

for (const file of ['rooms.js', 'terminals.js']) {
  const src = fs.readFileSync(path.join(worldDir, file), 'utf8');
  vm.runInContext(src, context, { filename: file });
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

function get(name) { vm.runInContext(`__extract_${name} = ${name};`, context); return context['__extract_' + name]; }
const ROOM_INTERACTABLES = get('ROOM_INTERACTABLES');
const ROOM_COLLISIONS = get('ROOM_COLLISIONS');
const TERMINAL_ZONE_DEPTH = 15;

console.log('terminals.js room interactable zones');

test('every interactable set matches a collision set (hangout has none by design)', () => {
  assert.deepEqual(Object.keys(ROOM_INTERACTABLES).sort(), ['bank', 'library', 'postoffice', 'workstations']);
  // hangout is a furniture-less gathering room -- no interactables.
  assert.equal(ROOM_INTERACTABLES.hangout, undefined);
});

test('workstations maps each desk to a terminal zone directly south of it', () => {
  const terminals = ROOM_INTERACTABLES.workstations;
  assert.equal(terminals.length, ROOM_COLLISIONS.workstations.length);
  for (let i = 0; i < terminals.length; i++) {
    const desk = ROOM_COLLISIONS.workstations[i];
    assert.equal(terminals[i].type, 'terminal');
    const z = terminals[i].zone;
    assert.equal(z.x, desk.x);
    assert.equal(z.y, desk.y + desk.h);
    assert.equal(z.w, desk.w);
    assert.equal(z.h, TERMINAL_ZONE_DEPTH);
  }
});

test('bank tellers carry their index and a zone under each counter', () => {
  const tellers = ROOM_INTERACTABLES.bank;
  assert.equal(tellers.length, 3);
  tellers.forEach((t, i) => {
    assert.equal(t.type, 'teller');
    assert.equal(t.tellerIndex, i);
    assert.equal(t.zone.y, ROOM_COLLISIONS.bank[i].y + ROOM_COLLISIONS.bank[i].h);
    assert.equal(t.zone.h, TERMINAL_ZONE_DEPTH);
  });
});

test('postoffice mailbox strip fronts the counter wall', () => {
  const mb = ROOM_INTERACTABLES.postoffice[0];
  assert.equal(mb.type, 'mailbox');
  assert.equal(mb.zone.y, ROOM_COLLISIONS.postoffice[0].y + ROOM_COLLISIONS.postoffice[0].h);
  assert.equal(mb.zone.w, ROOM_COLLISIONS.postoffice[0].w);
  assert.equal(mb.zone.h, TERMINAL_ZONE_DEPTH);
});

test('library shelves each get a walk-in strip', () => {
  const shelves = ROOM_INTERACTABLES.library;
  assert.equal(shelves.length, ROOM_COLLISIONS.library.length);
  assert.ok(shelves.every(s => s.type === 'bookshelf'));
});

test('no interactable zones overlap each other within a room', () => {
  for (const zones of Object.values(ROOM_INTERACTABLES)) {
    const all = zones.map(x => x.zone);
    for (let i = 0; i < all.length; i++) {
      for (let j = i + 1; j < all.length; j++) {
        const a = all[i], b = all[j];
        const overlap = a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
        assert.equal(overlap, false, `zone ${i} overlaps zone ${j}`);
      }
    }
  }
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
