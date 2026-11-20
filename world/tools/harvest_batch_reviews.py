#!/usr/bin/env python3
"""Harvest ideabrowser batch reviews from the spike archive into the canonical
library/analysis/ideabrowser-reviews/batch-NN-review.md paths.

Contract gap this closes: the rubric cards demand the review at
library/analysis/ideabrowser-reviews/batch-NN-review.md, but the worker tool
surface only exposes write_spike_report, which auto-archives to
library/archive/<ts>-spike-task-<id>.md. The completion gate therefore refuses
every completed batch card (reason=missing_saved_deliverable) and the repair
loop stops after 2 failures -- the lane output and the gate disagree by
construction. This tool is the operator-side half of the harvest plan: pick
the newest complete batch review per batch number, validate it, and place it
at the canonical path the gate (and the scoreboard) expect.

Validation floors mirror the rubric's sanctioned reality: every idea page is
paywalled, so title-only scoring is the accepted source. A review is
harvestable when it scores >= MIN_IDEAS ideas as table rows, every row
carries a known source label (title-only/viewable/gated) and a rationale.
Files that fail are reported, never copied.

Usage:
  python3 tools/harvest_batch_reviews.py             # harvest ready batches
  python3 tools/harvest_batch_reviews.py --dry-run   # report only, write nothing
"""
import glob
import json
import os
import re
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCHIVE = os.path.join(REPO, 'library', 'archive')
OUT_DIR = os.path.join(REPO, 'library', 'analysis', 'ideabrowser-reviews')
LOG = os.path.join(OUT_DIR, 'harvest-log.json')

MIN_IDEAS = 40
KNOWN_SOURCES = {'title-only', 'viewable', 'gated'}
SOURCE_RE = re.compile(
    r'^\|\s*\d+\s*\|.*\|\s*(title-only|viewable|gated)\s*\|\s*(\S.*)?\s*\|$',
    re.IGNORECASE)
BATCH_RE = re.compile(r'batch[- _]?0?(\d+)', re.IGNORECASE)


def _batch_of(path):
    """The batch number a spike file reviewed (from its header), or None."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            head = fh.read(4096)
    except OSError:
        return None
    m = BATCH_RE.search(head)
    if not m:
        return None
    return int(m.group(1))


def _reviews_per_batch():
    """{batch: [archive paths, newest first]}. Repair re-cards write several
    attempts per batch -- scored reviews and later fix-assessment stubs mix
    freely, so the caller validates down the list instead of trusting the
    newest file blindly."""
    per_batch = {}
    for path in glob.glob(os.path.join(ARCHIVE, '*-spike-task-*.md')):
        batch = _batch_of(path)
        if batch is None:
            continue
        per_batch.setdefault(batch, []).append(path)
    for paths in per_batch.values():
        paths.sort(key=os.path.getmtime, reverse=True)
    return per_batch


def _first_valid(paths):
    """(path, why) of the newest candidate that validates, or (None, why) of
    the newest candidate's failure."""
    first_why = None
    for path in paths:
        ok, why = _validate(path)
        if ok:
            return path, why
        if first_why is None:
            first_why = why
    return None, first_why


def _validate(path):
    """(ok, why): >= MIN_IDEAS scored rows, every row a known source with a
    rationale. A stub or a wrong-lane spike fails here."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            rows = [SOURCE_RE.match(line.strip()) for line in fh]
    except OSError as exc:
        return False, f'unreadable: {exc}'
    rows = [m for m in rows if m]
    if len(rows) < MIN_IDEAS:
        return False, f'only {len(rows)} scored rows (< {MIN_IDEAS})'
    bad_source = [i for i, m in enumerate(rows)
                  if m.group(1).lower() not in KNOWN_SOURCES]
    no_rationale = [i for i, m in enumerate(rows) if not (m.group(2) or '').strip()]
    if bad_source:
        return False, f'{len(bad_source)} rows with unknown source labels'
    if no_rationale:
        return False, f'{len(no_rationale)} rows missing rationale'
    return True, f'{len(rows)} rows, all sources labeled + rationales'


def main():
    dry = '--dry-run' in sys.argv
    os.makedirs(OUT_DIR, exist_ok=True)
    log = {'runs': []}
    if os.path.exists(LOG):
        with open(LOG, encoding='utf-8') as fh:
            log = json.load(fh)
    entry = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             'dry': dry, 'harvested': {}, 'skipped': {}}
    for batch, paths in sorted(_reviews_per_batch().items()):
        src, why = _first_valid(paths)
        rel = os.path.relpath(paths[0], REPO)
        if src is None:
            print(f'batch-{batch:02d}  SKIP    {rel}: {why}')
            entry['skipped'][f'batch-{batch:02d}'] = {'src': rel, 'why': why}
            continue
        dst = os.path.join(OUT_DIR, f'batch-{batch:02d}-review.md')
        src_rel = os.path.relpath(src, REPO)
        print(f'batch-{batch:02d}  {"READY  " if dry else "WROTE  "}'
              f'{src_rel} -> {os.path.relpath(dst, REPO)} ({why})')
        if not dry:
            shutil.copyfile(src, dst)
        entry['harvested'][f'batch-{batch:02d}'] = {'src': src_rel, 'dst': os.path.relpath(dst, REPO)}
    log['runs'].append(entry)
    if not dry:
        with open(LOG, 'w', encoding='utf-8') as fh:
            json.dump(log, fh, indent=2)
    print(f'{len(entry["harvested"])} ready, {len(entry["skipped"])} skipped'
          + ('' if dry else f'; log at {os.path.relpath(LOG, REPO)}'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
