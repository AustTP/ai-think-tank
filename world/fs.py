"""Scoped, capability-gated access to the player's own folders (Desktop,
Downloads, ...), structured data (SQLite + gzip + protobuf), and
player-approved apps, for the think tank's agents.

This is the think tank's answer to "give the app access to organize my
Desktop": agents never hold macOS paths or app credentials directly -- they
get scoped tools (`local_file`, `read_store`, `app_script`) whose every call
is checked against a player-issued grant (a scope root + capability set, or an
app bundle id) stored in state. The trust root is the player's explicit grant,
and three hard rules are enforced regardless of any grant:

1. CONTAINMENT: every path is realpath-resolved and must stay inside the
   granted scope root. `..`, symlink escapes, and absolute paths outside the
   scope are refused.
2. TRASH RULE: `~/.Trash` is never a valid target. Deletes move a file INTO
   the trash (reversible) -- they never permanently unlink, and they can never
   EMPTY the trash. `emptyTrash` is a capability that is never granted.
3. READ-ONLY DATA: `read_store` opens SQLite in read-only mode, bounds its
   rows and output size, and never writes.

The module is dependency-free (stdlib only). Grants live in state under
`fileGrants` (files) and `appGrants` (apps). Follows the extracted-module
pattern: lazy `import serve as _serve`, call-time `_serve.X`,
`with _serve._db()` for state reads.
"""
import os
import shutil
import subprocess

_TRASH_RULE = 'the trash is off-limits: deletes move to trash and never empty it'


def _state():
    import serve as _serve
    return _serve.get_state_from_db()


def _trash_dir():
    return os.path.expanduser(os.environ.get('TRASH_DIR', '~/.Trash'))


def _grants(state):
    import serve as _serve
    return _serve._effective_file_grants(state)


def _grant_for(state, scope):
    root = os.path.realpath(os.path.expanduser(scope))
    for g in _grants(state):
        if os.path.realpath(os.path.expanduser(g.get('scope') or '')) == root:
            return g
    return None


def _resolve(grant, rel_path):
    """Resolve a relative path inside a grant's scope, enforcing containment.
    Returns the absolute path, or raises PermissionError."""
    scope = os.path.realpath(os.path.expanduser(grant.get('scope') or ''))
    if not rel_path:
        return scope
    if rel_path.startswith('/'):
        raise PermissionError('path must be relative to the granted scope')
    joined = os.path.realpath(os.path.join(scope, rel_path))
    if joined != scope and not joined.startswith(scope + os.sep):
        raise PermissionError('path escapes the granted scope')
    trash = os.path.realpath(_trash_dir())
    if joined == trash or joined.startswith(trash + os.sep):
        raise PermissionError(_TRASH_RULE)
    return joined


def _require_cap(grant, cap):
    if not (grant.get('caps') or {}).get(cap):
        raise PermissionError(f'grant does not allow {cap}')


def list_scope(scope, rel_path=''):
    """List a directory inside the granted scope (default: the scope root).
    Returns [{name, type, size}]."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'read')
    root = os.path.realpath(os.path.expanduser(scope))
    p = _resolve(grant, rel_path)
    if not os.path.isdir(p):
        raise FileNotFoundError(f'not a directory: {rel_path}')
    entries = []
    for name in sorted(os.listdir(p)):
        fp = os.path.join(p, name)
        entries.append({'name': name,
                        'type': 'dir' if os.path.isdir(fp) else 'file',
                        'size': os.path.getsize(fp) if os.path.isfile(fp) else None})
    return entries


def read_file(scope, rel_path, max_bytes=200000):
    """Read a file inside a granted scope. Returns text (utf-8, errors=replace),
    truncated to max_bytes."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'read')
    p = _resolve(grant, rel_path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f'not a file: {rel_path}')
    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        return f.read(max_bytes)


def write_file(scope, rel_path, content):
    """Create or overwrite a file inside a granted scope (utf-8)."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'write')
    p = _resolve(grant, rel_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(content)
    return p


def move_file(scope, src_rel, dst_rel):
    """Move/rename a file inside a granted scope (organizing the desktop)."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'write')
    src = _resolve(grant, src_rel)
    dst = _resolve(grant, dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return dst


def delete_to_trash(scope, rel_path):
    """Move a file/folder inside a granted scope to the trash (reversible).
    Never empties the trash and never touches the trash itself."""
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'delete')
    p = _resolve(grant, rel_path)
    if not os.path.exists(p):
        raise FileNotFoundError(f'not found: {rel_path}')
    trash = os.path.realpath(_trash_dir())
    os.makedirs(trash, exist_ok=True)
    base = os.path.basename(p)
    dest = os.path.join(trash, base)
    n = 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(trash, f'{stem}-{n}{ext}')
        n += 1
    shutil.move(p, dest)
    return dest


# ---------------------------------------------------------------------------
# Generic structured-data reads: SQLite within a granted scope, with optional
# gzip decompression and protobuf string extraction. Read-only by
# construction -- the DB is opened in immutable, read-only mode, the query
# must be a read (SELECT/PRAGMA/EXPLAIN/WITH), and rows/output are bounded.
# This is the generic capability behind reading a store like macOS Notes'
# NoteStore.sqlite (gzip'd protobuf bodies) without any AppleScript.
# ---------------------------------------------------------------------------

def _osascript(script):
    try:
        proc = subprocess.run(
            ['osascript', '-e', script], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return None, (proc.stderr or '').strip()
        return proc.stdout.strip(), None
    except Exception as e:
        return None, str(e)


def _esc(s):
    return str(s or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


_READ_STORE_MAX_ROWS = 200
_READ_STORE_MAX_OUTPUT = 200_000
_READ_ONLY_QUERY_RE = __import__('re').compile(
    r'^\s*(SELECT|PRAGMA|EXPLAIN|WITH)\b', __import__('re').IGNORECASE)


def decode_proto(buf, depth=0, out=None, max_depth=40):
    """Extract readable strings from a protobuf-encoded buffer (no protobuf
    dependency, no schema). Recurses into length-delimited fields whose bytes
    don't decode as text, so nested message fields surface their strings."""
    if out is None:
        out = []
    if depth > max_depth or not buf:
        return out
    i = 0
    n = len(buf)
    while i < n:
        tag = 0
        shift = 0
        while True:
            if i >= n:
                return out
            b = buf[i]
            i += 1
            tag |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80):
                break
        wt = tag & 7
        if wt == 0:
            while True:
                if i >= n:
                    return out
                b = buf[i]
                i += 1
                if not (b & 0x80):
                    break
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln = 0
            shift = 0
            while True:
                if i >= n:
                    return out
                b = buf[i]
                i += 1
                ln |= (b & 0x7f) << shift
                shift += 7
                if not (b & 0x80):
                    break
            data = buf[i:i + ln]
            i += ln
            try:
                s = data.decode('utf-8')
            except UnicodeDecodeError:
                decode_proto(data, depth + 1, out, max_depth)
                continue
            if s and len(s) > 1 and sum(1 for c in s if c in '\n\t' or 32 <= ord(c) < 127 or ord(c) > 127) >= max(1, int(len(s) * 0.8)):
                out.append(s)
            else:
                decode_proto(data, depth + 1, out, max_depth)
        elif wt == 5:
            i += 4
        else:
            return out
    return out


def core_to_dt(ts):
    """Convert a Core Data timestamp (seconds since 2001-01-01) to a datetime."""
    import datetime
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = 0.0
    return datetime.datetime(2001, 1, 1) + datetime.timedelta(seconds=ts)


def read_store(scope, rel_path, query, gzip_columns=None, proto_columns=None, max_rows=50):
    """Read structured data from a SQLite database inside a granted scope.

    The DB is opened in immutable read-only mode, so no write, journal, or
    side-effect is possible. `gzip_columns` and `proto_columns` name columns
    to decompress / protobuf-extract before they're returned (applied in that
    order). Returns a list of row dicts (values as JSON-safe strings)."""
    import gzip
    import json
    import sqlite3
    state = _state()
    grant = _grant_for(state, scope)
    if not grant:
        raise PermissionError('no grant for this scope')
    _require_cap(grant, 'read')
    p = _resolve(grant, rel_path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f'not a file: {rel_path}')
    q = (query or '').strip()
    if not _READ_ONLY_QUERY_RE.match(q):
        raise PermissionError('query must be read-only (SELECT/PRAGMA/EXPLAIN/WITH)')
    gzip_cols = set(gzip_columns or [])
    proto_cols = set(proto_columns or [])
    max_rows = max(1, min(int(max_rows or 50), _READ_STORE_MAX_ROWS))
    conn = sqlite3.connect(f'file:{p}?mode=ro&immutable=1', uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(q).fetchmany(max_rows)
    finally:
        conn.close()

    def _decode(col, val):
        if val is None:
            return None
        if col in gzip_cols and isinstance(val, bytes):
            try:
                val = gzip.decompress(val)
            except Exception:
                pass
        if col in proto_cols and isinstance(val, bytes):
            try:
                val = '\n'.join(decode_proto(val))
            except Exception:
                val = val.decode('utf-8', errors='replace')
        if isinstance(val, bytes):
            val = val.decode('utf-8', errors='replace')
        return val

    out = []
    used = 0
    for r in rows:
        row = {k: _decode(k, r[k]) for k in r.keys()}
        out.append(row)
        used += len(json.dumps(row))
        if used >= _READ_STORE_MAX_OUTPUT:
            break
    return out


# ---------------------------------------------------------------------------
# App control via AppleScript: agents drive player-approved apps (by bundle
# id) for real work -- e.g. asking the current document app for its text, or
# triggering an export in a desktop tool that has no API. Gated the same way
# file access is: a player-issued `appGrants` entry (bundleId + caps) in
# state, checked per call, and the script body is validated so the ONLY thing
# it can do is talk to the granted app -- no shell, no URL opening, no
# privilege escalation, no cross-app scripting. AppleScript primitives that
# would escape the granted app (a raw shell is a filesystem + internet bypass
# around the file grants and the Jev egress gates) fail CLOSED.
# ---------------------------------------------------------------------------

_APP_DENY_PATTERNS = (
    'do shell',                    # arbitrary shell execution (filesystem + internet bypass)
    'open location',               # open an arbitrary URL / launch a different app
    'with administrator privileges',  # privilege escalation
    'tell application',            # script a different app than the one granted
    'current application',         # bridge to arbitrary Objective-C classes
)


def _app_grants(state):
    import serve as _serve
    return _serve._effective_app_grants(state)


def _app_grant_for(state, bundle_id):
    bid = (bundle_id or '').strip()
    for g in _app_grants(state):
        if (g.get('bundleId') or '').strip() == bid:
            return g
    return None


def _validate_app_script(script):
    s = (script or '').strip()
    if not s:
        raise PermissionError('empty AppleScript body')
    low = s.lower()
    for pat in _APP_DENY_PATTERNS:
        if pat in low:
            raise PermissionError(
                f'script uses a denied AppleScript primitive ({pat!r}) -- app_script only '
                'talks to the granted app, never a shell, a URL, or another app')
    return s


def app_script(bundle_id, script):
    """Run an AppleScript body against ONE player-approved app. The body is
    validated (no shell / URL / escalation / cross-app primitives), wrapped in
    `tell application id "<bundleId>"`, and executed via osascript. Returns the
    script's text output, or raises PermissionError on any denial."""
    state = _state()
    grant = _app_grant_for(state, bundle_id)
    if not grant:
        raise PermissionError(f'no grant for app {bundle_id!r} -- the player must grant it first')
    _require_cap(grant, 'use')
    body = _validate_app_script(script)
    wrapped = f'tell application id "{_esc(bundle_id)}"\n{body}\nend tell'
    out, err = _osascript(wrapped)
    if err:
        raise PermissionError(f'AppleScript failed for {bundle_id}: {err}')
    return out or '(no output)'