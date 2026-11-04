"""Dual-backend database layer for the think tank.

The app was written against sqlite3; THINK_TANK_DB_URL (e.g.
postgresql://poole86@/think_tank) switches the SAME call surface (_db() in
serve_parts/00_header.py and the core/* readers) to Postgres without
rewriting ~340 execute sites. The adapter translates exactly the SQLite
idioms this tree uses (measured against the source; the tree has no SQL
strftime/julianday/json_extract usage):

  ? placeholders        -> %s (quote-aware; ? inside literals untouched)
  INSERT OR REPLACE     -> ON CONFLICT (pk) DO UPDATE SET col=EXCLUDED.col
  INSERT OR IGNORE      -> ON CONFLICT DO NOTHING
  BEGIN IMMEDIATE       -> BEGIN (Postgres has no IMMEDIATE mode)
  PRAGMA journal_mode=  -> no-op (WAL is a SQLite concept)
  PRAGMA table_info(t)  -> information_schema emulation (same row shape)
  CREATE TABLE ... INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL
  INSERT (serial pk)    -> + RETURNING pk; cursor.lastrowid reports it
  assignment params     -> ::type cast per target column (Postgres is
                           strict about int->text assignment where SQLite
                           accepted anything)

Rows come back as CompatRow: integer and string indexing both work
(sqlite3.Row semantics), so sites that set conn.row_factory = sqlite3.Row
keep working and sites that unpack tuples keep working.

With THINK_TANK_DB_URL unset the adapter is a pass-through to plain
sqlite3.connect -- zero behavior change.
"""
import contextlib
import re
import sqlite3
import threading


class PostgresUnavailable(RuntimeError):
    pass


def _quote_aware_split_helpers():
    """Shared char-walk helpers: skip regions inside '...' and "..."
    literals (SQLite string literals; the tree never uses ? inside them,
    but a stray one must not corrupt the SQL)."""
    return None


def _sub_placeholders_outside_literals(sql, repl):
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2  # escaped ''
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == '?':
            out.append(repl())
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _translate_placeholders(sql):
    """? -> %s, but only outside string literals."""
    if '?' not in sql:
        return sql
    counter = {'n': 0}

    def repl():
        counter['n'] += 1
        return '%s'

    return _sub_placeholders_outside_literals(sql, repl)


_IDENT = r'[A-Za-z_][A-Za-z0-9_]*'


def translate_ddl(sql):
    """SQLite CREATE TABLE/INDEX -> Postgres DDL. Mechanical over the DDL
    this tree writes (010_external_write_once.py et al)."""
    s = sql
    s = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
               'BIGSERIAL PRIMARY KEY', s, flags=re.I)
    s = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY', 'BIGINT PRIMARY KEY', s,
               flags=re.I)
    s = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', s, flags=re.I)
    # BLOB as a column TYPE -> bytea. A bare global swap would also rename
    # columns literally named `blob` (kv_state.blob!) -- only rewrite the
    # token when it sits in type position (after a column name, followed by
    # a constraint/comma/close-paren/end).
    s = re.sub(r'(?i)\b([a-z_]\w*)\s+blob\b(?=\s*(?:[,)]|PRIMARY|NOT|'
               r'DEFAULT|UNIQUE|CHECK|REFERENCES|$))', r'\1 bytea', s)
    # SQLite accepts bare UNSIGNED / VARCHAR(n) etc.; normalize VARCHAR to
    # TEXT (the tree uses TEXT everywhere) in case older schemas did.
    s = re.sub(r'VARCHAR\s*\(\s*\d+\s*\)', 'TEXT', s, flags=re.I)
    return s


def _translate_insert_replace(sql, pk_map, col_types, tables):
    """INSERT OR REPLACE INTO t (cols) VALUES (...) ->
    INSERT INTO t (cols) VALUES (...) ON CONFLICT (pks) DO UPDATE SET
    col=EXCLUDED.col ... (the PK's columns are the conflict target; every
    other listed column is updated). SQLite's OR REPLACE also clears OTHER
    uniqueness conflicts by deleting them -- this tree's OR REPLACE sites
    only target PK tables, which is what the PG rewrite expresses."""
    m = re.search(
        r'INSERT\s+OR\s+REPLACE\s+INTO\s+(' + _IDENT + r')\s*'
        r'(\(([^)]*)\))?\s*(VALUES|SELECT|DEFAULT)',
        sql, flags=re.I)
    if not m:
        return sql
    table = m.group(1).lower()
    pks = pk_map.get(table)
    if not pks:
        return sql  # no PK known: leave as-is (will error loudly in PG)
    cols = None
    if m.group(2):
        cols = [c.strip().strip('"`').lower()
                for c in m.group(3).split(',') if c.strip()]
    if not cols:
        cols = list((col_types.get(table) or {}).keys())
    updatable = [c for c in cols if c not in pks]
    if not updatable:
        conflict = 'ON CONFLICT DO NOTHING'
    else:
        conflict = ('ON CONFLICT (' + ', '.join(pks) + ') DO UPDATE SET '
                    + ', '.join(f'{c} = EXCLUDED.{c}' for c in updatable))
    rewritten = (sql[:m.start()] + 'INSERT INTO ' + table + ' '
                 + (m.group(2) or '') + ' ' + sql[m.start(4):])
    # The rest of the statement (VALUES ...) is followed by the conflict
    # clause; a trailing ; moves to the very end.
    rewritten = rewritten.rstrip().rstrip(';')
    rewritten += ' ' + conflict
    return rewritten


def _translate_insert_ignore(sql):
    if not re.search(r'INSERT\s+OR\s+IGNORE\s+INTO', sql, flags=re.I):
        return sql
    body = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql,
                  count=1, flags=re.I)
    body = body.rstrip().rstrip(';')
    if re.search(r'\bON\s+CONFLICT\b', body, flags=re.I):
        return body + ';'
    return body + ' ON CONFLICT DO NOTHING;'


_UPDATE_SET_RE = re.compile(
    r'\bUPDATE\s+(' + _IDENT + r')\s+SET\s+(.*?)(\s+WHERE\s+.*)?$',
    flags=re.I | re.S)

_INSERT_RE = re.compile(
    r'\bINSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+(' + _IDENT + r')\s*'
    r'(\(([^()]*)\))?\s*(VALUES|DEFAULT\s+VALUES|SELECT)',
    flags=re.I)


def _cast_columns(sql, col_types, tables):
    """Add per-target-column casts to assignment-position placeholders.

    SQLite accepted inserting a Python int/float into a TEXT column (dynamic
    typing); Postgres rejects int->text assignment. Casting every assignment
    placeholder to its column's type restores the write behavior for str,
    int, and float alike. WHERE-comparison placeholders stay untyped so
    Postgres infers from the column type."""
    table_types = {t.lower(): {c.lower(): ty for c, ty in m.items()}
                   for t, m in col_types.items()}

    def cast_for(table, col):
        ty = (table_types.get(table) or {}).get(col)
        if not ty:
            return None
        t = ty.lower()
        if t in ('text', 'character varying', 'varchar', 'character'):
            return '::text'
        if t in ('bigint', 'integer', 'smallint', 'bigserial', 'serial'):
            return '::bigint'
        if t in ('double precision', 'real', 'numeric'):
            return '::double precision'
        if t == 'bytea':
            return '::bytea'
        return None

    def rewrite_insert(m):
        table = m.group(1).lower()
        types = table_types.get(table)
        if not types or m.group(4).upper().startswith('DEFAULT'):
            return m.group(0)
        if m.group(2):
            cols = [c.strip().strip('"`').lower()
                    for c in m.group(3).split(',') if c.strip()]
        else:
            cols = list(types)
        if not cols:
            return m.group(0)
        head = m.group(0)
        # Rewrite the FIRST VALUES group's placeholders positionally.
        vm = re.search(r'VALUES\s*\(([^()]*)\)', head, flags=re.I)
        if not vm:
            return head
        inner = vm.group(1)
        parts = _sub_placeholders_outside_literals(
            inner, lambda: '\x00').split(',')
        # ? marks were replaced by \x00 sentinels to split safely.
        out_parts = []
        for idx, part in enumerate(parts):
            if '\x00' in part:
                col = cols[idx] if idx < len(cols) else None
                cast = cast_for(table, col) if col else None
                out_parts.append(part.replace('\x00', '%s') + (cast or ''))
            else:
                out_parts.append(part)
        return head[:vm.start(1)] + ','.join(out_parts) + head[vm.end(1):]

    def rewrite_update(m):
        table = m.group(1).lower()
        types = table_types.get(table)
        if not types:
            return m.group(0)
        assignments = m.group(2)
        # Split top-level commas (assignments are col = ? pairs).
        segs = []
        depth = 0
        cur = ''
        for ch in assignments:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if ch == ',' and depth == 0:
                segs.append(cur)
                cur = ''
            else:
                cur += ch
        segs.append(cur)
        out = []
        for seg in segs:
            sm = re.match(r'\s*("?' + _IDENT + r'"?)\s*=\s*(\?|%s)(.*)$',
                          seg, flags=re.S)
            if sm:
                col = sm.group(1).strip('"').lower()
                cast = cast_for(table, col)
                out.append(f'{sm.group(1)} = %s{cast or ""}{sm.group(3)}')
            else:
                out.append(seg)
        return ('UPDATE ' + table + ' SET ' + ','.join(out)
                + (m.group(3) or ''))

    s = _INSERT_RE.sub(rewrite_insert, sql)
    s = _UPDATE_SET_RE.sub(rewrite_update, s)
    return s


_TABLE_INFO_RE = re.compile(r'\s*PRAGMA\s+table_info\s*\(\s*"?('
                            + _IDENT + r')"?\s*\)\s*;?\s*$', flags=re.I)
_JOURNAL_RE = re.compile(r'\s*PRAGMA\s+\w+\s*=\s*[^;]*;?\s*$', flags=re.I)
_BARE_PRAGMA_RE = re.compile(r'\s*PRAGMA\s+\w+\s*;?\s*$', flags=re.I)


def _translate_strftime(s):
    """SQL strftime -> Postgres. The tree uses exactly one shape:
    `strftime('%s', 'now')` (epoch seconds, server/migrations.py) — mapped
    to a UTC epoch extract. Other formats map through to_char for safety."""
    if not re.search(r'\bstrftime\s*\(', s, flags=re.I):
        return s

    def fmt_to_pg(fmt):
        out = fmt
        for a, b in (('%Y', 'YYYY'), ('%m', 'MM'), ('%d', 'DD'),
                     ('%H', 'HH24'), ('%M', 'MI'), ('%S', 'SS')):
            out = out.replace(a, b)
        if out != fmt:
            return out
        return None

    def repl(m):
        fmt, arg = m.group(1), m.group(2).strip()
        if fmt == '%s':
            src = ("now()" if arg.lower() == "'now'" else arg)
            return f"FLOOR(EXTRACT(EPOCH FROM {src}))::bigint"
        pg = fmt_to_pg(fmt)
        if pg:
            src = ("(now() AT TIME ZONE 'UTC')"
                   if arg.lower() == "'now'" else arg)
            return f"to_char({src}, '{pg}')"
        return m.group(0)

    return re.sub(r"strftime\s*\(\s*'([^']*)'\s*,\s*([^)]+?)\s*\)", repl, s,
                  flags=re.I)


def _escape_literal_percent(s):
    """Double % inside quoted literals so psycopg2 (which scans % when
    params are present) renders LIKE patterns like '%\"blocked\"%' intact.
    %s placeholders live outside literals and are untouched."""
    if '%' not in s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            j = i + 1
            span = ["'"]
            while j < n:
                if s[j] == "'" and j + 1 < n and s[j + 1] == "'":
                    span.append("''")
                    j += 2
                    continue
                if s[j] == "'":
                    break
                span.append('%%' if s[j] == '%' else s[j])
                j += 1
            span.append("'")
            out.append(''.join(span))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


_INTEGRITY_PG_ERRORS = {
    'UniqueViolation', 'CheckViolation', 'ForeignKeyViolation',
    'NotNullViolation', 'ExclusionViolation', 'IntegrityError',
}
_OPERATIONAL_PG_ERRORS = {
    'UndefinedTable', 'UndefinedColumn', 'UndefinedFunction',
    'DuplicateTable', 'DuplicateColumn', 'DatatypeMismatch', 'SyntaxError',
    'InvalidTextRepresentation', 'NumericValueOutOfRange',
    'StringDataRightTruncation', 'ReadOnlySqlTransaction',
    'LockNotAvailable', 'SerializationFailure', 'DeadlockDetected',
    'QueryCanceled', 'LockWaitTimeout', 'AdminShutdown', 'UndefinedObject',
}


def _as_sqlite_error(exc):
    """Re-raise Postgres errors as their sqlite3 counterparts.

    The call sites guard with `except sqlite3.OperationalError` (the ALTER
    ADD COLUMN idempotency pattern appears at a dozen sites, e.g.
    selfedit's _ensure_table) and `except sqlite3.IntegrityError` (unique
    writes). Postgres error classes would sail past those handlers and
    abort whole loops, so the adapter surface keeps the sqlite3 contract.
    """
    import psycopg2
    if isinstance(exc, psycopg2.Error):
        name = type(exc).__name__
        if name in _INTEGRITY_PG_ERRORS:
            return sqlite3.IntegrityError(str(exc).strip())
        if name in _OPERATIONAL_PG_ERRORS:
            return sqlite3.OperationalError(str(exc).strip())
        return sqlite3.DatabaseError(str(exc).strip())
    return exc


def _translate_boolean_sum(s):
    """SQLite's `SUM(col = 0)` boolean-arithmetic idiom: in Postgres SUM
    does not accept boolean, so comparisons inside SUM become
    SUM(CASE WHEN <expr> THEN 1 ELSE 0 END). Only fires when the SUM body
    contains a comparison operator."""
    def repl(m):
        inner = m.group(1)
        if re.search(r'[=!<>]', inner):
            return f'SUM(CASE WHEN {inner} THEN 1 ELSE 0 END)'
        return m.group(0)
    return re.sub(r'\bSUM\(\s*([^()]+?)\s*\)', repl, s, flags=re.I)


def _translate_sqlite_master(s):
    """sqlite_master existence/name checks -> information_schema.tables.
    Shapes in this tree: `SELECT 1 FROM sqlite_master WHERE type='table'
    AND name='x'`, `SELECT name FROM sqlite_master WHERE type='table' AND
    name = ?` (and LIKE variants). The bare `name` column becomes
    table_name (\\bname\\b does not match inside table_name, so the rename
    is order-safe)."""
    if not re.search(r'\bsqlite_master\b', s, flags=re.I):
        return s
    s = re.sub(r'\bsqlite_master\b', 'information_schema.tables', s,
               flags=re.I)
    s = re.sub(r"\btype\s*=\s*'(table|view)'",
               lambda m: ("table_type = 'BASE TABLE'" if
                          m.group(1).lower() == 'table'
                          else "table_type = 'VIEW'"), s, flags=re.I)
    s = re.sub(r'\bname\b', 'table_name', s, flags=re.I)
    if re.search(r'\bWHERE\b', s, flags=re.I):
        s = s.rstrip().rstrip(';') + " AND table_schema = 'public';"
    else:
        s = re.sub(r'\bFROM\s+information_schema\.tables\b',
                   "FROM information_schema.tables WHERE table_schema = "
                   "'public'", s, count=1, flags=re.I)
    return s


class CompatRow:
    """sqlite3.Row semantics over a Postgres row: index or column-name
    access, tuple unpacking, truthiness."""
    __slots__ = ('_values', '_map')

    def __init__(self, values, cols):
        self._values = tuple(values)
        self._map = {c: v for c, v in zip(cols, self._values)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def keys(self):
        return list(self._map)

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __bool__(self):
        return bool(self._values)

    def __repr__(self):
        return repr(self._values)


def _pg_column_meta(cur, table):
    cur.execute(
        "SELECT column_name, data_type, ordinal_position "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (table,))
    return [(r[0], r[1]) for r in cur.fetchall()]


def _pg_primary_keys(cur, table):
    cur.execute(
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name "
        "AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' "
        "AND tc.table_schema = 'public' AND tc.table_name = %s "
        "ORDER BY kcu.ordinal_position", (table,))
    return [r[0] for r in cur.fetchall()]


def _pg_serial_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "AND column_default LIKE 'nextval(%%'", (table,))
    return [r[0] for r in cur.fetchall()]


class _NoopCursor:
    """Result cursor for no-op statements (PRAGMA journal_mode etc.)."""

    def __iter__(self):
        return iter(())

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    lastrowid = None

    @property
    def description(self):
        return None


class _TableInfoCursor(_NoopCursor):
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


_TYPE_TO_SQLITE = {
    'text': 'TEXT', 'character varying': 'TEXT', 'character': 'TEXT',
    'bigint': 'INTEGER', 'integer': 'INTEGER', 'smallint': 'INTEGER',
    'bigserial': 'INTEGER', 'serial': 'INTEGER',
    'double precision': 'REAL', 'real': 'REAL', 'numeric': 'REAL',
    'bytea': 'BLOB', 'boolean': 'INTEGER',
}


class CompatCursor:
    def __init__(self, pg_cur, catalog, pg_conn=None):
        self._cur = pg_cur
        self._catalog = catalog
        self._pg_conn = pg_conn
        self._ctrl = None
        self._rows = None
        self._cols = None
        self.lastrowid = None

    def _ctrl_cursor(self):
        if self._ctrl is None:
            self._ctrl = self._pg_conn.cursor()
        return self._ctrl

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _wrap(self, rows):
        if self._cols is None:
            self._cols = ([d[0] for d in self._cur.description]
                          if self._cur.description else [])
        return [CompatRow(r, self._cols) for r in rows]

    def fetchone(self):
        row = self._cur.fetchone()
        return self._wrap([row])[0] if row is not None else None

    def fetchall(self):
        rows = self._cur.fetchall()
        if rows and not isinstance(rows[0], CompatRow):
            rows = self._wrap(rows)
        return rows

    def __iter__(self):
        for row in self._cur.fetchall():
            yield self._wrap([row])[0]

    def close(self):
        self._cur.close()

    def execute(self, sql, params=None):
        return self._execute_translated(sql, params)

    def executemany(self, sql, seq_of_params):
        if _READONLY and not _READ_ONLY_OK_RE.match(sql.strip()):
            raise sqlite3.OperationalError(
                'read-only connection: THINK_TANK_READ_ONLY is set')
        sql2 = self._catalog.translate(sql)
        seq = []
        for params in seq_of_params:
            seq.append(self._catalog.adapt_params(sql2, params))
        self._cur.executemany(sql2, seq)
        return self

    def _execute_translated(self, sql, params):
        catalog = self._catalog
        stripped = sql.strip()
        up = stripped.upper()
        if _READONLY and not _READ_ONLY_OK_RE.match(stripped):
            raise sqlite3.OperationalError(
                'read-only connection: THINK_TANK_READ_ONLY is set')
        if up.startswith('PRAGMA'):
            m = _TABLE_INFO_RE.match(stripped)
            if m:
                table = m.group(1).lower()
                cols = catalog.column_types(table)
                rows = []
                for cid, (name, data_type) in enumerate(cols.items()):
                    rows.append(CompatRow(
                        (cid, name, _TYPE_TO_SQLITE.get(data_type, data_type),
                         0, None, 1 if name in catalog.pk_columns(table)
                         else 0),
                        ('cid', 'name', 'type', 'notnull', 'dflt_value',
                         'pk')))
                return _TableInfoCursor(rows)
            return _NoopCursor()
        if up.startswith('BEGIN'):
            # psycopg2 opens the transaction implicitly at first statement;
            # the code's explicit BEGIN IMMEDIATE (sqlite write-lock idiom)
            # is a no-op here -- Postgres row locks + unique constraints
            # give the same duplicate-write protection.
            return _NoopCursor()
        m = re.search(r'INSERT\s+OR\s+(?:REPLACE|IGNORE)\s+INTO\s+('
                      + _IDENT + r')', sql, flags=re.I)
        if m and m.group(1).lower() not in catalog._tables:
            catalog.refresh(self._pg_conn)
        sql2 = catalog.translate(sql)
        if params:
            sql2 = _escape_literal_percent(sql2)
        params = catalog.adapt_params(sql2, params)
        sql2 = catalog.maybe_add_returning(sql2, table_of_insert(sql2))
        # Statement-level savepoint isolation: this tree's idiom "try a
        # guarded read/ALTER, catch, then create/migrate and continue"
        # (init_db's lazy schema paths) works on SQLite because a failed
        # statement leaves the transaction usable. In Postgres an aborted
        # statement poisons the transaction, so every statement runs inside
        # a savepoint that a failure rolls back to -- the surrounding
        # transaction keeps its prior work and the caught-error pattern
        # behaves exactly as it does on SQLite.
        ctrl = self._ctrl_cursor()
        ctrl.execute('SAVEPOINT _rea_sp')
        try:
            self._cur.execute(sql2, params or None)
        except Exception as e:
            ctrl.execute('ROLLBACK TO SAVEPOINT _rea_sp')
            ctrl.execute('RELEASE SAVEPOINT _rea_sp')
            raise _as_sqlite_error(e) from None
        ctrl.execute('RELEASE SAVEPOINT _rea_sp')
        if re.match(r'\s*(CREATE\s+TABLE|ALTER\s+TABLE)', stripped, re.I):
            tm = re.match(r'\s*(?:CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                          r'|ALTER\s+TABLE\s+)("?' + _IDENT + r'"?)',
                          stripped, re.I)
            catalog.refresh(self._pg_conn,
                            tm.group(1).strip('"').lower()
                            if tm else None)
        if self._cur.description \
                and (self._cur.statusmessage or '').startswith('INSERT'):
            serial = catalog.serial_columns(table_of_insert(sql2) or '')
            if serial:
                row = self._cur.fetchone()
                if row is not None:
                    self.lastrowid = row[0]
        return self


def table_of_insert(sql):
    m = re.search(r'\bINSERT\s+INTO\s+(' + _IDENT + r')', sql, flags=re.I)
    return m.group(1).lower() if m else None


def _split_sql_script(text):
    """Split a sqlite3-style script into statements on top-level ';',
    honoring '--' line comments, '/* */' blocks, and quoted literals."""
    out, buf = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '-' and text[i + 1:i + 2] == '-':
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if c == '/' and text[i + 1:i + 2] == '*':
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and text[j + 1:j + 2] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            j = text.find('"', i + 1)
            j = n - 1 if j < 0 else j
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if c == ';':
            out.append(''.join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = ''.join(buf)
    if tail.strip():
        out.append(tail)
    return [s for s in out if s.strip()]


class CompatConnection:
    """sqlite3.Connection-shaped wrapper over a psycopg2 connection: .execute
    (implicit cursor), .commit/.rollback/.close, and per-cursor Row access."""

    def __init__(self, pg_conn, catalog, pool=None):
        self._conn = pg_conn
        self._catalog = catalog
        self._pool = pool
        self.row_factory = None  # accepted; CompatRow is always provided

    def cursor(self, *a, **k):
        return CompatCursor(self._conn.cursor(), self._catalog,
                            pg_conn=self._conn)

    def execute(self, sql, params=None):
        cur = self.cursor()
        return cur.execute(sql, params)

    def executescript(self, script):
        """sqlite3's multi-statement runner. Each statement goes through the
        same translate + savepoint path as a plain execute."""
        cur = self.cursor()
        for stmt in _split_sql_script(script):
            cur._execute_translated(stmt, None)
        return None

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        with contextlib.suppress(Exception):
            self._conn.rollback()
        if self._pool is not None:
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        pass


class _Catalog:
    """Per-connection cache of table -> columns/types/PKs/serials, built
    from the Postgres catalogs and used by the SQL translator."""

    def __init__(self):
        self._ctrl = None  # dedicated savepoint-bookkeeping cursor; using
        # the statement cursor for RELEASE would discard its result set
        # (psycopg2 replaces the pending results on the next execute).
        self._tables = {}
        self._pks = {}
        self._serials = {}
        self._lock = threading.Lock()

    def ctrl(self):
        if self._ctrl is None:
            self._ctrl = self._conn.cursor()
        return self._ctrl

    def refresh(self, conn, table=None):
        """Refresh catalog metadata. Full sweep when table is None (init:
        3 queries per table); incremental (3 queries) for the one table a
        CREATE/ALTER just touched -- a full sweep per DDL statement was the
        dominant cost of schema-heavy startup (67 tables x 3 catalog
        queries per CREATE). `conn` is the raw psycopg2 connection to
        introspect on (the catalog is process-global and connection-free
        so pooled checkouts share it)."""
        with self._lock:
            if table:
                table = table.lower()
                cur = conn.cursor()
                self._tables[table] = dict(_pg_column_meta(cur, table))
                self._pks[table] = _pg_primary_keys(cur, table)
                self._serials[table] = _pg_serial_columns(cur, table)
                cur.close()
                return
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                self._tables[t.lower()] = dict(_pg_column_meta(cur, t))
                self._pks[t.lower()] = _pg_primary_keys(cur, t)
                self._serials[t.lower()] = _pg_serial_columns(cur, t)
            cur.close()

    def column_types(self, table):
        return self._tables.get(table.lower(), {})

    def pk_columns(self, table):
        return self._pks.get(table.lower(), [])

    def serial_columns(self, table):
        return self._serials.get(table.lower(), [])

    def translate(self, sql, allow_returning=False):
        s = _translate_placeholders(sql)
        s = _translate_sqlite_master(s)
        s = _translate_strftime(s)
        s = _translate_boolean_sum(s)
        s = _translate_insert_ignore(s)
        s = _translate_insert_replace(
            s, {t: pks for t, pks in self._pks.items() if pks},
            self._tables, self._tables)
        s = _cast_columns(s, self._tables, self._tables)
        s = translate_ddl(s) if re.match(r'\s*CREATE\s+(TABLE|INDEX|UNIQUE)',
                                         s, flags=re.I) else s
        return s

    def adapt_params(self, sql, params):
        if params is None:
            return None
        if isinstance(params, dict):
            return params
        if isinstance(params, (tuple, list)):
            # Python bool -> Postgres boolean vs SQLite's 0/1 INTEGER: the
            # codebase passes raw bools into INTEGER columns (SQLite stored
            # them as ints); normalize to int for parity.
            params = tuple(int(v) if isinstance(v, bool) else v
                           for v in params)
        # psycopg2 adapts tuples/lists natively.
        return tuple(params) if not isinstance(params, (tuple, list)) \
            else params

    def maybe_add_returning(self, sql, table):
        if not table:
            return sql
        if re.search(r'\bRETURNING\b', sql, flags=re.I):
            return sql
        serial = self.serial_columns(table)
        if serial and re.match(r'\s*INSERT\s+INTO', sql, flags=re.I):
            return sql.rstrip().rstrip(';') + f' RETURNING {serial[0]}'
        return sql


_DSN_ENV = 'THINK_TANK_DB_URL'
_CATALOG = _Catalog()
_POOL = None
_POOL_LOCK = threading.Lock()
_READONLY = False

_READ_ONLY_OK_RE = re.compile(
    r'\s*(SELECT|WITH|PRAGMA|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|'
    r'EXPLAIN)\b', re.I)


def set_read_only(flag):
    """Process-wide write guard for the read-only replica: every
    non-reading statement raises before it reaches the server. Belt to the
    middleware's suspenders -- a stray code path cannot mutate the store
    the primary tick-writer owns."""
    global _READ_ONLY
    _READ_ONLY = bool(flag)


def is_postgres():
    return bool(_dsn())


def _test_sandboxed():
    """True when this process must NEVER open the production database: a
    pytest run (tests/conftest.py copies the DB and points THINK_TANK_DB at
    the copy) or any process that sets THINK_TANK_FORCE_SQLITE explicitly.
    This is the guard that keeps the repo-.env DSN -- a PRODUCTION pointer --
    from silently redirecting test reads and writes into the live world:
    when the DSN was added to .env, every unguarded test process started
    hitting Postgres instead of its own isolated SQLite copy, and fixture
    saves clobbered the live row tables."""
    import os
    if os.environ.get('THINK_TANK_FORCE_SQLITE'):
        return True
    import sys
    return 'pytest' in sys.modules


def _dsn():
    """THINK_TANK_DB_URL from the process environment, else from the repo
    .env (the project's config convention; _load_env-style parse). The file
    is read once per process: config changes need a restart, like every
    other .env key. Tests NEVER get the .env DSN: _test_sandboxed() forces
    the SQLite path so a test run can never touch production."""
    import os
    if _test_sandboxed():
        return ''
    dsn = os.environ.get(_DSN_ENV)
    if dsn:
        return dsn
    cached = _dsn.__dict__.get('cache')
    if cached is None:
        env_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), '.env')
        cached = ''
        try:
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') \
                            and line.startswith(_DSN_ENV + '='):
                        cached = line.split('=', 1)[1].strip()
        except OSError:
            pass
        _dsn.cache = cached
    return cached or None


def _get_pool(dsn):
    """Threaded pool: a fresh PG connection per _db() call cost 10-30ms of
    connect handshake versus sqlite3's ~0.1ms file open; _db() is called
    dozens of times per request and per tick. Connections are checked out
    per call and returned on close (the _db() wrapper always commits or
    rolls back before closing, so a returned connection is idle-clean)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            import psycopg2.pool
            _POOL = psycopg2.pool.ThreadedConnectionPool(
                1, 50, dsn, **_PG_CONNECT_KWARGS())
        return _POOL


def _PG_CONNECT_KWARGS():
    """Liveness bounds for every Postgres connection (2026-11-02).

    The tank froze four times in one afternoon with the tick breadcrumb at a
    DIFFERENT _db() call site each time (save_blob_write, save_committed,
    ...): the common resource is the connection, not the statement. The DB is
    reached over a Tailscale link; a black-holed connect or a silently stalled
    socket used to hang a fresh connection FOREVER (no timeouts were set), one
    wedged pass held _PASS_LOCK, and the whole tank stopped. These bounds turn
    a dead link into a fast, retryable error instead of a frozen tank:
    - connect_timeout 10s: a black-holed TCP connect fails instead of hanging;
    - TCP keepalives (30s idle, 10s interval, 3 probes): a silently dead
      socket is detected in ~1 minute;
    - statement_timeout 60s (set session-level on checkout): a wedged query
      self-aborts rather than pinning the connection.
    """
    return {
        'connect_timeout': 10,
        'keepalives': 1,
        'keepalives_idle': 30,
        'keepalives_interval': 10,
        'keepalives_count': 3,
    }


_STATEMENT_TIMEOUT_MS = 60000


def _prime_session(raw):
    """Set the session statement timeout on a checked-out connection.
    Best-effort: if the SET itself fails, the connection is still usable for
    callers that tolerate degraded liveness bounds."""
    try:
        cur = raw.cursor()
        cur.execute('SET statement_timeout = %d' % _STATEMENT_TIMEOUT_MS)
        raw.commit()
        cur.close()
    except Exception:
        try:
            raw.rollback()
        except Exception:
            pass


def _apply_socket_timeouts(raw):
    """Bound the CLIENT side of every statement and commit.

    The DB rides a Tailscale link; when packets vanish mid-flight the socket
    just goes quiet. TCP keepalives never fire (the connection is not idle
    while unacked data is pending) and the server-side statement_timeout
    never triggers (the server never received the query) -- so libpq's recv
    waited forever and one wedged pass froze the whole tank (observed twice
    on 2026-11-04: a commit inside log_action, then a savepoint release).
    SO_RCVTIMEO/SO_SNDTIMEO on the connection's own fd bound every network
    wait client-side, including COMMIT, which statement_timeout does not
    cover. A timed-out socket raises OperationalError -- a fast, retryable
    error instead of a frozen tank.

    The socket object is constructed over the EXISTING fd and immediately
    detached, so ownership (and closing) stays with psycopg2.
    """
    import socket as _socket
    import struct as _struct
    tv = _struct.pack('ll', _SOCKET_TIMEOUT_S, 0)
    try:
        fd = raw.fileno()
        if not fd or fd < 0:
            return
        s = _socket.socket(fileno=fd)
        try:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVTIMEO, tv)
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDTIMEO, tv)
        finally:
            s.detach()
    except Exception:
        # Options are a hardening layer, not a dependency: a platform that
        # refuses them still gets the connect/keepalive/statement bounds.
        pass


_SOCKET_TIMEOUT_S = 90


def _checkout(dsn):
    try:
        pool = _get_pool(dsn)
        raw = pool.getconn()
        _apply_socket_timeouts(raw)
        _prime_session(raw)
        return raw, pool
    except Exception:
        # Pool exhausted or unusable: fall back to an unpooled connection
        # rather than failing the request (the caller closes it outright).
        # The connect kwargs matter MOST here: the fallback fires exactly
        # when the pool is wedged, and an unbounded connect used to hang the
        # calling pass forever on a stalled link.
        import psycopg2
        raw = psycopg2.connect(dsn, **_PG_CONNECT_KWARGS())
        _apply_socket_timeouts(raw)
        _prime_session(raw)
        return raw, None


def connect(path=None):
    """The _db() choke point. THINK_TANK_DB_URL (env or repo .env) set ->
    a pooled Postgres Compat connection; unset -> the historical sqlite3
    connection (path defaults to the caller's DB_PATH)."""
    import os
    dsn = _dsn()
    if not dsn:
        target = path or os.environ.get('THINK_TANK_DB') or 'think_tank.db'
        return sqlite3.connect(target)
    try:
        raw, pool = _checkout(dsn)
    except Exception as e:  # pragma: no cover - operator-environment error
        raise PostgresUnavailable(f'cannot connect to {dsn}: {e}') from e
    if not _CATALOG._tables:
        _CATALOG.refresh(raw)
    return CompatConnection(raw, _CATALOG, pool=pool)
