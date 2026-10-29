"""FastAPI server for the think tank: state, tasks, agents, auth, and the
operator/agent HTTP surface. Run with `python3 serve.py [port]`.
"""
import asyncio
import base64
import contextlib
import copy
import dataclasses
import datetime
import hashlib
import hmac
import html
import json
import math
import os
import re
import random
import secrets
import shutil
import statistics
import sqlite3
import threading
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# `python3 serve.py` executes this file as __main__, while sim/content/etc.
# import `serve` by name. Without this alias that creates a second module
# object with its own globals, so in-memory flags such as _DORMANT set by the
# __main__ idle loop are invisible to the sim. Bind the script identity to the
# module name before any local import runs.
if __name__ == '__main__':
    sys.modules.setdefault('serve', sys.modules[__name__])

from core.confirmations import gate as _confirmation_gate_or_response
from core.confirmations import gate_finish as _finish_confirmation

import core.runtime_limits as runtime_limits
from core.tool_policy import is_known_tool, task_tool_policy, tool_trust_tier
import core.governance as governance
import core.constitution as constitution
import server.cron_scripts as cron_scripts
from core.optimization_advisor import build_optimization_advice
import core.policies as policies

# Pure web/text/path/security helpers extracted to their own module
# to shrink the serve.py monolith. See web_helpers.py.
from integrations.web_helpers import (  # noqa: E402,F401
    _block_link_value,
    _download_dest_rel_path,
    _extract_links,
    _is_safe_public_host,
    _is_short_link,
    _looks_like_gmail_app_password,
    _parse_http_date_ms,
    _resolve_final_url,
    _resolve_public_ip,
    _safe_urlopen,
    _sanitize_download_filename,
    _sha256_file,
    _strip_html_to_text,
)

# ---------------------------------------------------------------------------
# Extracted modules (see DESIGN.md). The Bank (spend ledger + budget gates),
# player auth/sessions, and escalation/email/telegram notifications live in
# bank.py / auth.py / notify.py and are re-exported here so
# `serve._accrue_spend`, `serve.create_session`, `serve.create_escalation`,
# etc. stay the stable public surface the call sites and tests use. The
# modules reach serve.py's own state through `_serve.X` at call time (the
# same lazy pattern sim.py and content.py use), so there is no import-time
# cycle: by the time these functions RUN, this module is fully populated.
from core.auth import (  # noqa: E402,F401
    _LOGIN_ATTEMPT_LIMIT,
    _check_login_rate_limit,
    _get_or_create_admin_credentials,
    _get_or_create_device_key,
    _get_or_create_server_secret,
    _hash_password,
    _require_player_session,
    PBKDF2_ITERATIONS,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_S,
    create_session,
    destroy_session,
    verify_session,
)
from integrations.bank import (  # noqa: E402,F401
    DEFAULT_BUDGET_CAP_USD,
    OPENROUTER_CREDITS_CACHE_TTL_S,
    _OPENROUTER_CREDITS_CACHE,
    _SPEND_CAP_BASELINE_KEY,
    _accrue_apify_spend,
    _accrue_colab_units,
    _accrue_high_tier_spend,
    _agent_spend_cap_exceeded,
    _agent_spend_key,
    _accrue_page_request,
    _accrue_spend,
    _accrue_task_spend,
    _record_unpriced_usage,
    _task_unpriced_blocked,
    _resolve_unpriced_usage,
    _apify_budget_exceeded,
    _apify_budget_month,
    _apify_spend_this_month,
    _bank_budget_view,
    _budget_cap_usd,
    _colab_budget_exceeded,
    _colab_budget_month,
    _colab_spend_this_month,
    _director_budget_map,
    _director_of_agent,
    _director_spend_key,
    _director_spend_status,
    _forecast,
    _high_tier_budget_exceeded,
    _high_tier_budget_month,
    _high_tier_spend_this_month,
    _ledger_month_to_date,
    _openrouter_account_credits,
    _openrouter_month_to_date,
    _page_budget_exhausted,
    _page_budget_ledger_read,
    _page_budget_ledger_write,
    _page_budget_month,
    _page_budget_used,
    _spend_cap_month_view,
    _spend_cap_period,
    _spend_ledger_read,
    _spend_ledger_write,
    _task_budget_exhausted,
    _task_budget_spent,
    _task_spend_key,
    _think_tank_spend_cap_exceeded,
    _village_spend_view,
)
# The curated RSS/ATOM aggregator (see DESIGN.md "Current events"). Extracted
# into rss.py the same way: the poll loop, tolerant parser, ring buffer, and
# operator-managed feed registry are re-exported here so the routes, the
# read_rss_feed tool, and the lifespan wiring keep calling `serve._rss_*`.
from integrations.rss import (  # noqa: E402,F401
    _RSS_BUFFER,
    _RSS_BUFFER_LOCK,
    _RSS_BUFFER_MAX_PER_FEED,
    _RSS_FEED_SEED,
    _RSS_FEEDS,
    _RSS_FEEDS_LOAD_ERROR,
    _RSS_FETCH_TIMEOUT_S,
    _RSS_HTTP_HEADERS,
    _RSS_STATUS,
    _RSS_WINDOW_S,
    _fetch_feed_sync,
    _load_rss_feeds,
    _parse_rss_atom_xml,
    _probe_rss_url,
    _rss_feed_row_to_spec,
    _rss_feed_spec_to_row,
    _rss_feed_upsert,
    _rss_ingest,
    _rss_match_topics,
    _rss_poll_loop,
    _rss_recent,
    _seed_rss_feeds_if_empty,
    _validate_rss_feed_spec,
)
# The generic WebSocket feed collector (see DESIGN.md "WebSocket feeds").
# Kraken and AISStream are registered data, not code; the descriptor language
# and the supervisor live in ws.py and are re-exported here so /api/ws/*, the
# read_ais_feed tool, and the lifespan wiring keep using `serve._ws_*`.
from server.ws import (  # noqa: E402,F401
    _WS_BUFFERS,
    _WS_FEED_SEED,
    _WS_FEEDS,
    _WS_FEEDS_LOAD_ERROR,
    _WS_LOCK,
    _WS_TASKS,
    _kraken_normalize_pair,
    _load_ws_feeds,
    _seed_ws_feeds_if_empty,
    _validate_ws_feed_spec,
    _ws_collector_loop,
    _ws_extract_frame,
    _ws_feed_row_to_spec,
    _ws_feed_spec_to_row,
    _ws_feed_upsert,
    _ws_ingest,
    _ws_latest,
    _ws_recent,
    _ws_resolve_path,
    _ws_status,
    _ws_substitute,
    _ws_subscribe_payload,
    _ws_supervisor_loop,
)
# Inbound webhook intake (see DESIGN.md "Inbound loom requests"). The channel
# registry, goal formatter, self-activity gate, and consumer loop live in
# webhooks.py; the endpoint in this file stays a pure sink.
from server.webhooks import (  # noqa: E402,F401
    CHANNELS as WEBHOOK_CHANNEL_SPECS,
    WEBHOOK_CHANNELS,
    _WEBHOOK_POLL_INTERVAL_S,
    _WEBHOOK_SEEN,
    _webhook_channel_enabled,
    _webhook_channel_spec,
    _webhook_goal_text,
    _webhook_handle_entry,
    _webhook_poll_loop,
    _webhook_read_new,
    _webhook_seed_seen,
    _webhook_seen_key,
    _webhook_self_mention,
    active_webhook_channels,
)
# Live-reloadable .env field table (see env_reload.py). The reload loop below
# reads serve._ENV_RELOAD_FIELDS, so tests that patch that name still work.
from core.env_reload import ENV_RELOAD_FIELDS as _ENV_RELOAD_FIELDS, _env_flag  # noqa: E402,F401

# The api_call service registry, channel routing, credential injection, and
# per-service daily call budget (see DESIGN.md "Generic api_call"). The gates
# and the execution path stay here; the config half lives in apis.py.
from integrations.apis import (  # noqa: E402,F401
    MULLVAD_TOKEN_REFRESH_MARGIN_S,
    OAUTH2_REFRESH_MARGIN_S,
    _API_CHANNELS,
    _API_CHANNELS_LOAD_ERROR,
    _API_SERVICE_COLUMNS,
    _API_SERVICE_JSON_COLUMNS,
    _API_SERVICES,
    _API_SERVICES_FINGERPRINT,
    _API_SERVICES_FRESH_CHECK,
    _API_SERVICES_LOAD_ERROR,
    _API_SERVICES_REFRESH_TTL_S,
    _MULLVAD_TOKEN_CACHE,
    _MULLVAD_TOKEN_LOCK,
    _OAUTH2_TOKEN_CACHE,
    _OAUTH2_TOKEN_LOCK,
    _api_accrue_daily_call,
    _api_apply_auth,
    _api_backend_url,
    _api_base_url,
    _api_channel_for_host,
    _api_credential_value,
    _api_daily_calls_day,
    _api_daily_calls_exceeded,
    _api_daily_calls_ledger_read,
    _api_daily_calls_ledger_write,
    _api_daily_calls_used,
    _api_service_for_host,
    _api_service_path_allowed,
    _api_service_row_to_spec,
    _api_service_spec_to_row,
    _api_service_upsert,
    _approve_api_service_proposal,
    _ensure_api_services_fresh,
    _index_api_service,
    _load_api_channels,
    _load_api_services,
    _mullvad_access_token,
    _mullvad_token_request_sync,
    _oauth2_access_token,
    _oauth2_token_request_sync,
    _seed_api_services_if_empty,
    _validate_api_service_spec,
)
import integrations.apple_photos as _apple_photos  # noqa: E402

from integrations.notify import (  # noqa: E402,F401
    GMAIL_SMTP,
    GMAIL_SMTP_HOST,
    GMAIL_SMTP_PORT,
    _GMAIL_CRED_NAME,
    _credential_token,
    _load_escalations,
    _save_escalations,
    _send_escalation_email_sync,
    _send_player_email_sync,
    create_escalation,
    notify_human_of_escalation,
    provision_player_email,
    send_player_email_sync,
    send_player_telegram_sync,
)
# The Research Desk's claim-level evidence ledger + review-gated coverage
# ledger (see DESIGN.md "Research Desk" and evidence.py). Extracted into its
# own module exactly like bank.py: re-exported here so the stable public
# surface the endpoints and tests use is `serve._evidence_read` etc.
from core.evidence import (  # noqa: E402,F401
    CLAIM_DECISIONS,
    CLAIM_STATUSES,
    CLAIM_TYPES,
    COVERAGE_STATUSES,
    DEFAULT_OVERLAP_MS,
    UNRESOLVED_PREFIX,
    VERIFIER_COVERED_MAX,
    VERIFIER_COVERED_WINDOW_S,
    _coverage_read,
    _coverage_write,
    _evidence_read,
    _evidence_write,
    append_audit,
    count_actor_action,
    coverage_status,
    get_claim,
    is_source_covered,
    list_audit,
    list_claims,
    mark_covered,
    mark_reviewed,
    record_run_failure,
    record_observation_change,
    record_source_claim,
    search_since_ms,
    source_identity,
    update_claim,
)
# Sublow experiment grants (see DESIGN.md "Sublow experiment grants" and
# sublow.py): rotating server-side tokens gating sublow-model inference
# behind a declared cost estimate, so a Colab experiment can run at scale
# with NO OpenRouter key in the notebook. Re-exported here so the stable
# public surface the endpoints/tests use is `serve.mint_sublow_grant` etc.
from core.sublow import (  # noqa: E402,F401
    GRANT_TOKENS_DEFAULT,
    GRANT_TOKENS_MAX,
    GRANT_TTL_S_DEFAULT,
    GRANT_TTL_S_MAX,
    SUBLOW_LEDGER_SERVICE,
    accrue_sublow_grant,
    expire_sublow_grants,
    mint_sublow_grant,
    quote_sublow_grant,
    revoke_agent_sublow_grants,
    revoke_sublow_grant,
    validate_sublow_grant,
)
# JEV experiment grants (see DESIGN.md "JEV experiment grants" and
# jev_grants.py): the same rotating server-side token system pointed at the
# JEV decisions model -- same restrictions, different model. Re-exported so
# the stable public surface the endpoints/tests use is `serve.mint_jev_grant`
# etc.
from core.jev_grants import (  # noqa: E402,F401
    JEV_FALLBACK_PRICE_PER_M,
    JEV_LEDGER_SERVICE,
    accrue_jev_grant,
    expire_jev_grants,
    mint_jev_grant,
    quote_jev_grant,
    revoke_agent_jev_grants,
    revoke_jev_grant,
    validate_jev_grant,
)
# Operator-edited policy JSON (world/plain_writing.json, browse_policy.json,
# research_desk.json, watchlist.json) loaded by world/config.py. These are
# DATA, not code: the operator tunes the ban list, block categories, desk
# passes, and the research watchlist by editing the JSON files. Re-exported so
# the stable surface the endpoints/tests use is `serve.PLAIN_WRITING_BAN_LIST`
# etc. (see world/config.py for load discipline).
import core.config as config  # noqa: E402
from core.config import (  # noqa: E402,F401
    ALLOW_WATCHLIST_WRITE,
    API_LOG_ALLOWED_ACTIONS,
    NON_WAKING_READS_EXTRA,
    NON_WAKING_WRITES_EXTRA,
    ROOM_DELEGATABLE_OVERRIDES,
    ROOM_DEFINITION_OVERRIDES,
    SEED_DEFAULT_PROFILE_OVERRIDE,
    SEED_PROFILE_OVERRIDES,
    CEREMONY_ACTIONS,
    BAND_BENCHMARK,
    BAND_QUALITY_FLOOR_GAP,
    BROWSE_BLOCK_CATEGORIES,
    CREDENTIAL_ACCESS_CONFIG,
    MODEL_BAND_ALIASES,
    MODEL_BAND_PURPOSE,
    MODEL_PRICE_BANDS,
    MODEL_PROVIDER_OVERRIDES,
    PLAIN_WRITING_BAN_LIST,
    PROGRESS_ACTIONS,
    RESEARCH_WATCHLIST,
    REWORK_ACTIONS,
    ROOT,
    SECRET_PATTERNS,
    STANDING_SAFE_DOMAINS,
    TIER_GATE_CRITERIA,
    TOOL_DESCRIPTIONS,
    TREG_ENDPOINT_COSTS,
    ON_RELOAD_HOOKS,
    bind_policy,
    get_credential_access_config,
    get_grants_config,
    get_operator_policy,
    note_watchlist_topic,
    reload_policy_config,
)

# Original private names kept so existing call sites and the redaction tests
# stay byte-identical; the objects themselves are the config module's live
# containers, so an operator reload updates them in place.
_SECRET_PATTERNS = SECRET_PATTERNS
_TIER_GATE_CRITERIA = TIER_GATE_CRITERIA
# Accounting taxonomies are operator data in world/actions.json; these aliases
# are the same live containers, so a policy reload updates them in place.
_CEREMONY_ACTIONS = CEREMONY_ACTIONS
_PROGRESS_ACTIONS = PROGRESS_ACTIONS
_REWORK_ACTIONS = REWORK_ACTIONS
_API_LOG_ALLOWED_ACTIONS = API_LOG_ALLOWED_ACTIONS

# JEV decision engine (Phase 0c self-modification split): the modifiable
# engine lives in jev.py; these re-exports keep every serve.X call site and
# test patch path byte-identical. The apply flow for a self-edit of jev.py
# must reload jev and refresh these names from the reloaded module while the
# tank sleeps (serve itself must never be reloaded -- uvicorn owns `app`).
from core.jev import (  # noqa: E402,F401
    ESCALATION_DEFAULT_FLOOR,
    ESCALATION_ERROR_PENALTY_CAP,
    ESCALATION_ERROR_PENALTY_STEP,
    ESCALATION_KIND_RISK,
    JEV_SAFETY_CONFIDENCE,
    QUORUM_MIN_AGREEING,
    QUORUM_SAMPLE_SIZE,
    _jev_choice,
    _jev_quorum_choice_sync,
    _jev_quorum_decision,
)

# Self-edit pipeline (Phase 1): the modifiable-surface contract lives in
# selfedit.py (a leaf module -- it reaches serve lazily at call time, exactly
# like jev does). propose/revert/status are served through dedicated gated
# endpoints below; apply_pass is wired into _idle_shutdown_loop and acts only
# while DORMANT, per the operator decisions locked 2026-10-18.
import selfedit as _selfedit  # noqa: E402

# Conversation memory for the player's Telegram chat (see DESIGN.md "Memory
# Architecture" and world/memory.py). Extracted into its own module exactly
# like bank.py/evidence.py: STM stores each ask turn per chat session and
# injects recent context into the next ask (fixes the follow-up question
# failure); LTM distills durable facts/preferences from the conversation and
# recalls the relevant ones on each ask. Re-exported here so the stable public
# surface the call sites and tests use is `serve.memory_context` etc.
from agents.memory import (  # noqa: E402,F401
    LTM_TYPES,
    cleanup_expired_sessions,
    ltm_maybe_extract,
    ltm_recall,
    memory_context,
    stm_append,
    stm_history,
)
# Scoped access to the player's own folders (Desktop, Downloads) and iCloud
# Notes for agents, gated by player-issued grants (see DESIGN.md "Scoped File
# and Notes Access" and world/fs.py). Every call is checked for containment,
# the trash rule (deletes move to trash, never empty it), and Notes ownership.
# Re-exported here so the stable surface call sites/tests use is `serve.fs_*`.
from core.fs import (  # noqa: E402,F401
    delete_to_trash as fs_delete_to_trash,
    list_scope as fs_list_scope,
    move_file as fs_move_file,
    read_file as fs_read_file,
    write_file as fs_write_file,
)
# Local, credential-redacted interaction recording for fine-tuning
# datasets (opt-in via TRAINING_CAPTURE_ENABLED; see
# datasets/INTERACTION_RECORDING.md). Stdlib-only module, so
# importing it here cannot create a cycle.
import agents.training_capture as training_capture  # noqa: E402
import integrations.vpn_location as vpn_location  # noqa: E402
import core.theo_policy as theo_policy  # noqa: E402
import integrations.tank_accounts as tank_accounts  # noqa: E402
import agents.browser_session as browser_session  # noqa: E402
import agents.captcha as captcha  # noqa: E402
import integrations.vpn_pool as vpn_pool  # noqa: E402
import agents.containers as containers  # noqa: E402
import core.egress as egress  # noqa: E402
import integrations.phone as phone  # noqa: E402
import core.planner as planner  # noqa: E402
import agents.story_budget as story_budget  # noqa: E402
import village.schedule as schedule  # noqa: E402

# Optional, real dependencies for document ingestion (/api/library/ingest,
# below) -- openpyxl (Excel) was already installed for something else in
# this environment; pypdf (PDF text extraction) was added specifically for
# this feature. Both guarded so a missing install degrades to a clear
# per-file error instead of the whole server failing to start.
try:
    import pypdf
except ImportError:
    pypdf = None  # type: ignore[assignment]
try:
    import openpyxl
except ImportError:
    openpyxl = None
# Same pattern for Word (python-docx) and PowerPoint (python-pptx) text
# extraction -- guarded so a missing install degrades to a clear per-file
# error, never a failed server boot.
try:
    import docx as _docx
except ImportError:  # pragma: no cover
    _docx = None  # type: ignore[assignment]
try:
    import pptx as _pptx
except ImportError:  # pragma: no cover
    _pptx = None  # type: ignore[assignment]
# For /api/page-probe below -- the existing /api/screenshot's plain
# `chrome --headless --screenshot` is a one-shot, non-interactive capture;
# it can show what a page looks like after load, but it can't click a
# button, press a key, wait, and then check what actually happened, which
# is exactly the gap a real interactive-DOM tool needs to close (see
# DESIGN.md's page-probe entry: three separate fix attempts, across three
# different models, all guessed at a body class/global function that never
# existed, because none of them had any way to actually check). Already
# present on this machine (Claude Code's own tooling depends on it), so
# this doesn't add a new install -- only a new import in this file.
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect
from typing import Optional

# ROOT (world/) comes from config.py: one path definition for the codebase.
THINK_TANK_DIR = os.path.dirname(ROOT)


def _load_env():
    # Loads the same .env the CLI already uses for other service keys
    # (~/ai-think-tank/.env, one directory above world/) -- manual parsing,
    # matching the rest of the project's own convention, rather than
    # adding a python-dotenv dependency for one file read.
    # Reads through THINK_TANK_DIR (not ROOT): on the live server they are
    # the same directory, and hermetic tests that repoint THINK_TANK_DIR
    # then get an empty env instead of silently inheriting the operator's
    # real keys -- the free-pool picker tests were passing on that leak.
    env = {}
    env_path = os.path.join(THINK_TANK_DIR, '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if isinstance(line, bytes):
                        line = line.decode('utf-8', 'replace')
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        env[k] = v
        except OSError:
            # A missing/unreadable .env is not fatal: callers treat absent
            # configuration as defaults, and tests often mock open().
            pass
    return env


if __name__ == '__main__':
    try:
        _policy_mode = (_load_env().get('OPTIMIZATION_POLICY_MODE') or '').strip().lower()
        if _policy_mode in ('shadow', 'apply'):
            os.environ['OPTIMIZATION_POLICY_MODE'] = _policy_mode
    except Exception:
        pass


def _reload_google_rate_limits(env):
    """Rebuild the Google per-minute rate ceilings IN PLACE from .env. The
    dict is read at call time by the limiter, so mutating it takes effect
    without a restart; in-place (not rebind) keeps any held reference live."""
    limits = globals().get('_GOOGLE_RATE_LIMITS')
    if not isinstance(limits, dict):
        return False
    changed = False
    specs = {
        'calendar': ('GOOGLE_CALENDAR_RATE_PER_MIN', '400', 0.15),
        'sheets': ('GOOGLE_SHEETS_RATE_PER_MIN', '40', 0.4),
        'gmail': ('GOOGLE_GMAIL_RATE_PER_MIN', '80', 0.3),
        'docs': ('GOOGLE_DOCS_RATE_PER_MIN', '80', 0.3),
    }
    for api, (key, default, interval) in specs.items():
        try:
            new = (float(env.get(key, default) or default), interval)
        except (TypeError, ValueError):
            continue
        if limits.get(api) != new:
            limits[api] = new
            changed = True
    return changed


def _reload_env_config():
    """Re-read .env and rebind the live-swappable module globals. Returns a
    report of which names changed; values are never echoed back. Any rebind
    failure is isolated to that name (the previous value stays)."""
    env = _load_env()
    g = globals()
    changed = []
    for name, loader in _ENV_RELOAD_FIELDS.items():
        try:
            value = loader(env)
            if g.get(name) != value:
                g[name] = value
                changed.append(name)
        except Exception:
            pass  # a bad value for one name never blocks the rest
    # Derived values that depend on a reloaded name.
    try:
        best_of = g.get('TTC_BEST_OF')
        cap = int(env.get('TTC_MAX_BEST_OF', '3') or 3)
        new_max = max(best_of, cap)
        if g.get('TTC_MAX_BEST_OF') != new_max:
            g['TTC_MAX_BEST_OF'] = new_max
            changed.append('TTC_MAX_BEST_OF')
    except Exception:
        pass
    try:
        remote = ('https://github.com/' + g['PUBLISH_REPO'] + '.git') if g.get('PUBLISH_REPO') else ''
        if g.get('PUBLISH_REMOTE_URL') != remote:
            g['PUBLISH_REMOTE_URL'] = remote
            changed.append('PUBLISH_REMOTE_URL')
    except Exception:
        pass
    try:
        if _reload_google_rate_limits(env):
            changed.append('_GOOGLE_RATE_LIMITS')
    except Exception:
        pass
    # A changed egress allowlist must also refresh the running proxy so the
    # new domains are actually reachable -- best-effort, never blocks reload.
    if 'BROWSE_ALLOWLIST_DOMAINS' in changed:
        try:
            ensure_sandbox_networking()
        except Exception:
            pass
    # An env reload is the operator touching keys/config -- wipe the API-key
    # auth-health tracker so a swapped key starts from a clean slate (the
    # next real 401 will re-alert; a working key never re-alerts).
    with _api_key_health_lock:
        _api_key_health.clear()
    return {'reloaded': True, 'changed': sorted(changed), 'count': len(changed)}

# Deliberately OUTSIDE world/ -- same reasoning as the .env API key: this
# directory is served to the browser as static files, so anything under it
# is visible via view-source. A database of what agents actually did is
# exactly the kind of thing that must never be publicly fetchable.
SANDBOXES_DIR = os.path.join(THINK_TANK_DIR, 'sandboxes')
ESCALATIONS_PATH = os.path.join(THINK_TANK_DIR, 'escalations.json')
# Inbound loom requests (/api/webhook/{channel} below): one JSON line per
# relayed event, each tagged with the channel that produced it (x-mention is
# the Gmail-watcher channel for X notification emails; future watchers -- a
# phone-forwarder, a GitHub bot -- just pick a new channel name, no server
# changes). Kept outside world/ for the same reason as sandboxes/ -- the
# agent-facing evidence of what a human asked for must never be statically
# servable.
WEBHOOKS_PATH = os.path.join(THINK_TANK_DIR, 'webhooks.jsonl')
# The channels a webhook can write to live with their goal formatter and
# self-activity gate in webhooks.py (CHANNELS); adding an integration is adding
# one descriptor there, not a change here.
# Draft-failure ledger + rule proposals: the human-in-the-loop rule book (see
# DESIGN.md "Failure taxonomy and rule mining"). `failures.json` is the sorted
# raw material -- one classified failure per review/QA send-back. The weekly
# rule-mining pass aggregates it and writes `rule_proposals.json`: recurring
# patterns (>= RULE_MIN_RECURRENCE) become PROPOSED rules (rule text + test
# fixture), never auto-applied -- the operator turns a proposal into a real
# rule by editing the ban list / Jev criteria, exactly like the other
# operator-applied governance changes. Bounded so an aging think tank cannot
# grow the files without bound.
FAILURES_PATH = os.path.join(THINK_TANK_DIR, 'failures.json')
RULE_PROPOSALS_PATH = os.path.join(THINK_TANK_DIR, 'rule_proposals.json')
FAILURE_MAX_RECORDS = 200
RULE_PROPOSALS_MAX = 50
RULE_MIN_RECURRENCE = 2  # "happened more than once" -> rule-worthy
# Success ledger + success proposals: the POSITIVE half of the rule book (the
# ChatDev ECL lesson -- "agents get smarter" is not only about not repeating
# failures, it's about propagating what WORKED). `successes.json` is the raw
# material -- one high-graded deliverable per row. The weekly success-mining
# pass aggregates it and writes `success_proposals.json`: a room that keeps
# shipping high-graded work (>= SUCCESS_MIN_RECURRENCE rows) becomes a PROPOSED
# "keep doing what works here" lesson, never auto-applied -- the operator turns
# it into a real standard like the failure rules. Same bounded shape as the
# failure ledger so an aging think tank cannot grow the files without bound.
SUCCESSES_PATH = os.path.join(THINK_TANK_DIR, 'successes.json')
SUCCESS_PROPOSALS_PATH = os.path.join(THINK_TANK_DIR, 'success_proposals.json')
SUCCESS_MAX_RECORDS = 200
SUCCESS_PROPOSALS_MAX = 50
SUCCESS_MIN_RECURRENCE = 2    # same rule as failures: "happened more than once"
SUCCESS_GRADE_FLOOR = 8.0     # a deliverable graded at/above this is a "success"
# The four-bucket sort every draft failure goes into.
FAILURE_TYPES = ('factual_error', 'client_preference', 'missing_information', 'style')
FAILURE_TYPE_LABELS = {
    'factual_error': 'a number or claim is wrong or has no source',
    'client_preference': 'it contradicts the stated client/operator preference',
    'missing_information': 'a required fact or field is missing',
    'style': 'style, tone, or wording',
}
DB_PATH = os.environ.get('THINK_TANK_DB') or os.path.join(THINK_TANK_DIR, 'think_tank.db')


def _refuse_production_db_from_tests():
    """Fail loudly when a TEST process is about to open the production DB.

    The pytest suite is isolated by world/tests/conftest.py, which points
    THINK_TANK_DB at a per-session copy before serve is ever imported. But
    conftest only runs under pytest: a test module executed directly
    (`python world/tests/core/test_x.py`, `python -m unittest ...`) imports
    serve with the operator's real environment, and every _db() writer --
    decision tape, action_log, escalation saves -- then lands in the live
    database. That exact pollution happened before conftest existed (fixture
    titles in the live action_log). This guard makes the bypass impossible
    instead of merely unlikely: an import from a test-looking entrypoint that
    resolves to the production think_tank.db raises here, before any write.

    The live server, operator scripts, and ad-hoc operator tooling pass:
    their argv[0] is serve.py or a scripts/ path, not a tests/ path."""
    argv0 = (sys.argv[0] or '') if sys.argv else ''
    base = os.path.basename(argv0)
    test_entry = (os.sep + 'tests' + os.sep) in argv0 or base.startswith(('pytest', 'py.test'))
    if not test_entry:
        return
    production = os.path.realpath(os.path.join(THINK_TANK_DIR, 'think_tank.db'))
    if os.path.realpath(DB_PATH) != production:
        return  # already redirected (conftest / explicit THINK_TANK_DB)
    raise RuntimeError(
        'serve.py refuses to open the production think_tank.db from a test '
        f'process (entrypoint {argv0!r}). Run tests through pytest '
        '(world/tests/conftest.py redirects THINK_TANK_DB to a session copy), '
        'or set THINK_TANK_DB explicitly.')


_refuse_production_db_from_tests()
# The Library's real capability -- a shared file directory
# any agent can read from or write to (code, notes, completed-task
# records), with archive/ specifically for completed tasks. Same
# "deliberately outside world/" reasoning as everything else here.
LIBRARY_DIR = os.path.join(THINK_TANK_DIR, 'library')
LIBRARY_ARCHIVE_DIR = os.path.join(LIBRARY_DIR, 'archive')
# Immutable "product passport" -- a hash chain of every file promoted to the
# trusted Library (modeled on an EU digital product passport). Each
# promoted file's content hash is appended as a block whose index links to the
# previous block's hash, so tampering with or silently deleting any promoted
# file breaks the chain and is detectable. No mining/consensus -- it's a
# lightweight, auditable chain-of-blocks for one think tank.
PASSPORT_PATH = os.path.join(LIBRARY_DIR, '.passport.json')

# ---------------------------------------------------------------------------
# Operational guardrails. Two electric fences around the LIVE
# think tank store, because a long-running state-bearing server with real model
# spend is exactly the thing that silently eats money or loses state when
# nobody is watching it:
#   1) DB_BACKUP_DIR -- an automated `think_tank.db` checkpoint (sqlite .backup,
#      safe under WAL) goes here on a timer AND on clean shutdown, rotated to
#      the last DB_BACKUP_KEEP. This closes the "2-day-stale manual .bak"
#      hole: a crash no longer forfeits everything since the last human copy.
#   2) _MAX_IDLE_MINUTES -- when set (serve.py --max-idle-minutes N), the
#      server goes DORMANT after N minutes of no HTTP request at all (touched
#      in the no_store middleware, so it's any request, authed or not). This
#      turns the standing "pause the think tank when idle + wake it on a remote
#      request" requirement into an enforced default instead of a manual habit. It is a
#      SLEEP, not an exit: the process stays up and port-bound so any request
#      (e.g. to the admin, from a phone) flips the think tank back awake instantly
#      -- no cold start, no need to be at a computer. Only the simulation is
#      paused (no movement, no task cycle, no model spend); the DB checkpoint
#      and handle-expiry keep running.
DB_BACKUP_DIR = os.path.join(THINK_TANK_DIR, 'think-tank-db-backups')
DB_BACKUP_KEEP = 1
# Hourly, not every 5 min: the DB is ~33 MB, so the old cadence
# wrote ~9.5 GB/day of snapshots. Only DB_BACKUP_KEEP files are
# kept, so the interval just sets how stale a crash-recovery
# point may be.
DB_BACKUP_INTERVAL_S = 60 * 60
# Log retention: decision_tape and action_log are append-only audit
# tables whose heavy columns (prompt/raw/details) ballooned the DB to ~1 GB. The
# /api/decisions feed and activity feed only ever READ the recent window, so old
# rows are pure bloat -- prune to a rolling retention window on a coarse cadence
# so growth is bounded without losing the recent history anyone actually looks at.
# (LOG_RETENTION_DAYS is read lazily in _prune_logs because _load_env is defined
# later in this module.)
LOG_RETENTION_DAYS_DEFAULT = 7
LOG_PRUNE_INTERVAL_S = 6 * 3600          # twice a day is plenty for a rolling prune
LOG_PRUNE_MAX_ROWS = 50_000              # cap per run so a big backlog can't block the loop
_LAST_LOG_PRUNE = None
# Model-tier re-pick, two cadences (player's call):
#   1. MODEL_TIER_REFRESH_INTERVAL_S -- full re-pick of EVERY band from the
#      live catalog + current benchmark scores (catches new/better models,
#      availability changes, score updates even when selected prices hold).
#   2. TIER_PRICE_WATCH_INTERVAL_S -- cheap watch of ONLY the selected tier
#      models' catalog prices. If any selected model's price moved (or it
#      vanished from the catalog), trigger a full re-pick so a better option
#      at that band is found within ~30 min instead of at the next full run.
# OpenRouter prices change with no published schedule (provider list prices
# repriced overnight -- GLM 5.3 Flash went $0.65 -> $1.22 between two runs).
# Both cadences share ONE pick path (refresh_model_tiers) and only log a
# governance entry when a band's model actually CHANGED, so price-watch cycles
# that find nothing don't bury the activity feed. Costs: catalog read is free;
# a re-pick is ~6 sub-cent verify probes (~$0.001, measured) -- a few cents a
# month even with both loops active. tier_daily_log keyed (log_date, band) so
# same-day reruns overwrite in place. In-flight calls are unaffected by any
# re-pick: the model slug is resolved once when the call starts and the
# request is already in flight; model_tiers rows update in one transaction,
# so readers see the old complete set or the new complete set, never a mix.
MODEL_TIER_REFRESH_INTERVAL_S = 2 * 3600   # full re-pick: every other hour
TIER_PRICE_WATCH_INTERVAL_S = 30 * 60      # price watch: every 30 minutes
_MAX_IDLE_MINUTES = 0.0  # 0 = disabled; set via --max-idle-minutes (float: allows <1m)
_LAST_REQUEST_TIME = None  # touched by the no_store middleware below
# Sleep-not-die. When --max-idle-minutes elapses, the server does
# NOT exit -- that would leave nothing bound to the port to hear a remote wake
# request, forcing someone to be physically present to restart it. Instead it goes
# DORMANT: the process stays alive and keeps the port bound, but the think tank
# simulation is skipped (no movement, no task cycle, no content executors, no
# model spend -- the bill and the churn that mattered both die), until ANY
# request flips it back awake in the no_store middleware. Zero extra always-on
# infra; identical on this Mac or a headless VPS; wake is an instant request.
_DORMANT = False  # True = think tank paused, waiting for a wake request


def _dormant():
    return bool(_DORMANT)


def _set_dormant(value):
    global _DORMANT
    _DORMANT = bool(value)
    return _DORMANT

# ---------------------------------------------------------------------------
# JEV decide throttle. The live action_log showed ~48.9k
# unattributed 'decide' rows over ~50h -- a sustained ~0.3/s spend with no
# responsible agent. Every decide now REQUIRES an agentId (attribution), and
# each agent is rate-limited by a sliding window: a real task tick may make a
# small burst (driver + navigator + model-tier picks resolve quickly), but a
# tight/spammy loop is refused. The gate bounds the damage whether the caller
# is a browser tick, a server loop, or a genuine bug. In-memory (not persisted)
# is fine -- a server restart resets the window, and the cost is about spend,
# not about correctness.
DECIDE_BURST_ALLOW = 6          # how many calls an agent may issue in one burst
DECIDE_BURST_WINDOW_S = 5.0     # the burst is measured over these many seconds
DECIDE_MIN_INTERVAL_S = 0.4     # hard floor between any two calls from one agent
# Chat/ask per-request token cap and OTP SMS TTL bounds. Literals are
# the operator-policy seeds; bind_policy declared bindings rebind them from
# operator_policy.json (chatMaxTokensCap / smsTtl*).
CHAT_MAX_TOKENS_CAP = 4000
SMS_TTL_DEFAULT_S = 300
SMS_TTL_MIN_S = 60
SMS_TTL_MAX_S = 900
_decide_stamps: dict[str, list[float]] = {}   # agentId -> list[monotonic timestamps]
_decide_lock = threading.Lock()


@contextlib.contextmanager
def _db():
    # A fresh connection per call rather than one long-lived global one --
    # sqlite3 connections aren't safe to share across threads, and this
    # avoids ever having to reason about that given async handlers and
    # asyncio.to_thread() calls elsewhere in this file. The DB itself is
    # tiny and local; the per-call overhead is not worth the complexity
    # a shared connection would add.
    #
    # A plain sqlite3.Connection used as `with _db() as conn:` only commits
    # or rolls back on exit -- it never closes the fd. Every one of the ~40
    # call sites did exactly that, leaking one fd per call (get_state_from_db()
    # alone is on nearly every request and sim tick). Wrapping as our own
    # contextmanager keeps the same commit/rollback semantics but adds the
    # close() the stdlib one never did.
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def external_write_key(*parts):
    """Stable digest for a logical external write. Callers that have a
    requestId should pass it directly; this helper is for deriving a key from
    the operation's own inputs."""
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode('utf-8', errors='replace'))
        h.update(b'\x00')
    return h.hexdigest()
