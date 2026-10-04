// Tests for the client->server sim bridge (sim_bridge.js).
//
// The bridge polls /api/sim/status so a browser (or a "runs closed" probe)
// can confirm the server-side engine loop is alive -- tick increasing -- even
// with no browser attached. This module is deliberately thin (poll + expose,
// no agent-position writes until Phase 2), so the tests lock in the two
// contracts it does have: a healthy status is parsed and exposed, and a
// failing/non-OK poll leaves the last known status untouched instead of
// clobbering it to something misleading.
//
// Run: node tests/test_sim_bridge.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };

// Stub the two globals the bridge relies on, plus the globals it introduces.
context.apiFetch = async () => ({ ok: true, json: async () => ({ running: true, tick: 7, lastTickEpochS: 99.0, owner: 'client', ageS: 0.5 }) });
context.setInterval = () => {};
const events = [];
context.window = { dispatchEvent: (ev) => events.push(ev.type), addEventListener: () => {} };
context.CustomEvent = class { constructor(type, opts) { this.type = type; this.detail = opts && opts.detail; } };

vm.createContext(context);

const src = fs.readFileSync(path.join(worldDir, 'sim_bridge.js'), 'utf8');
vm.runInContext(src, context, { filename: 'sim_bridge.js' });

const refreshSimStatus = () => vm.runInContext('refreshSimStatus', context)();
const startSimPoll = (...a) => vm.runInContext('startSimPoll', context)(...a);

let passed = 0, failed = 0;
const queued = [];
function test(name, fn) {
  queued.push({ name, fn });
}

// Switchable apiFetch so we can exercise the failure path.
let nextResponse = { ok: true, json: async () => ({ running: true, tick: 7 }) };
context.apiFetch = async () => nextResponse;

test('refreshSimStatus parses a healthy server status and exposes it', async () => {
  await refreshSimStatus();
  const st = vm.runInContext('SIM_STATUS', context);
  assert.equal(st.tick, 7);
  assert.equal(st.running, true);
});

test('a non-OK poll leaves the last known status untouched', async () => {
  await refreshSimStatus(); // prime with the healthy status above
  const before = vm.runInContext('SIM_STATUS', context);
  nextResponse = { ok: false };
  await refreshSimStatus();
  const after = vm.runInContext('SIM_STATUS', context);
  assert.equal(after, before, 'a failed poll must not wipe prior status');
  assert.equal(after.tick, 7);
});

test('a thrown poll is swallowed, not raised', async () => {
  const prev = vm.runInContext('SIM_STATUS', context);
  context.apiFetch = async () => { throw new Error('network down'); };
  await refreshSimStatus(); // must not throw
  assert.equal(vm.runInContext('SIM_STATUS', context), prev);
});

test('startSimPoll does an immediate refresh then schedules', async () => {
  const scheduled = [];
  context.setInterval = (fn, ms) => { scheduled.push(ms); };
  context.apiFetch = async () => nextResponse;
  nextResponse = { ok: true, json: async () => ({ running: true, tick: 12 }) };
  await startSimPoll();
  assert.equal(vm.runInContext('SIM_STATUS', context).tick, 12);
  // status poll (1s, sim_bridge.js SIM_POLL_MS) + position poll (250ms) --
  // see sim_bridge.js startSimPoll.
  assert.equal(scheduled.length, 2);
  assert.ok(scheduled.includes(1000), 'status poll cadence unchanged');
  assert.ok(scheduled.includes(250), 'position poll schedules at 250ms');
});

test('refreshServerPositions stores the authoritative snapshot', async () => {
  context.apiFetch = async () => nextResponse;
  nextResponse = { ok: true, json: async () => ({
    owner: 'server', tick: 41,
    agents: { ada: { x: 130, y: 90, dir: 'east', busy: true, inRoom: 'weatherstation', offDuty: false, pathActive: true, task: 'check weather' } },
  }) };
  const refreshServerPositions = () => vm.runInContext('refreshServerPositions', context)();
  await refreshServerPositions();
  const sp = vm.runInContext('SERVER_POSITIONS', context);
  assert.equal(sp.owner, 'server');
  assert.equal(sp.tick, 41);
  assert.equal(sp.agents.ada.x, 130);
  assert.equal(sp.agents.ada.dir, 'east');
  // A failing position poll must not clobber the last known snapshot.
  nextResponse = { ok: false };
  await refreshServerPositions();
  assert.equal(vm.runInContext('SERVER_POSITIONS', context), sp);
});

test('a thrown position poll is swallowed, not raised', async () => {
  context.apiFetch = async () => { throw new Error('network down'); };
  const sp = vm.runInContext('SERVER_POSITIONS', context);
  const refreshServerPositions = () => vm.runInContext('refreshServerPositions', context)();
  await refreshServerPositions();
  assert.equal(vm.runInContext('SERVER_POSITIONS', context), sp);
});

test('applyServerPositions lerps toward server truth and snaps logical state', () => {
  const applyServerPositions = (...a) => vm.runInContext('applyServerPositions', context)(...a);
  // Prime SERVER_POSITIONS with a server-owned snapshot.
  vm.runInContext('SERVER_POSITIONS = {owner:"server", tick:50, agents:{ada:{x:200, y:100, dir:"south", busy:false, inRoom:"bank", offDuty:true}}}', context);
  // Client agent currently at a stale position, busy/offDuty wrong.
  const state = { ada: { x: 100, y: 100, dir: 'north', busy: true, inRoom: null, offDuty: false } };
  applyServerPositions(state);
  const a = state.ada;
  // Lerp factor 0.45 (sim_bridge.js SERVER_LERP, raised from 0.25):
  // x moves toward 200 by 45% of the gap.
  assert.ok(a.x > 100 && a.x < 200, `x lerped toward 200, got ${a.x}`);
  assert.ok(Math.abs(a.x - (100 + 0.45 * 100)) < 1e-6, 'x applies exponential lerp exactly');
  assert.equal(a.y, 100, 'y already at target, unchanged');
  assert.equal(a.dir, 'south', 'logical dir snaps');
  assert.equal(a.busy, false, 'logical busy snaps');
  assert.equal(a.inRoom, 'bank', 'logical inRoom snaps');
  assert.equal(a.offDuty, true, 'logical offDuty snaps');
});

test('applyServerPositions tolerates missing agents and partial snapshots', () => {
  const applyServerPositions = (...a) => vm.runInContext('applyServerPositions', context)(...a);
  vm.runInContext('SERVER_POSITIONS = {owner:"server", tick:55, agents:{ada:{x:10, y:10, dir:"east"}}}', context);
  const state = { ada: { x: 10, y: 10, dir: 'west' }, ben: { x: 5, y: 5, dir: 'north' } };
  applyServerPositions(state); // ben absent from server snapshot -> untouched
  assert.equal(state.ben.x, 5);
  assert.equal(state.ben.dir, 'north');
  assert.equal(state.ada.dir, 'east');
});

console.log(`\n${passed} passed, ${failed} failed`);
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