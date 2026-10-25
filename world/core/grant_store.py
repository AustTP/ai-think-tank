"""Shared storage for bounded experiment grants.

sublow and JEV grants differ only in the model they serve, the ledger bucket,
and the policy checks around the grant. The table shape and CRUD are identical,
so they live here once. Callers keep their own policy checks and public names.
"""
import time

# table -> agent index name. Fixed names only: never interpolate caller text.
TABLES = {
    'sublow_grants': 'idx_sublow_grants_agent',
    'jev_grants': 'idx_jev_grants_agent',
}

COLUMNS = ('id', 'token_hash', 'agent_id', 'task_id', 'purpose', 'estimate_usd',
           'accrued_usd', 'calls', 'max_tokens_per_call', 'granted_by',
           'created_at', 'expires_at')


def ensure_table(conn, table):
    """Create the grant table and its agent index if absent."""
    index = TABLES[table]
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        agent_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        purpose TEXT NOT NULL,
        estimate_usd REAL NOT NULL,
        accrued_usd REAL NOT NULL DEFAULT 0,
        calls INTEGER NOT NULL DEFAULT 0,
        max_tokens_per_call INTEGER NOT NULL,
        granted_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS {index} ON {table}(agent_id)')


def insert(conn, table, row):
    """Insert one grant row. `row` is a COLUMNS-ordered sequence."""
    placeholders = ', '.join('?' for _ in COLUMNS)
    columns = ', '.join(COLUMNS)
    conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(row))


def resolve(conn, table, token_hash, now=None):
    """Return the live grant row as a dict, deleting it when expired."""
    now = time.time() if now is None else now
    row = conn.execute(
        f'SELECT {", ".join(COLUMNS)} FROM {table} WHERE token_hash = ?',
        (token_hash,)).fetchone()
    if not row:
        return None
    if row[COLUMNS.index('expires_at')] <= now:
        conn.execute(f'DELETE FROM {table} WHERE id = ?', (row[0],))
        return None
    return dict(zip(COLUMNS, row))


def accrue(conn, table, grant_id, cost):
    """Add one call's real cost to the grant and return the new total."""
    conn.execute(f'UPDATE {table} SET accrued_usd = accrued_usd + ?, '
                 'calls = calls + 1 WHERE id = ?', (float(cost), grant_id))
    row = conn.execute(f'SELECT accrued_usd FROM {table} WHERE id = ?',
                       (grant_id,)).fetchone()
    return float(row[0]) if row else None


def revoke(conn, table, grant, token_hash):
    """Delete by id or raw-token hash. True when a row matched."""
    cur = conn.execute(f'DELETE FROM {table} WHERE id = ? OR token_hash = ?',
                       (grant, token_hash))
    return cur.rowcount > 0


def revoke_agent(conn, table, agent_id):
    """Delete every grant held by one agent; returns the row count."""
    cur = conn.execute(f'DELETE FROM {table} WHERE agent_id = ?', (agent_id,))
    return cur.rowcount or 0


def expire(conn, table, now=None):
    """Delete grants whose TTL has passed."""
    conn.execute(f'DELETE FROM {table} WHERE expires_at <= ?',
                 (time.time() if now is None else now,))
