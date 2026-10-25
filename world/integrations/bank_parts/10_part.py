@_serialized_ledger
def _accrue_high_tier_spend(cost):
    """Accrue a high-tier model call's cost against the monthly high-tier
    budget. Best-effort like _accrue_spend: an accounting failure must never
    break the actual call. Uses the SAME kv_spend ledger as _accrue_spend (so
    the bank sees it) but a reserved bucket + monthly series keyed by month."""
    import serve as _serve
    if not isinstance(cost, (int, float)) or not cost:
        return
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.setdefault(_serve.HIGH_TIER_LEDGER_KEY, {'used': 0.0, 'calls': 0, 'byMonth': {}})
        cost = float(cost)
        bucket['used'] = float(bucket.get('used', 0) or 0) + cost
        bucket['calls'] = int(bucket.get('calls', 0) or 0) + 1
        month = _serve._high_tier_budget_month()
        bucket['byMonth'][month] = float((bucket['byMonth'] or {}).get(month, 0) or 0) + cost
        _serve._spend_ledger_write(ledger)
    except Exception:
        pass  # accounting never blocks a real call


# ---------------------------------------------------------------------------
# Apify FREE-plan monthly budget: the player's Apify account is on the FREE
# plan with a ~$5/mo usage cap by choice -- no subscription, no pay-as-you-go.
# This is a MONTHLY budget, mirroring the high-tier cap: the Bank shows
# used/cap/left against it, and _accrue_apify_spend records real actor-run
# spend so the think tank never quietly exceeds what the plan allows.
# Constants (APIFY_MONTHLY_BUDGET_USD, APIFY_LEDGER_KEY) live in serve.py.
# ---------------------------------------------------------------------------


def _apify_budget_month():
    """The current UTC calendar month (2026-09) -- the rollover key: a fresh
    month resets the Apify allowance."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _apify_spend_this_month():
    """Total Apify spend accrued this calendar month (USD)."""
    import serve as _serve
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.get(_serve.APIFY_LEDGER_KEY) or {}
        series = bucket.get('byMonth') or {}
        return float(series.get(_serve._apify_budget_month(), 0) or 0)
    except Exception:
        return 0.0


def _apify_budget_exceeded():
    """True when the Apify account's monthly allowance is spent. Never true
    when the budget is disabled (0/unset)."""
    import serve as _serve
    if not _serve.APIFY_MONTHLY_BUDGET_USD:
        return False
    return _serve._apify_spend_this_month() >= _serve.APIFY_MONTHLY_BUDGET_USD


@_serialized_ledger
def _accrue_apify_spend(cost):
    """Accrue an Apify actor run's real cost against the monthly budget.
    Best-effort like _accrue_spend: an accounting failure must never break the
    actual run. Uses the same kv_spend ledger so the Bank sees it, under the
    reserved __apify__ bucket with a monthly series keyed by month."""
    import serve as _serve
    if not isinstance(cost, (int, float)) or not cost:
        return
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.setdefault(
            _serve.APIFY_LEDGER_KEY, {'used': 0.0, 'calls': 0, 'byMonth': {}})
        cost = float(cost)
        bucket['used'] = float(bucket.get('used', 0) or 0) + cost
        bucket['calls'] = int(bucket.get('calls', 0) or 0) + 1
        month = _serve._apify_budget_month()
        bucket['byMonth'][month] = \
            float((bucket['byMonth'] or {}).get(month, 0) or 0) + cost
        _serve._spend_ledger_write(ledger)
    except Exception:
        pass  # accounting never blocks a real run


# ---------------------------------------------------------------------------
# Colab compute-unit budget. Colab burns the account's prepaid compute units
# (a currency that is NOT dollars), so this is a unit budget with its own
# ledger bucket, its own Bank row, and its own Jev-gated shard ceiling.
# Constants (COLAB_MONTHLY_UNITS, COLAB_LEDGER_KEY, COLAB_FREE_TIER) and the
# live _colab_account_usage reconcile live in serve.py.
# ---------------------------------------------------------------------------


def _colab_budget_month():
    """The current UTC calendar month (2026-09) -- the rollover key: a fresh
    month resets the Colab compute-unit allowance."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _colab_spend_this_month():
    """Compute units accrued this calendar month (NOT USD -- the Bank row for
    this service reads units against the COLAB_MONTHLY_UNITS cap)."""
    import serve as _serve
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.get(_serve.COLAB_LEDGER_KEY) or {}
        series = bucket.get('byMonth') or {}
        return float(series.get(_serve._colab_budget_month(), 0) or 0)
    except Exception:
        return 0.0


def _colab_budget_exceeded():
    """True when Colab compute should refuse a new run:
      * the operator's think tank cap is spent (COLAB_MONTHLY_UNITS > 0 -- Google
        publishes no fixed quota, so this is a self-imposed convention, not a
        real contract);
      * OR, on a NON-free account only, `colab usage` reports the real prepaid
        balance is exhausted (0 or negative).
    On the free tier (COLAB_FREE_TIER=1) the real balance is NOT a gate: free
    tier has no prepaid wallet, `colab usage` will typically report 0.00, and
    Google enforces free-tier limits dynamically (session length, idle auto-
    disconnect, GPU availability, cooldowns) -- the refusal would just block
    every run for nothing. A None balance (CLI missing/unparseable) likewise is
    never treated as spent on its own."""
    import serve as _serve
    if _serve.COLAB_MONTHLY_UNITS and _serve._colab_spend_this_month() >= _serve.COLAB_MONTHLY_UNITS:
        return True
    if _serve.COLAB_FREE_TIER:
        return False
    usage = _serve._colab_account_usage()
    if usage is not None and float(usage.get('balance') or 0) <= 0:
        return True
    return False


@_serialized_ledger
def _accrue_colab_units(units):
    """Accrue a run's compute units against the monthly budget. Best-effort
    like _accrue_spend: an accounting failure must never break the real run.
    Uses the same kv_spend ledger so the Bank sees it, under the reserved
    __colab_compute__ bucket with a monthly series keyed by month."""
    import serve as _serve
    if not isinstance(units, (int, float)) or not units:
        return
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.setdefault(
            _serve.COLAB_LEDGER_KEY, {'used': 0.0, 'calls': 0, 'byMonth': {}})
        units = float(units)
        bucket['used'] = float(bucket.get('used', 0) or 0) + units
        bucket['calls'] = int(bucket.get('calls', 0) or 0) + 1
        month = _serve._colab_budget_month()
        bucket['byMonth'][month] = \
            float((bucket['byMonth'] or {}).get(month, 0) or 0) + units
        _serve._spend_ledger_write(ledger)
    except Exception:
        pass  # accounting never blocks a real run