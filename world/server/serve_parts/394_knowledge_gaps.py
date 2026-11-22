# Knowledge gap flags (operator, 2026-11-22): agents flag missing, stale,
# or WRONG knowledge while working -- the article's "flag what's wrong as
# you work" piece of the knowledge factory. The flag store is file-backed
# JSON in the Library (jev_lessons pattern: bounded, locked, best-effort).
# The curator pool 395 reads this store on a cadence, resolves verdict-
# carrying flags directly into Jev lessons (deterministic-first doctrine)
# and triages the rest with ONE free-pool LLM call per flag, filing wiki
# proposals through the existing operator-approved surface. Flags never
# page the operator on arrival; only aged-out (KNOWLEDGE_GAPS_ALERT_HOURS,
# default 48) or repeated flags can still park unresolved in the tank's
# escalation queue and page. Everything best-effort: a flags failure never
# breaks the worker's card.
import json
import os
import threading
import time

_KG_MAX_ENTRIES = 500
_KG_DIR_OVERRIDE = os.environ.get('KNOWLEDGE_GAPS_DIR')
_KG_ALERT_HOURS = float(os.environ.get('KNOWLEDGE_GAPS_ALERT_HOURS') or 48)

_KG_LOCK = threading.Lock()
_KG_SEQ = 0


def _kg_dir():
    return _KG_DIR_OVERRIDE or _safe_library_path('analysis')


def _kg_read():
    try:
        path = os.path.join(_kg_dir(), 'knowledge_gaps.json')
        if not os.path.isfile(path):
            return {'flags': [], 'cursor': ''}
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {'flags': [], 'cursor': ''}
        data.setdefault('flags', [])
        data.setdefault('cursor', '')
        return data
    except Exception:
        return {'flags': [], 'cursor': ''}


def _kg_write(store):
    try:
        base = _kg_dir()
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, 'knowledge_gaps.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(store, fh, indent=1, sort_keys=True)
            fh.write('\n')
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _kg_record(agent_id, kind, about, note, trace_hint=None, verdict=None,
               verdict_action=None, verdict_target=None):
    """One flag. kind: 'missing'|'wrong'|'data_access'|'insight'. verdict
    (approve/deny/unsure) makes the flag curator-ready WITHOUT an LLM call
    (deterministic-first doctrine). Returns the flag id or None. Duplicates
    (same agent+kind+about, open) just bump 'seen'. Best-effort."""
    global _KG_SEQ
    try:
        with _KG_LOCK:
            store = _kg_read()
            for flag in store['flags']:
                if flag.get('status') == 'open' \
                        and flag.get('agent') == agent_id \
                        and flag.get('kind') == kind \
                        and (flag.get('about') or '') == (about or ''):
                    flag['seen'] = int(flag.get('seen') or 1) + 1
                    flag['lastSeen'] = time.time()
                    if verdict and not flag.get('verdict'):
                        flag['verdict'] = verdict
                        flag['verdictAction'] = verdict_action
                        flag['verdictTarget'] = verdict_target
                    _kg_write(store)
                    return flag.get('id')
            _KG_SEQ += 1
            flag = {
                'id': f'kg-{int(time.time())}-{_KG_SEQ}',
                'agent': agent_id,
                'kind': kind,
                'about': (about or '')[:240],
                'note': (note or '')[:1000],
                'traceHint': trace_hint,
                'verdict': verdict,
                'verdictAction': verdict_action,
                'verdictTarget': verdict_target,
                'status': 'open',
                'seen': 1,
                'created': time.time(),
                'lastSeen': time.time(),
                'resolved': None,
                'resolvedBy': None,
            }
            store['flags'].append(flag)
            if len(store['flags']) > _KG_MAX_ENTRIES:
                store['flags'] = store['flags'][-_KG_MAX_ENTRIES:]
            _kg_write(store)
            return flag['id']
    except Exception:
        return None


def _kg_list(status=None, kind=None, min_seen=None, older_than_hours=None):
    try:
        with _KG_LOCK:
            store = _kg_read()
            out = []
            for flag in store['flags']:
                if status and flag.get('status') != status:
                    continue
                if kind and flag.get('kind') != kind:
                    continue
                if min_seen and int(flag.get('seen') or 1) < min_seen:
                    continue
                if older_than_hours and \
                        (time.time() - float(flag.get('created') or 0)) < older_than_hours * 3600:
                    continue
                out.append(flag)
            return out
    except Exception:
        return []


def _kg_resolve(flag_id, by='curator', note=None):
    try:
        with _KG_LOCK:
            store = _kg_read()
            for flag in store['flags']:
                if flag.get('id') == flag_id and flag.get('status') == 'open':
                    flag['status'] = 'resolved'
                    flag['resolved'] = time.time()
                    flag['resolvedBy'] = by
                    if note:
                        flag['resolvedNote'] = str(note)[:300]
                    _kg_write(store)
                    return True
            return False
    except Exception:
        return False


def flag_knowledge_gap(agent_id, kind, about, note=None, trace_hint=None,
                       verdict=None, verdict_action=None, verdict_target=None):
    """Agent-facing entry (tool executor). Returns {'ok': True, 'id': ...}
    or {'ok': False, 'error': ...}. Kinds: missing / wrong / data_access /
    insight."""
    kinds = ('missing', 'wrong', 'data_access', 'insight')
    kind = (kind or '').strip().lower()
    if kind not in kinds:
        return {'ok': False, 'error': f'kind must be one of: {", ".join(kinds)}'}
    about = (about or '').strip()
    if not about:
        return {'ok': False, 'error': 'about is required'}
    flag_id = _kg_record(agent_id, kind, about, note, trace_hint, verdict,
                         verdict_action, verdict_target)
    if flag_id:
        log_action(agent_id, 'knowledge_gap_flagged',
                   {'flagId': flag_id, 'kind': kind, 'about': about[:120]},
                   authorized=True)
        return {'ok': True, 'id': flag_id}
    return {'ok': False, 'error': 'flag store unavailable'}


@app.get('/api/knowledge-gaps')
async def knowledge_gaps_list(request: Request):
    """Open/resolved knowledge-gap flags. Player-gated. Query: status,
    kind, minSeen, olderThanHours (hours)."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    import urllib.parse as _up
    params = dict(_up.parse_qsl(str(request.url.query)))
    status = params.get('status') or None
    kind = params.get('kind') or None
    try:
        min_seen = int(params.get('minSeen') or 0) or None
        older = float(params.get('olderThanHours') or 0) or None
    except Exception:
        min_seen = older = None
    flags = _kg_list(status=status, kind=kind, min_seen=min_seen,
                     older_than_hours=older)
    return JSONResponse({'flags': flags, 'open': sum(
        1 for f in flags if f.get('status') == 'open')})


@app.post('/api/knowledge-gaps/{flag_id}/resolve')
async def knowledge_gaps_resolve(flag_id: str, request: Request):
    """Player-resolve one flag. Body: {note?}. Player-gated."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    ok = _kg_resolve(flag_id, by='player', note=(body.get('note') if isinstance(body, dict) else None))
    if not ok:
        return JSONResponse({'error': 'unknown or already-resolved flag'},
                            status_code=404)
    log_action('player', 'knowledge_gap_resolved',
               {'flagId': flag_id}, authorized=True)
    return JSONResponse({'ok': True, 'flagId': flag_id})


def _kg_alert_pass():
    """The only paging path: an aged-out or repeatedly-seen OPEN flag can
    still park unresolved -- page the operator once per flag via the
    escalation queue (24h idempotency flag, notified-escalation pattern).
    Aged-out/repeated flags that resolve in a later curator pass clear
    naturally by dropping the escalation (the queue is prefix-like; a
    resolved flag stops being re-escalated)."""
    try:
        aged = _kg_list(status='open', min_seen=2) + _kg_list(
            status='open', older_than_hours=_KG_ALERT_HOURS)
        seen_ids = set()
        for flag in aged:
            if flag['id'] in seen_ids:
                continue
            seen_ids.add(flag['id'])
            log_action('system', 'knowledge_gap_alert',
                       {'flagId': flag['id'], 'kind': flag['kind'],
                        'about': flag['about'][:120]}, authorized=True)
    except Exception:
        pass
