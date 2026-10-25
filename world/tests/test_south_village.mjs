// The south village: a separate second map reached by the unused south path.
// Tests the scene wiring and the authored map data, not the art.
//
// Run: node tests/test_south_village.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');
const readJson = (name) => JSON.parse(fs.readFileSync(path.join(worldDir, name), 'utf8'));

const context = {
  console, Math, Date, JSON, Array, Object, Set, Promise,
  state: { scene: 'main', player: { x: 0, y: 0, w: 20, h: 16 }, zoom: 1 },
  ZOOM_SHOW_ALL: 1, ZOOM_ROOM: 1,
  setSceneDoors: () => {},
  showToast: () => {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(worldDir, 'js', 'world.js'), 'utf8'), context, { filename: 'world.js' });
const call = (name, ...a) => vm.runInContext(name, context)(...a);
const evalIn = (src) => vm.runInContext(src, context);

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

console.log('south scene wiring (world.js)');

await test('the south scene is a separate map with its own data files', () => {
  const scene = evalIn('SCENES.south');
  assert.equal(scene.gridSprite, '/collision_grid_south.json');
  assert.equal(scene.doorsSprite, '/door_triggers_south.json');
  assert.equal(scene.bgSprite, '/assets/think_tank_south.png');
  assert.notEqual(scene.gridSprite, evalIn('SCENES.main.gridSprite'));
});

await test('switchScene lands the player on the south spawn and swaps the grid', () => {
  evalIn('SCENES.south.bg = {}; SCENES.south.grid = { cols: 86, rows: 48, cell: 8, grid: [] };');
  call('switchScene', 'south');
  assert.equal(context.state.scene, 'south');
  const spawn = evalIn('SCENES.south.spawn');
  assert.equal(context.state.player.x, spawn.x);
  assert.equal(context.state.player.y, spawn.y);
  assert.equal(evalIn('COLLISION_GRID'), evalIn('SCENES.south.grid'));
});

await test('returnToMainFromSouth lands just above the south outlet', () => {
  evalIn('SCENES.main.bg = {}; SCENES.main.grid = { cols: 86, rows: 48, cell: 8, grid: [] };');
  call('returnToMainFromSouth');
  assert.equal(context.state.scene, 'main');
  const outlet = evalIn('SOUTH_OUTLET');
  assert.equal(context.state.player.x, outlet.x + outlet.w / 2 - 20 / 2);
  assert.equal(context.state.player.y, outlet.y - 16 - 8);
});

await test('the south gate is on the north edge and does not overlap the spawn', () => {
  const gate = evalIn('SOUTH_GATE');
  const spawn = evalIn('SCENES.south.spawn');
  assert.equal(gate.y, 0);
  assert.ok(gate.x > 0 && gate.x + gate.w < 688 * 2);
  assert.ok(spawn.y > gate.y + gate.h, 'arrival spawn must sit inside the map, past the gate');
});

console.log('south outlet selector UI');

await test('the outlet opens a three-option selector, not a room', () => {
  const html = fs.readFileSync(path.join(worldDir, 'index.html'), 'utf8');
  assert.ok(html.includes('id="southOutletModal"'));
  assert.ok(html.includes('id="southOutletTravelBtn"'));
  assert.ok(html.includes('id="southOutletMeetingBtn"'));
  assert.ok(html.includes('id="southOutletCancelBtn"'));
  const loop = fs.readFileSync(path.join(worldDir, 'js', 'ui_loop.js'), 'utf8');
  assert.ok(loop.includes("building === 'south_outlet'"));
  assert.ok(loop.includes('openSouthMenu()'));
});

await test('all three selector outcomes are wired', () => {
  const ui = fs.readFileSync(path.join(worldDir, 'js', 'ui_agent.js'), 'utf8');
  assert.ok(ui.includes('function southMenuTravel()'));
  assert.ok(ui.includes("switchScene('south')"));
  assert.ok(ui.includes('function southMenuCallMeeting()'));
  assert.ok(ui.includes('function closeSouthMenu()'));
  assert.ok(ui.includes('function wireSouthOutletMenu()'));
});

console.log('south map data');

await test('the outlet trigger spans the painted south path on the main map', () => {
  const doors = readJson('door_triggers.json');
  const d = doors.south_outlet;
  assert.ok(d, 'door_triggers.json has a south_outlet rect');
  assert.ok(d.x >= 448 && d.x + d.w <= 536,
    `outlet ${JSON.stringify(d)} must sit on the painted path (native x 448..536)`);
  assert.ok(d.y + d.h <= 384 && d.y > 350, 'outlet touches the south edge');
});

await test('the south grid is mirrored-main sized and the arrival road is open', () => {
  const g = readJson('collision_grid_south.json');
  assert.equal(g.cols, 86);
  assert.equal(g.rows, 48);
  assert.equal(g.cell, 8);
  const gateX0 = Math.floor(evalIn('SOUTH_GATE').x / 2 / g.cell);
  const gateX1 = Math.floor((evalIn('SOUTH_GATE').x + evalIn('SOUTH_GATE').w) / 2 / g.cell);
  for (let x = gateX0; x <= gateX1; x++) {
    assert.equal(g.grid[0][x], 0, `north border must be open at column ${x}`);
    assert.equal(g.grid[4][x], 0, `arrival road must be open at column ${x}`);
  }
});

await test('every south building door is in bounds and differs from the main map', () => {
  const south = readJson('door_triggers_south.json');
  const main = readJson('door_triggers.json');
  const names = Object.keys(south).filter((n) => n !== 'south_outlet');
  assert.ok(names.length >= 9, 'walk-in buildings are present on the south map');
  for (const name of names) {
    const d = south[name];
    assert.ok(d.x >= 0 && d.y >= 0 && d.x + d.w <= 688 && d.y + d.h <= 384, `${name} is in bounds`);
    assert.ok(main[name], `${name} maps back to a main-map building`);
  }
  assert.notDeepEqual(south.pressoffice, main.pressoffice, 'the south map is mirrored, not a copy');
  assert.equal(south.south_outlet, undefined, 'the south map returns through its gate, not the outlet');
});

await test('the arrival spawn stands on walkable south ground', () => {
  const g = readJson('collision_grid_south.json');
  const spawn = evalIn('SCENES.south.spawn');
  const cx = Math.floor((spawn.x + 10) / 2 / g.cell);
  const cy = Math.floor((spawn.y + 8) / 2 / g.cell);
  assert.equal(g.grid[cy][cx], 0, `spawn cell ${cx},${cy} must be walkable`);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
