"""Durable, two-phase confirmation proposals.

A model may ask for a write, but it cannot make one happen by calling the write
itself. The caller first records a proposal with a target and arguments. The
owner confirms it with an exact phrase. Only then can the action run, and the
caller must present the same target and arguments again.

The proposal lives in SQLite, so a restart does not lose the owner's decision.
It is not a retry queue: after a crash, a confirmed proposal can be executed
once by the caller, but nothing is replayed automatically.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from migrations import apply as apply_migrations

ROOT = Path(__file__).resolve().parent.parent
MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_TTL_S = 900
MIN_TTL_S = 60
MAX_TTL_S = 86400


class ConfirmationError(Exception):
    def __init__(self, message: str, code: str = 'invalid'):
        super().__init__(message)
        self.code = code


def _db_path() -> str:
    return (os.environ.get('CONFIRMATIONS_DB')
            or os.environ.get('THINK_TANK_DB')
            or str(ROOT / 'think_tank.db'))


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    apply_migrations(conn)
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


def payload_hash(action: str, target, args) -> str:
    raw = '\n'.join((str(action or ''), _canonical(target), _canonical(args)))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _clean_json(value, label, default):
    if value is None:
        value = default
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)
    except (TypeError, ValueError) as error:
        raise ConfirmationError(f'{label} is not JSON-serializable') from error
    if len(text.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ConfirmationError(f'{label} is too large')
    return value


def _row(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item['target'] = json.loads(item.pop('target_json'))
        item['args'] = json.loads(item.pop('args_json'))
    except Exception:
        item['target'] = None
        item['args'] = None
    return item


def propose(action, target, args, actor, channel='web', ttl_s=DEFAULT_TTL_S) -> dict:
    action = str(action or '').strip()
    actor = str(actor or '').strip()
    if not action or not actor:
        raise ConfirmationError('action and actor are required')
    if len(action) > 80:
        raise ConfirmationError('action is too long')
    try:
        ttl = int(ttl_s or DEFAULT_TTL_S)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    ttl = max(MIN_TTL_S, min(MAX_TTL_S, ttl))
    target = _clean_json(target, 'target', {})
    args = _clean_json(args, 'args', {})
    now = time.time()
    proposal_id = 'cfm_' + secrets.token_hex(12)
    digest = payload_hash(action, target, args)
    phrase = f'CONFIRM {proposal_id} {digest[:10]}'
    with _connect() as conn:
        conn.execute(
            '''INSERT INTO confirmation_proposals
               (id, action, target_json, args_json, payload_hash, actor, channel,
                phrase, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)''',
            (proposal_id, action, _canonical(target), _canonical(args), digest,
             actor, str(channel or 'web')[:40], phrase, now, now + ttl))
        row = conn.execute('SELECT * FROM confirmation_proposals WHERE id = ?',
                           (proposal_id,)).fetchone()
    item = _row(row)
    assert item is not None
    return item


def get(proposal_id: str) -> dict | None:
    with _connect() as conn:
        return _row(conn.execute(
            'SELECT * FROM confirmation_proposals WHERE id = ?',
            (str(proposal_id or ''),)).fetchone())


def confirm(proposal_id: str, phrase: str, actor='player') -> dict:
    item = get(proposal_id)
    if not item:
        raise ConfirmationError('confirmation not found', 'not_found')
    if item['status'] == 'confirmed':
        return item
    if item['status'] != 'pending':
        raise ConfirmationError(f"confirmation is {item['status']}", item['status'])
    if item['expires_at'] <= time.time():
        with _connect() as conn:
            conn.execute("UPDATE confirmation_proposals SET status='expired' "
                         "WHERE id = ? AND status = 'pending'", (item['id'],))
        raise ConfirmationError('confirmation expired', 'expired')
    if not hmac.compare_digest(str(item.get('phrase') or ''), str(phrase or '').strip()):
        raise ConfirmationError('confirmation phrase does not match', 'phrase')
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE confirmation_proposals SET status='confirmed', confirmed_at=?, "
            "confirmed_by=? WHERE id=? AND status='pending'",
            (now, str(actor or 'player'), item['id']))
        row = conn.execute('SELECT * FROM confirmation_proposals WHERE id = ?',
                           (item['id'],)).fetchone()
    confirmed_item = _row(row)
    assert confirmed_item is not None
    return confirmed_item


def assert_ready(proposal_id: str, action: str, target, args, actor: str) -> dict:
    item = get(proposal_id)
    if not item:
        raise ConfirmationError('confirmation not found', 'not_found')
    if item['expires_at'] <= time.time():
        raise ConfirmationError('confirmation expired', 'expired')
    if item['status'] != 'confirmed':
        raise ConfirmationError(f"confirmation is {item['status']}", item['status'])
    if item['action'] != action:
        raise ConfirmationError('confirmation action changed', 'action')
    if item['actor'] != actor:
        raise ConfirmationError('confirmation belongs to another actor', 'actor')
    if item['payload_hash'] != payload_hash(action, target, args):
        raise ConfirmationError('confirmation target or arguments changed', 'payload')
    return item


def finish(proposal_id: str, status='executed', error: str | None = None) -> None:
    if status not in ('executed', 'failed', 'cancelled'):
        raise ConfirmationError('invalid completion status')
    with _connect() as conn:
        conn.execute(
            'UPDATE confirmation_proposals SET status=?, executed_at=?, error=? '
            'WHERE id=? AND status IN (\'confirmed\', \'pending\')',
            (status, time.time(), (error or '')[:500] or None, str(proposal_id or '')))


def cancel(proposal_id: str, actor: str) -> dict:
    item = get(proposal_id)
    if not item:
        raise ConfirmationError('confirmation not found', 'not_found')
    if actor != 'player' and item['actor'] != actor:
        raise ConfirmationError('confirmation belongs to another actor', 'actor')
    if item['status'] not in ('pending', 'confirmed'):
        raise ConfirmationError(f"confirmation is {item['status']}", item['status'])
    with _connect() as conn:
        conn.execute("UPDATE confirmation_proposals SET status='cancelled' "
                     "WHERE id=? AND status IN ('pending', 'confirmed')",
                     (item['id'],))
    cancelled = get(item['id'])
    assert cancelled is not None
    return cancelled


def list_for_actor(actor: str, status: str | None = None, limit=50) -> list[dict]:
    sql = 'SELECT * FROM confirmation_proposals WHERE actor = ?'
    params: list[object] = [str(actor)]
    if status:
        sql += ' AND status = ?'
        params.append(str(status))
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(max(1, min(200, int(limit or 50))))
    with _connect() as conn:
        items = [_row(row) for row in conn.execute(sql, params)]
    return [item for item in items if item is not None]


def expire_stale() -> int:
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE confirmation_proposals SET status='expired' "
            "WHERE status='pending' AND expires_at <= ?", (now,))
        return cur.rowcount
