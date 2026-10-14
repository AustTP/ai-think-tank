"""JEV experiment grants: rotating server-side tokens for bounded inference
on the JEV decisions model, under the same restrictions as the sublow lane
(see DESIGN.md "JEV experiment grants").

The rule this module exists to enforce is the sublow rule, pointed at a
different model: an experiment can run bounded calls against the JEV model
WITHOUT the OpenRouter key ever leaving the server and WITHOUT an unbounded
bill. The operator mints a grant (agent + purpose + a DECLARED cost estimate
+ TTL + per-call token cap) and is the only party who ever sees the raw
token; the experiment presents just that opaque nonce; the proxy picks the
model itself (the resolved JEV slug -- never a caller-supplied slug),
re-checks every Bank gate on every call, and refuses as soon as actual
spend reaches the declared estimate. The grant is the authorization: no
JEV vote, no credential, no key in a notebook.

The clamp constants and the pure helpers (_token_hash, _clamp_ttl,
_clamp_tokens, _agent_month_spend) are shared with sublow.py on purpose --
the two lanes differ only in which model they serve, how that model is
resolved, and which ledger bucket the spend accrues to, so one copy of the
machinery is the right number. All serve.py state is reached through
`_serve.X` at call time -- the same lazy pattern bank.py and sublow.py use.
Imported back into serve.py as `from jev_grants import ...`, so
`serve.mint_jev_grant` and friends stay the stable public surface the
endpoints and tests use.
"""

import secrets
import time

import sublow as _sublow_policy
from sublow import (
    _agent_month_spend,
    _clamp_tokens,
    _clamp_ttl,
    _token_hash,
)

# Spend-ledger service bucket every JEV experiment call accrues to, so the
# Bank view shows experiment spend as its own row (real dollars against the
# shared monthly cap, like any other model spend).
JEV_LEDGER_SERVICE = '__jev_experiment__'

# Worksheet-only blended $/M basis for a JEV quote when the slug has no
# model_tiers row. OpenRouter lists the Jev router with dynamic pricing
# (-1/-1), so no catalog price exists to read; the live decision_tape shows
# ~$0.00002 per short decision call, which sits far below this. Deliberately
# CONSERVATIVE (an over-quote only makes an operator declare more headroom,
# never less) and never enforced: the hard gate is always accrued ACTUAL
# cost from OpenRouter's reported usage, exactly as on the sublow lane.
JEV_FALLBACK_PRICE_PER_M = 0.10


def _ensure_table(conn):
    """Grant table DDL, run lazily on every entry point (the same pattern
    bank.py uses for kv_spend) so no caller can reach a missing table."""
    conn.execute('''CREATE TABLE IF NOT EXISTS jev_grants (
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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_jev_grants_agent ON jev_grants(agent_id)')


def _jev_slug():
    """The OpenRouter slug the JEV experiment-grant proxy serves. Resolved
    through serve.py's accessor so the operator switch (settings 'jev_model',
    /api/jev/model) and the env fallback both take effect without a restart,
    and the chain's URL fallbacks (the Colab/Laya standby is a provider
    endpoint, not a plain chat-completions slug) are skipped. None when the
    chain holds no real slug -- mint and inference fail closed on that."""
    import serve as _serve
    return _serve._jev_openrouter_slug()


def mint_jev_grant(agent_id, purpose, estimate_usd, granted_by,
                   task_id='', ttl_s=None, max_tokens_per_call=None):
    """Mint a JEV experiment grant: (token, None) on success, (None,
    refusal) otherwise. The declared estimate is the whole point -- it is the
    ceiling the proxy enforces per call, so an estimate of 0/unparseable is
    refused outright. Also refuses when no JEV slug resolves, when the shared
    monthly cap is already hit, or when this agent's own remaining headroom
    cannot cover the estimate (the grant must fit inside the agent's budget,
    not just the tank's). The raw token is returned once and never stored."""
    import serve as _serve
    agent_id = (agent_id or '').strip()
    purpose = (purpose or '').strip()
    if not agent_id or not purpose:
        return None, 'agentId and purpose are required'
    try:
        estimate = float(estimate_usd)
    except (TypeError, ValueError):
        estimate = 0.0
    if estimate <= 0:
        return None, 'a declared estimateUsd greater than 0 is required -- the estimate is the authorization'
    if not _jev_slug():
        return None, 'no JEV model slug resolved -- check JEV_MODEL / the /api/jev/model switch'
    if _serve._think_tank_spend_cap_exceeded():
        return None, 'the shared monthly spend cap is already reached'
    cap = float(getattr(_serve, 'AGENT_SPEND_CAP_USD', 0) or 0)
    if cap:
        month_used = _agent_month_spend(agent_id)
        if month_used + estimate > cap:
            return None, (f'estimate ${estimate:.4f} exceeds this agent\'s remaining monthly '
                          f'headroom (${max(0.0, cap - month_used):.4f} left of ${cap:.2f})')
    token = secrets.token_urlsafe(32)
    grant_id = 'jg-' + secrets.token_hex(8)
    now = time.time()
    with _serve._db() as conn:
        _ensure_table(conn)
        conn.execute(
            'INSERT INTO jev_grants (id, token_hash, agent_id, task_id, purpose, '
            'estimate_usd, accrued_usd, calls, max_tokens_per_call, granted_by, '
            'created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)',
            (grant_id, _token_hash(token), agent_id, (task_id or '').strip(), purpose,
             estimate, _clamp_tokens(max_tokens_per_call), granted_by, now,
             now + _clamp_ttl(ttl_s)),
        )
    return token, None


def validate_jev_grant(token):
    """Resolve a raw grant token to its row, or None when it is unusable --
    unknown, expired (and the stale row is swept on the way past, like
    resolve_capability_handle does for handles), so a presented nonce is
    either a live grant or nothing. Returns a dict of the row's columns."""
    import serve as _serve
    if not token or not isinstance(token, str):
        return None
    now = time.time()
    with _serve._db() as conn:
        _ensure_table(conn)
        row = conn.execute(
            'SELECT id, agent_id, task_id, purpose, estimate_usd, accrued_usd, calls, '
            'max_tokens_per_call, granted_by, created_at, expires_at '
            'FROM jev_grants WHERE token_hash = ?',
            (_token_hash(token),)).fetchone()
        if not row:
            return None
        if row[10] <= now:
            conn.execute('DELETE FROM jev_grants WHERE id = ?', (row[0],))
            return None
    return dict(zip(
        ('id', 'agent_id', 'task_id', 'purpose', 'estimate_usd', 'accrued_usd', 'calls',
         'max_tokens_per_call', 'granted_by', 'created_at', 'expires_at'),
        row))


def accrue_jev_grant(grant_id, cost):
    """Accrue one real call's cost against the grant's burn-down and return
    the new accrued total (None when nothing was recorded). Best-effort, like
    every ledger accrual: an accounting failure never breaks the call that
    just succeeded."""
    import serve as _serve
    if not grant_id or not isinstance(cost, (int, float)) or cost <= 0:
        return None
    try:
        with _serve._db() as conn:
            _ensure_table(conn)
            conn.execute('UPDATE jev_grants SET accrued_usd = accrued_usd + ?, '
                         'calls = calls + 1 WHERE id = ?', (float(cost), grant_id))
            row = conn.execute('SELECT accrued_usd FROM jev_grants WHERE id = ?',
                               (grant_id,)).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def revoke_jev_grant(grant):
    """Revoke by grant id OR by raw token (whichever the operator still has
    on hand). Deletes the row outright, same as _revoke_handle: a revoked
    grant must not be resolvable. True when a row matched."""
    import serve as _serve
    if not grant or not isinstance(grant, str):
        return False
    with _serve._db() as conn:
        _ensure_table(conn)
        cur = conn.execute('DELETE FROM jev_grants WHERE id = ? OR token_hash = ?',
                           (grant, _token_hash(grant)))
        return cur.rowcount > 0


def revoke_agent_jev_grants(agent_id):
    """Drop every grant a (fired) agent held, so nothing survives the fire --
    the same guarantee revoke_agent_credentials gives its handles."""
    import serve as _serve
    if not agent_id:
        return 0
    with _serve._db() as conn:
        _ensure_table(conn)
        cur = conn.execute('DELETE FROM jev_grants WHERE agent_id = ?', (agent_id,))
        return cur.rowcount or 0


def expire_jev_grants(now=None):
    """Sweep grants whose TTL has passed. Cheap and idempotent; runs beside
    the capability-handle and sublow-grant sweeps on each sim tick, and
    validation re-checks expiry on access regardless (belt and braces for a
    stale row)."""
    import serve as _serve
    with _serve._db() as conn:
        _ensure_table(conn)
        conn.execute('DELETE FROM jev_grants WHERE expires_at <= ?',
                     (time.time() if now is None else now,))


def quote_jev_grant(agent_id, estimated_calls=1, max_tokens_per_call=None):
    """Cost-estimate worksheet for the operator minting a grant: the model the
    proxy WILL serve (resolved JEV slug, hard-coded), its blended $/M price
    (a model_tiers row when one exists for the slug, otherwise the
    conservative JEV_FALLBACK_PRICE_PER_M basis, with `priceSource` saying
    which), a per-call and total estimate at the requested call count and
    token cap, the agent's month-to-date spend and remaining headroom, and
    the tank's month-to-date usage against the shared cap. Pure read."""
    import serve as _serve
    tokens = _clamp_tokens(max_tokens_per_call)
    try:
        estimated_calls = max(1, int(estimated_calls))
    except (TypeError, ValueError):
        estimated_calls = 1
    model = _jev_slug()
    name = None
    price_per_m = price_source = None
    if model:
        try:
            with _serve._db() as conn:
                row = conn.execute('SELECT name, price_per_m FROM model_tiers WHERE slug = ?',
                                   (model,)).fetchone()
            if row:
                name, price_per_m, price_source = row[0], float(row[1]), 'model_tiers'
        except Exception:
            price_per_m = None
    if price_per_m is None:
        price_per_m, price_source = JEV_FALLBACK_PRICE_PER_M, 'fallback'
    # Estimate basis: the per-call token cap is the billed-token budget for
    # one call (input + output), priced at the blended rate. The proxy still
    # accrues REAL reported cost against the grant, so a call that overruns
    # this basis eats the estimate faster -- never silently more.
    per_call = round(price_per_m * tokens / 1e6, 6) if price_per_m is not None else None
    cap = float(getattr(_serve, 'AGENT_SPEND_CAP_USD', 0) or 0)
    month_used = _agent_month_spend(agent_id)
    tank = _serve._spend_cap_month_view()
    return {
        'model': model,
        'modelName': name,
        'pricePerM': price_per_m,
        'priceSource': price_source,
        'maxTokensPerCall': tokens,
        'estimatedCalls': estimated_calls,
        'perCallUsd': per_call,
        'totalUsd': round(per_call * estimated_calls, 6) if per_call is not None else None,
        'agentId': (agent_id or '').strip(),
        'agentMonthUsd': round(month_used, 6),
        'agentCapUsd': cap or None,
        'agentRemainingUsd': round(max(0.0, cap - month_used), 6) if cap else None,
        'tankMonthUsd': round(tank['usage'], 6) if tank.get('usage') is not None else None,
        'tankCapUsd': tank.get('cap'),
        'tankCapExceeded': bool(tank.get('exceeded')),
        'grantTtlS': _sublow_policy.GRANT_TTL_S_DEFAULT,
        'grantTtlMaxS': _sublow_policy.GRANT_TTL_S_MAX,
        'grantTokensDefault': _sublow_policy.GRANT_TOKENS_DEFAULT,
        'grantTokensMax': _sublow_policy.GRANT_TOKENS_MAX,
    }
