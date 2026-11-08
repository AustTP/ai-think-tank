"""Tank Mission Control realtime: sealed live queries.

Reverse-engineered from @neon/realtime 0.2.0 (Apache-2.0) and rebuilt
self-hosted for the think tank:

- The SERVER seals an allowlisted query into a short-lived AES-GCM
  capability ({q, p, exp, fp}); the browser never sees SQL or credentials.
- The browser subscribes over one WebSocket with the capability; the proxy
  runs the query and pushes fresh rows whenever the underlying tables
  change (pg_notify -> debounced re-run), so no panel ever polls.
- Everything reads through the tank's own db_backend connection; only the
  Postgres LISTEN thread is Postgres-specific.

Mission Control invariants honored here: read-only toward the data the
panels show, only queries from the allowlist directory are ever sealed or
run, and secrets stay server-side.
"""
import base64
import hashlib
import json
import os
import re
import time

_QUERIES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'mc_queries')
_CAP_TTL_MS = 60_000
_NAME_RE = re.compile(r'^[a-z0-9-]+$')
_QUERIES = None
_KEY = None

TABLE_HEADER_RE = re.compile(r'^--\s*tables:\s*(.+)$', re.MULTILINE)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64url_dec(text: str) -> bytes:
    pad = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _key() -> bytes:
    global _KEY
    if _KEY is None:
        secret = os.environ.get('MC_REALTIME_SECRET')
        if not secret:
            dsn = os.environ.get('THINK_TANK_DB_URL') or 'local'
            secret = 'derived:' + hashlib.sha256(
                ('mc-realtime-v1:' + dsn).encode()).hexdigest()
        _KEY = hashlib.sha256(('mc-seal:' + secret).encode()).digest()
    return _KEY


def load_queries(dir_path=None):
    """The seal/run allowlist: every mc_queries/*.sql by its file name."""
    global _QUERIES
    source = dir_path or _QUERIES_DIR
    queries = {}
    for fn in sorted(os.listdir(source)):
        if not fn.endswith('.sql') or not _NAME_RE.match(fn[:-4]):
            raise ValueError(f'mc query file name not allowed: {fn}')
        with open(os.path.join(source, fn), encoding='utf-8') as fh:
            sql = fh.read().strip()
        m = TABLE_HEADER_RE.search(sql)
        tables = frozenset(
            t.strip() for t in m.group(1).split(',') if t.strip()) if m \
            else frozenset()
        queries[fn[:-4]] = {'sql': sql, 'tables': tables}
    _QUERIES = queries
    return queries


def _queries():
    if _QUERIES is None:
        load_queries()
    return _QUERIES


def query_tables(name: str) -> frozenset:
    q = _queries().get(name)
    return q['tables'] if q else frozenset()


def query_fingerprint(name: str, params=None) -> str:
    q = _queries().get(name)
    if q is None:
        raise KeyError(name)
    raw = json.dumps({'sql': q['sql'], 'params': list(params or [])},
                     sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def seal(name: str, params=None, now_ms=None):
    """Seal an ALLOWLISTED query into a bearer capability. Raises KeyError
    for a name outside the allowlist -- the browser can never introduce
    SQL, only reference these files."""
    if name not in _queries():
        raise KeyError(name)
    if params is None:
        params = []
    if not isinstance(params, (list, tuple)):
        raise TypeError('params must be a list')
    now_ms = int(now_ms or time.time() * 1000)
    payload = {
        'q': name,
        'p': list(params),
        'exp': now_ms + _CAP_TTL_MS,
        'fp': query_fingerprint(name, params),
        'iat': now_ms,
    }
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(_key()).encrypt(iv, json.dumps(payload).encode(), b'mc1')
    return {
        'capability': f'mc1.{_b64url(iv)}.{_b64url(ct)}',
        'queryFingerprint': payload['fp'],
        'expiresAt': payload['exp'],
    }


def unseal(capability: str, now_ms=None):
    """Verify + decrypt a capability back to (name, params, fingerprint).
    Raises ValueError for anything tampered, malformed, or expired."""
    now_ms = int(now_ms or time.time() * 1000)
    try:
        kind, iv_b64, ct_b64 = capability.split('.', 2)
        if kind != 'mc1':
            raise ValueError('unknown capability kind')
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(_key()).decrypt(_b64url_dec(iv_b64),
                                       _b64url_dec(ct_b64), b'mc1')
        payload = json.loads(plain)
    except Exception as exc:
        raise ValueError(f'invalid capability: {exc}') from exc
    if int(payload.get('exp') or 0) < now_ms:
        raise ValueError('capability expired')
    name = payload.get('q')
    if name not in _queries():
        raise ValueError('capability names a query outside the allowlist')
    params = payload.get('p') or []
    if query_fingerprint(name, params) != payload.get('fp'):
        raise ValueError('capability fingerprint mismatch')
    return name, params, payload['fp']


def run_query(name: str, params=None):
    """Run an allowlisted query through db_backend (read path only)."""
    q = _queries().get(name)
    if q is None:
        raise KeyError(name)
    import db_backend
    conn = db_backend.connect()
    try:
        cur = conn.execute(q['sql'], list(params or []))
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        close = getattr(conn, 'close', None)
        if close:
            close()
