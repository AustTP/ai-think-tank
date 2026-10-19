"""CAPTCHA relay for browser_act.

The browser driver detects a CAPTCHA and returns a stable selector. This module
records the request, stores the owner's answer, and lets the broker fill that
answer into the waiting page. It does not bypass an interactive challenge.

Policy is per host and defaults to `relay`. `attempt` only means the server may
try a vision read of a simple image CAPTCHA before asking the owner. Interactive
challenges (reCAPTCHA, hCaptcha, Turnstile, Arkose) always go to the owner.
"""
import secrets
import time

DEFAULT_TTL_S = 600
MIN_TTL_S = 60
MAX_TTL_S = 1800
ANSWER_MAX = 64
MODES = ('relay', 'attempt')
INTERACTIVE = ('recaptcha', 'hcaptcha', 'turnstile', 'arkose')


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS captcha_requests (
        id              TEXT PRIMARY KEY,
        agent_id        TEXT NOT NULL,
        sandbox_id      TEXT NOT NULL,
        host            TEXT,
        page_url        TEXT,
        kind            TEXT NOT NULL,
        selector        TEXT,
        input_selector  TEXT,
        submit_selector TEXT,
        purpose         TEXT,
        status          TEXT NOT NULL DEFAULT 'pending',
        answer          TEXT,
        answered_by     TEXT,
        created_at      REAL NOT NULL,
        expires_at      REAL NOT NULL,
        answered_at     REAL,
        solved_at       REAL,
        last_error      TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS captcha_host_policies (
        host        TEXT PRIMARY KEY,
        mode        TEXT NOT NULL DEFAULT 'relay',
        notes       TEXT,
        updated_by  TEXT,
        updated_at  REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_captcha_requests_agent '
                 'ON captcha_requests(agent_id, created_at)')


def _serve():
    import serve as _s
    return _s


def _row(row):
    if row is None:
        return None
    keys = ('id', 'agentId', 'sandboxId', 'host', 'pageUrl', 'kind', 'selector',
            'inputSelector', 'submitSelector', 'purpose', 'status', 'answer',
            'answeredBy', 'createdAt', 'expiresAt', 'answeredAt', 'solvedAt',
            'lastError')
    return dict(zip(keys, row))


def policy_for_host(host):
    host = (host or '').strip().lower()
    if not host:
        return 'relay'
    s = _serve()
    with s._db() as conn:
        row = conn.execute('SELECT mode FROM captcha_host_policies WHERE host = ?',
                           (host,)).fetchone()
    if row and row[0] in MODES:
        return row[0]
    return 'relay'


def set_policy(host, mode, notes='', updated_by='player'):
    host = (host or '').strip().lower()
    if not host:
        raise ValueError('host is required')
    if mode not in MODES:
        raise ValueError('mode must be relay or attempt')
    s = _serve()
    now = time.time()
    with s._db() as conn:
        conn.execute(
            'INSERT INTO captcha_host_policies (host, mode, notes, updated_by, updated_at) '
            'VALUES (?, ?, ?, ?, ?) ON CONFLICT(host) DO UPDATE SET '
            'mode=excluded.mode, notes=excluded.notes, updated_by=excluded.updated_by, '
            'updated_at=excluded.updated_at',
            (host, mode, (notes or '')[:500], updated_by, now))
    s.log_action(updated_by, 'captcha_policy', {'host': host, 'mode': mode},
                 authorized=True)
    return {'host': host, 'mode': mode, 'notes': notes or '', 'updatedAt': now}


def list_policies():
    s = _serve()
    with s._db() as conn:
        rows = conn.execute(
            'SELECT host, mode, notes, updated_by, updated_at '
            'FROM captcha_host_policies ORDER BY host').fetchall()
    return [{'host': r[0], 'mode': r[1], 'notes': r[2], 'updatedBy': r[3],
             'updatedAt': r[4]} for r in rows]


def find_open(agent_id, sandbox_id, page_url, kind):
    """Return an existing pending/answered request for the same page, so a
    second read of the same challenge does not spam Telegram."""
    s = _serve()
    now = time.time()
    with s._db() as conn:
        row = conn.execute(
            'SELECT id, agent_id, sandbox_id, host, page_url, kind, selector, '
            'input_selector, submit_selector, purpose, status, answer, answered_by, '
            'created_at, expires_at, answered_at, solved_at, last_error '
            'FROM captcha_requests WHERE agent_id = ? AND sandbox_id = ? AND page_url = ? '
            "AND kind = ? AND status IN ('pending', 'answered') AND expires_at > ? "
            'ORDER BY created_at DESC LIMIT 1',
            (agent_id, sandbox_id, page_url or '', kind, now)).fetchone()
    return _row(row)


def create_request(agent_id, sandbox_id, host, page_url, kind, selector='',
                   input_selector='', submit_selector='', purpose='', ttl_s=DEFAULT_TTL_S):
    s = _serve()
    ttl_s = max(MIN_TTL_S, min(int(ttl_s or DEFAULT_TTL_S), MAX_TTL_S))
    now = time.time()
    request_id = 'cap_' + secrets.token_hex(8)
    with s._db() as conn:
        conn.execute(
            'INSERT INTO captcha_requests '
            '(id, agent_id, sandbox_id, host, page_url, kind, selector, input_selector, '
            'submit_selector, purpose, status, created_at, expires_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (request_id, agent_id, sandbox_id, host, page_url, kind, selector,
             input_selector, submit_selector, purpose[:500], 'pending', now, now + ttl_s))
    s.log_action(agent_id, 'captcha_request',
                 {'requestId': request_id, 'host': host, 'kind': kind},
                 authorized=True)
    return get(request_id)


def get(request_id):
    s = _serve()
    with s._db() as conn:
        row = conn.execute(
            'SELECT id, agent_id, sandbox_id, host, page_url, kind, selector, '
            'input_selector, submit_selector, purpose, status, answer, answered_by, '
            'created_at, expires_at, answered_at, solved_at, last_error '
            'FROM captcha_requests WHERE id = ?', (request_id,)).fetchone()
    return _row(row)


def set_answer(request_id, answer, answered_by='player'):
    answer = (answer or '').strip()
    if not answer or len(answer) > ANSWER_MAX:
        raise ValueError('answer is empty or too long')
    s = _serve()
    now = time.time()
    with s._db() as conn:
        row = conn.execute('SELECT status, expires_at FROM captcha_requests WHERE id = ?',
                           (request_id,)).fetchone()
        if row is None:
            return None
        status, expires_at = row
        if now > expires_at:
            conn.execute("UPDATE captcha_requests SET status='expired' WHERE id = ?",
                         (request_id,))
            return None
        if status in ('solved', 'failed', 'expired'):
            return None
        conn.execute(
            "UPDATE captcha_requests SET answer=?, answered_by=?, answered_at=?, "
            "status='answered' WHERE id = ?",
            (answer, answered_by, now, request_id))
    s.log_action(answered_by, 'captcha_answer',
                 {'requestId': request_id, 'answerLength': len(answer)},
                 authorized=True)
    return get(request_id)


def mark_solved(request_id, actor='system'):
    s = _serve()
    now = time.time()
    with s._db() as conn:
        conn.execute("UPDATE captcha_requests SET status='solved', solved_at=? WHERE id=?",
                     (now, request_id))
    s.log_action(actor, 'captcha_solved', {'requestId': request_id}, authorized=True)
    return get(request_id)


def mark_failed(request_id, error='', actor='system'):
    s = _serve()
    with s._db() as conn:
        conn.execute("UPDATE captcha_requests SET status='failed', last_error=? WHERE id=?",
                     (str(error)[:500], request_id))
    s.log_action(actor, 'captcha_failed',
                 {'requestId': request_id, 'error': str(error)[:200]},
                 authorized=True)
    return get(request_id)


def wait_for_answer(request_id, wait_s=120, poll_s=1.0):
    deadline = time.time() + max(1, min(int(wait_s), 300))
    while time.time() < deadline:
        rec = get(request_id)
        if rec is None:
            return None
        if rec['status'] in ('answered', 'solved'):
            return rec
        if rec['status'] in ('failed', 'expired'):
            return rec
        time.sleep(poll_s)
    return get(request_id)


def needs_human(kind):
    return (kind or '').lower() in INTERACTIVE
