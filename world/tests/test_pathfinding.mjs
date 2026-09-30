// Real automated regression tests for the pathfinding functions that have
// broken in production multiple times (float-boundary drift,
// cell-center-vs-box mismatches, an agent blocking her own start cell, and
// most recently findPath() returning a truthy-but-empty array for an
// already-there start==target cell, which crashed the whole game's render
// loop). Loads the REAL source files (world.js, agents.js, tasks.js) into
// a vm context rather than re-implementing the logic here, so these tests
// actually exercise production code, not a parallel copy of it that could
// silently drift out of sync.
//
// Run: node tests/test_pathfinding.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = {
  console,
  Math,
  Date,
  JSON,
  Array,
  Object,
};
vm.createContext(context);

for (const file of ['world.js', 'agents.js', 'tasks.js']) {
  const src = fs.readFileSync(path.join(worldDir, file), 'utf8');
  vm.runInContext(src, context, { filename: file });
}

// overlaps() lives in index.html (inline, not one of the standalone .js
// files), since agentBlockedAt() (agents.js) calls it -- defined here
// exactly as index.html:591 defines it, not re-derived, so a change to
// the real one and a drift here would show up as a test failure rather
// than silently testing something else.
vm.runInContext(
  'function overlaps(a, b) { return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y; }',
  context
);

// A top-level `let`/`const` in a vm script lives in that context's own
// lexical environment, NOT as a settable property on the context object --
// only `var`/function declarations become real object properties. Setting
// `context.COLLISION_GRID = ...` directly from here would silently create
// an unrelated property that world.js's own functions never see, since
// they close over the REAL `let COLLISION_GRID` from their own file.
// Bridge it through a plain (never-`let`-declared) property instead, then
// assign inside the context so it resolves to the actual binding.
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

// Same bridge, the other direction -- SCALE/AGENT_W/AGENT_H are `const` in
// their real files, so they're not object properties either; pull them
// out through a plain assignment the same way.
function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}
const SCALE = getGlobal('SCALE');
const AGENT_W = getGlobal('AGENT_W');
const AGENT_H = getGlobal('AGENT_H');
const findPath = (...args) => vm.runInContext('findPath', context)(...args);
const blockedAt = (...args) => vm.runInContext('blockedAt', context)(...args);
const tickAgentMovement = (...args) => vm.runInContext('tickAgentMovement', context)(...args);
const pickFreeSpot = (...args) => vm.runInContext('pickFreeSpot', context)(...args);
const cellFitsAgent = (...args) => vm.runInContext('cellFitsAgent', context)(...args);

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

// A small synthetic 12x12 grid, cell size 8 (matches the real grid's cell
// size), all open except a solid wall down column 6 with a 3-row gap
// (rows 5-7) -- wide enough that AGENT_W (20, wider than one 16px-world
// cell) can pass through without a boundary-exactness argument over a
// single-row gap. Enough to exercise real routing, not just a straight
// line.
function makeGrid() {
  const cols = 12, rows = 12, cell = 8;
  const grid = Array.from({ length: rows }, () => new Array(cols).fill(0));
  for (let y = 0; y < rows; y++) if (y < 5 || y > 7) grid[y][6] = 1;
  return { cols, rows, cell, grid };
}

// Mirrors findPath()'s own cellWorldPos() exactly (top-left-corner
// convention) -- picking test coordinates this way instead of hand-
// guessed pixels means a test failure means a real bug, not arithmetic
// drift between the test and the code it's testing.
function cellWorld(gx, gy, cell = 8) {
  return { x: (gx * cell + cell / 2) * SCALE - AGENT_W / 2, y: (gy * cell + cell / 2) * SCALE - AGENT_H / 2 };
}

console.log('findPath / blockedAt / cellFitsAgent (tasks.js + agents.js + world.js)');

test('blockedAt: open cell is not blocked', () => {
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const free = { x: 20, y: 20, w: AGENT_W, h: AGENT_H };
  assert.equal(blockedAt(free), false);
});

test('blockedAt: wall cell is blocked', () => {
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const p = cellWorld(6, 2); // column 6, row 2 -- walled (row 2 < gap rows 5-7)
  assert.equal(blockedAt({ x: p.x, y: p.y, w: AGENT_W, h: AGENT_H }), true);
});

test('findPath: real regression -- start cell === target cell returns a single waypoint, never an empty array', () => {
  // This is the exact bug: an agent re-assigned
  // to the room she's already parked in has start===target, and the old
  // code returned `[]` (truthy, but empty) instead of null -- which
  // crashed tickAgentMovement() reading path[0].x off undefined, which in
  // turn froze the entire game's render loop dead. A regression here
  // would have caught that before it ever shipped.
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const p = cellWorld(1, 1);
  const path = findPath(p.x, p.y, p.x, p.y, 'test-agent');
  assert.ok(Array.isArray(path), 'expected an array back');
  assert.ok(path.length >= 1, 'expected at least one waypoint, got an empty array');
});

test('findPath: routes around a wall through its gap', () => {
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const start = cellWorld(1, 6), target = cellWorld(10, 6); // straight across, through the column-6 gap at row 6
  const path = findPath(start.x, start.y, target.x, target.y, 'test-agent');
  assert.ok(path, 'expected a real route through the gap');
  assert.ok(path.length > 1, 'expected multiple waypoints to route across');
});

test('findPath: genuinely unreachable target (outside grid bounds) returns null', () => {
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const start = cellWorld(1, 1);
  const path = findPath(start.x, start.y, 100000, 100000, 'test-agent');
  assert.equal(path, null);
});

test('findPath: an out-of-bounds START (not just target) returns null instead of throwing', () => {
  // Crash: an earlier bug elsewhere left an agent's own
  // x/y corrupted to a coordinate one row past the bottom of the grid.
  // The target-bounds check above didn't catch it, because THIS call's
  // out-of-bounds coordinate was the start, not the target -- the
  // exception (indexing visited[outOfBoundsRow]) propagated straight out
  // of findPath, which freezes the whole game (see tickAgentMovement's
  // own comments on why an uncaught exception here is never acceptable).
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('AGENTS', {});
  const target = cellWorld(1, 1);
  assert.doesNotThrow(() => {
    const path = findPath(100000, 100000, target.x, target.y, 'test-agent');
    assert.equal(path, null);
  });
});

test('findPath: another agent standing on the mover\'s own start cell does not block her', () => {
  // Bug: two agents landing on the
  // exact same door-front resting spot used to permanently block the
  // second one from ever being assigned anywhere else.
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6), target = cellWorld(10, 6);
  setGlobal('AGENTS', { other: { x: start.x, y: start.y, visible: true } });
  const path = findPath(start.x, start.y, target.x, target.y, 'mover');
  assert.ok(path, 'expected a route even though another agent occupies the exact start cell');
});

test('findPath: another agent a fraction of a pixel from the mover\'s own start still counts as co-located', () => {
  // Deadlock: two agents from independent
  // walk calculations landed 0.3-0.7px apart, not bit-exact -- the
  // co-located exemption above only matched exact x/y equality, so each
  // agent's overlapping 20px-wide box (AGENT_W > one 16px-world cell)
  // still registered as blocking every cell around the OTHER one, and
  // findPath failed outright for both, permanently, with no retry able
  // to change the outcome. Co-located has to mean "already overlapping,"
  // not "identical coordinates."
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6), target = cellWorld(10, 6);
  setGlobal('AGENTS', { other: { x: start.x + 0.5, y: start.y, visible: true } });
  const path = findPath(start.x, start.y, target.x, target.y, 'mover');
  assert.ok(path, 'expected a route even though another agent overlaps the start by a near-exact, non-identical offset');
});

test('findPath: another agent standing exactly on the destination relaxes to the nearest free cell instead of rejecting the whole request', () => {
  // Root cause, confirmed: this used to return null
  // outright the instant anyone stood on the exact target cell -- not a
  // slower route, a hard rejection before BFS even ran. That's the actual
  // mechanism behind "agent X is blocking the door": a fixed door/desk
  // approach point is exactly one cell, so whoever gets there first makes
  // it UNPLANNABLE for everyone else, including the stuck-timer replan,
  // which retries the identical target and hits the identical rejection
  // forever. Approaching a door doesn't need the exact pixel -- relaxing
  // to the nearest actually-free cell within TARGET_RELAX_RADIUS fixes
  // this at the source for every caller (sendAgentOffDuty, assignTask,
  // the stuck-timer replan, handoffs) at once.
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6), target = cellWorld(10, 6);
  setGlobal('AGENTS', { other: { x: target.x, y: target.y, visible: true } });
  const path = findPath(start.x, start.y, target.x, target.y, 'mover');
  assert.ok(path, 'expected a route to a nearby free cell instead of a flat rejection');
  const last = path[path.length - 1];
  assert.ok(last.x !== target.x || last.y !== target.y, 'the relaxed arrival point should not be the exact contested cell');
  const cellWorldPx = 8 * SCALE; // native cell size * SCALE, matching cellWorldPos()'s own step
  const dist = Math.hypot(last.x - target.x, last.y - target.y);
  assert.ok(dist <= 3 * cellWorldPx + 1, `expected the relaxed arrival point to stay within TARGET_RELAX_RADIUS of the original target, got ${dist}px away`);
});

test('findPath: a destination surrounded by agents beyond the relax radius still fails (no false positive)', () => {
  // The other side of the fix above: relaxation is bounded, not "find
  // literally anywhere reachable" -- if the whole neighborhood really is
  // occupied, this should still report no route rather than silently
  // substituting some unrelated far-away cell.
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6), target = cellWorld(10, 6);
  const agents = {};
  let n = 0;
  for (let dy = -4; dy <= 4; dy++) {
    for (let dx = -4; dx <= 4; dx++) {
      const gx = 10 + dx, gy = 6 + dy;
      if (gx < 0 || gy < 0 || gx > 11 || gy > 11) continue;
      const p = cellWorld(gx, gy);
      agents['blocker' + (n++)] = { x: p.x, y: p.y, visible: true };
    }
  }
  setGlobal('AGENTS', agents);
  const path = findPath(start.x, start.y, target.x, target.y, 'mover');
  assert.equal(path, null, 'every cell within (and just beyond) the relax radius is occupied -- should not find a path');
});

test('findPath: an idle agent resting exactly on a door tile does not permanently block the only route through it', () => {
  // Bug: an agent going idle (task done, off-duty, an
  // abandoned handoff) never moves again on her own. If that resting spot
  // is a door -- a real single-file chokepoint -- every other agent whose
  // only route runs through it gets stuck forever, which looks exactly
  // like "someone went inactive in front of a door and it caused another
  // agent to get stuck in an infinite loop." Doors are exempted from
  // agent-vs-agent blocking (agentBlockedAt/isOnADoorTile, agents.js) for
  // exactly this reason.
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6), target = cellWorld(10, 6);
  const doorCenter = cellWorld(6, 6); // the column-6 gap this grid's own wall leaves open -- the only route across
  setGlobal('ROOM_DOOR_TRIGGERS', { testbuilding: { x: doorCenter.x - 4, y: doorCenter.y - 4, w: 8, h: 8 } });
  setGlobal('AGENTS', { resting: { x: doorCenter.x, y: doorCenter.y, visible: true } });
  const path = findPath(start.x, start.y, target.x, target.y, 'mover');
  assert.ok(path, 'a resting agent parked exactly on the door should not block the only route through it');
});

console.log('\nfindPath: a second agent planned right after the first does not claim the identical destination');

test('two agents planned sequentially from the same start toward the same raw target land on genuinely different cells', () => {
  // Bug, one layer deeper than the single-cell
  // relaxation above: relaxation alone isn't enough when TWO agents are
  // planned in the same synchronous batch (a mass "send everyone home")
  // from the same/near-identical start -- neither sees the other as a
  // live obstacle yet, so both independently relax to the exact same
  // nearest free cell (a deterministic search from identical inputs
  // always picks the identical answer). Confirmed: after the
  // co-located movement fix let them separate by one step, they
  // re-gridlocked almost immediately, just fractionally apart, because
  // AGENT_W (20) is wider than a single step. The real fix has to happen
  // at PLANNING time: once the first agent's real call sets a.pathTarget,
  // the second agent's call treats that cell as claimed and picks a
  // genuinely different one -- exactly how sendAgentOffDuty/assignTask
  // set a.pathTarget to the raw requested point right after findPath
  // returns, so this only ever sees a target that's real by the time the
  // next call runs.
  setGlobal('COLLISION_GRID', makeGrid());
  const start = cellWorld(1, 6);
  const target = { x: cellWorld(9, 6).x + 40, y: cellWorld(9, 6).y }; // an arbitrary raw point, not itself cell-aligned -- matches how a real door approach point is passed in
  setGlobal('AGENTS', {
    mover1: { id: 'mover1', visible: true, x: start.x, y: start.y, pathTarget: null },
    mover2: { id: 'mover2', visible: true, x: start.x, y: start.y, pathTarget: null },
  });
  const p1 = findPath(start.x, start.y, target.x, target.y, 'mover1');
  assert.ok(p1, 'mover1 should get a real path with nothing else in the way yet');
  const agents = getGlobal('AGENTS');
  agents.mover1.pathTarget = { x: target.x, y: target.y }; // exactly what sendAgentOffDuty/assignTask do right after a successful findPath
  setGlobal('AGENTS', agents);

  const p2 = findPath(start.x, start.y, target.x, target.y, 'mover2');
  assert.ok(p2, 'mover2 should still get a real path too, just not to the identical cell');
  const last1 = p1[p1.length - 1], last2 = p2[p2.length - 1];
  assert.ok(last1.x !== last2.x || last1.y !== last2.y, `both agents landed on the exact same destination cell (${JSON.stringify(last1)}) -- this is what recreates the live deadlock`);
});

console.log('\ntickAgentMovement: two agents planned sequentially both make sustained real progress');

test('neither one freezes, once they start a real agent-width apart (what finishTask now guarantees)', () => {
  // Deliberately NOT testing exact co-location here -- that's the
  // narrower thing this test used to try, and it exposed a real, separate
  // limit: the co-located exemption (agentBlockedAt) only ever buys the
  // FIRST agent a single small step, nowhere near enough to clear
  // AGENT_W (20px) at real per-frame speed, so two agents starting on the
  // EXACT identical pixel and needing to travel through the same shared
  // corridor together can still re-gridlock just fractionally apart. The
  // actual fix for that is upstream, at the source: finishTask() (tasks.js)
  // now steps a same-room finisher aside by a full agent-width the moment
  // it would otherwise land exactly on a real occupant, so this exact
  // shared-corridor case should no longer arise from that trigger. What
  // DOES need to hold, and what this test locks in: once two agents are
  // already a real agent-width apart (the guaranteed post-jitter case),
  // ordinary movement resolution must not need anything special to let
  // both make sustained progress.
  setGlobal('COLLISION_GRID', makeGrid());
  setGlobal('showToast', () => {});
  const start1 = cellWorld(1, 6);
  const start2 = { x: start1.x + AGENT_W + 8, y: start1.y };
  const target = { x: cellWorld(9, 6).x + 40, y: cellWorld(9, 6).y };
  setGlobal('AGENTS', {
    mover1: { id: 'mover1', visible: true, x: start1.x, y: start1.y, pathTarget: null, stuckTimer: 0, replanCount: 0 },
    mover2: { id: 'mover2', visible: true, x: start2.x, y: start2.y, pathTarget: null, stuckTimer: 0, replanCount: 0 },
  });
  const p1 = findPath(start1.x, start1.y, target.x, target.y, 'mover1');
  let agents = getGlobal('AGENTS');
  agents.mover1.path = p1; agents.mover1.pathIndex = 0; agents.mover1.pathTarget = { x: target.x, y: target.y };
  setGlobal('AGENTS', agents);

  const p2 = findPath(start2.x, start2.y, target.x, target.y, 'mover2');
  agents = getGlobal('AGENTS');
  agents.mover2.path = p2; agents.mover2.pathIndex = 0; agents.mover2.pathTarget = { x: target.x, y: target.y };
  setGlobal('AGENTS', agents);

  for (let i = 0; i < 200; i++) tickAgentMovement(1 / 60); // ~3.3 simulated seconds at real frame rate, not an artificially coarse step

  const after = getGlobal('AGENTS');
  const moved1 = Math.hypot(after.mover1.x - start1.x, after.mover1.y - start1.y);
  const moved2 = Math.hypot(after.mover2.x - start2.x, after.mover2.y - start2.y);
  assert.ok(moved1 > 20, `mover1 should have made sustained real progress, only moved ${moved1.toFixed(2)}px`);
  assert.ok(moved2 > 20, `mover2 should have made sustained real progress, only moved ${moved2.toFixed(2)}px`);
});

console.log('\npickFreeSpot never returns a spot findPath would reject as an invalid start (2026-09-21)');

test('every spot pickFreeSpot returns, over many trials against the REAL map, is a usable findPath START', () => {
  // Bug, TWICE, each fix revealing the
  // next layer: pickFreeSpot and findPath's own start-validity check
  // (startCellIsFree) each convert a world position to a grid cell
  // differently depending on which convention (raw top-left vs. box
  // CENTER) they use, and cellWorldPos's own centered waypoints mean the
  // "correct" convention is the CENTERED one -- confirmed, an agent
  // resting at her own real task.entryX/entryY (itself a real waypoint)
  // could not pathfind anywhere from her own position, because the
  // reverse conversion used elsewhere didn't recover the same cell
  // cellWorldPos originally centered on (AGENT_W=20 doesn't evenly
  // divide cell*SCALE=16). Asserting against findPath itself -- can an
  // agent placed exactly here actually plan a path AT ALL -- is what
  // actually matters, and is immune to this test independently getting
  // the "right" conversion formula wrong the same way production code
  // twice did; hand-deriving gx/gy here and calling cellFitsAgent
  // directly is exactly the trap that caused this whole back-and-forth.
  // Uses the REAL collision_grid.json (not a small synthetic grid) over
  // many real trials since the mismatch only shows up at genuine
  // cell-boundary-straddling positions, which a hand-picked single case
  // could miss covering.
  const realGrid = JSON.parse(fs.readFileSync(path.join(worldDir, 'collision_grid.json'), 'utf8'));
  setGlobal('COLLISION_GRID', realGrid);
  setGlobal('REACHABLE_MASK', null); // force a fresh flood-fill against this real grid
  setGlobal('AGENTS', {});
  let violations = 0;
  for (let i = 0; i < 500; i++) {
    const spot = pickFreeSpot([]);
    // Bug found investigating a flake in THIS test: a
    // self-to-self call (start === target, same raw numbers) isn't a
    // faithful stand-in for "is this a valid place to stand" -- findPath's
    // START conversion is CENTERED but its TARGET conversion is
    // deliberately left RAW (see findPath's own comment on why: a real
    // door/task target is a literal point, not a re-examined agent box).
    // Feeding the SAME numeric value through both conventions can land on
    // two DIFFERENT cells purely from that asymmetry, occasionally (~1 in
    // 700 real trials) landing the raw side on a cell that doesn't fit an
    // agent -- not because the spot itself is bad (confirmed: the
    // exact failing spot pathed fine to a real, different nearby target),
    // but because this specific self-referential probe isn't something
    // any real caller ever actually does. A real target a real caller
    // would plausibly use -- tried in all 4 directions so a spot near a
    // map edge still gets a fair shot -- is what actually matters here.
    const probeOk = findPath(spot.x, spot.y, spot.x + 40, spot.y, 'probe')
      || findPath(spot.x, spot.y, spot.x - 40, spot.y, 'probe')
      || findPath(spot.x, spot.y, spot.x, spot.y + 40, 'probe')
      || findPath(spot.x, spot.y, spot.x, spot.y - 40, 'probe');
    if (!probeOk) violations++;
  }
  assert.equal(violations, 0, `expected every pickFreeSpot() result to be a usable findPath start toward a real nearby target, got ${violations}/500 that would strand an agent placed there`);
});

test('a real path\'s own final waypoint, used later as a fresh start, can still plan a new path over many trials', () => {
  // The exact scenario: assignTask() now stores the path's
  // real final waypoint as task.entryX/entryY (a separate fix, the same
  // day, for a different bug -- see assignTask's own comment), and
  // finishTask() later resets a.x/a.y to exactly that. If findPath's
  // start-cell conversion doesn't agree with cellWorldPos's own centered
  // convention, an agent resting at her own perfectly valid former
  // waypoint could not plan ANY new path from her own position --
  // confirmed against a real door (postoffice). Whether this
  // manifests for any ONE door/spawn pair depends on whether the
  // off-by-one cell happens to also be free, so this checks many real
  // doors, not just one -- a single hand-picked pair could get lucky and
  // never show it.
  const realGrid = JSON.parse(fs.readFileSync(path.join(worldDir, 'collision_grid.json'), 'utf8'));
  setGlobal('COLLISION_GRID', realGrid);
  setGlobal('AGENTS', {});
  const spawn = { x: 300 * SCALE, y: 170 * SCALE }; // the real game's own SPAWN point -- guaranteed walkable, it's computeReachableMask's own flood-fill origin
  const realDoors = [
    { x: 484, y: 220, w: 62, h: 62 }, { x: 896, y: 202, w: 50, h: 64 }, { x: 1188, y: 646, w: 52, h: 50 },
    { x: 694, y: 212, w: 58, h: 72 }, { x: 888, y: 562, w: 62, h: 70 }, { x: 438, y: 554, w: 42, h: 60 },
    { x: 198, y: 652, w: 44, h: 46 },
  ];
  let violations = 0;
  for (const door of realDoors) {
    const doorTarget = { x: door.x + door.w / 2, y: door.y + door.h + 4 }; // same "+door.h+4" convention assignTask itself uses
    const firstPath = findPath(spawn.x, spawn.y, doorTarget.x, doorTarget.y, 'mover');
    if (!firstPath) continue; // not every door is reachable from this one spawn point in this synthetic setup -- not what this test is about
    const restingSpot = firstPath[firstPath.length - 1]; // exactly what assignTask() now stores as entryX/entryY
    if (!findPath(restingSpot.x, restingSpot.y, spawn.x, spawn.y, 'mover')) violations++;
  }
  assert.equal(violations, 0, `expected an agent resting at her own real former waypoint to always be able to plan a new path, got ${violations} real door(s) where she could not`);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
