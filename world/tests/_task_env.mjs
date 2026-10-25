// Shared vm harness for the tasks.js-family JavaScript tests.
//
// The task system is classic-script JavaScript (globals in one shared scope),
// so every test loads the modules into a single vm context and talks to them
// through setGlobal/getGlobal/call. Keeping that here means the individual
// test files stay focused and under the 1,000-line script limit.
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const worldDir = path.join(__dirname, '..');
export const TASK_SCRIPTS = ['tasks.js', 'task_movement.js', 'task_coding.js', 'task_delivery.js'];

export const context = { console, Math, Date, JSON, Array, Object, Set, Promise, setTimeout, clearTimeout };
vm.createContext(context);
for (const file of TASK_SCRIPTS) {
  vm.runInContext(fs.readFileSync(path.join(worldDir, 'js', file), 'utf8'), context, { filename: file });
}

export { vm, assert };

export function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}

export function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}

export const call = (name, ...a) => vm.runInContext(name, context)(...a);

export let passed = 0;
export let failed = 0;

export async function test(name, fn) {
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

export function summary() {
  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

// Snapshot real implementations this file intentionally stubs in one section
// and wants back in another. setGlobal overwrites live globals permanently.
export function snapshotFns(names) {
  const snapshot = {};
  for (const name of names) snapshot[name] = getGlobal(name);
  return snapshot;
}

export function restoreFns(snapshot, ...names) {
  for (const name of names) setGlobal(name, snapshot[name]);
}

// Real implementations captured BEFORE installBaselineGlobals stubs them,
// so a test that needs the genuine function can restore it explicitly.
export const REAL = {};
for (const name of [
  'sendAgentOffDuty', 'attemptHandoff', 'writeLibraryFile', 'readLibraryFile',
  'listLibraryFiles', 'pickFreeSpot', 'overlaps', 'blockedAt', 'agentBlockedAt',
  '_resolveRoomWithOverflow', 'pickModelTierForAction', 'gatherUnifiedContext',
  'fetchPageSmart', 'requestPageProbe', 'reviewScreenshot',
]) {
  // Some of these live in other browser files that this harness does not
  // load; only capture what the task modules actually define.
  try { REAL[name] = getGlobal(name); } catch { REAL[name] = undefined; }
}

// Baseline globals every task area needs (overridden per test as needed).
export function installBaselineGlobals() {
  setGlobal('DEDICATED_PROJECT_ROLES', new Set());
  setGlobal('MAX_ACTIVE_AGENTS', 1000);
  setGlobal('AGENT_W', 20);
  setGlobal('AGENT_H', 16);
  setGlobal('ROOMS', { pressoffice: { collision: 'pressoffice' } });
  setGlobal('ROOM_INTERACTABLES', {});
  setGlobal('ROOM_NATIVE_W', 200);
  setGlobal('ROOM_NATIVE_H', 200);
  setGlobal('ROOM_DOOR_TRIGGERS', {
    pressoffice: { x: 100, y: 100, w: 16, h: 16 },
    observatory: { x: 200, y: 200, w: 16, h: 16 },
    library: { x: 300, y: 300, w: 16, h: 16 },
    media: { x: 400, y: 400, w: 16, h: 16 },
  });
  setGlobal('WORKROOM_SANDBOX_ID', 'workroom-shared');
  setGlobal('RESEARCH_SANDBOX_ID', 'research-shared');
  setGlobal('MODEL_TIERS', { small: { slug: 'test/small' }, mid: { slug: 'test/mid' }, planning: { slug: 'test/planning' } });
  setGlobal('agentMission', (def) => (def && def.profile && def.profile.mission) || 'general think tank work');
  setGlobal('SKILL_FILE_FORMAT_GUIDE', 'Format guide.');
  setGlobal('overlaps', () => false);
  setGlobal('blockedAt', () => false);
  setGlobal('agentBlockedAt', () => false);
  setGlobal('pickFreeSpot', () => ({ x: 500, y: 500 }));
  setGlobal('_resolveRoomWithOverflow', (room) => room);
  setGlobal('logThinkTankAction', () => {});
  setGlobal('showToast', () => {});
  setGlobal('writeLibraryFile', async () => {});
  setGlobal('readLibraryFile', async () => null);
  setGlobal('listLibraryFiles', async () => []);
  setGlobal('promoteLibraryFile', async () => true);
  setGlobal('rejectLibraryFile', async () => true);
  setGlobal('writeSkillFile', async () => {});
  setGlobal('attemptHandoff', async () => false);
  setGlobal('sendAgentOffDuty', () => {});
  setGlobal('pickModelTierForAction', async () => ({ slug: 'test/model', label: 'test' }));
  setGlobal('requestPageProbe', async () => ({ ok: true, results: {} }));
  setGlobal('formatPageProbeResult', (data) => 'PROBE: ' + JSON.stringify(data));
  setGlobal('reviewScreenshot', async () => ({ ok: true, review: 'looks fine' }));
  setGlobal('gatherUnifiedContext', async () => 'CURRENT CONTEXT');
  setGlobal('fetchPageSmart', async () => ({ allowed: true, text: 'feed page text' }));
  setGlobal('readWorkingGuide', async () => null);
  setGlobal('GRADE_MEETS', 'meets_requirement');
  setGlobal('GRADE_FAILS', 'fails_requirement');
  setGlobal('GRADE_UNSURE', 'insufficient_evidence');
  setGlobal('MAX_REVISION_ROUNDS', 2);
}
