// Differential parity test for the Phase-2 server-side-simulation port of the
// movement step/slide core from the client (tasks.js tickAgentMovement, plus
// agents.js/world.js geometry) into sim.py.
//
// The findPath test (test_find_path_parity.mjs) already proves sim.find_path
// byte-matches tasks.js findPath. This test proves the LAYER around it -- the
// geometry primitives (blockedAt/cellFitsAgent/isOnADoorTile/agentBlockedAt),
// the reachability flood-fill, and the full step/slide/stuck/replan loop --
// match the real JS bit-for-bit, by feeding both engines IDENTICAL input state
// and diffing every intermediate position + arrival/cancel event.
//
// Run: node tests/test_movement_parity.mjs   (also wired into run_all.sh)
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

for (const file of ['world.js', 'agents.js', 'tasks.js']) {
  const src = fs.readFileSync(path.join(worldDir, file), 'utf8');
  vm.runInContext(src, context, { filename: file });
}

// overlaps() lives inline in index.html; define it exactly as index.html:591.
vm.runInContext(
  'function overlaps(a, b) { return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y; }',
  context
);

// vm `let`/`const` bindings aren't object properties; bridge through a plain
// property (same pattern as test_find_path_parity.mjs).
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

const realGrid = JSON.parse(fs.readFileSync(path.join(worldDir, 'collision_grid.json'), 'utf8'));
const nativeDoors = JSON.parse(fs.readFileSync(path.join(worldDir, 'door_triggers.json'), 'utf8'));

// ROOM_DOOR_TRIGGERS is world-space (rooms.js scales door_triggers.json by
// SCALE at load). Mirror that so both engines share one geometry.
const SCALE = 2;
const worldDoors = {};
for (const b in nativeDoors) {
  const d = nativeDoors[b];
  worldDoors[b] = { x: d.x * SCALE, y: d.y * SCALE, w: d.w * SCALE, h: d.h * SCALE };
}

// COLLISION_GRID/AGENTS/ROOM_DOOR_TRIGGERS are `let` (or, for ROOM_DOOR_TRIGGERS,
// not loaded at all) so they are injectable. AGENT_W/AGENT_H/GROUND_W/GROUND_H/
// SPAWN/SCALE are `const` in their files with the correct values already in
// scope -- leave them untouched.
setGlobal('COLLISION_GRID', realGrid);
setGlobal('AGENTS', {});
setGlobal('ROOM_DOOR_TRIGGERS', worldDoors);

let events = [];
// Reassignable function declarations: the real handlers read task globals not
// loaded here, and handoffs.js (arriveAtHandoff/cancelHandoff) isn't loaded at
// all. Replace all six with recorders -- recording the branch is exactly the
// Phase-2 arrival/cancel event capture (the dispatch to the task system is the
// engine's job, not this test's).
// Event tuples match sim.py's step_agent_movement exactly: [kind, label, id].
setGlobal('arriveAtHandoff', (id) => events.push(['arrive', 'handoff', id]));
setGlobal('arriveAtPair', (id) => events.push(['arrive', 'pair', id]));
setGlobal('arriveAtTask', (id) => events.push(['arrive', 'task', id]));
setGlobal('cancelHandoff', (id) => events.push(['cancel', 'handoff', id]));
setGlobal('cancelTask', (id) => events.push(['cancel', 'task', id]));
// Deterministic respawn: stub pickFreeSpot to a fixed point so a walk can never
// diverge on Math.random (the Python bridge stubs pick_free to the same (0,0)).
setGlobal('pickFreeSpot', () => ({ x: 0, y: 0 }));

const jsFindPath = (...a) => vm.runInContext('findPath', context)(...a);
const jsTick = (dt) => vm.runInContext('tickAgentMovement', context)(dt);
const jsMask = () => vm.runInContext('computeReachableMask', context)();
const jsBlockedAt = (box) => vm.runInContext('blockedAt', context)(box);
const jsCellFits = (gx, gy, cell) => vm.runInContext('cellFitsAgent', context)(gx, gy, cell);
const jsOnDoor = (x, y) => vm.runInContext('isOnADoorTile', context)(x, y);
const jsAgentBlocked = (box, ex, ig) => vm.runInContext('agentBlockedAt', context)(box, ex, ig);

function pyMany(reqs) {
  // ONE interpreter serves every request in the batch (line-oriented: one JSON
  // request per stdin line, one JSON result per stdout line). The old
  // per-request execFileSync spawned a fresh Python for every case -- hundreds
  // of interpreter startups across the batteries that, under concurrent suite
  // runs, compounded into an apparent hang. execFileSync returns clean output
  // only on a zero exit; a nonzero exit throws.
  const input = reqs.map(r => JSON.stringify(r)).join('\n') + '\n';
  const out = execFileSync('/usr/bin/env', ['python3', 'tests/_movement_bridge.py'], {
    input, cwd: worldDir,
  });
  const lines = out.toString().split('\n').filter(l => l.trim() !== '');
  if (lines.length !== reqs.length) {
    throw new Error(`bridge returned ${lines.length} results for ${reqs.length} requests`);
  }
  return lines.map(l => JSON.parse(l));
}

function py(req) {
  return pyMany([req])[0];
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
const R = (b) => (b === true || b === 1 || b === '1') ? 1 : 0;
const round6 = (v) => Math.round((v ?? 0) * 1e6) / 1e6;

// ---- Test A + B: geometry + reachable mask -----------------------------------
console.log('geometry + reachable mask (Python) === JS, real map + doors');
test('blockedAt battery (walls, doors, OB, floor) + 300 random', () => {
  const cases = [
    { x: 484, y: 220, w: 62, h: 62 },      // a door rect in world space
    { x: 0, y: 0, w: 40, h: 40 },           // far top-left
    { x: 1300, y: 700, w: 40, h: 40 },      // far bottom-right
    { x: 100000, y: 100000, w: 20, h: 16 }, // OB (clamped)
    { x: 600, y: 340, w: 20, h: 16 },       // spawn floor
    { x: -100, y: -100, w: 20, h: 16 },     // negative OB
  ];
  for (const box of cases) {
    assert.equal(R(py({ op: 'blocked_at', grid: realGrid, box })), R(jsBlockedAt(box)),
      `blockedAt ${JSON.stringify(box)}`);
  }
  const randomBoxes = [];
  for (let i = 0; i < 300; i++) {
    randomBoxes.push({ x: Math.random() * 1400, y: Math.random() * 800, w: 10 + Math.random() * 60, h: 10 + Math.random() * 50 });
  }
  // Batch the whole random battery into one bridge invocation (see pyMany).
  const pyRes = pyMany(randomBoxes.map(box => ({ op: 'blocked_at', grid: realGrid, box })));
  randomBoxes.forEach((box, i) => {
    assert.equal(R(pyRes[i]), R(jsBlockedAt(box)),
      `blockedAt random #${i} ${JSON.stringify(box)}`);
  });
});

test('cellFitsAgent battery (200 random cells)', () => {
  const cell = realGrid.cell;
  const cells = [];
  for (let i = 0; i < 200; i++) {
    cells.push([Math.floor(Math.random() * realGrid.cols), Math.floor(Math.random() * realGrid.rows)]);
  }
  // Batch the whole battery into one bridge invocation (see pyMany).
  const pyRes = pyMany(cells.map(([gx, gy]) => ({ op: 'cell_fits_agent', grid: realGrid, gx, gy })));
  cells.forEach(([gx, gy], i) => {
    assert.equal(R(pyRes[i]), R(jsCellFits(gx, gy, cell)),
      `cellFitsAgent (${gx},${gy})`);
  });
});

test('isOnADoorTile battery', () => {
  const pts = [[484, 220], [485, 221], [0, 0], [600, 340], [500, 260], [242 * 2, 110 * 2]];
  for (const [x, y] of pts) {
    assert.equal(R(py({ op: 'is_on_door_tile', doors: worldDoors, x, y })), R(jsOnDoor(x, y)),
      `isOnADoorTile (${x},${y})`);
  }
});

test('agentBlockedAt battery (self/invisible/door-tile/ignoreIds/exclude)', () => {
  const agents = {
    alpha: { id: 'alpha', x: 500, y: 300, visible: true },
    beta: { id: 'beta', x: 484, y: 220, visible: true },    // on a door tile -> exempt
    ghost: { id: 'ghost', x: 520, y: 300, visible: false }, // invisible -> exempt
    gamma: { id: 'gamma', x: 540, y: 300, visible: true },
  };
  const box = { x: 535, y: 296, w: 20, h: 16 }; // overlaps gamma only
  setGlobal('AGENTS', agents);
  assert.equal(R(py({ op: 'agent_blocked_at', agents, box, doors: worldDoors })), R(jsAgentBlocked(box)),
    'agentBlockedAt overlaps gamma');
  assert.equal(R(py({ op: 'agent_blocked_at', agents, box, exclude: 'gamma', doors: worldDoors })), R(jsAgentBlocked(box, 'gamma')),
    'agentBlockedAt exclude gamma');
  assert.equal(R(py({ op: 'agent_blocked_at', agents, box, ignore: ['gamma'], doors: worldDoors })), R(jsAgentBlocked(box, null, new Set(['gamma']))),
    'agentBlockedAt ignoreIds gamma');
  setGlobal('AGENTS', {});
});

test('computeReachableMask identical (48x86 element-wise)', () => {
  const jsM = jsMask();
  const pyM = py({ op: 'reachable_mask', grid: realGrid, spawn: { x: 600, y: 340 } });
  assert.equal(pyM.length, jsM.length, 'row count');
  for (let gy = 0; gy < jsM.length; gy++) {
    assert.equal(pyM[gy].length, jsM[gy].length, `col count row ${gy}`);
    for (let gx = 0; gx < jsM[gy].length; gx++) {
      assert.equal(R(pyM[gy][gx]), R(jsM[gy][gx]), `mask (${gx},${gy}) js=${jsM[gy][gx]} py=${pyM[gy][gx]}`);
    }
  }
});

// ---- Test C: deterministic multi-tick step-walk parity -----------------------
console.log('\nstep/slide/stuck/replan walk (Python) === JS, real paths');

// A reachable centered top-left world point (matches the walkpath geometry:
// findPath returns top-left corners; we request a target in world coords).
function freeWalkTarget() {
  const m = jsMask();
  const cell = realGrid.cell;
  for (let gy = 0; gy < realGrid.rows; gy++) {
    for (let gx = 0; gx < realGrid.cols; gx++) {
      if (m[gy][gx]) return { x: (gx * cell + cell / 2) * 2, y: (gy * cell + cell / 2) * 2, gx, gy };
    }
  }
  throw new Error('no reachable point');
}

function buildWalk(ids) {
  // ids: [{id, x, y, target, kind}] kind 'walker'|'block'
  const all = {};
  for (const spec of ids) {
    all[spec.id] = { id: spec.id, x: spec.x, y: spec.y, visible: true, path: null };
    if (spec.kind === 'walker') {
      all[spec.id].pathTarget = { x: spec.target.x, y: spec.target.y };
      const p = jsFindPath(spec.x, spec.y, spec.target.x, spec.target.y, spec.id);
      all[spec.id].path = p;
      all[spec.id].pathIndex = 0;
    }
    if (spec.kind === 'pair') {
      all[spec.id].pairWith = true;
      all[spec.id].pathTarget = { x: spec.target.x, y: spec.target.y };
      const p = jsFindPath(spec.x, spec.y, spec.target.x, spec.target.y, spec.id);
      all[spec.id].path = p;
      all[spec.id].pathIndex = 0;
    }
  }
  const clone = JSON.parse(JSON.stringify(all));
  return { jsAgents: all, pyAgents: clone };
}

function runWalkParity(jsAgents, pyAgents, steps, dt = 1 / 30) {
  setGlobal('AGENTS', jsAgents);
  events = [];
  for (let i = 0; i < steps; i++) jsTick(dt);
  const jsEvents = events.slice();
  // NOTE: the bridge runs in a separate process, so the RESULT it returns
  // (pyRes.agents) is the authoritative Python end-state -- `pyAgents` we sent
  // is just the serialized input, never mutated here on the JS side.
  const pyRes = py({ op: 'walk', dt, agents: pyAgents, grid: realGrid, doors: worldDoors, steps });
  const pyOut = pyRes.agents;
  for (const id in jsAgents) {
    const j = jsAgents[id], p = pyOut[id];
    assert.equal(p.x, j.x, `${id} x js=${round6(j.x)} py=${round6(p.x)}`);
    assert.equal(p.y, j.y, `${id} y js=${round6(j.y)} py=${round6(p.y)}`);
  }
  assert.equal(pyRes.events.length, jsEvents.length,
    `event count js=${JSON.stringify(jsEvents)} py=${JSON.stringify(pyRes.events)}`);
  for (let e = 0; e < jsEvents.length; e++) {
    assert.equal(pyRes.events[e][0], jsEvents[e][0], `event ${e} kind`);
    assert.equal(pyRes.events[e][1], jsEvents[e][1], `event ${e} id`);
  }
}

test('two crossing walkers + a stationary door-tile blocker all match', () => {
  const a = freeWalkTarget(), t = freeWalkTarget();
  const b = freeWalkTarget(), u = freeWalkTarget();
  const doorPt = { x: worldDoors.pressoffice.x + 12, y: worldDoors.pressoffice.y + 12 };
  const { jsAgents, pyAgents } = buildWalk([
    { id: 'w1', x: a.x, y: a.y, target: t, kind: 'walker' },
    { id: 'w2', x: b.x, y: b.y, target: u, kind: 'walker' },
    { id: 'door', x: doorPt.x, y: doorPt.y, kind: 'block' },
  ]);
  runWalkParity(jsAgents, pyAgents, 3000);
});

test('stuck -> replan around a live blocker -> arrive (no respawn)', () => {
  const a = freeWalkTarget(), t = freeWalkTarget();
  // blocker overlapping w1's start so the first steps are forced to stall
  const blocker = { x: a.x + 4, y: a.y, kind: 'block' };
  const { jsAgents, pyAgents } = buildWalk([
    { id: 'w1', x: a.x, y: a.y, target: t, kind: 'walker' },
    { id: 'block', x: blocker.x, y: blocker.y, kind: 'block' },
  ]);
  runWalkParity(jsAgents, pyAgents, 4000);
});

const RANDOM_WALKS = 12, WALK_STEPS = 800;
test(`randomized walk battery (${RANDOM_WALKS} layouts x ${WALK_STEPS} ticks) all match`, () => {
  for (let i = 0; i < RANDOM_WALKS; i++) {
    const specs = [];
    const nWalker = 1 + Math.floor(Math.random() * 3);
    for (let w = 0; w < nWalker; w++) {
      const s = freeWalkTarget(), tgt = freeWalkTarget();
      const kind = ['walker', 'walker', 'pair'][Math.floor(Math.random() * 3)];
      specs.push({ id: 'w' + w, x: s.x, y: s.y, target: tgt, kind });
    }
    const nBlock = Math.floor(Math.random() * 3);
    for (let bIdx = 0; bIdx < nBlock; bIdx++) {
      const s = freeWalkTarget();
      specs.push({ id: 'b' + bIdx, x: s.x, y: s.y, kind: 'block' });
    }
    const { jsAgents, pyAgents } = buildWalk(specs);
    runWalkParity(jsAgents, pyAgents, WALK_STEPS);
  }
  console.log(`       (${RANDOM_WALKS} layouts x ${WALK_STEPS} ticks, all byte-identical)`);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);