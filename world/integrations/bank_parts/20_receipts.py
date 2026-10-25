"""Durable usage receipts for the Bank.

The kv_spend projection stays the fast read model, but every priced call is
first recorded as a receipt. A receipt written before the projection is
replayed on the next process, so an interrupted accrual is not silently lost.
Unpriced usage is recorded too, and blocks further calls for that task by
default until an operator resolves it.
"""

import datetime
import json
import os
import time
import uuid

_RECEIPT_STATUSES = ('reported', 'estimated', 'unpriced')


def _ensure_usage_receipts(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS usage_receipts (
        id TEXT PRIMARY KEY,
        service TEXT NOT NULL,
        village_id TEXT,
        agent_id TEXT,
        task_id TEXT,
        cost_usd TEXT NOT NULL,
        status TEXT NOT NULL,
        idempotency_key TEXT UNIQUE,
        applied INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        applied_at REAL
    )''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_usage_receipts_task
        ON usage_receipts(task_id, status, resolved)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_usage_receipts_applied
        ON usage_receipts(applied)''')


def _record_usage_receipt(service, cost, *, task_id=None, village_id=None,
                          agent_id=None, status='reported',
                          idempotency_key=None):
    """Persist one receipt and return its id. An idempotency key returns the
    existing receipt instead of double-recording a retry."""
    import serve as _serve
    if status not in _RECEIPT_STATUSES:
        status = 'reported'
    cost_text = '0.000000000' if status == 'unpriced' else f'{float(cost):.9f}'
    with _serve._db() as conn:
        _ensure_usage_receipts(conn)
        if idempotency_key:
            row = conn.execute(
                'SELECT id FROM usage_receipts WHERE idempotency_key = ?',
                (idempotency_key,)).fetchone()
            if row:
                return row[0]
        receipt_id = uuid.uuid4().hex
        conn.execute(
            '''INSERT INTO usage_receipts
               (id, service, village_id, agent_id, task_id, cost_usd, status,
                idempotency_key, applied, resolved, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)''',
            (receipt_id, service, village_id, agent_id, task_id, cost_text,
             status, idempotency_key, time.time()))
    return receipt_id


def _mark_usage_receipt_applied(receipt_id):
    import serve as _serve
    with _serve._db() as conn:
        _ensure_usage_receipts(conn)
        conn.execute(
            'UPDATE usage_receipts SET applied = 1, applied_at = ? WHERE id = ?',
            (time.time(), receipt_id))


def _apply_usage_receipt(receipt_id):
    """Apply one unapplied priced receipt exactly once. The kv_spend write and
    the applied flag share one SQLite transaction, and the ledger carries the
    receipt id as a second guard against replay after a crash."""
    import serve as _serve
    with _serve._db() as conn:
        _ensure_usage_receipts(conn)
        row = conn.execute(
            '''SELECT service, village_id, agent_id, cost_usd
               FROM usage_receipts
               WHERE id = ? AND applied = 0 AND status != 'unpriced' ''',
            (receipt_id,)).fetchone()
        if not row:
            return False
        service, village_id, agent_id, cost_text = row
        ledger_row = conn.execute(
            'SELECT blob FROM kv_spend WHERE id = 1').fetchone()
        ledger = json.loads(ledger_row[0]) if ledger_row else {}
        if receipt_id in (ledger.get(_RECEIPTS_LEDGER_KEY) or {}):
            conn.execute(
                'UPDATE usage_receipts SET applied = 1, applied_at = ? WHERE id = ?',
                (time.time(), receipt_id))
            return False
        _apply_spend_buckets(
            ledger, service, float(cost_text), village_id, agent_id,
            datetime.datetime.now(datetime.timezone.utc))
        receipts = ledger.setdefault(_RECEIPTS_LEDGER_KEY, {})
        receipts[receipt_id] = cost_text
        conn.execute(
            '''INSERT INTO kv_spend (id, blob, updated_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET blob = excluded.blob,
               updated_at = excluded.updated_at''',
            (json.dumps(ledger), time.time()))
        conn.execute(
            'UPDATE usage_receipts SET applied = 1, applied_at = ? WHERE id = ?',
            (time.time(), receipt_id))
    return True


def _replay_usage_receipts():
    """Apply every priced receipt that was written but never projected."""
    import serve as _serve
    with _serve._db() as conn:
        _ensure_usage_receipts(conn)
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM usage_receipts WHERE applied = 0 AND status != 'unpriced'"
        ).fetchall()]
    applied = 0
    for receipt_id in ids:
        if _apply_usage_receipt(receipt_id):
            applied += 1
    return applied


def _record_unpriced_usage(service, *, task_id=None, village_id=None,
                           agent_id=None, idempotency_key=None):
    """Record usage the provider returned without a reliable price. The
    receipt is already 'applied' (there is nothing to sum) but unresolved, so
    the task budget can hold further calls until an operator resolves it."""
    receipt_id = _record_usage_receipt(
        service, 0, task_id=task_id, village_id=village_id, agent_id=agent_id,
        status='unpriced', idempotency_key=idempotency_key)
    _mark_usage_receipt_applied(receipt_id)
    return receipt_id


def _unpriced_policy():
    import serve as _serve
    raw = os.environ.get('UNPRICED_USAGE_POLICY')
    if not raw:
        try:
            raw = _serve._load_env().get('UNPRICED_USAGE_POLICY', 'block')
        except Exception:
            raw = 'block'
    policy = (raw or 'block').strip().lower()
    return policy if policy in ('block', 'allow', 'warn') else 'block'


def _task_unpriced_blocked(task_id):
    """True when a task has unresolved unpriced usage and policy is block."""
    import serve as _serve
    if not task_id or _unpriced_policy() != 'block':
        return False
    try:
        with _serve._db() as conn:
            _ensure_usage_receipts(conn)
            row = conn.execute(
                '''SELECT 1 FROM usage_receipts
                   WHERE task_id = ? AND status = 'unpriced' AND resolved = 0
                   LIMIT 1''', (task_id,)).fetchone()
            return row is not None
    except Exception:
        return False


def _resolve_unpriced_usage(*, task_id=None, receipt_id=None):
    """Resolve unpriced receipts after an operator prices or accepts them."""
    import serve as _serve
    if not task_id and not receipt_id:
        return 0
    with _serve._db() as conn:
        _ensure_usage_receipts(conn)
        if receipt_id:
            cur = conn.execute(
                'UPDATE usage_receipts SET resolved = 1 WHERE id = ? AND status = ?',
                (receipt_id, 'unpriced'))
        else:
            cur = conn.execute(
                'UPDATE usage_receipts SET resolved = 1 WHERE task_id = ? AND status = ?',
                (task_id, 'unpriced'))
        return cur.rowcount
