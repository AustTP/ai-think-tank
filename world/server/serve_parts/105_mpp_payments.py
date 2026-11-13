# MPP -- Machine Payments Protocol (operator decision, 2026-11-13).
#
# The rail lets the tank settle PER-CALL micro-payments against vendors that
# price through a payment challenge (HTTP 402 + a machine-readable
# Payment-Required payload) instead of prepaid API keys. This is a governance
# shift the operator accepted: the transports gain a bounded spending
# instrument that is not a monthly-key allowance. The fences, in order:
#
#   1. DORMANT BY DEFAULT. Nothing pays until the operator sets
#      MPP_ENABLED=true in .env AND configures a treasury (MPP_SETTLE_URL +
#      MPP_WALLET_KEY). With MPP off, a 402 behaves exactly as before.
#   2. ONE LANE, ONE CAP. Every settled payment accrues to the __mpp__
#      treasury lane (MPP_SPEND_CAP_USD, month-to-date) -- the same lane-cap
#      machinery that bounds JEV decisions. Tripped treasury = every payment
#      refused, one alert.
#   3. PER-VENDOR CAP for free: a payment is also accrued to the payee's
#      provider rollup (__provider__/<vendor>), so the standard
#      <VENDOR>_SPEND_CAP_USD allowance governs it with zero new code.
#   4. PER-CALL CEILING (MPP_MAX_PAYMENT_USD, default $0.05): a challenge
#      asking for more is refused and ledgered, never paid.
#   5. ESCALATION FLOOR (MPP_ESCALATION_USD, default $1.00): above it the
#      payment is refused and logged for the operator -- no autonomous
#      large spends, by construction.
#   6. EVERY DECISION IS LEDGERED. Paid or refused, a row lands in
#      mpp_payments with the reason; paid rows also log_action so the
#      activity feed shows them like any other spend.
#   7. EXACTLY ONE RETRY. A paid challenge retries the original POST once;
#      a second 402 fails the call like any other error.
#
# Registering an MPP-priced vendor is one registry entry in _PROVIDER_DIALING
# with settlement='mpp' (see 130_run_browser_act_sync.py) -- the same
# provider-generic shape as every other dialing vendor.
import base64 as _base64
import json as _json
import secrets as _secrets
import time as _time
import urllib.parse as _urlparse
import urllib.request as _urlrequest

# The treasury: an empty cap means no money is authorized at all. 0.50
# mirrors the JEV lane -- enough to prove the rail end to end, small enough
# that a runaway cannot matter.
MPP_SPEND_CAP_USD = float(_load_env().get('MPP_SPEND_CAP_USD', '0') or 0)
# Per-call ceiling: a challenge above this is refused and ledgered.
MPP_MAX_PAYMENT_USD = float(_load_env().get('MPP_MAX_PAYMENT_USD', '0.05') or 0.05)
# Escalation floor: above this the operator must move first. No autonomous
# payment ever exceeds it -- refusal is by construction, not judgment.
MPP_ESCALATION_USD = float(_load_env().get('MPP_ESCALATION_USD', '1.00') or 1.00)


def _mpp_enabled():
    """The operator switch: settings row 'mpp_enabled' wins (live toggle),
    env MPP_ENABLED is the boot default. Both must say yes; absent = no."""
    saved = (_get_setting('mpp_enabled') or '').strip().lower()
    if saved:
        return saved in ('1', 'true', 'yes', 'on')
    env = (_load_env().get('MPP_ENABLED') or '').strip().lower()
    return env in ('1', 'true', 'yes', 'on')


def _mpp_ensure_table(conn):
    # init_db() only runs in the __main__ path, not under uvicorn, so a
    # table added after the DB was first created must be ensured where it
    # is used (same pattern as _ensure_tier_fallbacks_table).
    conn.execute('''CREATE TABLE IF NOT EXISTS mpp_payments (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        vendor TEXT NOT NULL,
        resource TEXT,
        amount_usd REAL NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        receipt TEXT
    )''')


def _mpp_ledger_write(vendor, resource, amount, status, reason=None,
                      receipt=None):
    """One row per payment DECISION -- paid or refused, with the reason a
    refusal happened. The audit trail is the governance shift's receipt."""
    try:
        with _db() as conn:
            _mpp_ensure_table(conn)
            conn.execute(
                'INSERT INTO mpp_payments (id, ts, vendor, resource, '
                'amount_usd, status, reason, receipt) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (_secrets.token_hex(8), _time.time(), vendor, resource,
                 float(amount), status, reason, receipt))
    except Exception:
        pass  # a ledger write failure must never block the payment decision


def _parse_mpp_challenge(error):
    """The machine-readable payment demand from a 402: the MPP/x402 shape
    carries a base64 JSON challenge in the Payment-Required header; a plain
    JSON body with amountUsd/payTo is accepted as the same contract. Returns
    {amount, payTo, resource, scheme} or None -- a challenge this cannot
    parse is NEVER an excuse to pay blind."""
    payload = None
    raw = ''
    try:
        raw = (error.headers or {}).get('Payment-Required') or ''
    except Exception:
        raw = ''
    if raw:
        try:
            payload = _json.loads(_base64.b64decode(raw.strip()))
        except Exception:
            payload = None
    if payload is None:
        try:
            body = error.read().decode('utf-8', 'ignore')
            data = _json.loads(body)
            if isinstance(data, dict):
                payload = data.get('paymentRequired') or data.get('payment') or data
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return None
    try:
        amount = float(payload.get('amountUsd', payload.get('amount')))
    except (TypeError, ValueError):
        return None
    if not amount or amount <= 0 or not payload.get('payTo'):
        return None
    return {'amount': amount,
            'payTo': str(payload.get('payTo')),
            'resource': str(payload.get('resource') or ''),
            'scheme': str(payload.get('scheme') or 'mpp/1')}


def _mpp_settle(challenge):
    """Submit one settlement to the treasury service and return its receipt.
    The treasury service (MPP_SETTLE_URL) holds the actual wallet; the tank
    never stores chain keys -- only the bearer credential for the settle
    endpoint, which the treasury service can rate-limit and revoke."""
    env = _load_env()
    url = (env.get('MPP_SETTLE_URL') or '').strip()
    key = (env.get('MPP_WALLET_KEY') or '').strip()
    if not url or not key:
        raise RuntimeError('MPP treasury not configured '
                           '(MPP_SETTLE_URL / MPP_WALLET_KEY) -- payment refused')
    parsed = _urlparse.urlparse(url)
    if parsed.scheme != 'https':
        raise RuntimeError('MPP_SETTLE_URL must be https -- payment refused')
    req = _urlrequest.Request(
        url,
        data=_json.dumps({
            'spec': challenge['scheme'],
            'amountUsd': challenge['amount'],
            'payTo': challenge['payTo'],
            'resource': challenge['resource'],
            'nonce': _secrets.token_hex(8),
        }).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {key}',
                 'User-Agent': 'AIThinkTankAgent/1.0'},
        method='POST')
    with _urlrequest.urlopen(req, timeout=20) as resp:  # nosec B310 -- operator-configured https URL, key-authenticated
        return _json.loads(resp.read()) or {}


def _mpp_settle_and_retry(error, provider, model_slug):
    """The transport's 402 hook. Returns True exactly when the challenge was
    PAID and the caller may retry the original POST once. Raises RuntimeError
    on any refusal (the caller's normal failure fallbacks take over). Returns
    False when MPP is disabled -- the pre-existing 402 semantics apply."""
    if not _mpp_enabled():
        return False
    challenge = _parse_mpp_challenge(error)
    if challenge is None:
        _mpp_ledger_write((provider or {}).get('provider') or 'unknown',
                          '', 0.0, 'refused', reason='unparseable challenge')
        raise RuntimeError('MPP 402 with unparseable payment challenge '
                           '-- refusing to pay blind')
    vendor = (provider or {}).get('provider') or 'unknown'
    amount = challenge['amount']
    if amount > MPP_MAX_PAYMENT_USD:
        _mpp_ledger_write(vendor, challenge['resource'], amount, 'refused',
                          reason=f'above per-call cap ${MPP_MAX_PAYMENT_USD:.2f}')
        raise RuntimeError(f'MPP challenge ${amount:.4f} exceeds the '
                           f'per-call cap (${MPP_MAX_PAYMENT_USD:.2f}) -- refused')
    if MPP_ESCALATION_USD and amount > MPP_ESCALATION_USD:
        _mpp_ledger_write(vendor, challenge['resource'], amount, 'refused',
                          reason=f'above escalation floor ${MPP_ESCALATION_USD:.2f}')
        log_action(None, 'mpp_payment_refused',
                   {'vendor': vendor, 'amountUsd': amount,
                    'reason': 'above escalation floor',
                    'resource': challenge['resource']})
        raise RuntimeError(f'MPP challenge ${amount:.4f} exceeds the '
                           f'escalation floor (${MPP_ESCALATION_USD:.2f}) '
                           '-- operator must move first')
    if _lane_cap_exceeded(_MPP_LEDGER_KEY):
        _mpp_ledger_write(vendor, challenge['resource'], amount, 'refused',
                          reason='treasury cap reached')
        raise RuntimeError('machine payments treasury cap reached -- '
                           'payment refused')
    if _provider_cap_exceeded(vendor):
        _mpp_ledger_write(vendor, challenge['resource'], amount, 'refused',
                          reason=f'{vendor} spend cap reached')
        raise RuntimeError(f'{vendor} spend cap reached -- MPP payment refused')
    receipt = _mpp_settle(challenge)
    # The settled dollar is real spend in BOTH dimensions the caps read:
    # the treasury lane (how much machine money moved this month) and the
    # payee's provider rollup (their own vendor allowance).
    _accrue_spend(_MPP_LEDGER_KEY, amount)
    _accrue_provider_spend(vendor, amount)
    receipt_id = (receipt or {}).get('id') or (receipt or {}).get('receipt')
    _mpp_ledger_write(vendor, challenge['resource'], amount, 'paid',
                      receipt=receipt_id)
    log_action(None, 'mpp_payment',
               {'vendor': vendor, 'amountUsd': amount,
                'resource': challenge['resource'], 'receipt': receipt_id})
    return True
