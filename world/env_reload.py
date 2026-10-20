"""world/env_reload.py -- the live-reloadable .env field table.

Extracted from serve.py so a new hot-swappable setting is a one-line data edit
in an editable module, not a kernel change. Each entry maps a serve module
global to a loader (env mapping -> value) that reproduces exactly the formula
the import-time global used, so POST /api/env/reload applies a new .env the
same way a reboot would for these names.

Contract for a new entry:
  - key   = the serve.py module global to rebind
  - value = a loader taking the parsed .env dict
Failures are isolated per name by the caller: one malformed value only keeps
that name's previous value. Boot-only settings do NOT belong here (TELEGRAM_*,
SEED_* roster seeds, and the GOOGLE_*_RATE_PER_MIN values, which are rebuilt
separately in serve._reload_google_rate_limits).
"""


def _env_flag(value, default='true'):
    return str(value or default).strip().lower() != 'false'


ENV_RELOAD_FIELDS = {
    'OPENROUTER_API_KEY': lambda e: e.get('OPENROUTER_API_KEY'),
    'TAVILY_API_KEY': lambda e: e.get('TAVILY_API_KEY'),
    'GITHUB_TOKEN': lambda e: e.get('GITHUB_TOKEN'),
    'APIFY_API_KEY': lambda e: e.get('APIFY_API_KEY'),
    'HIGGSFIELD_API_KEY_ID': lambda e: e.get('HIGGSFIELD_API_KEY_ID'),
    'HIGGSFIELD_API_KEY_SECRET': lambda e: e.get('HIGGSFIELD_API_KEY_SECRET'),
    'OPEN_SKY_CLIENT_ID': lambda e: e.get('OPEN_SKY_CLIENT_ID'),
    'OPEN_SKY_CLIENT_SECRET': lambda e: e.get('OPEN_SKY_CLIENT_SECRET'),
    'AISSTREAM_API_KEY': lambda e: e.get('AISSTREAM_API_KEY'),
    'HF_TOKEN': lambda e: e.get('HF_TOKEN'),
    'DEVICE_API_KEY': lambda e: e.get('DEVICE_API_KEY'),
    'SPEND_CAP_USD': lambda e: float(e.get('SPEND_CAP_USD', '50') or 0),
    'AGENT_SPEND_CAP_USD': lambda e: float(e.get('AGENT_SPEND_CAP_USD', '0') or 0),
    'OPENROUTER_MONTHLY_BUDGET_USD': lambda e: float(e.get('OPENROUTER_MONTHLY_BUDGET_USD', '0') or 0),
    'PAGE_REQUEST_MONTHLY_BUDGET': lambda e: int(e.get('PAGE_REQUEST_MONTHLY_BUDGET', '1000') or 1000),
    'HIGH_TIER_MONTHLY_BUDGET_USD': lambda e: float(e.get('HIGH_TIER_MONTHLY_BUDGET_USD', '0') or 0),
    'HIGH_TIER_MAX_PRICE_USD': lambda e: float(e.get('HIGH_TIER_MAX_PRICE_USD', '0') or 0),
    'CODING_TIER_MAX_PRICE_USD': lambda e: float(e.get('CODING_TIER_MAX_PRICE_USD', '0') or 0),
    'VISION_TIER_MAX_PRICE_USD': lambda e: float(e.get('VISION_TIER_MAX_PRICE_USD', '0') or 0),
    'SUBLOW_TIER_MAX_PRICE_USD': lambda e: float(e.get('SUBLOW_TIER_MAX_PRICE_USD', '0.09') or 0),
    'APIFY_MONTHLY_BUDGET_USD': lambda e: float(e.get('APIFY_MONTHLY_BUDGET_USD', '5') or 0),
    'COLAB_MONTHLY_UNITS': lambda e: float(e.get('COLAB_MONTHLY_UNITS', '0') or 0),
    'DECISION_CACHE_TTL_S': lambda e: float(e.get('DECISION_CACHE_TTL_S', '3600') or 0),
    'DECISION_KEY_WINDOW_S': lambda e: float(e.get('DECISION_KEY_WINDOW_S', str(7 * 24 * 3600)) or (7 * 24 * 3600)),
    'JEV_MODEL': lambda e: e.get('JEV_MODEL', 'typesafe/jev-1.13'),
    'JEV_JUDGE_MODEL': lambda e: (e.get('JEV_JUDGE_MODEL') or '').strip() or None,
    'API_CALL_ENABLED': lambda e: _env_flag(e.get('API_CALL_ENABLED', 'true')),
    'BROWSING_ENABLED': lambda e: _env_flag(e.get('AGENT_BROWSING_ENABLED', 'true')),
    'EXECUTION_ENABLED': lambda e: _env_flag(e.get('AGENT_EXECUTION_ENABLED', 'true')),
    'PLAYER_EMAIL_ENABLED': lambda e: _env_flag(e.get('PLAYER_EMAIL_ENABLED', 'true')),
    # Sandbox execution mode + the sandbox egress allowlist. Rebinding the
    # allowlist also refreshes the egress proxy (see _reload_env_config).
    'SANDBOX_EXECUTION': lambda e: e.get('SANDBOX_EXECUTION', 'local').strip().lower(),
    'BROWSE_ALLOWLIST_DOMAINS': lambda e: {d.strip().lower() for d in
                                           (e.get('BROWSE_ALLOWLIST_DOMAINS', '') or '').split(',') if d.strip()},
    # Player notifications (read via _serve.* from notify.py).
    'MULLVAD_ACCOUNT_NUMBER': lambda e: e.get('MULLVAD_ACCOUNT_NUMBER'),
    'SMTP_HOST': lambda e: e.get('SMTP_HOST'),
    'SMTP_PORT': lambda e: int(e.get('SMTP_PORT', '587') or 587),
    'SMTP_USER': lambda e: e.get('SMTP_USER'),
    'SMTP_PASSWORD': lambda e: e.get('SMTP_PASSWORD'),
    'ESCALATION_EMAIL_TO': lambda e: e.get('ESCALATION_EMAIL_TO'),
    # Default follows SELF_BASE_URL's boot value, not a hardcoded port --
    # resolve links in escalation messages must hit THIS process, and the
    # old localhost:8010 default silently produced dead Approve/Deny URLs
    # whenever the real server ran elsewhere (e.g. :8937).
    'ESCALATION_BASE_URL': lambda e: e.get('ESCALATION_BASE_URL') or e.get('SELF_BASE_URL', 'http://localhost:8010'),
    # Publishing target (PUBLISH_REMOTE_URL is recomputed after the rebind).
    'PUBLISH_REPO': lambda e: (e.get('AI_THINK_TANK_PUBLISH_REPO') or '').strip(),
    # Caches + sync cadence (read at call time).
    'AGENT_DIR_SYNC_INTERVAL_S': lambda e: float(e.get('AGENT_DIR_SYNC_INTERVAL_S', '15') or 15),
    'DIGITALOCEAN_BALANCE_CACHE_TTL_S': lambda e: float(e.get('BALANCE_CACHE_TTL_S', '300') or 300),
    'TREG_BALANCE_CACHE_TTL_S': lambda e: float(e.get('BALANCE_CACHE_TTL_S', '300') or 300),
    'PIXELLAB_BALANCE_CACHE_TTL_S': lambda e: float(e.get('BALANCE_CACHE_TTL_S', '300') or 300),
    'GOOGLE_ACCESS_TOKEN_TTL_S': lambda e: float(e.get('GOOGLE_ACCESS_TOKEN_TTL_S', '3000') or 3000),
    'COLAB_ENABLED': lambda e: str(e.get('COLAB_ENABLED', '1') or '1').lower() not in ('0', 'false', 'no'),
    'COLAB_STANDBY_ENABLED': lambda e: str(e.get('COLAB_STANDBY_ENABLED', '') or '').lower() in ('1', 'true', 'yes'),
    'COLAB_FREE_TIER': lambda e: str(e.get('COLAB_FREE_TIER', '') or '').lower() in ('1', 'true', 'yes'),
    # TTC cap: the clamp depends on both names, so TTC_BEST_OF is loaded here
    # and TTC_MAX_BEST_OF is recomputed in _reload_env_config after it.
    'TTC_BEST_OF': lambda e: max(2, min(int(e.get('TTC_BEST_OF', '2') or 2), int(e.get('TTC_MAX_BEST_OF', '3') or 3))),
    # Conversation memory (read via serve.* from memory.py at call time).
    'MEMORY_ENABLED': lambda e: _env_flag(e.get('MEMORY_ENABLED', 'true')),
    'STM_MAX_TURNS': lambda e: int(e.get('STM_MAX_TURNS', '40') or 40),
    'STM_MAX_TOKENS': lambda e: int(e.get('STM_MAX_TOKENS', '8000') or 8000),
    'STM_TTL_HOURS': lambda e: int(e.get('STM_TTL_HOURS', '168') or 168),
    'LTM_MAX_RECALL': lambda e: int(e.get('LTM_MAX_RECALL', '5') or 5),
    'LTM_MAX_PER_SESSION': lambda e: int(e.get('LTM_MAX_PER_SESSION', '200') or 200),
    'LTM_EXTRACT_EVERY_TURNS': lambda e: int(e.get('LTM_EXTRACT_EVERY_TURNS', '6') or 6),
    'LTM_MIN_CONFIDENCE': lambda e: float(e.get('LTM_MIN_CONFIDENCE', '0.3') or 0.3),
    'LTM_MAX_AGE_DAYS': lambda e: int(e.get('LTM_MAX_AGE_DAYS', '180') or 180),
    'LTM_CONSOLIDATE_ENABLED': lambda e: e.get('LTM_CONSOLIDATE_ENABLED', 'true') not in ('0', 'false', 'False'),
    'LTM_CONSOLIDATE_EVERY_TURNS': lambda e: int(e.get('LTM_CONSOLIDATE_EVERY_TURNS', '12') or 12),
    'LTM_CONSOLIDATE_MAX_CLUSTERS': lambda e: int(e.get('LTM_CONSOLIDATE_MAX_CLUSTERS', '4') or 4),
}
