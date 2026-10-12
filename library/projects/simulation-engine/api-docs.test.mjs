// Ensures README.md documents every real export in the codebase, so the
// public-API docs cannot drift from the code.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';

function parseExports(source) {
  const exports = [];
  const re = /export\s+(?:const|function|class|let|var)\s+([A-Za-z_$][\w$]*)/g;
  let m;
  while ((m = re.exec(source)) !== null) exports.push(m[1]);
  const brace = /export\s*\{([^}]*)\}/g;
  while ((m = brace.exec(source)) !== null) {
    for (const part of m[1].split(',')) {
      const name = part.trim().split(/\s+as\s+/).pop().trim();
      if (name) exports.push(name);
    }
  }
  return exports;
}

test('README documents every exported symbol', async () => {
  const readme = await readFile('README.md', 'utf8');
  assert.ok(readme.includes('<!-- API:GENERATED START -->'), 'README must contain generated API markers');
  assert.ok(readme.includes('<!-- API:GENERATED END -->'), 'README must contain generated API markers');
  const files = (await readdir('.')).filter((f) => f.endsWith('.js') && f !== 'main.js');
  for (const file of files) {
    const names = parseExports(await readFile(file, 'utf8'));
    if (names.length === 0) continue;
    assert.ok(readme.includes(`\`${file}\``), `README should have a section for ${file}`);
    for (const name of names) {
      assert.ok(readme.includes(`\`${name}\``), `README should document ${name} from ${file}`);
    }
  }
});
