"""Tank-owned account identities and one-time-code access.

The tank creates its own accounts, so it owns the TOTP secret from the start.
Secrets live in the existing Fernet credential vault (`external_credentials`)
under a `tank_account:<id>` name. This module stores only metadata; passwords,
TOTP secrets, and recovery codes stay encrypted.

Agents never receive a secret. An owner mints a single-use grant for one
account, and the agent redeems it for the current code. Codes are short-lived
and are never logged.
"""
import secrets
import time

import totp

DEFAULT_GRANT_S = 300
MIN_GRANT_S = 60
MAX_GRANT_S = 900
VAULT_PREFIX = 'tank_account:'
STATUSES = ('active', 'disabled')


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS tank_accounts (
        id              TEXT PRIMARY KEY,
        service         TEXT NOT NULL,
        account         TEXT NOT NULL,
        username        TEXT,
        credential_name TEXT NOT NULL UNIQUE,
        status          TEXT NOT NULL DEFAULT 'active',
        owner           TEXT NOT NULL,
        created_at      REAL NOT NULL,
        updated_at      REAL,
        last_code_at    REAL,
        last_login_at   REAL,
        last_rotated_at REAL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tank_account_grants (
        grant_id    TEXT PRIMARY KEY,
        account_id  TEXT NOT NULL,
        agent_id    TEXT NOT NULL,
        granted_by  TEXT NOT NULL,
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL,
        used_at     REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tank_account_grants_account '
                 'ON tank_account_grants(account_id)')


def _serve():
    import serve as _s
    return _s


def _vault_name(account_id):
    return f'{VAULT_PREFIX}{account_id}'


def _row(row):
    if row is None:
        return None
    keys = ('id', 'service', 'account', 'username', 'credentialName', 'status',
            'owner', 'createdAt', 'updatedAt', 'lastCodeAt', 'lastLoginAt',
            'lastRotatedAt')
    return dict(zip(keys, row))


def _fetch(account_id):
    s = _serve()
    with s._db() as conn:
        row = conn.execute(
            'SELECT id, service, account, username, credential_name, status, owner, '
            'created_at, updated_at, last_code_at, last_login_at, last_rotated_at '
            'FROM tank_accounts WHERE id = ?', (account_id,)).fetchone()
    return _row(row)


def _totp_fields(secret_or_uri):
    """Return (secret, algorithm, digits, period, issuer, label)."""
    value = (secret_or_uri or '').strip()
    if value.startswith('otpauth://'):
        parsed = totp.parse_otpauth(value)
        return (parsed['secret'], parsed['algorithm'], parsed['digits'],
                parsed['period'], parsed['issuer'], parsed['account'])
    secret = totp.normalize_secret(value) if value else totp.generate_secret()
    return (secret, totp.DEFAULT_ALGORITHM, totp.DEFAULT_DIGITS,
            totp.DEFAULT_PERIOD, '', '')


def create(service='', account='', username='', password='', totp_secret='',
           recovery_codes=None, owner='player', status='active'):
    """Create a tank-owned account record and seal its secrets.

    `totp_secret` may be a base32 secret or the full otpauth:// URI a site
    shows as a QR code. Missing values are generated.
    """
    s = _serve()
    secret, algorithm, digits, period, issuer, label = _totp_fields(totp_secret)
    service = (service or issuer or '').strip()
    account = (account or label or username or '').strip()
    username = (username or account).strip()
    if not service:
        raise ValueError('service is required')
    if not account:
        raise ValueError('account is required')
    if status not in STATUSES:
        raise ValueError('invalid status')
    password = password or secrets.token_urlsafe(24)
    recovery_codes = list(recovery_codes or totp.recovery_codes())
    now = time.time()
    account_id = 'acc_' + secrets.token_hex(12)
    credential_name = _vault_name(account_id)
    payload = {
        'kind': 'login',
        'username': username,
        'password': password,
        'totp_secret': secret,
        'totp_algorithm': algorithm,
        'totp_digits': int(digits),
        'totp_period': int(period),
        'recovery_codes': recovery_codes,
    }
    s._store_credential(credential_name, service, payload, status=status,
                        proposed_by=owner)
    try:
        with s._db() as conn:
            conn.execute(
                'INSERT INTO tank_accounts '
                '(id, service, account, username, credential_name, status, owner, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (account_id, service, account, username, credential_name, status,
                 owner, now, now))
    except Exception:
        s._delete_credential(credential_name)
        raise
    s.log_action(owner, 'tank_account_create',
                 {'accountId': account_id, 'service': service, 'account': account},
                 authorized=True)
    rec = _fetch(account_id)
    rec['provisioningUri'] = totp.provisioning_uri(
        secret, account, issuer=service, digits=digits, period=period,
        algorithm=algorithm)
    # Shown once, at creation, so the human can keep an offline backup.
    rec['recoveryCodes'] = recovery_codes
    return rec


def list_accounts():
    s = _serve()
    with s._db() as conn:
        rows = conn.execute(
            'SELECT id, service, account, username, credential_name, status, owner, '
            'created_at, updated_at, last_code_at, last_login_at, last_rotated_at '
            'FROM tank_accounts ORDER BY created_at DESC').fetchall()
    return [_row(r) for r in rows]


def get(account_id):
    return _fetch(account_id)


def code(account_id, actor='player', agent_id=None):
    s = _serve()
    meta = _fetch(account_id)
    if meta is None:
        raise KeyError('account not found')
    if meta['status'] != 'active':
        raise ValueError('account is not active')
    payload = s._credential_payload(meta['credentialName'])
    if not payload or not payload.get('totp_secret'):
        raise ValueError('TOTP secret is unavailable')
    info = totp.code_info(
        payload['totp_secret'],
        digits=int(payload.get('totp_digits') or totp.DEFAULT_DIGITS),
        period=int(payload.get('totp_period') or totp.DEFAULT_PERIOD),
        algorithm=payload.get('totp_algorithm') or totp.DEFAULT_ALGORITHM,
    )
    now = time.time()
    with s._db() as conn:
        conn.execute('UPDATE tank_accounts SET last_code_at = ? WHERE id = ?',
                     (now, account_id))
    s.log_action(agent_id or actor, 'tank_account_code',
                 {'accountId': account_id, 'service': meta['service'],
                  'agentId': agent_id}, authorized=True)
    return {
        'accountId': account_id,
        'service': meta['service'],
        'account': meta['account'],
        'code': info['code'],
        'expiresIn': info['expiresIn'],
        'period': info['period'],
        'digits': info['digits'],
        'algorithm': info['algorithm'],
    }


def rotate(account_id, actor='player', new_secret=''):
    s = _serve()
    meta = _fetch(account_id)
    if meta is None:
        raise KeyError('account not found')
    payload = s._credential_payload(meta['credentialName'])
    if not payload:
        raise ValueError('credential payload is unavailable')
    secret, algorithm, digits, period, _, _ = _totp_fields(new_secret)
    payload['totp_secret'] = secret
    payload['totp_algorithm'] = algorithm
    payload['totp_digits'] = int(digits)
    payload['totp_period'] = int(period)
    s._store_credential(meta['credentialName'], meta['service'], payload,
                        status=meta['status'], proposed_by=meta['owner'])
    with s._db() as conn:
        conn.execute('UPDATE tank_accounts SET last_rotated_at = ?, updated_at = ? '
                     'WHERE id = ?', (time.time(), time.time(), account_id))
        conn.execute('DELETE FROM tank_account_grants WHERE account_id = ?', (account_id,))
    s.log_action(actor, 'tank_account_rotate',
                 {'accountId': account_id, 'service': meta['service']},
                 authorized=True)
    return {
        'accountId': account_id,
        'provisioningUri': totp.provisioning_uri(
            secret, meta['account'], issuer=meta['service'], digits=digits,
            period=period, algorithm=algorithm),
        'rotatedAt': time.time(),
    }


def delete(account_id, actor='player'):
    s = _serve()
    meta = _fetch(account_id)
    if meta is None:
        raise KeyError('account not found')
    s._delete_credential(meta['credentialName'])
    with s._db() as conn:
        conn.execute('DELETE FROM tank_account_grants WHERE account_id = ?', (account_id,))
        conn.execute('DELETE FROM tank_accounts WHERE id = ?', (account_id,))
    s.log_action(actor, 'tank_account_delete',
                 {'accountId': account_id, 'service': meta['service']},
                 authorized=True)
    return {'ok': True, 'accountId': account_id}


def mint_grant(account_id, agent_id, granted_by='player', ttl_s=DEFAULT_GRANT_S):
    s = _serve()
    meta = _fetch(account_id)
    if meta is None:
        raise KeyError('account not found')
    if meta['status'] != 'active':
        raise ValueError('account is not active')
    agent_id = (agent_id or '').strip()
    if not agent_id:
        raise ValueError('agentId is required')
    ttl_s = max(MIN_GRANT_S, min(int(ttl_s or DEFAULT_GRANT_S), MAX_GRANT_S))
    grant_id = secrets.token_hex(16)
    now = time.time()
    with s._db() as conn:
        conn.execute(
            'INSERT INTO tank_account_grants '
            '(grant_id, account_id, agent_id, granted_by, created_at, expires_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (grant_id, account_id, agent_id, granted_by, now, now + ttl_s))
    s.log_action(granted_by, 'tank_account_grant',
                 {'accountId': account_id, 'agentId': agent_id, 'ttlSec': ttl_s},
                 authorized=True)
    return {'grant': grant_id, 'accountId': account_id, 'agentId': agent_id,
            'expiresIn': ttl_s}


def consume_grant(grant_id, agent_id, account_id):
    s = _serve()
    now = time.time()
    with s._db() as conn:
        row = conn.execute(
            'SELECT account_id, agent_id, expires_at, used_at FROM tank_account_grants '
            'WHERE grant_id = ?', (grant_id,)).fetchone()
        if row is None:
            return False
        grant_account, grant_agent, expires_at, used_at = row
        if used_at is not None or now > expires_at:
            conn.execute('DELETE FROM tank_account_grants WHERE grant_id = ?', (grant_id,))
            return False
        if grant_account != account_id or grant_agent != agent_id:
            return False
        updated = conn.execute(
            'UPDATE tank_account_grants SET used_at = ? '
            'WHERE grant_id = ? AND used_at IS NULL', (now, grant_id))
        if updated.rowcount != 1:
            return False
    return True
