"""Versioned SQLite migrations.

The project grew by adding idempotent CREATE TABLE / ALTER TABLE blocks inside
serve.init_db. That works, but it has no record of what ran and no checksum, so
an accidental edit to an old block can silently diverge between databases.

New schema changes go in world/migrations/NNNN_name.sql. The runner records the
version and a SHA-256 of the file in schema_migrations, applies each file once,
and refuses to run if an already-applied file changed.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / 'migrations'


def _files():
    return sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9][0-9]_*.sql'))


def apply(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply every pending migration. Returns the versions applied this call."""
    directory = migrations_dir or MIGRATIONS_DIR
    conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        checksum TEXT NOT NULL,
        applied_at REAL NOT NULL
    )''')
    applied = {row[0]: row[1] for row in
               conn.execute('SELECT version, checksum FROM schema_migrations')}
    ran = []
    for path in sorted(directory.glob('[0-9][0-9][0-9][0-9]_*.sql')):
        version = path.stem
        text = path.read_text(encoding='utf-8')
        checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(
                    f'migration {version} changed after it was applied; '
                    'add a new migration instead of editing history')
            continue
        conn.executescript(text)
        conn.execute(
            'INSERT INTO schema_migrations (version, checksum, applied_at) '
            'VALUES (?, ?, strftime(\'%s\', \'now\'))',
            (version, checksum))
        ran.append(version)
    return ran
