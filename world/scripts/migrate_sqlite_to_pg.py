#!/usr/bin/env python3
"""Migrate the think tank's SQLite database to Postgres.

Reads every user table out of the SQLite think_tank.db, recreates the
schema in Postgres through the same DDL translation db_backend applies at
runtime, copies the rows with per-column type coercion (SQLite's dynamic
typing -> Postgres strict columns), rebuilds indexes, bumps serial
sequences past the copied maxima, and verifies per-table row counts.

Usage:
  python3 scripts/migrate_sqlite_to_pg.py \
      [--from think_tank.db] [--dsn postgresql://poole86@/think_tank]

The target schema is DROPPED first (the script is idempotent; it always
produces a full copy of the source). Never point it at a live Postgres
database you have written to independently.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db_backend  # noqa: E402


def sqlite_tables(src):
    cur = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [(r[0], r[1]) for r in cur.fetchall() if r[1]]


def sqlite_indexes(src):
    cur = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql "
        "IS NOT NULL ORDER BY name")
    return [(r[0], r[1]) for r in cur.fetchall()]


def coerce(value, pg_type):
    if value is None:
        return None
    t = (pg_type or '').lower()
    if t.startswith('text') or t.startswith('character') or t == 'varchar':
        # Postgres text cannot hold NUL bytes; SQLite text can. Strip them
        # (they only occur in long free-text blobs; loss is cosmetic).
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace').replace('\x00', '')
        if isinstance(value, str):
            return value.replace('\x00', '')
        return str(value)
    if t in ('bigint', 'integer', 'smallint'):
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(str(value))
        except ValueError:
            return None  # unrepresentable: NULL rather than a failed copy
    if t in ('double precision', 'real', 'numeric'):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value))
        except ValueError:
            return None
    if t == 'bytea':
        return value if isinstance(value, (bytes, memoryview)) \
            else str(value).encode('utf-8')
    return value  # unknown type: pass through


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='src', default=None)
    ap.add_argument('--dsn', dest='dsn', default=None)
    args = ap.parse_args()
    src_path = (args.src or os.environ.get('THINK_TANK_DB')
                or os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))),
                    'think_tank.db'))
    dsn = args.dsn or os.environ.get('THINK_TANK_DB_URL') \
        or 'postgresql://poole86@/think_tank'

    src = sqlite3.connect(src_path)
    import psycopg2
    raw = psycopg2.connect(dsn)
    catalog = db_backend._Catalog()
    catalog.refresh(raw)
    pg_cur = raw.cursor()
    # Fresh copy: drop everything this script is about to recreate.
    pg_cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    for (t,) in pg_cur.fetchall():
        pg_cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    raw.commit()

    copied = 0
    for table, ddl in sqlite_tables(src):
        pg_ddl = db_backend.translate_ddl(ddl)
        try:
            pg_cur.execute(pg_ddl)
            catalog.refresh(raw, table)
        except Exception as e:
            print(f'SKIP {table}: DDL failed: {str(e)[:160]}',
                  file=sys.stderr, flush=True)
            continue
        cols = [c for c, _ in catalog.column_types(table).items()]
        types = dict(catalog.column_types(table).items())
        cur = src.execute(f'SELECT * FROM "{table}"')
        rows = []
        for row in cur.fetchall():
            rows.append(tuple(coerce(v, types.get(c))
                              for c, v in zip(cols, row)))
        if rows:
            collist = ', '.join(cols)
            # execute_values batches rows per round trip; psycopg2's
            # executemany is one round trip PER ROW (minutes at 6k+ rows).
            from psycopg2.extras import execute_values
            execute_values(
                raw.cursor(),
                f'INSERT INTO "{table}" ({collist}) VALUES %s',
                rows, page_size=500)
        catalog.refresh(raw, table)
        copied += 1
        print(f'{table}: {len(rows)} rows', flush=True)
    raw.commit()

    for _, ddl in sqlite_indexes(src):
        try:
            pg_cur.execute(db_backend.translate_ddl(ddl))
        except Exception as e:
            print(f'index skipped: {str(e)[:120]}', file=sys.stderr)
    raw.commit()

    # Bump serial sequences past the copied maxima so later inserts don't
    # collide with migrated ids.
    for table in [t for t, _ in sqlite_tables(src)]:
        for col in catalog.serial_columns(table):
            try:
                pg_cur.execute(
                    f'SELECT setval(pg_get_serial_sequence(\'{table}\', '
                    f'\'{col}\'), COALESCE((SELECT MAX({col}) FROM '
                    f'"{table}"), 0) + 1, false)')
            except Exception as e:
                print(f'sequence {table}.{col}: {str(e)[:120]}',
                      file=sys.stderr)
    raw.commit()

    # Verify.
    mismatches = 0
    for table, _ in sqlite_tables(src):
        try:
            src_n = src.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:
            continue  # skipped table (DDL failed)
        try:
            pg_cur.execute(f'SELECT count(*) FROM "{table}"')
            pg_n = pg_cur.fetchone()[0]
        except Exception as e:
            print(f'VERIFY FAIL {table}: {str(e)[:120]}', file=sys.stderr)
            mismatches += 1
            continue
        if src_n != pg_n:
            print(f'VERIFY FAIL {table}: sqlite={src_n} pg={pg_n}',
                  file=sys.stderr)
            mismatches += 1
    print(f'done: {copied} tables copied, {mismatches} mismatches')
    return 1 if mismatches else 0


if __name__ == '__main__':
    sys.exit(main())
