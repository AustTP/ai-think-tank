

# Additional tools offered to /api/intent/ask ONLY when the dispatched agent's
# role is Red Team Auditor -- real calls through the SAME gated
# endpoints any agent goes through (/api/curl, /api/keys/handles), never a
# shortcut around them. Built after the spike task type (a single free-text
# completion with no tool access at all) produced two independently
# fabricated "test reports" that invented a nonexistent endpoint and its
# responses rather than actually calling anything -- real tool access is
# what makes a security-test result real instead of a guess.
# Scoped local file access for agents (world/fs.py). Offered to the
# ask lane only when FILE_ACCESS_ENABLED AND the player has issued at least
# one grant; every call is still checked for containment + the trash rule
# inside fs.py. (iCloud Notes is NOT special-cased here -- agents drive the
# Notes app through the generic `app_script` tool once the player grants
# com.apple.Notes, and can read the Notes SQLite store via `read_store`.)
_LOCAL_FILE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'local_file',
        'description': 'Read/write/organize files in a folder the player granted you access to '
                       '(Desktop, Downloads, etc.). Every action is checked '
                       'against the player\'s grant: you can only touch paths inside the granted scope, '
                       'deletes move to the trash (you can never empty the trash). '
                       'Actions: list (list a folder in the scope; pass `path` to list a subfolder, '
                       'omit it to list the scope root), read (read a file\'s text), write '
                       '(create/overwrite a file), move (move/rename within the folder), delete (move to '
                       'trash). Supply the exact '
                       '`scope` path the player granted and a relative '
                       '`path`/`from`/`to` within it.',
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string',
                           'enum': ['list', 'read', 'write', 'move', 'delete']},
                'scope': {'type': 'string', 'description': 'The granted folder path (absolute).'},
                'path': {'type': 'string', 'description': 'Relative path within the scope (read/write/delete).'},
                'from': {'type': 'string', 'description': 'Source relative path (move).'},
                'to': {'type': 'string', 'description': 'Destination relative path (move).'},
                'content': {'type': 'string', 'description': 'File content (write).'},
            },
            'required': ['action'],
        },
    },
}


def _local_file_tool(agent_id, args):
    """Dispatch a `local_file` tool call to the scoped fs layer. Every failure
    (no grant, cap denied, containment escape, trash rule) is surfaced to the
    model as a denial -- never a bypass."""
    import core.fs as _fs
    action = (args or {}).get('action') or ''
    scope = (args or {}).get('scope') or ''
    try:
        if action == 'list':
            entries = _fs.list_scope(scope, (args or {}).get('path') or '')
            where = ((args or {}).get('path') or '').strip() or scope
            return ('Files in ' + where + ':\n' + '\n'.join(
                f"- {e['name']} ({e['type']})"
                + (f", {e['size']} bytes" if e.get('size') is not None else '')
                for e in entries)) or f'{where} is empty'
        if action == 'read':
            return _fs.read_file(scope, (args or {}).get('path') or '')
        if action == 'write':
            p = _fs.write_file(scope, (args or {}).get('path') or '',
                               (args or {}).get('content') or '')
            return f'wrote {p}'
        if action == 'move':
            p = _fs.move_file(scope, (args or {}).get('from') or '', (args or {}).get('to') or '')
            return f'moved to {p}'
        if action == 'delete':
            p = _fs.delete_to_trash(scope, (args or {}).get('path') or '')
            return f'moved to trash: {p}'
        return 'Unknown local_file action. Actions: list, read, write, move, delete.'
    except PermissionError as e:
        return f'Denied: {e}'
    except FileNotFoundError as e:
        return f'Not found: {e}'
    except Exception as e:
        return f'local_file failed: {e}'


# Generic structured-data reads (world/fs.py read_store): SQLite inside a
# granted scope, with optional gzip + protobuf decoding. Offered alongside
# local_file (FILE_ACCESS_ENABLED AND a grant); the DB is opened read-only and
# the query must be SELECT/PRAGMA/EXPLAIN/WITH. This is how an agent reads a
# store like the macOS Notes SQLite database as JSON without AppleScript.
_READ_STORE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_store',
        'description': 'Read structured data from a SQLite database inside a folder the player '
                       'granted you access to (read-only). Optionally gzip-decompress and/or '
                       'protobuf-decode chosen columns. Use this to pull data out of a local store '
                       'like the macOS Notes database (NoteStore.sqlite) as JSON rows -- no '
                       'AppleScript or app needed for reads. Supply the granted `scope` path, the '
                       'DB path relative to it, a read-only SQL `query` (SELECT/PRAGMA/EXPLAIN/WITH), '
                       'and column names to decode.',
        'parameters': {
            'type': 'object',
            'properties': {
                'scope': {'type': 'string', 'description': 'The granted folder path (absolute).'},
                'path': {'type': 'string', 'description': 'Relative path to the SQLite DB within the scope.'},
                'query': {'type': 'string', 'description': 'Read-only SQL: SELECT/PRAGMA/EXPLAIN/WITH (no writes).'},
                'gzipColumns': {'type': 'array', 'items': {'type': 'string'},
                                'description': 'Column names whose bytes are gzip-compressed (decompressed first).'},
                'protoColumns': {'type': 'array', 'items': {'type': 'string'},
                                 'description': 'Column names that are protobuf-encoded; extracts readable text.'},
                'maxRows': {'type': 'integer', 'description': 'Max rows to return (default 50, capped at 200).'},
            },
            'required': ['scope', 'path', 'query'],
        },
    },
}


def _read_store_tool(agent_id, args):
    """Dispatch a `read_store` tool call to the gated fs layer."""
    import core.fs as _fs
    try:
        rows = _fs.read_store(
            (args or {}).get('scope') or '',
            (args or {}).get('path') or '',
            (args or {}).get('query') or '',
            gzip_columns=(args or {}).get('gzipColumns') or [],
            proto_columns=(args or {}).get('protoColumns') or [],
            max_rows=(args or {}).get('maxRows') or 50,
        )
        import json as _json
        log_action(agent_id, 'read_store', {'scope': (args or {}).get('scope'), 'rows': len(rows)},
                   authorized=True)
        return _json.dumps(rows, indent=1, default=str)
    except PermissionError as e:
        log_action(agent_id, 'read_store_denied', {'reason': str(e)}, authorized=True)
        return f'Denied: {e}'
    except FileNotFoundError as e:
        return f'Not found: {e}'
    except Exception as e:
        return f'read_store failed: {e}'


# App control via AppleScript (world/fs.py): offered to lanes that also get
# local_file, under APP_ACCESS_ENABLED AND at least one player-issued
# appGrant. Every call is gated inside fs.py: the bundleId must be granted,
# and the script body is validated so it can only talk to that one app (no
# `do shell script`, no `open location`, no admin escalation, no cross-app
# `tell application`). A denial is surfaced to the model, never bypassed.
_VALID_BUNDLE_ID_RE = re.compile(r'^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$')
_APP_SCRIPT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'app_script',
        'description': 'Run AppleScript commands against ONE macOS app the player granted you '
                       'access to (e.g. read the current document, trigger an export, fetch '
                       'something from a desktop app that has no API). Pass the app\'s exact bundle '
                       'id (e.g. com.apple.Notes) and the AppleScript commands for that app ONLY. '
                       'Shell commands, opening URLs, escalating privileges, and scripting other '
                       'apps are denied -- keep the script simple and short.',
        'parameters': {
            'type': 'object',
            'properties': {
                'bundleId': {'type': 'string',
                             'description': 'The granted app bundle id (e.g. com.apple.Notes).'},
                'script': {'type': 'string',
                           'description': 'AppleScript body for that app only (no tell application, '
                                          'no do shell script, no open location).'},
            },
            'required': ['bundleId', 'script'],
        },
    },
}


def _app_script_tool(agent_id, args):
    """Dispatch an `app_script` tool call to the gated fs layer. Every failure
    (no grant, denied primitive, TCC/automation refusal, script error) is
    surfaced to the model as a denial -- never a bypass."""
    import core.fs as _fs
    bundle_id = (args or {}).get('bundleId') or ''
    script = (args or {}).get('script') or ''
    try:
        out = _fs.app_script(bundle_id, script)
        log_action(agent_id, 'app_script', {'bundleId': bundle_id, 'ok': True}, authorized=True)
        return out
    except PermissionError as e:
        log_action(agent_id, 'app_script_denied', {'bundleId': bundle_id, 'reason': str(e)}, authorized=True)
        return f'Denied: {e}'
    except Exception as e:
        log_action(agent_id, 'app_script_failed', {'bundleId': bundle_id, 'reason': str(e)}, authorized=True)
        return f'app_script failed: {e}'


SECURITY_TEST_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'attempt_curl',
            'description': 'Make a REAL outbound HTTP call through this think tank\'s gated /api/curl '
                           'endpoint -- the same one any agent uses, with the same room/Jev gates. '
                           'Optionally attach a capabilityHandle you were given to authorize a '
                           'scoped external credential. Returns the real response (or refusal).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'Full https:// URL to call.'},
                    'method': {'type': 'string', 'description': 'HTTP method, e.g. GET.'},
                    'purpose': {'type': 'string', 'description': 'Why you are making this call.'},
                    'capabilityHandle': {'type': 'string',
                                         'description': 'A capability handle you were given, if any. '
                                                        'Omit to test making the call with none.'},
                },
                'required': ['url', 'purpose'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'request_capability_handle',
            'description': 'Attempt to self-grant a capability handle for a named credential via '
                           'the real /api/keys/handles endpoint, AS YOURSELF (not by asking a '
                           'director). This endpoint is player-only; use this to verify that gate '
                           'actually refuses an agent, and report the real result.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'credentialName': {'type': 'string', 'description': 'e.g. "digitalocean".'},
                    'purpose': {'type': 'string'},
                    'allowedHosts': {'type': 'string', 'description': 'e.g. "api.digitalocean.com" or "*".'},
                },
                'required': ['credentialName', 'purpose'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'adversarial_village',
            'description': 'Admin-only (Theo). Toggle the adversarial (winter) village on or off. '
                           'Enabling spawns a winter director and small team in a second, winter-'
                           'themed map and files the SAME task to BOTH the main village director and '
                           'the winter director so the two sides work the same goal adversarially. '
                           'Action "enable" requires a "task" (the shared goal); action "disable" '
                           'shuts it down: the winter team returns to the main village and the winter '
                           'map becomes non-walkable. The winter map is only reachable by the player '
                           'while enabled.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'enum': ['enable', 'disable'], 'description': '"enable" to launch the adversarial village (with a task), "disable" to shut it down.'},
                    'task': {'type': 'string', 'description': 'Required when enabling: the shared goal both villages will work adversarially.'},
                },
                'required': ['action'],
            },
        },
    },
]

_TAVILY_SEARCH_URL = 'https://api.tavily.com/search'


def _tavily_search_sync(query, max_results=5):
    """Real web search via Tavily (an API built for AI-agent search -- not
    scraping a search engine's own results page, which every major engine
    CAPTCHA-blocks for automated traffic; confirmed against both Google
    and DuckDuckGo before adding this). Returns a plain-text summary (Tavily's
    own synthesized answer, when it has one, plus each result's title/url/
    snippet so the caller has real URLs to follow with browse_page), or a
    __TOOL_ERROR__-style note on failure. EXTERNAL DATA -- wrapped by the
    caller before it ever reaches a model, same as every other tool here.

    Each call is ONE external page-request against the monthly budget
    -- Search_web is a real network round trip, so it counts the
    same as a browse_page fetch."""
    if not TAVILY_API_KEY:
        return '__TOOL_ERROR__: search is not configured (no TAVILY_API_KEY).'
    if _api_key_is_broken('tavily'):
        return '__TOOL_ERROR__: Tavily API key is blocked after repeated auth failures -- swap the key in .env and reload env.'
    if _page_budget_exhausted():
        return '__TOOL_ERROR__: the think tank has used its monthly page-request budget (browsing/search is paused until next month).'
    payload = json.dumps({
        'api_key': TAVILY_API_KEY,
        'query': query,
        'search_depth': 'basic',
        'max_results': max(1, min(int(max_results or 5), 10)),
        'include_answer': True,
    }).encode()
    req = urllib.request.Request(_TAVILY_SEARCH_URL, data=payload, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 -- fixed allow-listed search API host
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _note_api_key_failure('tavily', e.code)
        return f'__TOOL_ERROR__: search failed: {e}'
    except Exception as e:
        return f'__TOOL_ERROR__: search failed: {e}'
    _note_api_key_success('tavily')
    _accrue_page_request()
    parts = []
    if data.get('answer'):
        parts.append(f"Quick answer: {data['answer']}")
    for r in (data.get('results') or [])[:max_results]:
        title = r.get('title') or '(untitled)'
        url = r.get('url') or ''
        snippet = (r.get('content') or '')[:300]
        parts.append(f"- {title} ({url}): {snippet}")
    if not parts:
        return f'No search results found for "{query}".'
    return '\n'.join(parts)


def _normalize_decision_questions(questions):
    """The decisions API expects `choice.criteria` as a record (id -> text),
    not the list-of-{id,description} shape most call sites use. Sending the
    list made every gate a HTTP 400 and silently opened the model circuit,
    which looked like a reviewer refusal. Normalize once at the wire edge."""
    if not isinstance(questions, dict):
        return questions
    choice = questions.get('choice')
    if not isinstance(choice, dict):
        return questions
    criteria = choice.get('criteria')
    if isinstance(criteria, list):
        record = {}
        for item in criteria:
            if isinstance(item, dict):
                key = str(item.get('id') or '').strip()
                if key:
                    record[key] = str(item.get('description') or '')
        out = dict(questions)
        out['choice'] = {**choice, 'criteria': record}
        return out
    return questions


def _decision_request(model, state, questions):
    # A '://' chain entry is a Jev-compatible provider reached directly (the
    # Colab/Laya standby), not an OpenRouter slug: literally the URL, the same
    # {state, questions} wire format. No 'model' field -- laya routes
    # internally on the Colab runtime. The standby is LOCALHOST-only (loopback
    # ssh forward), so there is no bearer key to attach and nothing public.
    questions = _normalize_decision_questions(questions)
    if '://' in model:
        src = json.dumps({'state': state, 'questions': questions}).encode()
        return urllib.request.Request(
            model, data=src, headers={'Content-Type': 'application/json'},
            method='POST')
    payload = json.dumps({'model': model, 'state': state, 'questions': questions}).encode()
    return urllib.request.Request(
        'https://openrouter.ai/api/alpha/decisions',
        data=payload,
        headers={
            'Authorization': f'Bearer {OPENROUTER_API_KEY}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )


def _finalize_decision(data, prompt, criteria, trace_id, model, village_id=None):
    choice, confidence, cost = _jev_choice(data)
    # Gap: every OTHER real model call accrues
    # into the spend ledger at its own chokepoint, but Jev's cost was only
    # ever LOGGED per-call (log_action), never summed anywhere -- meaning a
    # spend cap reading the ledger alone would undercount real spend by every
    # Jev decision ever made. One dedicated bucket for the whole decisions
    # model class, whichever slug in the failover chain answered.
    _accrue_spend('__jev__', cost, village_id=village_id)
    _append_decision_tape(
        _decision_kind(prompt), model, prompt, criteria,
        choice, confidence, cost, data, True,
        trace_id=trace_id,
    )
    data['trace_id'] = trace_id
    return data


# Measurement-only instrumentation: does the same Jev decision recur often
# enough to be worth caching? We hash each request two ways -- question-only
# ('q:' = model + questions) and full-request ('r:' = model + state +
# questions) -- and count occurrences over a rolling window. A high repeat
# rate means a cache would pay; near-zero means it would not. This never
# changes a decision and is safe to remove.
DECISION_KEY_WINDOW_S = float(_load_env().get('DECISION_KEY_WINDOW_S', str(7 * 24 * 3600)) or (7 * 24 * 3600))
# How long a cached Jev decision stays valid. 0 disables the cache entirely.
DECISION_CACHE_TTL_S = float(_load_env().get('DECISION_CACHE_TTL_S', '3600') or 0)


def _decision_key_hash(prefix, payload):
    blob = json.dumps(payload, sort_keys=True, default=str)
    return prefix + hashlib.sha256(blob.encode('utf-8', errors='replace')).hexdigest()[:32]


def _decision_cache_key(model, state, questions, chain=None):
    # The configured model chain is part of the request identity: an operator
    # switching the Jev chain (via /api/jev/model) must not be served answers
    # produced under the old chain.
    return _decision_key_hash('c:', {'model': model, 'chain': chain,
                                     'state': state, 'questions': questions})


def _decision_cache_get(model, state, questions, chain=None):
    """Return the cached response for an identical request, or None. Never
    raises; a cache fault is just a miss (we call the model as before). An
    expired row is deleted on access."""
    if DECISION_CACHE_TTL_S <= 0:
        return None
    key = _decision_cache_key(model, state, questions, chain)
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT response, created_at FROM decision_cache WHERE key = ?',
                (key,)).fetchone()
            if not row:
                return None
            response, created_at = row
            if time.time() - created_at > DECISION_CACHE_TTL_S:
                conn.execute('DELETE FROM decision_cache WHERE key = ?', (key,))
                return None
            conn.execute('UPDATE decision_cache SET hits = hits + 1 WHERE key = ?', (key,))
        return json.loads(response)
    except Exception:
        return None


def _decision_cache_put(model, state, questions, data, chain=None):
    """Store the raw API response for this request, replacing any expired row
    that a concurrent reader has not pruned yet. Best-effort; never raises."""
    if DECISION_CACHE_TTL_S <= 0:
        return
    key = _decision_cache_key(model, state, questions, chain)
    try:
        with _db() as conn:
            conn.execute(
                'INSERT INTO decision_cache (key, response, created_at, hits) VALUES (?, ?, ?, 0) '
                'ON CONFLICT(key) DO UPDATE SET response = excluded.response, created_at = excluded.created_at',
                (key, json.dumps(data), time.time()))
    except Exception:
        pass


def _finalize_cached_decision(data, prompt, criteria, trace_id, model, village_id=None):
    """Serve a cached Jev decision: tape it (marked cached) so the audit trail
    stays complete, but accrue no spend and hit no network. A fresh trace_id
    keeps this decision event distinct in action_log like any other."""
    choice, confidence, _cost = _jev_choice(data)
    _append_decision_tape(
        _decision_kind(prompt), model, prompt, criteria,
        choice, confidence, 0.0, data, True,
        trace_id=trace_id,
    )
    out = dict(data)
    out['trace_id'] = trace_id
    out['cached'] = True
    return out


def decision_cache_report():
    """Hit-rate report for the decision cache over the current window. Requests
    are counted by the measurement table (which sees every call, hit or miss);
    hits by the cache table's per-key counters."""
    try:
        with _db() as conn:
            rows = conn.execute(
                'SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM decision_cache').fetchone()
            requests = conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM decision_key_stats WHERE key LIKE 'r:%'"
            ).fetchone()[0]
        distinct, hits = int(rows[0]), int(rows[1])
        total = int(requests)
        return {
            'ttl_s': DECISION_CACHE_TTL_S,
            'cached_keys': distinct,
            'hits': hits,
            'requests': total,
            'hit_pct': round(100.0 * hits / total, 1) if total else 0.0,
        }
    except Exception:
        return {'ttl_s': DECISION_CACHE_TTL_S, 'cached_keys': 0,
                'hits': 0, 'requests': 0, 'hit_pct': 0.0}


def _record_decision_keys(model, state, questions):
    """Count this decision request by key (best-effort; never raises)."""
    try:
        qkey = _decision_key_hash('q:', {'model': model, 'questions': questions})
        rkey = _decision_key_hash('r:', {'model': model, 'state': state, 'questions': questions})
        now = time.time()
        with _db() as conn:
            for key in (qkey, rkey):
                conn.execute(
                    'INSERT INTO decision_key_stats (key, count, first_ts, last_ts) VALUES (?, 1, ?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET count = count + 1, last_ts = excluded.last_ts',
                    (key, now, now))
    except Exception:
        pass


def _prune_decision_key_stats():
    try:
        with _db() as conn:
            conn.execute('DELETE FROM decision_key_stats WHERE last_ts < ?',
                         (time.time() - DECISION_KEY_WINDOW_S,))
            if DECISION_CACHE_TTL_S > 0:
                conn.execute('DELETE FROM decision_cache WHERE created_at < ?',
                             (time.time() - DECISION_CACHE_TTL_S,))
    except Exception:
        pass


def decision_key_report(prefix):
    """Repeat-rate summary for one key family ('q' or 'r') over the window."""
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT COUNT(*), COALESCE(SUM(count), 0) FROM decision_key_stats WHERE key LIKE ?',
                (prefix + ':%',)).fetchone()
            top = conn.execute(
                'SELECT key, count, last_ts FROM decision_key_stats WHERE key LIKE ? '
                'ORDER BY count DESC LIMIT 10', (prefix + ':%',)).fetchall()
        distinct, total = int(row[0]), int(row[1])
        repeats = total - distinct
        return {
            'calls': total,
            'distinct_keys': distinct,
            'repeat_calls': repeats,
            'repeat_pct': round(100.0 * repeats / total, 1) if total else 0.0,
            'top': [{'key': k, 'count': c, 'last_ts': t} for k, c, t in top],
        }
    except Exception:
        return {'calls': 0, 'distinct_keys': 0, 'repeat_calls': 0, 'repeat_pct': 0.0, 'top': []}


def _call_openrouter_decision_sync(model, state, questions, village_id=None):
    # Jev (TypeSafe's System One decision model) via OpenRouter -- a
    # genuinely different endpoint from chat completions, confirmed only
    # after two wrong assumptions first (see memory/DESIGN.md): the
    # /v1/models catalog doesn't list "decisions"-type models at all, and
    # calling this model on /chat/completions fails outright with a
    # pointer to this endpoint instead.
    #
    # Multi-model failover + per-slug circuit breaker: the old
    # comment said "deliberately NOT the circuit breaker -- there's only one
    # Jev slug in this whole project, so blocking it for a cooldown after 3
    # failures would disable every Jev-dependent feature at once". That was a
    # symptom of the SINGLE-SLUG configuration, not a need: the breaker is
    # only dangerous when breaking the only slug == breaking all decisions.
    # With a configured chain (_decision_model_chain, comma-separated), a dead
    # slug is skipped for its cooldown and decisions FAIL OVER to the next
    # candidate instead of degrading to deterministic fallbacks. When exactly
    # one slug is configured the breaker is deliberately NOT armed and the
    # legacy behavior is byte-for-byte: resilient retry, tape-on-failure,
    # re-raise -- no blast-radius widening.
    #
    # Every Jev call lands here, so the decision tape is written here too -- the
    # caller's behavior is unchanged, but the model-facing side of the decision
    # (prompt, candidates, parsed choice/confidence/cost, full response) is
    # recorded before the caller throws the details away. `_jev_choice` is pure
    # (parses the same data the caller parses), so calling it for the tape adds
    # no behavior beyond the row itself. On a healthy failover the tape records
    # ONE row (the slug that answered); a total failure records one ok=0 row
    # listing every slug actually attempted -- so the Jev-health failure RATE
    # counts only decisions that genuinely lost (a hiccup recovered by the
    # fallback is NOT a degraded decision).
    #
    # A single trace_id is generated per call and stamped on both the tape entry
    # and (if the caller extracts it from the returned data) the resulting tool
    # action(s) in action_log, so the decision chain is queryably self-consistent.
    trace_id = secrets.token_hex(8)
    _record_decision_keys(model, state, questions)
    prompt = (questions or {}).get('choice', {}).get('instructions') if isinstance(questions, dict) else None
    criteria = (questions or {}).get('choice', {}).get('criteria') if isinstance(questions, dict) else None
    # decision_tape.prompt/.criteria are NOT NULL; calls without instructions
    # (health probes, pure escalation checks) would fail the row write silently.
    prompt = prompt if prompt is not None else ''
    criteria = criteria if criteria is not None else ''
    chain = _decision_model_chain()
    if model not in chain:
        # An explicit model (tests, forwarders) leads the chain, fallbacks follow.
        chain = [model] + [m for m in chain if m != model]
    # An identical request answered before is served from the cache: no network,
    # no spend (so it also works past the spend cap), still taped as a cached
    # decision. The measured exact-repeat rate is what justified this. The
    # configured chain is part of the key so a chain switch invalidates it.
    cache_chain = chain
    cached = _decision_cache_get(model, state, questions, cache_chain)
    if cached is not None:
        return _finalize_cached_decision(cached, prompt, criteria, trace_id, model, village_id=village_id)
    if _think_tank_spend_cap_exceeded():
        raise RuntimeError(f'Think Tank spend cap (${SPEND_CAP_USD}) reached -- no further model calls until it is raised in .env')
    armed = len(chain) > 1  # the breaker only arms once a real fallback exists
    if armed:
        # Skip slugs whose breaker is OPEN (cooldown not spent); a HALF_OPEN
        # slug is let through as its own recovery probe by the breaker itself.
        probe_chain = [c for c in chain if not is_model_circuit_broken(c)]
        if not probe_chain:
            # Everything cold-open right now: probing happens on the NEXT call
            # as cooldowns elapse, so mean 'no candidate available', fail closed.
            _append_decision_tape(
                _decision_kind(prompt), ','.join(chain), prompt, criteria,
                None, None, None, {'error': 'all decision models circuit-broken', 'tried': chain}, False,
                trace_id=trace_id,
            )
            raise RuntimeError('all configured decision models are circuit-broken')
        chain = probe_chain
    attempted = []
    last_error = None
    for slug in chain:
        try:
            data = json.loads(_urlopen_with_resilience(_decision_request(slug, state, questions), timeout=30))
        except Exception as e:
            attempted.append(slug)
            last_error = e
            if armed:
                record_model_result(slug, False)
            continue
        if armed:
            record_model_result(slug, True)
        _decision_cache_put(model, state, questions, data, cache_chain)
        return _finalize_decision(data, prompt, criteria, trace_id, slug, village_id=village_id)
    # All candidates failed (or were already open). Tape the failure -- an ok=0
    # row marks the decision as having been attempted and lost, so a gap in the
    # tape is distinguishable from a decision that never happened. Then re-raise;
    # the caller owns the recovery (deterministic fallback), exactly as before.
    # Tape the failure WITH the concrete exception detail -- the Sep 2026
    # outage showed that a bare 'decision call raised' is useless for
    # triage (429 rate-limit vs 5xx outage vs timeout look identical on
    # the tape). The ok=0 row is the incident record, so it should carry
    # the kind/status/one-line detail that makes it diagnosable.
    exc = 'none' if last_error is None else f'{type(last_error).__name__}: {str(last_error)[:160]}'
    _append_decision_tape(
        _decision_kind(prompt), ','.join(attempted or chain), prompt, criteria,
        None, None, None, {
            'error': 'decision call raised', 'tried': attempted,
            'kind': type(last_error).__name__ if last_error is not None else None,
            'status': getattr(last_error, 'code', None), 'exc': exc,
        }, False,
        trace_id=trace_id,
    )
    if last_error is not None:
        raise last_error
    raise RuntimeError('all decision model calls failed')  # pragma: no cover -- chain is never empty, so last_error is always set here


# Jev returns typed decisions with calibrated confidence (see DESIGN.md and
# the Jev research). Every call site used to read only `choice`, throwing
# away the confidence (Jev's reason to exist: "act when confident, escalate
# when unsure"). This helper extracts all of them with safe defaults --
# missing confidence defaults to 1.0 (behave as before, don't suddenly
# block a field that was always present), so adding confidence handling
# never changes behavior by itself; callers opt into thresholding.
# _jev_choice moved to jev.py (Phase 0c self-modification split); it is
# re-exported below with the rest of the JEV engine's public surface.
# The single decisions-type model uses (see
# _call_openrouter_decision_sync for why there's exactly one). The env var /
# default here is the FALLBACK; the runtime value resolves through _jev_model(),
# which checks the `settings` DB table first (switchable live via the
# /api/jev/model chief of staff endpoint, no restart, no daily auto-refresh) and only
# then falls back to this constant. Centralized so swapping the slug is one
# change instead of touching ~15 call sites across serve.py, sim.py, content.py
# and the browser client. serve.py is the authority: /api/decide ignores
# whatever model a client sends and always uses the resolved value, so a
# switch takes effect everywhere, including jev.js.
JEV_MODEL = _load_env().get('JEV_MODEL', 'typesafe/jev-1.13')


def _get_setting(key, default=None):
    try:
        with _db() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def _set_setting(key, value, conn=None):
    if conn is None:
        with _db() as conn:
            return _set_setting(key, value, conn)
    conn.execute(
        'INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
        (key, value, time.time()),
    )


def _decision_model_chain():
    """Ordered Jev candidate slugs, primary first -- the failover chain
    Resolution order: a comma-separated `settings` row
    ('jev_model', the operator switch via /api/jev/model) > env `JEV_MODELS`
    (comma list) > the JEV_MODEL default. A single slug means no failover; a
    multi-slug value makes _call_openrouter_decision_sync fall through to the
    next candidate when the active one fails or its circuit breaker is open.
    Never auto-updated -- adding a decision model is deliberate, exactly like
    the model switch itself. The per-band model_tier_fallbacks table does NOT
    apply here: JEV is a decision engine with a choice protocol, and the
    catalog holds no comparable decision model at its price point (2026-10-18);
    the generic OpenRouter routers at the same sentinel price are not
    substitutes."""
    chain = None
    saved = (_get_setting('jev_model') or '').strip()
    if saved:
        cand = [s.strip() for s in saved.split(',') if s.strip()]
        if cand:
            chain = cand
    if chain is None:
        env_models = (_load_env().get('JEV_MODELS', '') or '').strip()
        if env_models:
            cand = [s.strip() for s in env_models.split(',') if s.strip()]
            if cand:
                chain = cand
    if chain is None:
        chain = [JEV_MODEL]
    # The CLI-managed Colab/Laya standby trails the chain as the LAST fallback:
    # when the colab CLI is available (and not operator-disabled), the loopback
    # Laya provider is advertised so the failover can reach it during an outage
    # -- nothing is registered, keyed, or paired; like every OpenRouter
    # candidate it is just a chain entry that _decision_request routes by '://'.
    if _colab_standby_enabled():
        provider = COLAB_STANDBY_URL.rstrip('/') + COLAB_STANDBY_DECISION_PATH
        if provider not in chain:
            chain.append(provider)
    return chain


def _jev_model():
    """The PRIMARY decisions model this think tank actually runs on, resolved
    fresh at call time like before: DB `settings` row (which may now be a
    comma-separated FAILOVER chain) > env/default constant. Returns the head
    of _decision_model_chain() so every existing call site keeps using the
    primary slug unchanged while the chain handles failover internally."""
    return _decision_model_chain()[0]


def _jev_openrouter_slug():
    """The OpenRouter slug the JEV experiment-grant proxy serves: the first
    chain candidate that is a real model slug, skipping any URL fallback (the
    Colab/Laya standby is a provider endpoint, not something the plain
    chat-completions proxy can name as a model). None when the chain holds no
    real slug -- mint and inference fail closed on that, exactly like the
    sublow lane failing closed with no tier pick."""
    for cand in _decision_model_chain():
        if '://' not in cand:
            return cand
    return None


# ---- Colab/Laya standby ---------------------------------
# The decisions-model backing layer can include a REMOTE Jev-compatible
# server (Laya) running on the operator's Google Colab runtime. The original
# sentinel design used a notebook with a public cloudflared tunnel paired via
# /api/colab/register; the current standby is instead owned ENTIRELY by the
# think tank through the colab CLI on this machine -- no notebook, no tunnel, no
# pairing step, nothing ever exposed beyond loopback. When Jev degrades,
# _colab_failover_loop provisions a dedicated CPU session ('think-tank-standby')
# on demand, boots laya-serve in it over `colab exec`, and keeps a
# LOCALHOST-ONLY ssh forward (`127.0.0.1:8939 -> session:8000`) alive through
# the same CLI's --proxy-mode bridge. The chain appends
# `http://127.0.0.1:8939/v1/systemone` as the last failover candidate; the
# laptop hosts only routing, and the only code ever run in the runtime is the
# guardrailed laya boot (compute-only, same boundary as run_on_colab).
COLAB_FAILOVER_INTERVAL_S = 15 * 60   # 15 min, matching the health cadence
COLAB_STANDBY_HEALTHY_CYCLES = 2      # consecutive healthy cycles -> teardown
COLAB_STANDBY_DECISION_PATH = '/v1/systemone'
COLAB_STANDBY_SESSION = 'think-tank-standby'
COLAB_STANDBY_PORT = 8939             # loopback; the dev server uses 8936
COLAB_STANDBY_URL = f'http://127.0.0.1:{COLAB_STANDBY_PORT}'
COLAB_STANDBY_SSH_KEY = os.path.expanduser('~/.ssh/id_ed25519_colab')
COLAB_STANDBY_ENABLED = str(_load_env().get('COLAB_STANDBY_ENABLED', '') or '').lower() in ('1', 'true', 'yes')
_COLAB_STANDBY_FORWARD = {'pid': None, 'proc': None, 'session': None}


def _colab_standby_enabled():
    """The CLI-managed Laya standby is only ever on the board when the colab
    CLI is present AND the operator hasn't flipped COLAB_ENABLED=0 (the same
    switch that turns agent compute on/off) AND has explicitly opted into the
    standby itself (COLAB_STANDBY_ENABLED=1). Deliberate triple opt-in: this
    appends a real provider to the decision chain and spawns an ssh process,
    so it must never turn on accidentally -- keeping hermetic test suites (and
    machines without Colab) single-chain by default."""
    return bool(COLAB_ENABLED and COLAB_CLI_AVAILABLE and COLAB_STANDBY_ENABLED)


def _colab_standby_ensure_session(session=None):
    """Make sure the dedicated CPU standby session exists, creating it via
    `colab new` if not. CPU on purpose -- it is the LAST-RESORT fallback that
    should still boot when GPU quota is gone or unavailable, and it doesn't
    burn Colab compute units sitting idle. Returns (ok, msg)."""
    session = session or COLAB_STANDBY_SESSION
    if _colab_session_exists(session):
        return True, 'ok'
    rc, out = _colab_cli('new', '-s', session, timeout=300)
    if rc != 0:
        return False, f'provision failed: {out[-500:]}'
    return True, 'ok'


_COLAB_STANDBY_BOOT_CODE = (
    "import os, shutil, subprocess, time, urllib.request\n"
    "def _ready():\n"
    "    try:\n"
    "        with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4) as r:\n"
    "            return r.status == 200\n"
    "    except Exception:\n"
    "        return False\n"
    "if not _ready() and shutil.which('laya-serve') is None:\n"
    "    subprocess.run(['pip', 'install', '-q', 'laya[serve]'], timeout=540)\n"
    "if not _ready() and shutil.which('laya-serve') is not None:\n"
    "    log = open('/content/laya-standby.log', 'ab')\n"
    "    env = dict(os.environ); env['LAYA_DEVICE'] = 'cpu'; env['LAYA_PRELOAD'] = '1'\n"
    "    subprocess.Popen(['nohup', 'laya-serve'], env=env, stdout=log, stderr=subprocess.STDOUT)\n"
    "    for _ in range(30):\n"
    "        time.sleep(2)\n"
    "        if _ready():\n"
    "            break\n"
    "print('__STANDBY_UP__' if _ready() else '__STANDBY_STARTING__')\n"
)


def _colab_standby_ensure_service(session=None):
    """Idempotent laya boot over `colab exec`: installs laya on first use,
    preloads the model on the CPU runtime, and leaves laya-serve running
    detached (verified: a backgrounded child survives the exec call that
    spawned it). Returns the exec tail for the loop's log line."""
    session = session or COLAB_STANDBY_SESSION
    rc, out = _colab_cli('exec', '-s', session, '--timeout', '600',
                         input=_COLAB_STANDBY_BOOT_CODE)
    return out[-300:] or 'no output'


def _colab_standby_teardown_service(session=None):
    """Stop laya-serve on the standby session once Jev has recovered -- the
    expensive model process is the only thing worth cycling; the session and
    the forward stay warm for the next outage."""
    session = session or COLAB_STANDBY_SESSION
    _colab_cli('exec', '-s', session, '--timeout', '60',
               input='import subprocess\n'
                     'subprocess.run(["pkill", "-f", "laya-serve"], capture_output=True)\n'
                     'print("__STANDBY_TORN_DOWN__")\n')


def _colab_standby_ensure_forward(session=None):
    """Own the persistent loopback ssh forward to the standby session:
    spawn `ssh -N -L 127.0.0.1:8939:localhost:8000` through the colab CLI's
    --proxy-mode bridge when we don't already hold a live one for THIS
    session (a VM-recycled / reaped forward is detected and respawned). The
    forward binds localhost only, which is exactly why the standby needs no
    tunnel, no pairing, and no key material in the DB. Returns the
    subprocess, or None on a spawn failure (the loop logs and retries)."""
    session = session or COLAB_STANDBY_SESSION
    state = _COLAB_STANDBY_FORWARD
    if state.get('session') == session and state.get('pid') is not None:
        try:
            os.kill(state['pid'], 0)
            return state['proc']
        except OSError:
            pass  # forward died; respawn it below
    _colab_standby_stop_forward()
    identity = os.path.expanduser(COLAB_STANDBY_SSH_KEY)
    proxy = f'{COLAB_CLI_PATH} ssh --proxy-mode -s {session} -i {identity}'
    log = open('/tmp/colab-standby-forward.log', 'ab')  # nosec B108 -- fixed operational log path
    try:
        proc = subprocess.Popen(
            ['ssh', '-N', '-o', 'User=root',
             '-o', f'ProxyCommand={proxy}',
             '-o', 'ExitOnForwardFailure=yes',
             '-o', 'ServerAliveInterval=30',
             '-o', 'ServerAliveCountMax=2',
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=/dev/null',
             '-o', 'IdentitiesOnly=yes',
             '-i', identity,
             '-L', f'127.0.0.1:{COLAB_STANDBY_PORT}:localhost:8000',
             'root@laya-standby'],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        print(f'[colab-standby] forward spawn failed: {e}', flush=True)
        return None
    state.update({'pid': proc.pid, 'proc': proc, 'session': session})
    return proc


def _colab_standby_stop_forward():
    state = _COLAB_STANDBY_FORWARD
    if state.get('proc') is not None:
        try:
            state['proc'].kill()
        except Exception:
            pass
    state.update({'pid': None, 'proc': None, 'session': None})
