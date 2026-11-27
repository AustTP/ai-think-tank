"""Shared findings buffer (2026-11-27), from arXiv 2610.31355
("Muffin - A Shared Memory Buffer for Asynchronous AI Agents", UTN
Nuremberg).

The paper's mechanism, applied at orchestration level:

  - every agent that finishes REAL work on a request drops ONE finding
    (what it learned, not its trace) into an append-only buffer keyed by
    the request's root story;
  - the next dispatch on that request reads the k most recent findings
    from OTHER agents only, so peers build on what landed instead of
    re-deriving it (the paper's 73.9% token cut vs full-trace sharing);
  - early findings matter most (the paper's highest per-share
    answer-change rate happens at the first transition), so the buffer is
    read at EVERY dispatch, not only at the end;
  - the buffer is plain JSONL on disk -- inspectable, no locks, no
    coordination, tolerant of concurrent appends from the per-dispatch
    threads.

Scope caveat the paper itself states: it validates single-shot reasoning
benchmarks, not long-horizon tool use. Here the buffer is a hint channel,
never an authority: best-effort on both write and read, and a buffer
failure can never break a dispatch or a result merge. Peer-review verdicts
and code gates still gate deliverables exactly as before.
"""
import json
import os
import re
import time

_DIR_NAME = 'findings'
_MAX_FINDING_CHARS = 300      # one finding: dense conclusion, not a trace
_MAX_FILE_ENTRIES = 200       # rewrite cap; keep the newest entries
_MAX_FILE_BYTES = 256 * 1024
_DEFAULT_K = 8                # paper: more context than ~k=10 degrades small models


def enabled():
    """Kill switch: FINDINGS_BUFFER=off disables reads AND writes.
    .env flags in this codebase are read through serve._load_env() (a dict;
    os.environ is NOT populated from .env), so serve is imported lazily and
    the feature defaults to ON when the env cannot be read."""
    try:
        import serve
        return str(serve._load_env().get('FINDINGS_BUFFER', 'on')).strip().lower() != 'off'
    except Exception:
        return True


def _root_key(state, task):
    """The buffer key: the request the task ultimately serves.

    Walks the review/investigation/recovery lineage to the root story and
    prefers its work order id, so every child (review, fix, QA) of one
    request shares one buffer -- review N sees review N-1's finding. Falls
    back to the root story id, then the task id. Never mutates the task
    (unlike story_tracking.stamp) and never raises."""
    try:
        import agents.story_tracking as story_tracking
        parent = story_tracking.root(state, task) if isinstance(state, dict) else {}
        candidates = [parent, task] if parent else [task]
        for src in candidates:
            if not isinstance(src, dict):
                continue
            rid = (src.get('workOrderId') or src.get('requestId')
                   or (src.get('reviewContract') or {}).get('parentWorkOrderId'))
            if rid:
                return str(rid)
        if parent and parent.get('id'):
            return str(parent['id'])
    except Exception:
        pass
    return str((task or {}).get('id') or 'unknown')


def _key_slug(key):
    slug = re.sub(r'[^A-Za-z0-9_.-]', '_', str(key or 'unknown')).strip('._')
    return slug[:120] or 'unknown'


def path(state, task):
    return os.path.join(serve_library(), _DIR_NAME,
                        _key_slug(_root_key(state, task)) + '.jsonl')


def serve_library():
    try:
        import serve
        return serve.LIBRARY_DIR
    except Exception:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'library')


def _read_entries(path):
    entries = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    entries.append(e)
    except OSError:
        pass
    return entries


def _trim_file(path, entries):
    """Bounded file: when it outgrows the entry/byte caps, rewrite keeping
    only the newest half. Appends are what make it grow; the rewrite is the
    only write that touches existing bytes."""
    try:
        if len(entries) <= _MAX_FILE_ENTRIES // 2:
            return
        with open(path, 'w', encoding='utf-8') as f:
            for e in entries[-(_MAX_FILE_ENTRIES // 2):]:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
    except OSError:
        pass


def append(state, agent_id, task, note, library_path=None):
    """Drop ONE finding for a completed run. Deduplicates by task id (one
    finding per run, even if the run's result is merged twice) and skips
    empty notes. Returns True when a finding landed. Never raises."""
    if not enabled():
        return False
    try:
        text = (note or '').strip()
        if not text or not isinstance(agent_id, str) or not agent_id:
            return False
        target = path(state, task)
        task_id = str((task or {}).get('id') or 'unknown')
        entries = _read_entries(target)
        if any(e.get('task') == task_id for e in entries):
            return False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        entry = {'ts': time.time(),
                 'agent': agent_id,
                 'task': task_id,
                 'finding': text[:_MAX_FINDING_CHARS]}
        if library_path:
            entry['libraryPath'] = str(library_path)
        entries.append(entry)
        with open(target, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        if (len(entries) > _MAX_FILE_ENTRIES
                or os.path.getsize(target) > _MAX_FILE_BYTES):
            _trim_file(target, entries)
        return True
    except Exception:
        return False


def recent(state, agent_id, task, k=_DEFAULT_K):
    """The k most recent findings from OTHER agents on this request,
    oldest-first. Own entries are skipped: the agent does not need its own
    words quoted back to it. Returns [] on any failure."""
    if not enabled():
        return []
    try:
        entries = _read_entries(path(state, task))
    except Exception:
        return []
    mine = str(agent_id or '')
    picked = []
    for e in reversed(entries):
        if e.get('agent') == mine:
            continue
        picked.append(e)
        if len(picked) >= max(1, int(k or 1)):
            break
    picked.reverse()
    return picked


def digest(entries):
    """Render entries as the compact markdown block the dispatcher folds
    into the perception string (base_ctx is a STRING by contract)."""
    rows = []
    for e in entries or []:
        agent = str(e.get('agent') or '?')
        finding = str(e.get('finding') or '').replace('\n', ' ')
        tail = f' (file: {e["libraryPath"]})' if e.get('libraryPath') else ''
        rows.append(f'- [{agent}] {finding}{tail}')
    return '\n'.join(rows)
