"""Local, credential-redacted interaction recording for fine-tuning.

Opt-in via TRAINING_CAPTURE_ENABLED in the repo-root .env. Records the
actual request/response pairs that flow through the OpenRouter choke
point (serve._openrouter_chat_post), attributed to the agent and task
that made each call, into a local SQLite DB (WAL) that lives outside
the app's audit pruning. Recording is fail-open: a capture error is
logged as a warning and the agent's work continues. Credentials are
redacted before anything is written -- known .env secret values,
token patterns, private keys, sensitive header fields, binary blobs
and oversized payloads. Oversized payloads are replaced by their
sha256 and byte count, never silently truncated.

Attribution is set at the request/task entry points (/api/chat,
/api/decide, sim._server_content_dispatcher) and read by the choke
point; every OpenRouter POST -- including retries and
reasoning-recovery attempts -- is recorded as its own row. Task
outcomes (status, pipeline and peer-review state) are linked to the
recorded interactions when the state is saved (serve.save_state_to_db).

This module is stdlib-only and imports nothing from world/, so
serve.py and content.py can import it at module level without a cycle.
See datasets/INTERACTION_RECORDING.md for the review/export workflow.
"""
import contextlib
import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CURRENT = contextvars.ContextVar('training_interaction', default=None)
_LOCK = threading.RLock()
_TRACKED = {}
_OUTCOMES = {}
_SECRET = re.compile(r'(?<![\w])(?:github_pat_[\w]+|gh[pousr_][A-Za-z0-9]{20,}|sk-(?:proj-|or-v1-|unsloth-)?[\w-]{16,})|(?i:Bearer\s+)[\w.~-]{12,}')
_ASSIGNMENT = re.compile(r'(?im)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)[\"\']?\s*[=:]\s*[\"\']?[^\s\"\',;]{8,}')
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
_SENSITIVE_KEY = re.compile(r'(?i)^(?:authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|secret|credential)$')

# Story lineage links, innermost first: a task's root story is its
# nearest review/investigation/recovery ancestor. Resolved inline (no
# separate story-tracking module) so this module stays dependency-free.
_LINKS = ('reviewOf', 'investigationOf', 'recoveryOf')


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def config():
    values = {}
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and not key.startswith('#'):
                values[key] = value.strip().strip('\"\'')
    for key in ('TRAINING_CAPTURE_ENABLED', 'TRAINING_CAPTURE_PATH', 'TRAINING_CAPTURE_MAX_BYTES'):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def enabled():
    return config().get('TRAINING_CAPTURE_ENABLED', 'false').lower() in ('1', 'true', 'yes')


def database():
    path = Path(config().get('TRAINING_CAPTURE_PATH', str(ROOT / 'datasets/local/interactions/recordings.sqlite3')))
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=3, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS interactions (
          id TEXT PRIMARY KEY, ts REAL NOT NULL, trace_id TEXT NOT NULL,
          agent_id TEXT, task_id TEXT, group_id TEXT NOT NULL, provider TEXT,
          model TEXT, seed INTEGER, duration_ms INTEGER, ok INTEGER NOT NULL,
          payload TEXT NOT NULL, flags TEXT NOT NULL,
          review_status TEXT NOT NULL DEFAULT 'pending', review_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS interaction_task ON interactions(task_id);
        CREATE INDEX IF NOT EXISTS interaction_time ON interactions(ts);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, trace_id TEXT, task_id TEXT,
          kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_outcomes (
          task_id TEXT PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL
        );
    ''')
    return conn


def sanitize(value):
    secrets = sorted({v for k, v in config().items()
                      if re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', k, re.I) and len(v) >= 8}, key=len, reverse=True)
    flags = set()
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if _SENSITIVE_KEY.fullmatch(str(key)):
                    result[key] = '[REDACTED]'
                    flags.add('credentials_redacted')
                else:
                    result[key] = clean(val)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(x) for x in item]
        if isinstance(item, str):
            original = item
            if item.lstrip().startswith(('{', '[')):
                try:
                    parsed = json.loads(item)
                    cleaned = clean(parsed)
                    if cleaned != parsed:
                        item = json.dumps(cleaned, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
            for secret in secrets:
                item = item.replace(secret, '[REDACTED]')
            item = _PRIVATE_KEY.sub('[REDACTED]', item)
            item = _ASSIGNMENT.sub('[REDACTED]', item)
            item = _SECRET.sub('[REDACTED]', item)
            if item.startswith('data:') and ';base64,' in item:
                flags.add('binary_content_omitted')
                return '[BINARY CONTENT OMITTED]'
            if original != item:
                flags.add('credentials_redacted')
            return item
        if item is None or isinstance(item, (int, float, bool)):
            return item
        return str(item)
    result = clean(value)
    text = json.dumps(result, ensure_ascii=False)
    limit = int(config().get('TRAINING_CAPTURE_MAX_BYTES', 8_000_000))
    if len(text.encode()) > limit:
        flags.add('oversize_payload_omitted')
        result = {'omitted': True, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}
    return result, sorted(flags)


def _root_story(state, task):
    """Walk review/investigation/recovery links to the task's root story."""
    seen = set()
    while isinstance(task, dict) and task.get('id') not in seen:
        seen.add(task.get('id'))
        parent_id = next((task.get(k) for k in _LINKS if task.get(k)), None)
        parent = (state.get('tasks') or {}).get(parent_id) if parent_id else None
        if not isinstance(parent, dict):
            break
        task = parent
    return task if isinstance(task, dict) else {}


def metadata(state, agent_id, task_id=None, purpose=None):
    state = state or {}
    agent = (state.get('agents') or {}).get(agent_id, {})
    task = (state.get('tasks') or {}).get(task_id, {}) if task_id else agent.get('task') or {}
    if isinstance(task, str):
        task = (state.get('tasks') or {}).get(task, {})
    if not isinstance(task, dict):
        task = {}
    root = _root_story(state, task) if task else {}
    return {'agent_id': agent_id, 'role': agent.get('role'),
            'task_id': task_id or task.get('id'),
            'group_id': task.get('requestId') or root.get('id') or task_id or task.get('id'),
            'purpose': purpose, 'task_type': task.get('taskType'),
            'title': task.get('title'),
            'root_story_id': root.get('id') or task.get('rootStoryId'),
            'request_id': task.get('requestId') or root.get('requestId'),
            'review_of': task.get('reviewOf'), 'recovery_of': task.get('recoveryOf')}


@contextlib.contextmanager
def context(info):
    prior = CURRENT.get()
    token = CURRENT.set({**info, 'trace_id': (prior or {}).get('trace_id') or str(uuid.uuid4())})
    try:
        yield
    finally:
        CURRENT.reset(token)


def _warn(error):
    # Never log exception bodies: they may contain provider-supplied secrets.
    logging.getLogger(__name__).warning('Training recording failed (%s); work continues', type(error).__name__)


def record(provider, request, response=None, error=None, duration_ms=0):
    """Record one provider interaction. Fail-open by design."""
    if not enabled():
        return
    info = CURRENT.get() or {'trace_id': str(uuid.uuid4()), 'purpose': 'unattributed'}
    try:
        payload, flags = sanitize({'context': info, 'request': request, 'response': response,
                                   'error': str(error) if error else None})
        if info.get('purpose') in ('health_check', 'diagnostic'):
            flags.append('diagnostic_interaction')
        if info.get('purpose') == 'unattributed':
            flags.append('unattributed_call')
        with _LOCK, database() as conn:
            conn.execute('INSERT INTO interactions (id,ts,trace_id,agent_id,task_id,group_id,provider,model,seed,duration_ms,ok,payload,flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), time.time(), info['trace_id'], info.get('agent_id'), info.get('task_id'),
                 info.get('group_id') or info['trace_id'], provider, (payload.get('request') or {}).get('model'), request.get('seed'),
                 duration_ms, int(error is None and isinstance(response, dict) and not response.get('error') and bool(response.get('choices'))), json.dumps(payload, ensure_ascii=False), json.dumps(flags)))
            db_path = str(conn.execute('PRAGMA database_list').fetchone()[2])
            if db_path not in _TRACKED:
                _TRACKED[db_path] = {row[0] for row in conn.execute('SELECT DISTINCT task_id FROM interactions WHERE task_id IS NOT NULL')}
                _TRACKED[db_path].update(row[0] for row in conn.execute('SELECT task_id FROM task_outcomes'))
            _TRACKED[db_path].update(
                x for x in (info.get('task_id'), info.get('root_story_id'), info.get('review_of')) if x)
    except Exception as exc:
        _warn(exc)


def event(kind, task_id, payload):
    if not enabled():
        return
    try:
        clean, flags = sanitize(payload)
        with _LOCK, database() as conn:
            conn.execute('INSERT INTO events(ts,trace_id,task_id,kind,payload) VALUES (?,?,?,?,?)',
                (time.time(), (CURRENT.get() or {}).get('trace_id'), task_id, kind,
                 json.dumps({'data': clean, 'flags': flags}, ensure_ascii=False)))
    except Exception as error:
        _warn(error)


def sync_outcomes(state):
    """Link current task outcomes to their recorded interactions."""
    if not enabled():
        return
    try:
        with _LOCK, database() as conn:
            path = str(conn.execute('PRAGMA database_list').fetchone()[2])
            if path not in _TRACKED:
                _TRACKED[path] = {row[0] for row in conn.execute('SELECT DISTINCT task_id FROM interactions WHERE task_id IS NOT NULL')}
            for task_id in _TRACKED[path]:
                task = (state.get('tasks') or {}).get(task_id)
                if not task:
                    continue
                fields = {k: task.get(k) for k in ('id','status','rootStoryId','requestId','reviewOf','recoveryOf','pipelineOk','deliveryState','peerReviews','_peerGate','failNote','reviewFailureKind','playerAcceptance','libraryPath','completedAt') if task.get(k) is not None}
                clean, flags = sanitize(fields)
                text = json.dumps({'data': clean, 'flags': flags}, sort_keys=True)
                if _OUTCOMES.get((path, task_id)) == text:
                    continue
                conn.execute('INSERT INTO task_outcomes VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload', (task_id, time.time(), text))
                conn.execute('INSERT INTO events(ts,task_id,kind,payload) VALUES (?,?,?,?)', (time.time(), task_id, 'task_state', text))
                _OUTCOMES[(path, task_id)] = text
    except Exception as error:
        _warn(error)
