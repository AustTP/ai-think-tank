"""The Bank: the think tank's spend ledger and every budget gate.

Extracted from serve.py (see DESIGN.md "Budget Controls" and "The Bank").
The ledger (its own kv_spend row in SQLite) records every real model call's
cost by service; the functions here enforce the page-request / dollar-spend /
high-tier / Apify / Colab caps that every money-spending chokepoint consults
before a call goes out, and render the director-facing Bank view.

All serve.py state is reached through `_serve.X` at call time -- the same lazy
pattern sim.py and content.py use. Two reasons:

1. serve.py remains the source of truth for the env-derived constants
   (SPEND_CAP_USD, COLAB_MONTHLY_UNITS, APIFY_MONTHLY_BUDGET_USD, ...),
   which are defined later in serve.py than a top-level import could see.
2. A test patching `serve._spend_ledger_read` (or any other Bank name) on the
   serve module is honored by these functions, because every internal call
   also goes through `_serve.X`.

Imported back into serve.py as `from bank import ...`, so `serve._accrue_spend`
and friends stay the stable public surface the call sites and tests use.
"""

import datetime
import json
import time
import urllib.request

# The default per-service budget when no product record pins a cap.
DEFAULT_BUDGET_CAP_USD = 50.0

# Reserved spend-ledger key carrying the spend-cap baseline (a float or a
# {period, baseline} record), excluded from every service view and from the
# cap's own total.
_SPEND_CAP_BASELINE_KEY = '__spend_cap_baseline__'

# Brief cache for the live OpenRouter credits reconcile (a bank readout
# shouldn't always cost a network round-trip).
_OPENROUTER_CREDITS_CACHE = {'at': 0.0, 'data': None}
OPENROUTER_CREDITS_CACHE_TTL_S = 300


def _budget_cap_usd(service, products=None):
    """A service's fixed $ cap. A per-product cap (budgetCapUsd) on the product
    record wins when the service names a product; productIds and names both
    match. Everything else -- the __general__ / __player_ask__ / __jev__ lanes
    and any product without an explicit cap -- falls back to the default. The
    cumulative cap is the sum of each service's cap (its product cap if pinned
    else the default), which is the cleanest stand-in for "how much is the
    whole think tank allowed to spend before directors must re-budget."""
    import serve as _serve
    if not isinstance(service, str):
        return DEFAULT_BUDGET_CAP_USD
    if service == _serve.COLAB_LEDGER_KEY:
        # Compute-UNIT budget (not USD) -- the generic ledger row would
        # otherwise show the $ default cap against a units number. Constants
        # are defined later in serve.py; resolved at call time.
        return float(_serve.COLAB_MONTHLY_UNITS)
    products = products or []
    for p in products:
        if p.get('id') == service or p.get('name') == service:
            cap = p.get('budgetCapUsd')
            if isinstance(cap, (int, float)) and cap > 0:
                return float(cap)
            break
    return DEFAULT_BUDGET_CAP_USD


def _accrue_spend(service, cost):
    """Accrue a single model call's cost to a service's ledger bucket. The whole
    point is that accrual happens exactly once per model call, at the choke
    point, so whatever the ledger shows is exactly what the think tank has spent."""
    import serve as _serve
    if not isinstance(cost, (int, float)) or not cost:
        return  # no usage.cost reported -- nothing to record
    cost = float(cost)
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.setdefault(service, {'used': 0.0, 'calls': 0})
        bucket['used'] = float(bucket.get('used', 0) or 0) + cost
        bucket['calls'] = int(bucket.get('calls', 0) or 0) + 1
        now = datetime.datetime.now(datetime.timezone.utc)
        bucket['lastAt'] = now.isoformat()
        # Daily series for the burn-rate forecast. Keyed by UTC date so the
        # trailing-7-day rate stays cheap and monotonic across process restarts.
        series = bucket.setdefault('byDay', {})
        day = now.strftime('%Y-%m-%d')
        series[day] = float(series.get(day, 0) or 0) + cost
        _serve._spend_ledger_write(ledger)
    except Exception:
        # Spend accounting must never take the think tank down: a failed read or
        # write just means this call's cost isn't reflected in the ledger.
        pass


def _spend_ledger_read():
    """Read the spend ledger from its own kv_spend row. Never the whole-think tank
    blob -- see the kv_spend DDL comment for why accounting is independent."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS kv_spend (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute('SELECT blob FROM kv_spend WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _spend_ledger_write(ledger):
    import serve as _serve
    with _serve._db() as conn:
        conn.execute(
            'INSERT INTO kv_spend (id, blob, updated_at) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
            (json.dumps(ledger), time.time()),
        )


def _spend_cap_period():
    """The current UTC month window for the spend cap, e.g. '2026-10'. The
    cap is a MONTHLY budget: at each month boundary the baseline rolls
    forward to the current ledger total, so only spend accrued WITHIN the
    current month counts against SPEND_CAP_USD. Rollover happens lazily on
    the first check of a new month, so there is no timer to drift or die
    with a restart."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _think_tank_spend_cap_exceeded():
    """Hard, absolute MONTHLY spend-cap check -- called at the top of EVERY
    real money-spending chokepoint (_call_openrouter_sync, _post_openrouter_raw,
    _call_openrouter_decision_sync), before any network call. Disabled
    (returns False) when SPEND_CAP_USD is explicitly 0. The baseline (a
    reserved key in the same ledger row) is per-month: on the first real check
    in a month the CURRENT total ledger spend is stored as that month's
    baseline, so spend from previous months is never counted -- only what
    accrues within the current month counts against the monthly budget. The
    rollover is lazy (first check of the new month) and the period rides the
    real ledger, so it survives restarts. Deliberately a plain function with
    no bypass/override path from inside the think tank -- see SPEND_CAP_USD's
    own comment for why."""
    import serve as _serve
    if not _serve.SPEND_CAP_USD:
        return False
    ledger = _serve._spend_ledger_read()
    # The spend cap is a USD ceiling on real money, so only dollar-
    # denominated service buckets count. COLAB_LEDGER_KEY carries Colab
    # COMPUTE UNITS, not dollars -- it has its own budget gate
    # (COLAB_MONTHLY_UNITS) and its own Bank row, so counting it here
    # would trip the USD cap on a currency that isn't money (a single T4
    # run accruing ~4 units looked like $4 of spend and failed every
    # classifier call closed).
    excluded = {_serve._SPEND_CAP_BASELINE_KEY, _serve.COLAB_LEDGER_KEY}
    total = sum(float(v.get('used') or 0) for k, v in ledger.items()
               if k not in excluded and isinstance(v, dict))
    period = _serve._spend_cap_period()
    rec = ledger.get(_serve._SPEND_CAP_BASELINE_KEY)
    # Legacy migration: the old format stored a bare float (the baseline at
    # install time). Adopt it as this month's baseline so pre-existing
    # historical spend still never counts -- same guarantee, same month.
    if not isinstance(rec, dict):
        baseline = rec if isinstance(rec, (int, float)) else total
        ledger[_serve._SPEND_CAP_BASELINE_KEY] = {'period': period, 'baseline': baseline}
        _serve._spend_ledger_write(ledger)
        return False
    if rec.get('period') != period:
        # New month: roll the baseline forward to the current total so only
        # this month's accrual counts against the monthly budget.
        ledger[_serve._SPEND_CAP_BASELINE_KEY] = {'period': period, 'baseline': total}
        _serve._spend_ledger_write(ledger)
        return False
    baseline = rec.get('baseline')
    if not isinstance(baseline, (int, float)):
        ledger[_serve._SPEND_CAP_BASELINE_KEY] = {'period': period, 'baseline': total}
        _serve._spend_ledger_write(ledger)
        return False
    return (total - baseline) >= _serve.SPEND_CAP_USD


def _bank_budget_view(snapshot):
    """Snap the spend ledger + per-service caps into a director-facing view:
       used / cap / left / a forecast (trailing-7-day burn rate projected to
       when the cap is hit) for every service that has spent anything, plus a
       cumulative row across all services so directors can see the whole
       think tank and not exceed cumulatively. Pure read -- never mutates state.
       Forecast uses real wall-clock spend (real money, real calls). The ledger
       comes from its own kv_spend row; `snapshot` (the whole-think tank state)
       contributes only the product records the caps are read from."""
    import serve as _serve
    products = (snapshot.get('products') or {}).values() if isinstance(snapshot.get('products'), dict) \
        else (snapshot.get('products') or [])
    products = list(products)
    services = {}
    for svc, bucket in (_serve._spend_ledger_read() or {}).items():
        if svc == _serve._SPEND_CAP_BASELINE_KEY or not isinstance(bucket, dict):
            continue  # the spend-cap baseline is a reserved float, not a service bucket
        used = float(bucket.get('used', 0) or 0)
        cap = _serve._budget_cap_usd(svc, products)
        service_row = {
            'service': svc,
            'used': round(used, 6),
            'cap': cap,
            'left': round(max(0.0, cap - used), 6),
            'over': used > cap,
            'calls': int(bucket.get('calls', 0) or 0),
            'lastAt': bucket.get('lastAt'),
            'burnPerDay': 0.0,
            'daysLeft': None,
        }
        service_row['burnPerDay'], service_row['daysLeft'] = _serve._forecast(bucket, cap)
        services[svc] = service_row
    # A director-set budget (e.g. DigitalOcean's
    # $25/mo cap) should be visible to a teller from the moment it's set, not
    # only after the service's first real charge lands in the ledger -- a cap
    # nobody can see until it's already being spent against isn't much of a
    # budget. Seed a zero-usage row for any product that names an explicit cap
    # and hasn't accrued anything yet.
    for p in products:
        svc = p.get('id') or p.get('name')
        cap = p.get('budgetCapUsd')
        if not svc or svc in services or not isinstance(cap, (int, float)) or cap <= 0:
            continue
        services[svc] = {
            'service': svc, 'used': 0.0, 'cap': float(cap), 'left': float(cap),
            'over': False, 'calls': 0, 'lastAt': None,
            'burnPerDay': 0.0, 'daysLeft': None,
        }
    # Apify FREE-plan monthly budget: same "visible from day
    # one" rule -- the $5/month cap is real (enforced by the plan, not just a
    # think tank convention), so it belongs in the Bank even before the first
    # actor run accrues anything. Only when the account is actually configured
    # (APIFY_API_KEY) AND the budget is enabled (APIFY_MONTHLY_BUDGET_USD > 0)
    # -- a clone without an Apify account sees no phantom row, mirroring how
    # the OpenRouter reconcile line is omitted without a key.
    if _serve.APIFY_API_KEY and _serve.APIFY_MONTHLY_BUDGET_USD > 0 \
            and _serve.APIFY_LEDGER_KEY not in services:
        services[_serve.APIFY_LEDGER_KEY] = {
            'service': _serve.APIFY_LEDGER_KEY,
            'used': 0.0, 'cap': float(_serve.APIFY_MONTHLY_BUDGET_USD),
            'left': float(_serve.APIFY_MONTHLY_BUDGET_USD), 'over': False,
            'calls': 0, 'lastAt': None, 'burnPerDay': 0.0, 'daysLeft': None,
        }
    # Colab agent compute: same "visible from day one" rule -- a
    # GPU session burns the account's compute units fast, so the cap belongs
    # in the Bank before the first run_on_colab run. Shown only when the
    # colab CLI actually exists on this machine (the think tank's lever) and the
    # budget is enabled (>0); a clone without the CLI sees no phantom row.
    if _serve.COLAB_CLI_AVAILABLE and _serve.COLAB_MONTHLY_UNITS > 0 \
            and _serve.COLAB_LEDGER_KEY not in services:
        _used = _serve._colab_spend_this_month()
        _usage = _serve._colab_account_usage()
        services[_serve.COLAB_LEDGER_KEY] = {
            'service': _serve.COLAB_LEDGER_KEY,
            'used': round(_used, 3), 'cap': float(_serve.COLAB_MONTHLY_UNITS),
            'left': round(max(0.0, _serve.COLAB_MONTHLY_UNITS - _used), 3),
            'over': _used > _serve.COLAB_MONTHLY_UNITS,
            'calls': 0, 'lastAt': None, 'burnPerDay': 0.0, 'daysLeft': None,
            'balance_units': None if _usage is None else round(_usage['balance'], 3),
        }
    return services


def _forecast(bucket, cap):
    """Return (daily_burn, days_until_cap_at_that_rate). daily burn is the last
    7 UTC days' spend / the number of days actually present in that window (not
    a hard 7 -- a fresh ledger with two days of spend is burning fast, and
    dividing by 7 would hide it); daysLeft is None when there's no signal (no
    series, zero burn, or already over -- you're not forecasting your way out
    of an overrun, you're re-budgeting). Cheap integer-date bucketing, no
    timezone wrangling: days older than 7 fall out of a rolling window
    naturally."""
    used = float(bucket.get('used', 0) or 0)
    by_day = bucket.get('byDay') or {}
    if not by_day or used <= 0:
        return 0.0, None
    today = datetime.date.today()
    window = 0.0
    days_present = 0
    for day_str, amt in by_day.items():
        try:
            day = datetime.date.fromisoformat(day_str)
        except (ValueError, TypeError):
            continue
        if (today - day).days <= 7:
            window += float(amt or 0)
            days_present += 1
    if not days_present:
        return 0.0, None
    burn = window / days_present
    if burn <= 0 or used >= cap:
        return round(burn, 6), None
    left = cap - used
    return round(burn, 6), max(0.0, left / burn)


def _openrouter_account_credits():
    """Live GET https://openrouter.ai/api/v1/credits -- real total_credits
    (purchased) and total_usage (spent), account-wide (not the think tank's own
    per-service ledger). Returns None on any failure (no key, network, bad
    response) so the Bank teller can fail closed and just omit this line
    rather than ever showing stale or fabricated numbers."""
    import serve as _serve
    if not _serve.OPENROUTER_API_KEY:
        return None
    now = time.time()
    cached = _serve._OPENROUTER_CREDITS_CACHE
    if cached['data'] is not None and (now - cached['at']) < _serve.OPENROUTER_CREDITS_CACHE_TTL_S:
        return cached['data']
    try:
        req = urllib.request.Request(
            'https://openrouter.ai/api/v1/credits',
            headers={'Authorization': f'Bearer {_serve.OPENROUTER_API_KEY}'})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- fixed OpenRouter API host
            data = json.loads(resp.read().decode('utf-8', errors='replace')).get('data') or {}
        total_credits = float(data.get('total_credits') or 0)
        total_usage = float(data.get('total_usage') or 0)
        result = {'totalCredits': total_credits, 'totalUsage': total_usage,
                  'remaining': max(0.0, total_credits - total_usage)}
        cached['at'] = now
        cached['data'] = result
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Page-request budget: the think tank has a MONTHLY allowance of EXTERNAL page
# requests -- each browse_page fetch (/api/browse) and each search_web call
# (Tavily) counts as ONE request. A count-based quota, not a dollar cap: 1000
# free page requests/month (set PAGE_REQUEST_MONTHLY_BUDGET in .env; 0/unset
# disables). Enforced at the two chokepoints so a runaway research request
# can't blow the month. Rollover: a fresh calendar month resets the counter.
# Constants (PAGE_REQUEST_MONTHLY_BUDGET, PAGE_REQUEST_LEDGER_KEY,
# PAGE_REQUEST_BUDGET_START_KEY) live in serve.py; read through _serve at call
# time so a test patching serve.PAGE_REQUEST_MONTHLY_BUDGET is honored.
# ---------------------------------------------------------------------------


def _page_budget_ledger_read():
    """Read the page-request ledger from its own kv_pagebudget row (independent
    of the whole-think tank blob -- same accounting-isolation reason as kv_spend)."""
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS kv_pagebudget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                blob TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            row = conn.execute('SELECT blob FROM kv_pagebudget WHERE id = 1').fetchone()
            return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def _page_budget_ledger_write(ledger):
    import serve as _serve
    try:
        with _serve._db() as conn:
            conn.execute(
                'INSERT INTO kv_pagebudget (id, blob, updated_at) VALUES (1, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, updated_at = excluded.updated_at',
                (json.dumps(ledger), time.time()),
            )
    except Exception:
        pass


def _page_budget_month():
    """The current UTC calendar month as a sortable string (2026-09) -- the
    rollover key: a fresh month starts a new 1000-request allowance."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _page_budget_used(now=None):
    """How many page requests have been consumed this calendar month."""
    import serve as _serve
    ledger = _serve._page_budget_ledger_read()
    month = _serve._page_budget_month()
    bucket = ledger.get(month) or {}
    return int(bucket.get('used', 0) or 0)


def _page_budget_exhausted():
    """True when the monthly page-request allowance is spent (no more external
    fetches may be made). Never true when the budget is disabled (0/unset)."""
    import serve as _serve
    if not _serve.PAGE_REQUEST_MONTHLY_BUDGET:
        return False
    return _serve._page_budget_used() >= _serve.PAGE_REQUEST_MONTHLY_BUDGET


def _accrue_page_request():
    """Record ONE external page request (browse fetch or search_web call) against
    this month's budget. Best-effort like _accrue_spend: a ledger failure must
    never break the actual fetch. Returns True if the request was within budget
    (recorded), False if the month's allowance is exhausted."""
    import serve as _serve
    if _serve._page_budget_exhausted():
        return False
    try:
        ledger = _serve._page_budget_ledger_read()
        month = _serve._page_budget_month()
        bucket = ledger.setdefault(month, {'used': 0})
        bucket['used'] = int(bucket.get('used', 0) or 0) + 1
        ledger[_serve.PAGE_REQUEST_LEDGER_KEY] = month
        if _serve.PAGE_REQUEST_BUDGET_START_KEY not in ledger:
            ledger[_serve.PAGE_REQUEST_BUDGET_START_KEY] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _serve._page_budget_ledger_write(ledger)
    except Exception:
        pass  # accounting never breaks a real fetch
    return True


# ---------------------------------------------------------------------------
# High-tier monthly budget. The high tier is the expensive one (a stronger
# model for high-stakes planning), and the player wants its USE kept very
# limited -- a dollar budget, not a confidence one. Once the month's allowance
# is consumed, the JEV tier gate fails CLOSED on high (routing to mid instead).
# Accrual happens at the /api/chat choke point (see _accrue_high_tier_spend).
# Constants (HIGH_TIER_MONTHLY_BUDGET_USD, HIGH_TIER_LEDGER_KEY) live in
# serve.py; read through _serve at call time.
# ---------------------------------------------------------------------------


def _high_tier_budget_month():
    """The current UTC calendar month (2026-09) -- the rollover key: a fresh
    month resets the high-tier allowance."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')


def _high_tier_spend_this_month():
    """Total high-tier model spend this calendar month (USD)."""
    import serve as _serve
    try:
        ledger = _serve._spend_ledger_read()
        bucket = ledger.get(_serve.HIGH_TIER_LEDGER_KEY) or {}
        series = bucket.get('byMonth') or {}
        return float(series.get(_serve._high_tier_budget_month(), 0) or 0)
    except Exception:
        return 0.0


def _high_tier_budget_exceeded():
    """True when the high tier has consumed its monthly budget (no more
    high-tier calls allowed unless the cap is raised / the month rolls over).
    Never true when the cap is disabled (0/unset)."""
    import serve as _serve
    if not _serve.HIGH_TIER_MONTHLY_BUDGET_USD:
        return False
    return _serve._high_tier_spend_this_month() >= _serve.HIGH_TIER_MONTHLY_BUDGET_USD


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