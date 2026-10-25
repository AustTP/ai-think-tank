"""Sublow experiment grants: rotating server-side tokens for bounded
sublow-model inference at scale.

Extracted from serve.py (see DESIGN.md "Sublow experiment grants"). The rule
this module exists to enforce: an experiment can run hundreds of short
reenactment calls WITHOUT the OpenRouter key ever leaving the server and
WITHOUT an unbounded bill. The operator mints a grant (agent + purpose + a
DECLARED cost estimate + TTL + per-call token cap) and is the only party who
ever sees the raw token; the experiment presents just that opaque nonce; the
proxy picks the model itself (the sublow tier pick -- never a caller-supplied
slug), re-checks every Bank gate on every call, and refuses as soon as actual
spend reaches the declared estimate. The grant is the authorization: no JEV
vote, no credential, no key in a notebook.

All serve.py state is reached through `_serve.X` at call time -- the same lazy
pattern bank.py and evidence.py use: the tests patch `serve`, and the
env-derived constants (SPEND_CAP_USD, AGENT_SPEND_CAP_USD, ...) are defined
later in serve.py than a top-level import could see. Imported back into
serve.py as `from sublow import ...`, so `serve.mint_sublow_grant` and friends
stay the stable public surface the endpoints and tests use.
"""

import hashlib
import secrets
import time

import grant_store

from config import bind_policy

# Grant defaults, all hard-clamped by the caps beside them: an operator can
# ask for a shorter TTL or a bigger per-call token budget, never a longer or
# larger one than these. The TTL is deliberately short -- a grant is a
# session, not a standing credential (revoked or expired, it is gone).
GRANT_TTL_S_DEFAULT = 1800
GRANT_TTL_S_MAX = 86400
GRANT_TOKENS_DEFAULT = 600
GRANT_TOKENS_MAX = 4000

# Spend-ledger service bucket every sublow experiment call accrues to, so the
# Bank view shows experiment spend as its own row (real dollars against the
# shared monthly cap, like any other model spend).
SUBLOW_LEDGER_SERVICE = '__sublow_experiment__'


def _ensure_table(conn):
    """Grant table DDL, run lazily on every entry point."""
    grant_store.ensure_table(conn, 'sublow_grants')

def _token_hash(token):
    """The stored form of a grant token. The raw nonce is shown exactly once
    at mint; only its SHA-256 lives in the DB, so a DB leak cannot be replayed
    as a live grant (same property capability_handles rely on)."""
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def _clamp_ttl(ttl_s):
    try:
        ttl_s = int(ttl_s)
    except (TypeError, ValueError):
        return GRANT_TTL_S_DEFAULT
    return max(1, min(ttl_s, GRANT_TTL_S_MAX))


def _clamp_tokens(max_tokens_per_call):
    try:
        tokens = int(max_tokens_per_call)
    except (TypeError, ValueError):
        return GRANT_TOKENS_DEFAULT
    return max(1, min(tokens, GRANT_TOKENS_MAX))


def _agent_month_spend(agent_id):
    """$ this agent has accrued in the current UTC month, from its own
    `__agent__/<id>` byDay series. Best-effort: 0.0 on a missing agent or a
    ledger read failure (never a false refusal on its own)."""
    import serve as _serve
    if not agent_id:
        return 0.0
    try:
        bucket = (_serve._spend_ledger_read() or {}).get(_serve._agent_spend_key(agent_id)) or {}
    except Exception:
        return 0.0
    month = time.strftime('%Y-%m', time.gmtime())
    series = bucket.get('byDay') or {}
    return sum(float(v or 0) for k, v in series.items()
               if isinstance(k, str) and k.startswith(month))


def mint_sublow_grant(agent_id, purpose, estimate_usd, granted_by,
                      task_id='', ttl_s=None, max_tokens_per_call=None):
    """Mint a sublow experiment grant: (token, None) on success, (None,
    refusal) otherwise. The declared estimate is the whole point -- it is the
    ceiling the proxy enforces per call, so an estimate of 0/unparseable is
    refused outright. Also refuses when there is no sublow pick to serve
    (fresh machine, tier refresh hasn't run), when the shared monthly cap is
    already hit, or when this agent's own remaining headroom cannot cover the
    estimate (the grant must fit inside the agent's budget, not just the
    tank's). The raw token is returned once and never stored."""
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
    if not _serve._model_tier_slug('sublow'):
        return None, 'no sublow model pick yet -- run a model-tier refresh first'
    if _serve._think_tank_spend_cap_exceeded():
        return None, 'the shared monthly spend cap is already reached'
    cap = float(getattr(_serve, 'AGENT_SPEND_CAP_USD', 0) or 0)
    if cap:
        month_used = _agent_month_spend(agent_id)
        if month_used + estimate > cap:
            return None, (f'estimate ${estimate:.4f} exceeds this agent\'s remaining monthly '
                          f'headroom (${max(0.0, cap - month_used):.4f} left of ${cap:.2f})')
    token = secrets.token_urlsafe(32)
    grant_id = 'sg-' + secrets.token_hex(8)
    now = time.time()
    with _serve._db() as conn:
        _ensure_table(conn)
        grant_store.insert(conn, 'sublow_grants', (
            grant_id, _token_hash(token), agent_id, (task_id or '').strip(), purpose,
            estimate, 0, 0, _clamp_tokens(max_tokens_per_call), granted_by, now,
            now + _clamp_ttl(ttl_s)))
    return token, None


def validate_sublow_grant(token):
    """Resolve a raw grant token to its row, or None when unusable. Expired
    rows are swept on the way past."""
    import serve as _serve
    if not token or not isinstance(token, str):
        return None
    with _serve._db() as conn:
        _ensure_table(conn)
        return grant_store.resolve(conn, 'sublow_grants', _token_hash(token))

def accrue_sublow_grant(grant_id, cost):
    """Accrue one real call's cost against the grant's burn-down."""
    import serve as _serve
    if not grant_id or not isinstance(cost, (int, float)) or cost <= 0:
        return None
    try:
        with _serve._db() as conn:
            _ensure_table(conn)
            return grant_store.accrue(conn, 'sublow_grants', grant_id, cost)
    except Exception:
        return None

def revoke_sublow_grant(grant):
    """Revoke by grant id OR raw token. True when a row matched."""
    import serve as _serve
    if not grant or not isinstance(grant, str):
        return False
    with _serve._db() as conn:
        _ensure_table(conn)
        return grant_store.revoke(conn, 'sublow_grants', grant, _token_hash(grant))


def revoke_agent_sublow_grants(agent_id):
    """Drop every grant a (fired) agent held."""
    import serve as _serve
    if not agent_id:
        return 0
    with _serve._db() as conn:
        _ensure_table(conn)
        return grant_store.revoke_agent(conn, 'sublow_grants', agent_id)


def expire_sublow_grants(now=None):
    """Sweep grants whose TTL has passed."""
    import serve as _serve
    with _serve._db() as conn:
        _ensure_table(conn)
        grant_store.expire(conn, 'sublow_grants', now)

def quote_sublow_grant(agent_id, estimated_calls=1, max_tokens_per_call=None):
    """Cost-estimate worksheet for the operator minting a grant: the model the
    proxy WILL serve (sublow pick, hard-coded), its blended $/M price from
    model_tiers, a per-call and total estimate at the requested call count and
    token cap, the agent's month-to-date spend and remaining headroom, and the
    tank's month-to-date usage against the shared cap. Pure read."""
    import serve as _serve
    tokens = _clamp_tokens(max_tokens_per_call)
    try:
        estimated_calls = max(1, int(estimated_calls))
    except (TypeError, ValueError):
        estimated_calls = 1
    model = _serve._sublow_tier_slug()
    price_per_m = name = None
    if model:
        try:
            with _serve._db() as conn:
                row = conn.execute('SELECT name, price_per_m FROM model_tiers WHERE slug = ?',
                                   (model,)).fetchone()
            if row:
                name, price_per_m = row[0], float(row[1])
        except Exception:
            price_per_m = None
    # Estimate basis: the per-call token cap is the billed-token budget for
    # one call (input + output), priced at the band's blended rate. The proxy
    # still accrues REAL reported cost against the grant, so a call that
    # overruns this basis eats the estimate faster -- never silently more.
    per_call = round(price_per_m * tokens / 1e6, 6) if price_per_m is not None else None
    cap = float(getattr(_serve, 'AGENT_SPEND_CAP_USD', 0) or 0)
    month_used = _agent_month_spend(agent_id)
    tank = _serve._spend_cap_month_view()
    return {
        'model': model,
        'modelName': name,
        'pricePerM': price_per_m,
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
        'grantTtlS': GRANT_TTL_S_DEFAULT,
        'grantTtlMaxS': GRANT_TTL_S_MAX,
        'grantTokensDefault': GRANT_TOKENS_DEFAULT,
        'grantTokensMax': GRANT_TOKENS_MAX,
    }


# Declarative operator-policy bindings: rebinds the grant default/cap
# constants so an operator can tighten or loosen grant bounds via
# operator_policy.json; every config.reload_policy_config() run re-applies
# them via apply_policy_bindings.
bind_policy(__name__, [
    ('GRANT_TTL_S_DEFAULT', 'grantTtlDefaultS'),
    ('GRANT_TTL_S_MAX', 'grantTtlMaxS'),
    ('GRANT_TOKENS_DEFAULT', 'grantTokensDefault'),
    ('GRANT_TOKENS_MAX', 'grantTokensMax'),
])
