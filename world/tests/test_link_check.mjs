// Real regression test for the orphaned-file check added to
// runCodingTask() (tasks.js) -- a real fix attempt wrote a genuinely
// correct new .js file (verified against the real running page's own
// property names) that had zero effect anyway, because it was never
// referenced by a <script src> tag in index.html. _extractWrittenJsFiles
// is the pure, testable half of that check: pulling every .js filename a
// heredoc-based shell command actually wrote.
//
// Run: node tests/test_link_check.mjs
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const context = { console, Math, Date, JSON, Array, Object, Set, RegExp };
vm.createContext(context);

// tasks.js references several globals from other files at parse time only
// inside function bodies, never at top level, so loading it alone is safe
// for testing a single pure function -- same reasoning as test_pathfinding.mjs.
const src = fs.readFileSync(path.join(worldDir, 'tasks.js'), 'utf8');
vm.runInContext(src, context, { filename: 'tasks.js' });

const extractWrittenJsFiles = (...args) => vm.runInContext('_extractWrittenJsFiles', context)(...args);

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

console.log('_extractWrittenJsFiles (tasks.js)');

test('finds a single file written with cat >', () => {
  const command = "cat > fix-thing.js << 'EOF'\nconsole.log('hi');\nEOF";
  assert.equal(JSON.stringify(extractWrittenJsFiles(command)), JSON.stringify(['fix-thing.js']));
});

test('finds a file appended with cat >>', () => {
  const command = "cat >> fix-thing.js << 'EOF'\nmore();\nEOF";
  assert.equal(JSON.stringify(extractWrittenJsFiles(command)), JSON.stringify(['fix-thing.js']));
});

test('finds multiple distinct .js files in one command', () => {
  const command = "cat > a.js << 'EOF'\nfoo();\nEOF\n\ncat > b.js << 'EOF'\nbar();\nEOF";
  assert.equal(JSON.stringify(extractWrittenJsFiles(command)), JSON.stringify(['a.js', 'b.js']));
});

test('does not report the same file twice', () => {
  const command = "cat > a.js << 'EOF'\nfoo();\nEOF\n\ncat >> a.js << 'EOF'\nbar();\nEOF";
  assert.equal(JSON.stringify(extractWrittenJsFiles(command)), JSON.stringify(['a.js']));
});

test('ignores non-.js heredoc targets (index.html, .py, .md)', () => {
  const command = "cat >> index.html << 'EOF'\n<script src=\"a.js\"></script>\nEOF\n\ncat > notes.md << 'EOF'\nhi\nEOF";
  assert.equal(JSON.stringify(extractWrittenJsFiles(command)), JSON.stringify([]));
});

test('a command that writes no files returns an empty array', () => {
  assert.equal(JSON.stringify(extractWrittenJsFiles('echo hello')), JSON.stringify([]));
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
