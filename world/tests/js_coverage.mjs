// JS coverage harness: runs every tests/test_*.mjs in a fresh NODE_V8_COVERAGE
// dir, attributes line coverage per coverage dump (which include files loaded
// through node:vm) using smallest-range-wins, then ORs the covered line sets
// across dumps and prints per-file line coverage for the world/*.js sources.
//
// Attribution is done per dump (per test-file process) rather than by merging
// all dumps first: V8 emits different range granularities for the same function
// depending on how it was optimized during that run, so range arrays from
// different dumps do not line up by index and naive merging mis-reports whole
// branches as uncovered when a count-0 block from one dump shadows a positive
// range from another. Per-dump attribution + OR keeps each dump's own
// granularity consistent.
//
// Usage: node tests/js_coverage.mjs
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import os from 'node:os';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const worldDir = path.join(__dirname, '..');

const covRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'js-cov-'));

const testFiles = fs.readdirSync(__dirname)
  .filter((f) => /^test_.*\.mjs$/.test(f) && f !== 'js_coverage.mjs')
  .sort();

// srcPath -> Set of covered line numbers, OR-ed across every dump.
const coveredByFile = new Map();

for (const tf of testFiles) {
  const covDir = path.join(covRoot, tf);
  fs.mkdirSync(covDir, { recursive: true });
  const r = spawnSync(process.execPath, [path.join(__dirname, tf)], {
    cwd: worldDir,
    env: { ...process.env, NODE_V8_COVERAGE: covDir },
    encoding: 'utf8',
    timeout: 180000,
  });
  if (r.status !== 0) console.error(`  [${r.status}] ${tf}`);
  const jsons = fs.readdirSync(covDir).filter((f) => f.endsWith('.json'));
  for (const jf of jsons) {
    const dump = JSON.parse(fs.readFileSync(path.join(covDir, jf), 'utf8'));
    for (const entry of dump.result ?? []) {
      const url = entry.url;
      if (!url || url.startsWith('node:internal') || url.startsWith('node:vm')) continue;
      const srcPath = sourceFor(url);
      if (!srcPath.endsWith('.js') && !srcPath.endsWith('.mjs')) continue;
      let src;
      try { src = fs.readFileSync(srcPath, 'utf8'); } catch { continue; }
      const lines = src.split('\n');
      const lineOffsets = [];
      let off = 0;
      for (const ln of lines) { lineOffsets.push(off); off += ln.length + 1; }
      // V8 block-coverage artifact for code compiled via vm.runInContext: when a
      // function contains nested functions, the outer body's block ranges are
      // emitted with count 0 even though the function genuinely ran (the
      // function-level range carries the real count) -- see tasks.js's
      // arriveAtTask/assignBigTask/queueWork etc. Treating those zero-count
      // blocks as "never executed" would mis-report whole functions as
      // uncovered no matter how well they're tested, and the plain
      // "smallest range wins" attribution below would pick them over the
      // executed function range. A genuinely partially-executed function is
      // distinguishable: V8 counts the EXECUTED branch blocks >0 (blockedAt in
      // world.js does this), so a function whose function-level range has
      // count > 0 while EVERY other block range is 0 is the artifact, not a
      // real unexecuted branch -- fall back to its function-level range.
      const allRanges = [];
      for (const fn of entry.functions ?? []) {
        const rs = fn.ranges.map((r) => ({ s: r.startOffset, e: r.endOffset, c: r.count }));
        if (fn.isBlockCoverage && rs.length > 1) {
          let max = rs[0];
          for (const r of rs) if (r.e - r.s > max.e - max.s) max = r;
          if (max.c > 0 && rs.every((r) => r.c === 0 || r === max)) {
            allRanges.push(max);
            continue;
          }
        }
        for (const r of rs) allRanges.push(r);
      }
      // Dedupe identical (s,e) ranges keeping the max count.
      const seen = new Map();
      for (const r of allRanges) {
        const k = r.s + ':' + r.e;
        if (!seen.has(k) || seen.get(k).c < r.c) seen.set(k, r);
      }
      const deduped = [...seen.values()];
      const covered = new Set();
      for (let i = 1; i <= lines.length; i++) {
        const pos = lineOffsets[i - 1];
        const candidates = deduped.filter((r) => pos >= r.s && pos < r.e);
        if (!candidates.length) continue;
        candidates.sort((a, b) => (a.e - a.s) - (b.e - b.s));
        if (candidates[0].c > 0) covered.add(i);
      }
      if (!coveredByFile.has(srcPath)) coveredByFile.set(srcPath, new Set());
      for (const ln of covered) coveredByFile.get(srcPath).add(ln);
    }
  }
}

function sourceFor(url) {
  if (url.startsWith('file://')) return path.normalize(decodeURIComponent(new URL(url).pathname));
  if (url.startsWith('/')) return url;
  const base = path.basename(url);
  if (base.endsWith('.js')) return path.join(worldDir, base);
  if (base.endsWith('.mjs')) return path.join(worldDir, 'tests', base);
  if (base.endsWith('.html')) return path.join(worldDir, base);
  return url;
}

const report = [];
for (const [srcPath, covered] of coveredByFile) {
  const src = fs.readFileSync(srcPath, 'utf8');
  const lines = src.split('\n');
  let hit = 0, miss = 0;
  const missing = [];
  for (let i = 1; i <= lines.length; i++) {
    const t = lines[i - 1].trim();
    if (!t || t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) continue;
    if (covered.has(i)) hit++;
    else { miss++; missing.push(i); }
  }
  report.push({ url: path.basename(srcPath), pct: Math.round((100 * hit) / Math.max(1, hit + miss)), hit, miss, missing: missing.slice(0, 60) });
}

report.sort((a, b) => a.pct - b.pct);
for (const r of report) {
  console.log(`${String(r.pct).padStart(4)}%  ${r.hit}/${r.hit + r.miss}  ${r.url}`);
  if (r.missing.length) console.log(`       missing: ${r.missing.join(', ')}`);
}