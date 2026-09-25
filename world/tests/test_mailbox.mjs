// Real regression tests for mailbox read/unread tracking (agents.js).
// Added after a real gap was found: mailbox entries used to be plain
// strings with no read/unread concept, so the HUD's "unread" count (its
// own tooltip's word, not this test's) actually summed EVERY message ever
// received, forever, and nothing an agent or the player did ever cleared
// it. Loads the real agents.js into a vm context rather than
// re-implementing sendMail/markMailRead here, same reasoning as
// test_pathfinding.mjs.
//
// Run: node tests/test_mailbox.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object };
vm.createContext(context);

// agents.js calls logVillageAction() (world.js) from sendMail() -- stub it
// rather than pulling in the whole world.js network stack, since these
// tests only care about mailbox state, not action-log side effects.
vm.runInContext('function logVillageAction() {}', context);

const src = fs.readFileSync(path.join(worldDir, 'agents.js'), 'utf8');
vm.runInContext(src, context, { filename: 'agents.js' });

function setGlobal(name, value) {
  context['__inject_' + name] = value;
  vm.runInContext(`${name} = __inject_${name};`, context);
}
function getGlobal(name) {
  vm.runInContext(`__extract_${name} = ${name};`, context);
  return context['__extract_' + name];
}
const sendMail = (...args) => vm.runInContext('sendMail', context)(...args);
const markMailRead = (...args) => vm.runInContext('markMailRead', context)(...args);

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

function freshAgents() {
  return {
    sam: { id: 'sam', name: 'Sam', mailbox: [] },
    faye: { id: 'faye', name: 'Faye', mailbox: [] },
  };
}

console.log('mailbox read/unread tracking (agents.js)');

test('sendMail: new message starts unread', () => {
  setGlobal('AGENTS', freshAgents());
  sendMail('faye', 'sam', 'please review the fix');
  const inbox = getGlobal('AGENTS').sam.mailbox;
  assert.equal(inbox.length, 1);
  assert.equal(inbox[0].read, false);
  assert.ok(inbox[0].text.includes('please review the fix'));
});

test('sendMail: unread count reflects only unread messages, not total history', () => {
  setGlobal('AGENTS', freshAgents());
  sendMail('faye', 'sam', 'first');
  sendMail('faye', 'sam', 'second');
  markMailRead('sam');
  sendMail('faye', 'sam', 'third');
  const inbox = getGlobal('AGENTS').sam.mailbox;
  const unread = inbox.filter(m => !m.read).length;
  assert.equal(inbox.length, 3, 'all three messages should still be in the inbox');
  assert.equal(unread, 1, 'only the message sent after the last check should read as unread');
});

test('markMailRead: marks every current message read, does not empty the inbox', () => {
  setGlobal('AGENTS', freshAgents());
  sendMail('faye', 'sam', 'one');
  sendMail('faye', 'sam', 'two');
  markMailRead('sam');
  const inbox = getGlobal('AGENTS').sam.mailbox;
  assert.equal(inbox.length, 2, 'checking mail must not delete messages');
  assert.ok(inbox.every(m => m.read === true), 'every message present at check time should be marked read');
});

test('markMailRead: unknown agent id is a no-op, does not throw', () => {
  setGlobal('AGENTS', freshAgents());
  assert.doesNotThrow(() => markMailRead('nobody'));
});

test('markMailRead: normalizes legacy plain-string entries (pre-migration saved state) without throwing', () => {
  const agents = freshAgents();
  agents.sam.mailbox = ['Old-format message from before read tracking existed'];
  setGlobal('AGENTS', agents);
  markMailRead('sam');
  const inbox = getGlobal('AGENTS').sam.mailbox;
  assert.equal(inbox[0].read, true);
  assert.equal(inbox[0].text, 'Old-format message from before read tracking existed');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
