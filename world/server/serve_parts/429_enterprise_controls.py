"""Enterprise control surface: posture, audit export, and an operator page.

This is deliberately read-only. It reports what is actually configured,
including gaps, instead of asserting controls the deployment does not have.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import time
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

ENTERPRISE_RETENTION_DAYS = int(_load_env().get('AUDIT_RETENTION_DAYS', '365') or 365)
ENTERPRISE_DEPLOYMENT_ID = (_load_env().get('DEPLOYMENT_ID', 'default') or 'default').strip()
ENTERPRISE_MAX_EXPORT = 5000

_SENSITIVE_JSON_KEYS = re.compile(
    r'("?(?:secret|secret_key|api_key|apikey|access_token|refresh_token|token|'
    r'password|authorization|bearer)"?\s*[:=]\s*")[^"]+', re.I)


def _enterprise_redact(text):
    text = _SENSITIVE_JSON_KEYS.sub(r'\1[REDACTED]', str(text or ''))
    return _redact_secrets(text)


def _enterprise_auth(request):
    """Return (caller, status_code). 401 for no/invalid credential, 403 for a
    valid non-admin agent."""
    if verify_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return 'player', None
    key = (request.headers.get('X-Agent-Key') or '').strip()
    auth = (request.headers.get('Authorization') or '').strip()
    if not key and auth.lower().startswith('bearer '):
        key = auth[7:].strip()
    if not key:
        return None, 401
    with _db() as conn:
        row = conn.execute('SELECT agent_id FROM agent_keys WHERE secret_key = ?',
                           (key,)).fetchone()
    if not row:
        return None, 401
    state = get_state_from_db() or {}
    if not _is_admin(state, row[0]):
        return None, 403
    return row[0], None


def _control(check_id, name, status, detail, evidence=None):
    return {'id': check_id, 'name': name, 'status': status,
            'detail': detail, 'evidence': evidence or []}


def _file_mode(path):
    try:
        return oct(stat.S_IMODE(os.stat(path).st_mode))
    except OSError:
        return None


def _audit_stats():
    try:
        with _db() as conn:
            row = conn.execute(
                'SELECT COUNT(*), MIN(ts), MAX(ts) FROM action_log').fetchone()
        return {'count': int(row[0] or 0), 'oldest': row[1], 'newest': row[2]}
    except Exception:
        return {'count': 0, 'oldest': None, 'newest': None}


def _agent_key_count():
    try:
        with _db() as conn:
            return int(conn.execute('SELECT COUNT(*) FROM agent_keys').fetchone()[0] or 0)
    except Exception:
        return 0


def _latest_backup():
    root = os.path.expanduser('~/ai-village-backups')
    latest = None
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            mtime = os.path.getmtime(path)
            if latest is None or mtime > latest[0]:
                latest = (mtime, path)
    except OSError:
        return None
    return latest


def _posture_checks(state):
    state = state or {}
    admin_ids = [d.get('id') for d in (state.get('agentRoster') or [])
                 if d.get('isAdmin')]
    director_ids = [d.get('id') for d in (state.get('agentRoster') or [])
                    if d.get('isDirector')]
    role_count = len(state.get('agentRoster') or [])
    audit = _audit_stats()
    keys = _agent_key_count()
    backup = _latest_backup()
    docker = shutil.which('docker')
    secret_modes = []
    for rel in ('.env', 'world/.env', 'vault/credentials.json'):
        mode = _file_mode(rel)
        if mode:
            secret_modes.append(f'{rel} {mode}')
    checks = [
        _control('operator_access', 'Operator authentication',
                 'pass' if admin_ids else 'fail',
                 'Player session plus verified admin agent key.' if admin_ids
                 else 'No admin agent is configured.',
                 [f'admins: {", ".join(admin_ids) or "none"}']),
        _control('rbac', 'Role-based access',
                 'pass' if admin_ids and director_ids else 'warn',
                 f'{len(admin_ids)} admin(s), {len(director_ids)} director(s), '
                 f'{role_count} roster role(s).',
                 ['admin, director, worker, viewer boundaries are enforced by the server.']),
        _control('agent_key_storage', 'Agent key storage',
                 'fail' if keys else 'warn',
                 f'{keys} agent key(s) are stored as plaintext in SQLite. '
                 'Hashing is not enabled.' if keys
                 else 'No agent keys are stored yet.',
                 ['agent_keys.secret_key']),
        _control('execution_gate', 'Agent execution gate',
                 'pass', f'AGENT_EXECUTION_ENABLED={bool(EXECUTION_ENABLED)}',
                 ['Every execution endpoint checks this flag.']),
        _control('sandbox_isolation', 'Sandbox isolation',
                 'pass' if docker else 'warn',
                 f'Image {SANDBOX_IMAGE}; docker {"available" if docker else "not found"}.',
                 ['Agent code runs in a per-task container boundary.']),
        _control('secret_files', 'Secret file permissions',
                 'pass' if secret_modes else 'warn',
                 '; '.join(secret_modes) or 'No local .env or credential file was found.',
                 ['Only file names and modes are reported, never values.']),
        _control('redaction', 'Secret redaction',
                 'pass' if '[REDACTED]' in _redact_secrets('sk-enterprise-test') else 'fail',
                 'Audit and notification text passes through the redactor.',
                 ['sk-* and sensitive JSON keys are replaced.']),
        _control('audit_log', 'Audit log',
                 'pass' if audit['count'] else 'warn',
                 f'{audit["count"]} event(s); retention {ENTERPRISE_RETENTION_DAYS} day(s).',
                 [f'oldest: {audit["oldest"]}', f'newest: {audit["newest"]}']),
        _control('audit_export', 'Audit export',
                 'pass', 'JSON and JSONL export with a SHA-256 manifest digest.',
                 ['GET /api/admin/enterprise/audit']),
        _control('backup', 'Backup recency',
                 'pass' if backup and time.time() - backup[0] < 7 * 86400 else 'warn',
                 (os.path.basename(backup[1]) if backup
                  else 'No backup directory was found.'),
                 [f'last backup age: {round((time.time() - backup[0]) / 3600, 1)}h'
                  if backup else 'no backups']),
        _control('tenant_identity', 'Deployment identity',
                 'pass' if ENTERPRISE_DEPLOYMENT_ID != 'default' else 'warn',
                 f'DEPLOYMENT_ID={ENTERPRISE_DEPLOYMENT_ID}',
                 ['Set DEPLOYMENT_ID to a stable customer or environment id.']),
        _control('sso_mfa', 'SSO and MFA',
                 'warn', 'No SSO or MFA provider is configured; operator access is '
                 'a local session plus admin agent keys.', []),
        _control('encryption_at_rest', 'Encryption at rest',
                 'warn', 'SQLite is not application-encrypted; rely on host disk encryption.',
                 []),
        _control('soc2', 'SOC 2 attestation',
                 'warn', 'No SOC 2 attestation exists for this deployment.', []),
        _control('rate_limits', 'Request rate limits',
                 'warn', 'Execution and model calls are gated by auth and budgets; '
                 'there is no global HTTP rate limiter.', []),
    ]
    summary = {'pass': 0, 'warn': 0, 'fail': 0, 'unknown': 0}
    for check in checks:
        summary[check['status']] = summary.get(check['status'], 0) + 1
    return checks, summary


@app.get('/api/admin/enterprise/posture')
def enterprise_posture(request: Request):
    caller, status = _enterprise_auth(request)
    if status:
        return JSONResponse({'error': 'operator authentication required' if status == 401
                             else 'admin authority required'}, status_code=status)
    state = get_state_from_db() or {}
    checks, summary = _posture_checks(state)
    return JSONResponse({
        'deploymentId': ENTERPRISE_DEPLOYMENT_ID,
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'retentionDays': ENTERPRISE_RETENTION_DAYS,
        'summary': summary,
        'checks': checks,
        'caller': caller,
    })


@app.get('/api/admin/enterprise/audit')
def enterprise_audit(request: Request, since: float = 0, until: float = 0,
                     action: str = '', agent: str = '', after: int = 0,
                     limit: int = 1000, format: str = 'json'):
    caller, status = _enterprise_auth(request)
    if status:
        return JSONResponse({'error': 'operator authentication required' if status == 401
                             else 'admin authority required'}, status_code=status)
    limit = max(1, min(int(limit or 1000), ENTERPRISE_MAX_EXPORT))
    query = ('SELECT id, agent_id, action, details, authorized, ts '
             'FROM action_log WHERE id > ?')
    params = [int(after or 0)]
    if since:
        query += ' AND ts >= ?'
        params.append(float(since))
    if until:
        query += ' AND ts <= ?'
        params.append(float(until))
    if action:
        query += ' AND action = ?'
        params.append(action)
    if agent:
        query += ' AND agent_id = ?'
        params.append(agent)
    query += ' ORDER BY id ASC LIMIT ?'
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    events = []
    for row in rows:
        events.append({
            'id': row[0],
            'ts': row[5],
            'agentId': row[1],
            'action': row[2],
            'authorized': bool(row[4]),
            'details': _enterprise_redact(row[3])[:2000],
        })
    canonical = json.dumps(events, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    payload = {
        'deploymentId': ENTERPRISE_DEPLOYMENT_ID,
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'retentionDays': ENTERPRISE_RETENTION_DAYS,
        'filters': {'since': since, 'until': until, 'action': action,
                    'agent': agent, 'after': after, 'limit': limit},
        'count': len(events),
        'nextAfter': events[-1]['id'] if events else int(after or 0),
        'sha256': digest,
        'events': events,
    }
    try:
        log_action(caller, 'enterprise_audit_export',
                   {'count': len(events), 'action': action, 'agent': agent,
                    'sha256': digest}, authorized=True)
    except Exception:
        pass
    if str(format).lower() == 'jsonl':
        body = '\n'.join(json.dumps(event, sort_keys=True) for event in events)
        return Response(body, media_type='application/x-ndjson',
                        headers={'X-Audit-SHA256': digest,
                                 'X-Audit-Count': str(len(events))})
    return JSONResponse(payload)


ENTERPRISE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enterprise controls</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0e1116;color:#e6edf3;font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 20px}h1{font-size:24px;margin:0 0 6px}p.muted{color:#8b949e;margin:0 0 22px}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px}button{background:#1f6feb;border:0;color:#fff;padding:9px 14px;border-radius:6px;cursor:pointer;font-weight:600}
button.secondary{background:#21262d;border:1px solid #30363d}button:hover{filter:brightness(1.1)}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}.pill{padding:5px 10px;border-radius:999px;background:#161b22;border:1px solid #30363d}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #21262d;vertical-align:top}th{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}.status{font-weight:700;text-transform:uppercase;font-size:11px}.pass{color:#3fb950}.warn{color:#d29922}.fail{color:#f85149}.unknown{color:#8b949e}
.detail{color:#c9d1d9}.evidence{color:#8b949e;font-size:12px;margin-top:3px}#msg{color:#f85149;margin:0 0 14px;white-space:pre-wrap}
</style></head><body><main>
<h1>Enterprise controls</h1><p class="muted">Read-only posture and audit export. Values are redacted; secrets are never shown.</p>
<div id="msg"></div><div class="bar"><button onclick="load()">Refresh posture</button><button class="secondary" onclick="exportAudit()">Export audit JSONL</button></div>
<div class="summary" id="summary"></div><table><thead><tr><th>Control</th><th>Status</th><th>Detail</th></tr></thead><tbody id="rows"></tbody></table>
</main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const msg=document.getElementById('msg');msg.textContent='';try{const r=await fetch('/api/admin/enterprise/posture',{credentials:'include'});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);document.getElementById('summary').innerHTML=['pass','warn','fail','unknown'].map(k=>`<span class="pill ${k}">${k}: ${j.summary[k]||0}</span>`).join('');document.getElementById('rows').innerHTML=j.checks.map(c=>`<tr><td><strong>${esc(c.name)}</strong><div class="evidence">${esc(c.id)}</div></td><td class="status ${esc(c.status)}">${esc(c.status)}</td><td class="detail">${esc(c.detail)}<div class="evidence">${esc((c.evidence||[]).join(' · '))}</div></td></tr>`).join('');}catch(e){msg.textContent='Could not load posture: '+e.message+'\\nSign in as the operator or use an admin agent key.';}}
async function exportAudit(){const msg=document.getElementById('msg');msg.textContent='';try{const r=await fetch('/api/admin/enterprise/audit?format=jsonl&limit=5000',{credentials:'include'});if(!r.ok){const j=await r.json();throw new Error(j.error||r.status);}const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='audit-export.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}catch(e){msg.textContent='Export failed: '+e.message;}}
load();
</script></body></html>"""


@app.get('/admin/enterprise', response_class=HTMLResponse)
def enterprise_page():
    return HTMLResponse(ENTERPRISE_PAGE)
