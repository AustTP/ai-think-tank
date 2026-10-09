"""Content-addressed validation reuse and concise repair context."""
import copy
import hashlib
import json
import os
import threading
import time

_LOCK = threading.Lock()
_CHECKS = {}
_DOCS = {}
_CHECK_LOCKS = {}
SKIP = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.venv', 'node_modules', 'htmlcov', '.cache'}


def validation_lock(sandbox_id):
    with _LOCK:
        return _CHECK_LOCKS.setdefault(sandbox_id, threading.Lock())


def tree_digest(path):
    if not os.path.isdir(path):
        return None
    h = hashlib.sha256()
    count = 0
    total = 0
    for directory, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            if name in ('.coverage',) or name.endswith(('.pyc', '.log')):
                continue
            target = os.path.join(directory, name)
            if os.path.islink(target):
                return None
            try:
                total += os.path.getsize(target)
                if total > 100 * 1024 * 1024 or count >= 5000:
                    return None
                h.update(os.path.relpath(target, path).encode())
                with open(target, 'rb') as stream:
                    for chunk in iter(lambda: stream.read(65536), b''):
                        h.update(chunk)
                count += 1
            except OSError:
                return None
    return h.hexdigest() if count else None


def check_key(path, steps, runtime):
    digest = tree_digest(path)
    return hashlib.sha256(json.dumps([digest, steps, runtime], sort_keys=True).encode()).hexdigest() if digest else None


def get_check(key):
    with _LOCK:
        entry = _CHECKS.get(key)
        if not entry or time.time() - entry['at'] > 3600:
            return None
        result = copy.deepcopy(entry['result'])
        result.update(cached=True, checkedAt=entry['at'])
        return result


def save_check(key, result):
    if key and result.get('ok'):
        with _LOCK:
            if len(_CHECKS) >= 128:
                _CHECKS.clear()
            _CHECKS[key] = {'at': time.time(), 'result': copy.deepcopy(result)}


def compact(task, limit=7000):
    contract = task.get('reviewContract') or {}
    original = contract.get('instructions') or str(task.get('instructions') or '').split('\n\nHuman guidance:', 1)[0].split('\nPeer-review findings:', 1)[0]
    original = original.split('Story:', 1)[-1]
    guidance = str(task.get('instructions') or '').rsplit('\n\nHuman guidance:', 1)
    correction = str(task.get('currentFailure') or task.get('failNote') or '')
    if not correction and '\nPeer-review findings:' in str(task.get('instructions') or ''):
        correction = str(task['instructions']).split('\nPeer-review findings:',1)[1].split('\nExisting Library document:',1)[0]
    if len(guidance) == 2:
        correction += '\nLatest human guidance: ' + guidance[-1][-1800:]
    # Keep the contract exact. Compact feedback/history, never required clauses.
    pieces = ['Original contract:\n' + original,
              'Acceptance criteria:\n' + str(contract.get('acceptanceCriteria') or task.get('acceptanceCriteria') or ''),
              'Current correction:\n' + correction[:2500],
              'Saved evidence: ' + str(task.get('libraryPath') or 'none')]
    return '\n\n'.join(pieces)


def doc_get(key):
    with _LOCK:
        entry = _DOCS.get(key)
        return copy.deepcopy(entry['result']) if entry and time.time() - entry['at'] < 21600 else None


def doc_save(key, result):
    with _LOCK:
        if len(_DOCS) >= 128:
            _DOCS.clear()
        _DOCS[key] = {'at': time.time(), 'result': copy.deepcopy(result)}
