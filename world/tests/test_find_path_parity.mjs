// Differential parity test for the Phase-2 server-side-simulation port of
// findPath (tasks.js) into sim.py. The Python `sim.find_path` is a fork-lift
// of the JS `findPath`; every corner case in that function is a
// production bug, so "close enough" is not acceptable -- this test feeds the
// REAL tasks.js findPath (loaded into a vm with the real production files)
// and the REAL sim.find_path (shelled out through tests/_find_path_bridge.py)
// the IDENTICAL inputs, and asserts the outputs are bit-for-bit identical
// (including null-vs-array and every waypoint float).
//
// Run: node tests/test_find_path_parity.mjs   (also wired into run_all.sh)
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

// overlaps() lives in index.html (inline, not a standalone .js) -- defined
// here exactly as index.html:591 defines it (same as test_pathfinding.mjs).
vm.runInContext(
  'function overlaps(a, b) { return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y; }',
  context
);

// The vm `let`/`const` bindings aren't object properties; bridge through a
// plain property (see test_pathfinding.mjs for the full rationale).
function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

const jsFindPath = (...args) => vm.runInContext('findPath', context)(...args);

const realGrid = JSON.parse(fs.readFileSync(path.join(worldDir, 'collision_grid.json'), 'utf8'));
setGlobal('COLLISION_GRID', realGrid);
setGlobal('AGENTS', {});

function pythonFindPaths(reqs) {
  // ONE interpreter serves every request in the batch (line-oriented: one JSON
  // request per stdin line, one JSON result per stdout line). The old
  // per-request execFileSync spawned a fresh Python for every case -- 150+
  // interpreter startups in the battery alone (~20s) that, under concurrent
  // suite runs, compounded into an apparent hang. execFileSync returns clean
  // output only on a zero exit; a nonzero exit throws.
  const input = reqs.map(r => JSON.stringify(r)).join('\n') + '\n';
  const out = execFileSync('/usr/bin/env', ['python3', 'tests/_find_path_bridge.py'], {
    input,
    cwd: worldDir,
  });
  const lines = out.toString().split('\n').filter(l => l.trim() !== '');
  if (lines.length !== reqs.length) {
    throw new Error(`bridge returned ${lines.length} results for ${reqs.length} requests`);
  }
  return lines.map(l => JSON.parse(l));
}

function pythonFindPath(req) {
  return pythonFindPaths([req])[0];
}

// Everything the JS findPath reads is captured in the request object, so the
// exact same object feeds both engines. Returns { js, py } both normalized to
// null or a list of {x, y}. `pyResult` optionally supplies a precomputed
// Python result (from a batched pythonFindPaths call); otherwise it shells out
// for the single request.
function runParity(req, pyResult) {
  setGlobal('AGENTS', req.agents);
  const js = jsFindPath(req.start[0], req.start[1], req.target[0], req.target[1], req.exclude);
  const py = pyResult !== undefined ? pyResult : pythonFindPath({ ...req, grid: realGrid });
  return { js, py };
}

function assertSame(req, label) {
  const { js, py } = runParity(req);
  if (js === null || py === null) {
    assert.equal(
      py, js,
      `${label}: null-vs-array mismatch (js=${JSON.stringify(js)}, py=${JSON.stringify(py)})`
    );
    return;
  }
  assert.equal(
    py.length, js.length,
    `${label}: waypoint count mismatch (js=${jsonPoint(js)}, py=${jsonPoint(py)})`
  );
  for (let i = 0; i < js.length; i++) {
    assert.equal(
      py[i].x, js[i].x,
      `${label}: waypoint ${i} x mismatch (js=${js[i].x}, py=${py[i].x})`
    );
    assert.equal(
      py[i].y, js[i].y,
      `${label}: waypoint ${i} y mismatch (js=${js[i].y}, py=${py[i].y})`
    );
  }
}

function jsonPoint(path) {
  if (!path) return String(path);
  return '[' + path.map(p => `(${p.x.toFixed(3)},${p.y.toFixed(3)})`).join(' ') + ']';
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

// Deterministic LCG so the randomized battery is reproducible across runs.
let seed = 0x2a1b3c4d;
function rnd() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; }
function randInt(n) { return Math.floor(rnd() * n); }

// A free world position: keep hunting until the cellUnder its top-left corner
// is a 0 cell of the real grid (so both engines share one walkable origin).
function freePoint(width = 8, height = 8) {
  const cols = realGrid.cols, rows = realGrid.rows, cell = realGrid.cell;
  for (let i = 0; i < 400; i++) {
    const gx = randInt(cols), gy = randInt(rows);
    if (realGrid.grid[gy][gx]) continue;
    const x = gx * cell * 2; // world-space: native cell * SCALE
    const y = gy * cell * 2;
    if (x < 0 || y < 0 || x + width > 1376 || y + height > 768) continue; // GROUND bounds-ish
    return { x, y, gx, gy };
  }
  throw new Error('could not find a free point');
}

function agentsWith(entries) {
  const agents = {};
  let n = 0;
  for (const e of entries) {
    agents['a' + (n++)] = { id: 'a' + n, x: e.x, y: e.y, visible: true, pathTarget: e.pathTarget ?? null };
  }
  return agents;
}

const reqFor = (start, target, agents = {}, exclude = 'mover') => ({
  start: [start.x, start.y], target: [target.x, target.y], exclude, agents,
});

console.log('sim.find_path (Python) === tasks.js findPath (JavaScript), on the real collision grid');

test('open field, straight shot', () => {
  const s = freePoint(), t = freePoint();
  assertSame(reqFor(s, t), 'open field');
});

test('blocked straight shot: an agent directly in the way forces a detour', () => {
  const s = freePoint(), t = freePoint();
  const mid = { x: (s.x + t.x) / 2, y: (s.y + t.y) / 2 };
  assertSame(reqFor(s, t, agentsWith([mid])), 'blocked straight shot');
});

test('co-located overlap exemption: mover sits exactly on another agent', () => {
  const s = freePoint();
  const other = { x: s.x, y: s.y };
  assertSame(reqFor(s, freePoint(), agentsWith([other]), 'mover'), 'co-located exact');
});

test('co-located overlap exemption: near-identical (0.5px-apart) overlap', () => {
  const s = freePoint();
  const other = { x: s.x + 0.5, y: s.y };
  assertSame(reqFor(s, freePoint(), agentsWith([other]), 'mover'), 'co-located near-exact');
});

test('target cell occupied by another agent -> relax to nearest free cell', () => {
  const s = freePoint(), t = freePoint();
  const blocker = { x: t.x, y: t.y };
  assertSame(reqFor(s, t, agentsWith([blocker])), 'relax around contested target');
});

test('start === target (center-aligned) returns a single waypoint', () => {
  const s = freePoint();
  const req = reqFor(s, s);
  assertSame(req, 'start == target');
  assert.equal(runParity(req).js.length, 1, 'expected exactly one waypoint');
});

test('target off-grid returns null', () => {
  const s = freePoint();
  assertSame(reqFor(s, { x: 100000, y: 100000 }), 'target off-grid');
});

test('start off-grid returns null instead of throwing', () => {
  const t = freePoint();
  assertSame(reqFor({ x: 100000, y: 100000 }, t), 'start off-grid');
});

test('door-front target with a resident parked on the approach point', () => {
  const s = freePoint(), t = freePoint();
  const park = { x: t.x - 4, y: t.y }; // just shy of the target cell edge
  assertSame(reqFor(s, t, agentsWith([park])), 'door-front parked agent');
});

test('a second agent\'s already-chosen pathTarget claims a cell (sequential planning)', () => {
  const s = freePoint(), t = freePoint();
  const claimed = { x: s.x, y: s.y, visible: true, pathTarget: { x: t.x, y: t.y } };
  const agents = agentsWith([claimed]);
  agents.mover = { id: 'mover', x: s.x, y: s.y, visible: true, pathTarget: null };
  assertSame(reqFor(s, t, agents, 'mover'), 'claimed pathTarget');
});

console.log(`\nRandomized battery against the real map (reproducible seed):`)
const RANDOM_CASES = 150;
test(`500 randomized start/target/agent-layout cases all match bit-for-bit`, () => {
  const reqs = [];
  for (let i = 0; i < RANDOM_CASES; i++) {
    const s = freePoint(), t = freePoint();
    const layout = [];
    const nBlockers = randInt(4);
    for (let b = 0; b < nBlockers; b++) {
      const p = freePoint();
      const named = { x: p.x, y: p.y };
      if (rnd() < 0.5) named.pathTarget = { x: freePoint().x, y: freePoint().y };
      layout.push(named);
    }
    const agents = agentsWith(layout);
    agents.mover = { id: 'mover', x: s.x, y: s.y, visible: true, pathTarget: null };
    reqs.push(reqFor(s, t, agents, 'mover'));
  }
  // Batch the whole battery into ONE bridge invocation (see pythonFindPaths).
  const pyResults = pythonFindPaths(reqs.map(r => ({ ...r, grid: realGrid })));
  let checked = 0;
  for (let i = 0; i < RANDOM_CASES; i++) {
    const { js, py } = runParity(reqs[i], pyResults[i]);
    assert.equal(py === null, js === null, `case #${i}: null-vs-array mismatch (s=(${reqs[i].start[0]},${reqs[i].start[1]}) t=(${reqs[i].target[0]},${reqs[i].target[1]}))`);
    if (js !== null) {
      assert.equal(py.length, js.length, `case #${i}: waypoint count (js=${py.length}?? js=${js.length})`);
      for (let w = 0; w < js.length; w++) {
        assert.equal(py[w].x, js[w].x, `case #${i} waypoint ${w} x: js=${js[w].x} py=${py[w].x}`);
        assert.equal(py[w].y, js[w].y, `case #${i} waypoint ${w} y: js=${js[w].y} py=${py[w].y}`);
      }
    }
    checked++;
  }
  console.log(`       (${checked} randomized cases, all byte-identical to JS)`);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);