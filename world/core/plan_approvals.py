"""Durable plan proposals with revisions and an explicit approval gate.

A large request is planned before anything is filed or staffed. The plan is
stored in SQLite as a chain of immutable revisions: a pending revision can be
approved, rejected with a reason, or superseded by a revised plan. Approval
binds the exact revision hash, so changing the goal, route, constraints, or
payload after approval cannot silently inherit the decision.

Nothing here executes work. The caller approves a revision and then applies
the stored route itself, once, using the approved hash.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TTL_S = 86400
MIN_TTL_S = 300
MAX_TTL_S = 604800
MAX_VERSIONS = 20
MAX_REASON = 1000


class PlanError(Exception):
    def __init__(self, message: str, code: str = 'invalid'):
        super().__init__(message)
        self.code = code


def _db_path() -> str:
    return (os.environ.get('PLAN_APPROVALS_DB')
            or os.environ.get('CONFIRMATIONS_DB')
            or os.environ.get('THINK_TANK_DB')
            or str(ROOT / 'think_tank.db'))


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS plan_revisions (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        goal TEXT NOT NULL,
        constraints_json TEXT NOT NULL,
        route TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        decided_by TEXT,
        decided_at REAL,
        reason TEXT,
        applied_at REAL,
        expires_at REAL NOT NULL,
        UNIQUE(plan_id, version)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_plan_revisions_pending '
                 'ON plan_revisions (status, created_at)')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _clean_json(value, label, default):
    if value is None:
        value = default
    try:
        _canonical(value)
    except (TypeError, ValueError) as error:
        raise PlanError(f'{label} is not JSON-serializable') from error
    return value


def plan_hash(goal: str, route: str, constraints, payload) -> str:
    raw = '\n'.join((str(goal or ''), str(route or ''),
                     _canonical(constraints or {}), _canonical(payload or {})))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _row(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item['constraints'] = json.loads(item.pop('constraints_json'))
        item['payload'] = json.loads(item.pop('payload_json'))
    except Exception:
        item['constraints'] = None
        item['payload'] = None
    return item


def _ttl(ttl_s) -> int:
    try:
        ttl = int(ttl_s or DEFAULT_TTL_S)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    return max(MIN_TTL_S, min(MAX_TTL_S, ttl))


def _insert(conn, plan_id, version, goal, route, constraints, payload, actor, ttl_s):
    goal = str(goal or '').strip()
    route = str(route or '').strip()
    actor = str(actor or '').strip()
    if not goal or not route or not actor:
        raise PlanError('goal, route and actor are required')
    constraints = _clean_json(constraints, 'constraints', {})
    payload = _clean_json(payload, 'payload', {})
    digest = plan_hash(goal, route, constraints, payload)
    now = time.time()
    revision_id = f'{plan_id}:v{int(version)}'
    conn.execute(
        '''INSERT INTO plan_revisions
           (id, plan_id, version, goal, constraints_json, route, payload_json,
            plan_hash, status, created_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)''',
        (revision_id, plan_id, int(version), goal, _canonical(constraints),
         route, _canonical(payload), digest, actor, now, now + _ttl(ttl_s)))
    return _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                             (revision_id,)).fetchone())


def propose(goal, route, payload, actor, constraints=None, ttl_s=DEFAULT_TTL_S) -> dict:
    plan_id = 'plan_' + secrets.token_hex(12)
    with _connect() as conn:
        item = _insert(conn, plan_id, 1, goal, route, constraints, payload,
                       actor, ttl_s)
    assert item is not None
    return item


def get(plan_id: str, version: int | None = None) -> dict | None:
    with _connect() as conn:
        if version is None:
            row = conn.execute(
                'SELECT * FROM plan_revisions WHERE plan_id = ? '
                'ORDER BY version DESC LIMIT 1', (str(plan_id or ''),)).fetchone()
        else:
            row = conn.execute(
                'SELECT * FROM plan_revisions WHERE plan_id = ? AND version = ?',
                (str(plan_id or ''), int(version))).fetchone()
        return _row(row)


def _latest(conn, plan_id: str) -> dict | None:
    row = conn.execute(
        'SELECT * FROM plan_revisions WHERE plan_id = ? '
        'ORDER BY version DESC LIMIT 1', (str(plan_id or ''),)).fetchone()
    return _row(row)


def _expire_conn(conn) -> int:
    cur = conn.execute(
        "UPDATE plan_revisions SET status='expired' "
        "WHERE status='pending' AND expires_at <= ?", (time.time(),))
    return cur.rowcount


def expire_stale() -> int:
    with _connect() as conn:
        return _expire_conn(conn)


def _decision(row, action, actor, reason=None):
    if row is None:
        raise PlanError('plan not found', 'not_found')
    if row['status'] == action:
        return row
    if row['status'] != 'pending':
        raise PlanError(f"plan is {row['status']}", row['status'])
    if row['expires_at'] <= time.time():
        with _connect() as conn:
            conn.execute("UPDATE plan_revisions SET status='expired' "
                         "WHERE id=? AND status='pending'", (row['id'],))
        raise PlanError('plan expired', 'expired')
    return row


def approve(plan_id: str, version: int, actor: str) -> dict:
    row = get(plan_id, version)
    row = _decision(row, 'approved', actor)
    if row['status'] == 'approved':
        return row
    with _connect() as conn:
        conn.execute("UPDATE plan_revisions SET status='approved', decided_by=?, "
                     "decided_at=? WHERE id=? AND status='pending'",
                     (str(actor or 'player'), time.time(), row['id']))
        updated = _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                                    (row['id'],)).fetchone())
    assert updated is not None
    return updated


def reject(plan_id: str, version: int, actor: str, reason: str) -> dict:
    reason = str(reason or '').strip()
    if not reason:
        raise PlanError('a rejection reason is required')
    row = get(plan_id, version)
    row = _decision(row, 'rejected', actor)
    if row['status'] == 'rejected':
        return row
    with _connect() as conn:
        conn.execute("UPDATE plan_revisions SET status='rejected', decided_by=?, "
                     "decided_at=?, reason=? WHERE id=? AND status='pending'",
                     (str(actor or 'player'), time.time(),
                      reason[:MAX_REASON], row['id']))
        updated = _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                                    (row['id'],)).fetchone())
    assert updated is not None
    return updated


def revise(plan_id: str, goal, route, payload, actor,
           constraints=None, ttl_s=DEFAULT_TTL_S) -> dict:
    with _connect() as conn:
        latest = _latest(conn, plan_id)
        if latest is None:
            raise PlanError('plan not found', 'not_found')
        _expire_conn(conn)
        if latest['status'] not in ('pending', 'rejected', 'failed'):
            raise PlanError(f"plan is {latest['status']}", latest['status'])
        if latest['version'] >= MAX_VERSIONS:
            raise PlanError('too many plan revisions', 'versions')
        if latest['status'] == 'pending':
            conn.execute("UPDATE plan_revisions SET status='superseded' "
                         "WHERE id=? AND status='pending'", (latest['id'],))
        item = _insert(conn, latest['plan_id'], latest['version'] + 1, goal,
                       route, constraints, payload, actor, ttl_s)
    assert item is not None
    return item


def claim_apply(plan_id: str, version: int, actor='player') -> dict:
    row = get(plan_id, version)
    if row is None:
        raise PlanError('plan not found', 'not_found')
    if row['status'] in ('applying', 'applied'):
        return row
    if row['status'] != 'approved':
        raise PlanError(f"plan is {row['status']}", row['status'])
    with _connect() as conn:
        conn.execute("UPDATE plan_revisions SET status='applying', decided_by=COALESCE(decided_by, ?) "
                     "WHERE id=? AND status='approved'",
                     (str(actor or 'player'), row['id']))
        updated = _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                                    (row['id'],)).fetchone())
    assert updated is not None
    return updated


def mark_failed(plan_id: str, version: int, reason: str) -> dict:
    row = get(plan_id, version)
    if row is None:
        raise PlanError('plan not found', 'not_found')
    if row['status'] == 'failed':
        return row
    if row['status'] != 'applying':
        raise PlanError(f"plan is {row['status']}", row['status'])
    with _connect() as conn:
        conn.execute("UPDATE plan_revisions SET status='failed', reason=? WHERE id=?",
                     (str(reason or 'apply failed')[:MAX_REASON], row['id']))
        updated = _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                                    (row['id'],)).fetchone())
    assert updated is not None
    return updated


def mark_applied(plan_id: str, version: int, actor='player') -> dict:
    row = get(plan_id, version)
    if row is None:
        raise PlanError('plan not found', 'not_found')
    if row['status'] == 'applied':
        return row
    if row['status'] not in ('applying', 'approved'):
        raise PlanError(f"plan is {row['status']}", row['status'])
    with _connect() as conn:
        conn.execute("UPDATE plan_revisions SET status='applied', applied_at=?, "
                     "decided_by=COALESCE(decided_by, ?) WHERE id=? AND status IN ('applying','approved')",
                     (time.time(), str(actor or 'player'), row['id']))
        updated = _row(conn.execute('SELECT * FROM plan_revisions WHERE id = ?',
                                    (row['id'],)).fetchone())
    assert updated is not None
    return updated


def list_revisions(status: str | None = 'pending', limit=50) -> list[dict]:
    sql = 'SELECT * FROM plan_revisions'
    params: list[object] = []
    if status:
        sql += ' WHERE status = ?'
        params.append(str(status))
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(max(1, min(200, int(limit or 50))))
    with _connect() as conn:
        _expire_conn(conn)
        rows = conn.execute(sql, params).fetchall()
    return [item for item in (_row(row) for row in rows) if item is not None]


def history(plan_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM plan_revisions WHERE plan_id = ? ORDER BY version',
            (str(plan_id or ''),)).fetchall()
    return [item for item in (_row(row) for row in rows) if item is not None]


def diff(plan_id: str, from_version: int, to_version: int) -> dict:
    left = get(plan_id, from_version)
    right = get(plan_id, to_version)
    if left is None or right is None:
        raise PlanError('plan revision not found', 'not_found')
    fields = {}
    for field in ('goal', 'route', 'constraints', 'payload', 'plan_hash'):
        if left.get(field) != right.get(field):
            fields[field] = {'from': left.get(field), 'to': right.get(field)}
    return {'planId': right['plan_id'], 'fromVersion': left['version'],
            'toVersion': right['version'], 'changed': bool(fields),
            'fields': fields}
