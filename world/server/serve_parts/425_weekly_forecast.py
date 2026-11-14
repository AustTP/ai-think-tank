# Weekly forecast (operator decision, 2026-11-13): every Friday 20:00
# America/New_York, the tank sends the owner one Telegram digest --
#
#   SPEND      the trailing-7-day burn forecast per service, the
#              per-provider caps, the JEV/MPP lanes, and this week's
#              tier price moves. The same numbers the Bank shows, but
#              delivered on a cadence instead of on demand -- the
#              OpenRouter blowout was visible weeks early in exactly
#              these numbers.
#   WORLD CLOCK which continents and major countries are awake or asleep
#              right now (population-weighted), plus the major exchange
#              sessions. This is the money-timing lens: at 20:00 ET
#              Friday, Europe is asleep, Oceania/Asia are in their
#              Saturday, and the only always-open market is crypto.
#   REVENUE    dormant by design -- the tank has no inbound ledger yet.
#              The section activates the moment one exists; until then
#              it says so, so the owner never mistakes absence for zero.
#
# A report is not an action: the loop is read-only (DB reads + one Telegram
# push), so it needs no change-window. Scheduling is zone-aware
# (America/New_York) so DST can never drift the fire time.
import json
import time
import urllib.request
import zoneinfo as _zoneinfo
from datetime import datetime as _datetime, timedelta as _timedelta

_FORECAST_CREDITS_TTL_S = 300
_STRAITLY_CREDITS_CACHE = {'at': 0.0, 'data': None}


def _straitly_account_credits():
    """Live GET api.straitly.ai/api/v1/credits -- the operator's prepaid
    balance on the Straitly account (2026-11-13: they fund it once, ~$99,
    and expect it to last the month; the digest should SHOW that number,
    not just the monthly cap). Same fail-closed contract as the OpenRouter
    probe: None on any failure, so the digest omits the line rather than
    showing stale or fabricated numbers."""
    key = (_load_env().get('STRAITLY_API_KEY') or '').strip()
    if not key:
        return None
    now = time.time()
    cached = _STRAITLY_CREDITS_CACHE
    if cached['data'] is not None and (now - cached['at']) < _FORECAST_CREDITS_TTL_S:
        return cached['data']
    try:
        req = urllib.request.Request(
            'https://api.straitly.ai/api/v1/credits',
            headers={'Authorization': f'Bearer {key}',
                     'User-Agent': 'AIThinkTankAgent/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 -- fixed Straitly API host
            data = json.loads(resp.read().decode('utf-8', errors='replace')) \
                .get('data') or {}
        total_credits = float(data.get('total_credits') or 0)
        total_usage = float(data.get('total_usage') or 0)
        result = {'totalCredits': total_credits, 'totalUsage': total_usage,
                  'remaining': max(0.0, total_credits - total_usage)}
        cached['at'] = now
        cached['data'] = result
        return result
    except Exception:
        return None


FORECAST_TZ = 'America/New_York'
WEEKLY_FORECAST_LOCAL_HOUR = 20      # 8:00 pm Eastern, Fridays
WEEKLY_FORECAST_WEEKDAY = 4          # Monday=0 ... Friday=4

# Population in millions (approximate, rounded) -- the weights for the
# awake/asleep share. Tracked population is ~5.0B; the digest says so
# rather than pretending world coverage.
_WORLD_CLOCK = (
    # (continent, country, tz, population_m)
    ('Asia', 'China', 'Asia/Shanghai', 1412),
    ('Asia', 'India', 'Asia/Kolkata', 1428),
    ('Asia', 'Indonesia', 'Asia/Jakarta', 277),
    ('Asia', 'Pakistan', 'Asia/Karachi', 240),
    ('Asia', 'Bangladesh', 'Asia/Dhaka', 173),
    ('Asia', 'Japan', 'Asia/Tokyo', 124),
    ('Asia', 'Philippines', 'Asia/Manila', 117),
    ('Asia', 'Vietnam', 'Asia/Ho_Chi_Minh', 99),
    ('Asia', 'Thailand', 'Asia/Bangkok', 72),
    ('Asia', 'Turkey', 'Asia/Istanbul', 85),
    ('Asia', 'South Korea', 'Asia/Seoul', 52),
    ('Asia', 'Saudi Arabia', 'Asia/Riyadh', 36),
    ('Europe', 'Russia', 'Europe/Moscow', 144),
    ('Europe', 'UK', 'Europe/London', 68),
    ('Europe', 'Germany', 'Europe/Berlin', 84),
    ('Europe', 'France', 'Europe/Paris', 65),
    ('Europe', 'Italy', 'Europe/Rome', 59),
    ('Europe', 'Spain', 'Europe/Madrid', 48),
    ('Europe', 'Poland', 'Europe/Warsaw', 37),
    ('Europe', 'Netherlands', 'Europe/Amsterdam', 18),
    ('Africa', 'Nigeria', 'Africa/Lagos', 224),
    ('Africa', 'Ethiopia', 'Africa/Addis_Ababa', 126),
    ('Africa', 'Egypt', 'Africa/Cairo', 113),
    ('Africa', 'DR Congo', 'Africa/Kinshasa', 102),
    ('Africa', 'South Africa', 'Africa/Johannesburg', 60),
    ('Africa', 'Kenya', 'Africa/Nairobi', 55),
    ('Americas', 'US East', 'America/New_York', 260),
    ('Americas', 'Mexico', 'America/Mexico_City', 128),
    ('Americas', 'Brazil', 'America/Sao_Paulo', 216),
    ('Americas', 'US West', 'America/Los_Angeles', 55),
    ('Americas', 'Colombia', 'America/Bogota', 52),
    ('Americas', 'Argentina', 'America/Argentina/Buenos_Aires', 46),
    ('Americas', 'Canada', 'America/Toronto', 40),
    ('Oceania', 'Australia', 'Australia/Sydney', 26),
    ('Oceania', 'New Zealand', 'Pacific/Auckland', 5),
)

# Major exchange sessions: (name, tz, open_min, close_min) in exchange-local
# minutes-from-midnight, weekdays only. Approximate windows; the digest says
# "approx" for the lunch-split venues. Crypto is 24/7 and always shown open.
_EXCHANGES = (
    ('NYSE', 'America/New_York', 9 * 60 + 30, 16 * 60),
    ('LSE', 'Europe/London', 8 * 60, 16 * 60 + 30),
    ('XETRA', 'Europe/Berlin', 9 * 60, 17 * 60 + 30),
    ('TSE', 'Asia/Tokyo', 9 * 60, 15 * 60),
    ('SSE', 'Asia/Shanghai', 9 * 60 + 30, 15 * 60),
)


def _awake_view(now=None):
    """Who is awake right now: per (continent, country) local time, the
    awake flag (07:00-23:00 local), the weekend flag (Sat/Sun locally),
    and the population-weighted share. `now` is UTC; None = wall clock."""
    now = now or _datetime.now(_zoneinfo.ZoneInfo('UTC'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=_zoneinfo.ZoneInfo('UTC'))
    total = 0.0
    awake = 0.0
    rows = []
    for continent, country, tzname, pop_m in _WORLD_CLOCK:
        try:
            local = now.astimezone(_zoneinfo.ZoneInfo(tzname))
        except Exception:
            continue
        is_awake = 7 <= local.hour < 23
        is_weekend = local.weekday() >= 5
        total += pop_m
        if is_awake:
            awake += pop_m
        rows.append({'continent': continent, 'country': country,
                     'tz': tzname, 'popM': pop_m,
                     'local': local.strftime('%H:%M %a'),
                     'awake': is_awake, 'weekend': is_weekend})
    share = round(awake / total, 2) if total else None
    return {'rows': rows, 'awakeShare': share,
            'trackedPopM': round(total), 'awakePopM': round(awake)}


def _sessions_view(now=None):
    """The major exchange sessions at `now`: open/closed per venue, plus
    crypto's always-open line. Weekends are closed for every listed venue."""
    now = now or _datetime.now(_zoneinfo.ZoneInfo('UTC'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=_zoneinfo.ZoneInfo('UTC'))
    out = []
    for name, tzname, open_min, close_min in _EXCHANGES:
        try:
            local = now.astimezone(_zoneinfo.ZoneInfo(tzname))
        except Exception:
            continue
        minutes = local.hour * 60 + local.minute
        open_now = local.weekday() < 5 and open_min <= minutes < close_min
        out.append({'name': name, 'open': open_now,
                    'local': local.strftime('%H:%M %a')})
    return {'venues': out, 'crypto': True}


def _next_weekly_run(now=None):
    """The next Friday 20:00 America/New_York STRICTLY after `now` (UTC or
    naive-UTC). Zone-aware: the local 20:00 target is materialized through
    ZoneInfo, so DST never drifts the fire time."""
    now = now or _datetime.now(_zoneinfo.ZoneInfo('UTC'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=_zoneinfo.ZoneInfo('UTC'))
    tz = _zoneinfo.ZoneInfo(FORECAST_TZ)
    local = now.astimezone(tz)
    candidate = local.replace(hour=WEEKLY_FORECAST_LOCAL_HOUR, minute=0,
                              second=0, microsecond=0)
    days_ahead = (WEEKLY_FORECAST_WEEKDAY - candidate.weekday()) % 7
    candidate += _timedelta(days=days_ahead)
    if candidate <= local:
        candidate += _timedelta(days=7)
    return candidate


async def _weekly_forecast_loop():
    while True:
        try:
            target = _next_weekly_run()
            await asyncio.sleep(max(1.0, (target - _datetime.now(
                _zoneinfo.ZoneInfo('UTC'))).total_seconds()))
            await asyncio.to_thread(_send_weekly_forecast)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            import traceback
            print(f'[weekly-forecast] loop error: {e}\n{traceback.format_exc()}',
                  flush=True)
            await asyncio.sleep(600)


def _weekly_forecast_text(now=None):
    """The digest body: spend forecast, world clock, sessions, dormant
    revenue. Pure read -- ledger, tiers, bank budget view."""
    now = now or _datetime.now(_zoneinfo.ZoneInfo('UTC'))
    lines = []
    stamp = now.astimezone(_zoneinfo.ZoneInfo(FORECAST_TZ)) \
        .strftime('%a %Y-%m-%d %H:%M ET')
    lines.append(f'WEEKLY FORECAST -- {stamp}')
    lines.append('')

    # --- SPEND ------------------------------------------------------------
    lines.append('SPEND')
    try:
        # A fresh/restored DB can yield None state -- a digest section
        # degrades, it never kills the whole report. _bank_budget_view
        # RETURNS the services dict (service -> row).
        services = _bank_budget_view(get_state_from_db() or {}) or {}
        rows = sorted(services.values(),
                      key=lambda r: -(r.get('monthUsed') or 0))
        shown = 0
        for row in rows:
            if shown >= 6:
                break
            month = row.get('monthUsed') or 0
            cap = row.get('cap')
            if not month and not cap:
                continue
            piece = f'  {row.get("service")}: ${month:.2f} this month'
            if cap:
                piece += f' / cap ${cap:.2f}'
            days_left = row.get('daysLeft')
            if days_left is not None:
                piece += f' (cap hit ~{days_left:.0f}d)'
            lines.append(piece)
            shown += 1
    except Exception as e:
        lines.append(f'  budget view unavailable: {e}')
    try:
        ledger = _spend_ledger_read() or {}
        # The shared model-router pool is the binding constraint (operator,
        # 2026-11-13): OpenRouter + Straitly draw from ONE monthly budget,
        # no hard half each. Per-router lines show where it went.
        pool_used = _model_pool_month_used(ledger)
        pool_flag = ' TRIPPED' if _model_pool_cap_exceeded() else ''
        lines.append(f'  model pool: ${pool_used:.2f}/${_model_pool_cap():.2f}{pool_flag}')
        for provider in DIALING_PROVIDERS:
            used = _provider_month_used_for_cap(ledger, provider)
            cap = _provider_spend_cap(provider)
            flag = ' TRIPPED' if _provider_cap_exceeded(provider) else ''
            lines.append(f'  {provider}: ${used:.2f}/${cap:.2f}{flag}')
        # Prepaid balances (operator, 2026-11-13): the caps divide the $50
        # monthly budget across the routers, but Straitly is FUNDED once
        # (~$99 prepaid) -- the digest shows what is actually left on each
        # account, not just the monthly allowance. Omitted when the probe
        # fails: never stale, never fabricated.
        or_credits = _openrouter_account_credits()
        if or_credits:
            lines.append(f"  openrouter credits: ${or_credits['remaining']:.2f} prepaid left")
        st_credits = _straitly_account_credits()
        if st_credits:
            lines.append(f"  straitly credits: ${st_credits['remaining']:.2f} prepaid left")
        jev = _service_month_to_date(ledger, '__jev__')
        lines.append(f'  JEV lane: ${jev:.2f}/${float(JEV_SPEND_CAP_USD or 0):.2f}')
        mpp = _service_month_to_date(ledger, _MPP_LEDGER_KEY)
        state = 'on' if _mpp_enabled() else 'off'
        lines.append(f'  MPP treasury: ${mpp:.2f}/${float(MPP_SPEND_CAP_USD or 0):.2f}'
                     f' ({state})')
    except Exception as e:
        lines.append(f'  lanes unavailable: {e}')
    try:
        ratios = tier_value_ratios()
        moving = {b: r for b, r in ratios.items() if r and abs(r - 1.0) >= 0.1}
        if moving:
            named = ', '.join(f'{b} {r:+.0%}' for b, r in sorted(moving.items()))
            lines.append(f'  tier value vs 7d median: {named}')
        else:
            lines.append('  tier value vs 7d median: stable')
    except Exception:
        pass
    lines.append('')

    # --- WORLD CLOCK ------------------------------------------------------
    view = _awake_view(now)
    lines.append('WORLD CLOCK')
    if view['awakeShare'] is not None:
        lines.append(f"  {view['awakeShare']:.0%} of tracked population awake "
                     f"({view['awakePopM']}M/{view['trackedPopM']}M)")
    by_continent = {}
    for row in view['rows']:
        by_continent.setdefault(row['continent'], []).append(row)
    for continent in ('Americas', 'Europe', 'Africa', 'Asia', 'Oceania'):
        entries = by_continent.get(continent) or []
        if not entries:
            continue
        up = [r for r in entries if r['awake']]
        down = [r for r in entries if not r['awake']]

        def _fmt(group):
            wk = [r for r in group if r['weekend']]
            base = ', '.join(f"{r['country']} {r['local']}" for r in group)
            if wk:
                base += f' (weekend: {", ".join(r["country"] for r in wk)})'
            return base

        lines.append(f'  {continent} AWAKE: {_fmt(up)}' if up
                     else f'  {continent} AWAKE: none')
        lines.append(f'  {continent} asleep: {_fmt(down)}' if down
                     else f'  {continent} asleep: none')
    sessions = _sessions_view(now)
    markets = ', '.join(
        f"{v['name']} {'OPEN' if v['open'] else 'closed'} ({v['local']})"
        for v in sessions['venues'])
    lines.append(f'  Sessions: {markets}; crypto open 24/7')
    lines.append('')

    # --- REVENUE (dormant until an inbound ledger exists) ------------------
    lines.append('REVENUE')
    lines.append('  No inbound ledger yet -- dormant. This section activates '
                 'the moment the tank sells anything (e.g. MPP inbound '
                 'receipts).')
    return '\n'.join(lines)


def _send_weekly_forecast(now=None):
    """Build the digest, push it to Telegram, keep the last one in settings
    so a UI surface can read it later, and log the send. Read-only on the
    simulation; the only writes are the audit trail of the report itself."""
    text = _weekly_forecast_text(now)
    try:
        _set_setting('last_weekly_forecast', text)
    except Exception:
        pass
    sent = 0
    try:
        sent = _telegram_send_text_sync(text) or 0
    except Exception as e:
        print(f'[weekly-forecast] telegram push failed: {e}', flush=True)
    try:
        log_action(None, 'weekly_forecast_sent',
                   {'chars': len(text), 'telegramSent': sent})
    except Exception:
        pass
    print(f'[weekly-forecast] digest built ({len(text)} chars, '
          f'telegram sent to {sent} chat(s))', flush=True)
    return text
