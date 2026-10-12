// Extracts the real `export` statements from every module in the repo and
// regenerates the API section of README.md between markers, so the docs
// can never drift from the code.
import { readFile, writeFile, readdir } from 'node:fs/promises';

const START = '<!-- API:GENERATED START -->';
const END = '<!-- API:GENERATED END -->';

const files = (await readdir('.')).filter((f) => f.endsWith('.js') && f !== 'main.js');

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
  const star = /export\s+\*\s+from\s+['"]([^'"]+)['"]/g;
  while ((m = star.exec(source)) !== null) exports.push(`* (re-export from ${m[1]})`);
  return exports;
}

let section = '\n## Public API\n\nThe list below is generated from the actual `export` statements in each module — it is verified against the sources by `api-docs.test.mjs`.\n\n';
for (const file of files) {
  const source = await readFile(file, 'utf8');
  const names = parseExports(source);
  if (names.length === 0) continue;
  section += `### \`${file}\`\n\n`;
  section += names.map((n) => `- \`${n}\``).join('\n');
  section += '\n\n';
}

const readme = await readFile('README.md', 'utf8');
const startIdx = readme.indexOf(START);
const endIdx = readme.indexOf(END);
let out;
if (startIdx !== -1 && endIdx !== -1) {
  out = readme.slice(0, startIdx + START.length) + '\n' + section + readme.slice(endIdx);
} else {
  out = readme.trimEnd() + '\n\n' + START + '\n' + section + END + '\n';
}
await writeFile('README.md', out);
console.log('README API section regenerated from', files.length, 'modules');
