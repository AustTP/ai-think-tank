"""One-shot SQLite -> Postgres migration for the think-tank database.

Reads every user table from a SQLite snapshot (take one with the sqlite3
backup API -- consistent under WAL, safe while the server runs), rebuilds
the schema through the app's OWN init_db (so the DDL is always the current
code's, translated by db_backend), copies the data table by table, fixes
BIGSERIAL sequences for AUTOINCREMENT tables, and verifies per-table row
counts before declaring success.

Usage:
    THINK_TANK_DB_URL=postgresql://... python3 server/pg_migrate.py \
        --source /path/think_tank.db [--drop-existing]

The script deliberately runs OUTSIDE pytest (db_backend._test_sandboxed()
would force SQLite); it sets THINK_TANK_DB_URL itself and refuses to run
when THINK_TANK_FORCE_SQLITE is set.
"""
import argparse
import os
import sqlite3
import sys

BATCH = 2000
_SKIP_TABLES = {'sqlite_sequence', 'sqlite_stat1', 'sqlite_stat4'}


def _pg_conn(dsn):
    import psycopg2
    return psycopg2.connect(dsn)


def _drop_existing(pg):
    cur = pg.cursor()
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    tables = [r[0] for r in cur.fetchall()]
    for t in tables:
        cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    pg.commit()
    print(f'dropped {len(tables)} existing tables')


def _build_schema():
    """The app's own DDL: THINK_TANK_DB_URL routes _db() to Postgres and
    init_db translates + creates every table the current code expects."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import serve  # noqa: F401  (env routing happens at db_backend level)
    serve.init_db()
    print('schema built via serve.init_db()')


def _ensure_table(pg, src, table):
    """Some tables are created lazily at first use, so init_db's schema is a
    subset of a lived-in database's. For anything missing, create it from the
    snapshot's own DDL run through db_backend.translate_ddl."""
    cur = pg.cursor()
    cur.execute('SELECT to_regclass(%s)', (f'public."{table}"',))
    if cur.fetchone()[0] is not None:
        return False
    ddl = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0]
    import db_backend
    cur.execute(db_backend.translate_ddl(ddl))
    pg.commit()
    print(f'  ({table}: created from snapshot DDL)')
    return True


_NUL_STRIPPED = {}


def _copy_table_rows(pg, src, table, columns, col_types):
    _ensure_table(pg, src, table)
    cur = pg.cursor()
    cur.execute(f'DELETE FROM "{table}"')
    collist = ', '.join(f'"{c}"' for c in columns)
    placeholders = ', '.join(['%s'] * len(columns))
    sql = f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})'
    sel = f'SELECT {collist} FROM "{table}"'
    import psycopg2
    total = 0
    batch = []
    for row in src.execute(sel):
        vals = []
        stripped = 0
        for v, t in zip(row, col_types):
            if isinstance(v, (bytes, bytearray, memoryview)) and 'blob' not in t.lower():
                v = psycopg2.Binary(bytes(v))
            elif isinstance(v, str) and '\x00' in v:
                # Postgres TEXT cannot store NUL; SQLite could. Strip and
                # report -- a NUL inside app text is invisible junk anyway.
                stripped += v.count('\x00')
                v = v.replace('\x00', '')
            vals.append(v)
        if stripped:
            _NUL_STRIPPED[table] = _NUL_STRIPPED.get(table, 0) + stripped
        batch.append(vals)
        if len(batch) >= BATCH:
            cur.executemany(sql, batch)
            total += len(batch)
            batch = []
    if batch:
        cur.executemany(sql, batch)
        total += len(batch)
    pg.commit()
    return total


def _fix_sequences(pg, src):
    """AUTOINCREMENT tables got BIGSERIAL PKs in Postgres; explicit-PK
    inserts leave the sequence behind, so the next app insert would collide.
    setval each to max(pk)."""
    cur = pg.cursor()
    fixed = []
    for (name, seq) in src.execute('SELECT name, seq FROM sqlite_sequence'):
        info = src.execute(f'PRAGMA table_info("{name}")').fetchall()
        pk_cols = [r[1] for r in info if r[5]]
        if not pk_cols:
            continue
        pk = pk_cols[0]
        try:
            cur.execute(f'SELECT COALESCE(MAX("{pk}"), 1) FROM "{name}"')
            mx = cur.fetchone()[0]
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (name, pk))
            seqreg = cur.fetchone()[0]
            if not seqreg:
                continue
            cur.execute('SELECT setval(%s, %s, true)', (seqreg, mx))
            fixed.append(f'{name}.{pk} -> {mx}')
        except Exception as e:
            print(f'  sequence fix skipped for {name}: {e}')
    pg.commit()
    if fixed:
        print('sequences:', ', '.join(fixed))


def _verify(pg, src):
    bad = []
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f'{"table":34s} {"sqlite":>9s} {"postgres":>9s}')
    for t in tables:
        s = src.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        try:
            p = pg.cursor()
            p.execute(f'SELECT count(*) FROM "{t}"')
            d = p.fetchone()[0]
        except Exception as e:
            print(f'{t:34s} {s:9d}    ERR {str(e)[:40]}')
            bad.append(t)
            continue
        flag = '' if s == d else '  <-- MISMATCH'
        print(f'{t:34s} {s:9d} {d:9d}{flag}')
        if s != d:
            bad.append(t)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--dsn', default=None)
    ap.add_argument('--drop-existing', action='store_true')
    args = ap.parse_args()

    if os.environ.get('THINK_TANK_FORCE_SQLITE'):
        print('refusing to run: THINK_TANK_FORCE_SQLITE is set')
        return 2

    src = sqlite3.connect(args.source)
    dsn = args.dsn or os.environ.get('THINK_TANK_DB_URL')
    if not dsn:
        print('no DSN: pass --dsn or set THINK_TANK_DB_URL')
        return 2
    os.environ['THINK_TANK_DB_URL'] = dsn

    pg = _pg_conn(dsn)
    if args.drop_existing:
        _drop_existing(pg)
    pg.close()

    _build_schema()
    pg = _pg_conn(dsn)

    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f'copying {len(tables)} tables...')
    for t in tables:
        info = src.execute(f'PRAGMA table_info("{t}")').fetchall()
        columns = [r[1] for r in info]
        col_types = [r[2] or '' for r in info]
        n = _copy_table_rows(pg, src, t, columns, col_types)
        print(f'  {t}: {n} rows')
    _fix_sequences(pg, src)

    bad = _verify(pg, src)
    pg.close()
    src.close()
    if _NUL_STRIPPED:
        for t, n in sorted(_NUL_STRIPPED.items()):
            print(f'NUL bytes stripped from {t}: {n}')
    if bad:
        print(f'FAILED: {len(bad)} table(s) mismatched: {", ".join(bad)}')
        return 1
    print('migration verified: every table matches')
    return 0


if __name__ == '__main__':
    sys.exit(main())
