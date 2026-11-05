"""Trusted-codebase fetch with the owner's size-approval gate (2026-11-05).

Owner directive: downloading a trusted codebase artifact below
TRUSTED_CODEBASE_AUTO_APPROVE_MB (default 100 MB) needs no approval;
anything larger requires the owner's approval with a stated reason, and
the requested download's size is surfaced to the agent and to the owner.

POST /api/codebase/fetch  {source, reason?}   (agent key)

Flow:
  1. The source must already be on the trusted-codebase registry
     (agents add sources there first -- 187_trusted_codebase).
  2. Preflight: a HEAD request resolves the artifact size before any
     body is downloaded. The response always carries sizeBytes (or
     sizeUnknown), the threshold, and the decision class.
  3. At or under the threshold: the artifact streams into the shared
     cache (THINK_TANK_DIR/codebase_cache/<key>/), sha256 recorded,
     audited. The agent gets path + size + digest.
  4. Over the threshold (or unknown size): nothing downloads. The
     request files an owner escalation carrying the SIZE, the source,
     and the agent's reason. When the owner approves, the agent re-issues
     the same fetch within 24h and it proceeds; a denial keeps it shut.

Downloads never execute: an archive lands in the cache; building or
installing is a separate, separately judged step.
"""
import hashlib
import os
import time
import urllib.parse
import urllib.request

_CODEBASE_CACHE_DIR = os.path.join(THINK_TANK_DIR, 'codebase_cache')
_CODEBASE_FETCH_TIMEOUT_S = 600
_CODEBASE_APPROVAL_WINDOW_S = 24 * 3600


def _codebase_cache_key(source):
    key = urllib.parse.quote(str(source or '').strip().lower(), safe='')
    return key[:120] or 'unknown'


def _head_size(url):
    """Content-Length from a HEAD request, or None when the server does not
    answer HEAD truthfully. No body is ever read here."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', 'AIThinkTankAgent/1.0')
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.headers.get('Content-Length')
            return int(raw) if raw and raw.isdigit() else None
    except Exception:
        return None


def _resolve_fetch_url(source):
    """A concrete artifact URL for a registry source. An https source is its
    own URL; an owner/name slug resolves to the GitHub codeload archive of
    the default branch (a stable, deterministic artifact per ref)."""
    src = str(source or '').strip()
    if src.lower().startswith('https://'):
        return src
    if _TRUSTED_SLUG_RE.match(src):
        return f'https://codeload.github.com/{src}/zip/refs/heads/main'
    return None


def _recent_approval(agent_id, source):
    """An owner-approved escalation for this agent+source within the window,
    or None. The escalation record keeps the source in `question`."""
    try:
        with open(ESCALATIONS_PATH) as fh:
            store = json.load(fh)
    except Exception:
        return None
    now = time.time()
    needle = str(source or '').strip().lower()
    for esc in store.values():
        if esc.get('kind') != 'codebase download approval':
            continue
        if esc.get('status') != 'approved':
            continue
        resolved_at = esc.get('resolvedAt') or 0
        if now - resolved_at > _CODEBASE_APPROVAL_WINDOW_S:
            continue
        if needle in str(esc.get('question') or '').lower() \
                and str(esc.get('requestedBy') or '') == str(agent_id or ''):
            return esc
    return None


def _file_oversize_approval(agent_id, source, size_bytes, reason):
    """File the owner escalation for an oversize download. Returns the
    escalation id. The question carries the size so the owner sees it."""
    esc_id = 'esc-' + os.urandom(8).hex()
    mb = round((size_bytes or 0) / (1024 * 1024), 1)
    threshold = trusted_codebase_auto_mb()
    question = (f'Oversize codebase download: {source} is '
                f'{mb} MB (auto-approval threshold {threshold} MB). '
                f'Reason: {(reason or "none given")[:300]}. Approving lets '
                f'{agent_id} fetch it into the shared cache within 24h.')
    store = {}
    try:
        with open(ESCALATIONS_PATH) as fh:
            store = json.load(fh)
    except Exception:
        store = {}
    store[esc_id] = {
        'kind': 'codebase download approval', 'status': 'open',
        'token': os.urandom(16).hex(), 'ts': time.time(),
        'question': question, 'note': f'requestedBy: {agent_id}',
        'requestedBy': agent_id, 'humanNotified': True,
    }
    try:
        with open(ESCALATIONS_PATH, 'w') as fh:
            json.dump(store, fh, indent=1)
    except Exception:
        pass
    _mail_oversize_approval(esc_id, agent_id, source, mb, threshold, reason)
    return esc_id


def _mail_oversize_approval(esc_id, agent_id, source, mb, threshold, reason):
    """One owner mail for the approval. Best-effort: the escalation row is
    the durable record even if the mail transport hiccups."""
    try:
        text = (f'Codebase download needs your approval.\\n\\n'
                f'Source: {source}\\nSize: {mb} MB '
                f'(auto-approval threshold: {threshold} MB)\\n'
                f'Requested by: {agent_id}\\n'
                f'Reason: {(reason or "none given")[:400]}\\n\\n'
                f'Resolve: /api/escalation/resolve?token=… for {esc_id} '
                f'(approve keeps it fetchable for 24h).')
        _notify_codebase_approval(text)
    except Exception:
        pass


def _notify_codebase_approval(text):
    """The owner notification channel: the same human-notification helper
    the other owner-escalating flows use, when one is loaded."""
    for name in ('_notify_human_escalation', 'notify_human_of_escalation'):
        fn = globals().get(name)
        if callable(fn):
            try:
                fn('codebase download approval', text)
            except Exception:
                pass
            return


@app.post('/api/codebase/fetch')
async def api_codebase_fetch(request: Request):
    agent_id = _agent_id_for_key(request.headers.get('X-Agent-Key'))
    if not agent_id or verify_agent_key(
            agent_id, request.headers.get('X-Agent-Key')) is not True:
        return JSONResponse({'error': 'Unauthorized -- please log in'},
                            status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'rate limited'}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    source = str(body.get('source') or '').strip()
    reason = str(body.get('reason') or '').strip()
    if not source:
        return JSONResponse({'error': 'source required'}, status_code=400)
    if not source_is_trusted(source):
        return JSONResponse(
            {'error': 'source is not on the trusted codebase list; add it '
                      'via POST /api/trusted-codebase with a purpose, then '
                      'fetch it'}, status_code=403)
    url = _resolve_fetch_url(source)
    if not url:
        return JSONResponse({'error': 'source has no fetchable artifact URL'},
                            status_code=400)
    size_bytes = _head_size(url)
    threshold_bytes = int(trusted_codebase_auto_mb() * 1024 * 1024)
    needs_approval = size_bytes is None or size_bytes > threshold_bytes
    size_payload = {
        'source': source,
        'sizeBytes': size_bytes,
        'sizeMb': round(size_bytes / (1024 * 1024), 1) if size_bytes else None,
        'autoApproveMb': trusted_codebase_auto_mb(),
        'requiresOwnerApproval': needs_approval,
    }
    log_action(agent_id, 'codebase_fetch_preflight',
               {'source': source, 'sizeBytes': size_bytes,
                'requiresApproval': needs_approval}, authorized=True)
    if needs_approval:
        if _recent_approval(agent_id, source):
            needs_approval = False
            size_payload['ownerApproval'] = 'approved-in-window'
        else:
            esc_id = _file_oversize_approval(agent_id, source, size_bytes,
                                             reason)
            size_payload['escalationId'] = esc_id
            size_payload['note'] = (
                'This download is over the auto-approval threshold (or its '
                'size is unknown). Nothing was downloaded. The owner has '
                'been asked with the size and your reason; re-issue this '
                'fetch after approval, within 24h.')
            return JSONResponse(size_payload, status_code=403)
    # Approved (by policy or by the owner): stream the artifact into the cache.
    key = _codebase_cache_key(source)
    dest_dir = os.path.join(_CODEBASE_CACHE_DIR, key)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.chmod(_CODEBASE_CACHE_DIR, 0o700)
    except OSError:
        pass
    name = os.path.basename(urllib.parse.urlparse(url).path) or 'artifact.bin'
    dest_path = os.path.join(dest_dir, name)
    try:
        fetched = await asyncio.to_thread(
            _codebase_stream, url, dest_path, threshold_bytes * 4)
    except Exception as exc:
        log_action(agent_id, 'codebase_fetch_failed',
                   {'source': source, 'error': type(exc).__name__},
                   authorized=True)
        return JSONResponse({'error': f'fetch failed: {type(exc).__name__}'},
                            status_code=502)
    size_payload.update({
        'ok': True, 'path': fetched['path'], 'bytes': fetched['bytes'],
        'sha256': fetched['sha256'],
    })
    log_action(agent_id, 'codebase_fetch',
               {'source': source, 'bytes': fetched['bytes'],
                'sha256': fetched['sha256'][:16], 'path': fetched['path']},
               authorized=True)
    return JSONResponse(size_payload)


def _codebase_stream(url, dest_path, max_bytes):
    """Stream the artifact to dest_path with a hard cap; returns
    path/bytes/sha256. Never executes the artifact."""
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'AIThinkTankAgent/1.0')
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=_CODEBASE_FETCH_TIMEOUT_S) as resp, \
                open(dest_path, 'wb') as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError('artifact exceeds the fetch cap')
                digest.update(chunk)
                fh.write(chunk)
    except Exception:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    return {'path': dest_path, 'bytes': total,
            'sha256': digest.hexdigest()}
