"""Operator-owned, allowlisted scripts for CRON-style standing tasks.

These scripts are deliberately narrow. A script must be listed in
`scripts/cron_allowlist.json` with its SHA-256 and an explicit runner:

  * runner "sandbox" -- executed inside the same internal-only Docker
    sandbox as execute_script. Network egress goes through the sandbox proxy,
    so the normal package/browse allowlist applies at runtime.
  * runner "host" -- executed on the host only for genuinely host-bound work
    (Messages.app, headful browser, etc.). A host script also requires the
    standing task to carry `allowHostExecution: true`, which only a player
    session can set.

Every entry also lists `allowedHosts` for literal URLs in the script. The
tripwire catches static URLs; the sandbox proxy is the runtime guarantee for
sandboxed scripts. Dynamic URLs in a host script cannot be caught by the
tripwire and are why host scripts need explicit operator approval.

A standing task normally queues an agent card. When its declaration names a
`script`, the due window instead runs that script and does not wake an agent.
The script is the default action; it can print a JSON object with
{"notify": true, "summary": "..."} to request a follow-up card, but that is an
exception, not the normal path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

SCRIPT_DIR = 'scripts'
ALLOWLIST_NAME = 'cron_allowlist.json'
MAX_SCRIPT_BYTES = 2_000_000
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
_SAFE_ENV_KEYS = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'USER', 'SHELL')
_URL_RE = re.compile(r'https?://[^\s\'"<>]+', re.IGNORECASE)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _allowed_scripts():
    """Return {repo-relative script path: allowlist entry}, or None when the
    manifest is missing, unreadable, or malformed. Fail closed."""
    path = os.path.join(_repo_root(), SCRIPT_DIR, ALLOWLIST_NAME)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('scripts'), list):
        return None
    out = {}
    for item in data['scripts']:
        if not isinstance(item, dict):
            return None
        rel = str(item.get('path') or '').strip().replace('\\', '/')
        if not rel:
            return None
        out[rel] = item
    return out


def _validate_script(relpath):
    """Return (path, entry, source, error) for one allowlisted script.

    Checks path shape, allowlist membership, SHA-256 pin, file size, and the
    literal-URL host tripwire before anything runs."""
    raw = str(relpath or '').strip().replace('\\', '/')
    if not raw:
        return None, None, None, 'script path is required'
    if raw.startswith('/') or '..' in raw.split('/'):
        return None, None, None, 'script must be a repo-relative path under scripts/'
    if not raw.startswith(SCRIPT_DIR + '/') or not raw.endswith('.py'):
        return None, None, None, 'script must be a .py file under scripts/'
    allowed = _allowed_scripts()
    if allowed is None:
        return None, None, None, 'cron script allowlist is missing or unreadable'
    entry = allowed.get(raw)
    if not isinstance(entry, dict):
        return None, None, None, 'script is not on the operator cron allowlist'
    runner = str(entry.get('runner') or '').strip().lower()
    if runner not in ('sandbox', 'host'):
        return None, None, None, 'cron allowlist entry has an invalid runner'
    expected = str(entry.get('sha256') or '').strip().lower()
    if not expected:
        return None, None, None, 'cron allowlist entry is missing sha256'
    root = os.path.realpath(_repo_root())
    path = os.path.realpath(os.path.join(root, raw))
    scripts = os.path.join(root, SCRIPT_DIR)
    if not path.startswith(scripts + os.sep):
        return None, None, None, 'script resolves outside scripts/'
    if not os.path.isfile(path):
        return None, None, None, f'script not found: {raw}'
    try:
        with open(path, 'rb') as f:
            data = f.read(MAX_SCRIPT_BYTES + 1)
    except OSError as exc:
        return None, None, None, f'could not read script: {exc}'
    if len(data) > MAX_SCRIPT_BYTES:
        return None, None, None, 'script is too large'
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        return None, None, None, 'script hash does not match the allowlist entry'
    try:
        source = data.decode('utf-8')
    except UnicodeDecodeError:
        source = data.decode('utf-8', errors='replace')
    allowed_hosts = {
        str(host).strip().lower().strip('.')
        for host in (entry.get('allowedHosts') or [])
    }
    for url in _URL_RE.findall(source):
        try:
            host = (urllib.parse.urlparse(url).hostname or '').lower().strip('.')
        except ValueError:
            continue
        if host and host not in allowed_hosts:
            return None, None, None, (
                f'script references a URL host not on its allowlist: {host}')
    return path, entry, source, None


def resolve_script(relpath):
    """Resolve one repo-relative scripts/*.py path. Returns (path, error)."""
    path, _entry, _source, error = _validate_script(relpath)
    return path, error


def _timeout(declaration):
    try:
        seconds = int(declaration.get('scriptTimeoutSeconds')
                      or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT_S
    return max(1, min(MAX_TIMEOUT_S, seconds))


def _parse_output(stdout):
    text = (stdout or '').strip()
    notify = False
    summary = text[-1500:]
    if not text:
        return notify, summary
    candidate = text.splitlines()[-1]
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        notify = bool(payload.get('notify'))
        summary = str(payload.get('summary') or summary)[:1500]
    return notify, summary


def _safe_env(entry, sandbox=False):
    """The exact environment a script gets: a small base plus only the names
    its allowlist entry declares. Never dict(os.environ).

    Sandbox scripts get container paths and no host PATH/HOME. Host scripts
    get the host base because they are explicitly approved host-bound work."""
    if sandbox:
        env = {
            'HOME': '/tmp',
            'TMPDIR': '/tmp',
            'LANG': 'C.UTF-8',
            'PYTHONPATH': '/workspace:/workspace/world',
            'PYTHONDONTWRITEBYTECODE': '1',
        }
    else:
        env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        env.setdefault('HOME', os.path.expanduser('~') or '/tmp')
        env.setdefault('TMPDIR', tempfile.gettempdir())
        root = _repo_root()
        world = os.path.join(root, 'world')
        env['PYTHONPATH'] = os.pathsep.join(
            p for p in (root, world, env.get('PYTHONPATH')) if p)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
    for key in (entry.get('env') or []):
        name = str(key).strip()
        if name and name in os.environ:
            env[name] = os.environ[name]
    return env


def _run_sandboxed(relpath, timeout_s, env):
    import serve as _serve
    return _serve.run_cron_script_sandboxed(relpath, timeout_s, env)


def run(declaration):
    """Run one declaration's script. Never raises for a normal failure."""
    rel = declaration.get('script')
    path, entry, _source, error = _validate_script(rel)
    if error:
        return {'ok': False, 'error': error, 'notify': False,
                'summary': error, 'script': rel,
                'runner': (entry or {}).get('runner')}
    runner = str(entry.get('runner') or '').strip().lower()
    if runner == 'host' and not declaration.get('allowHostExecution'):
        message = 'host-only script requires explicit allowHostExecution approval'
        return {'ok': False, 'error': message, 'notify': False,
                'summary': message, 'script': rel, 'runner': runner}
    timeout_s = _timeout(declaration)
    env = _safe_env(entry, sandbox=(runner == 'sandbox'))
    if runner == 'sandbox':
        result = _run_sandboxed(str(rel), timeout_s, env)
        if result.get('timedOut'):
            return {'ok': False, 'error': 'script timed out', 'notify': False,
                    'summary': 'script timed out', 'script': rel,
                    'runner': runner}
        returncode = result.get('exitCode')
        stdout = result.get('stdout') or ''
        stderr = result.get('stderr') or ''
    else:
        try:
            proc = subprocess.run(  # nosec B603 -- fixed argv, no shell
                [sys.executable, path],
                cwd=_repo_root(), env=env, capture_output=True, text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': 'script timed out', 'notify': False,
                    'summary': 'script timed out', 'script': rel,
                    'runner': runner}
        except OSError as exc:
            return {'ok': False, 'error': str(exc), 'notify': False,
                    'summary': str(exc), 'script': rel, 'runner': runner}
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    notify, summary = _parse_output(stdout)
    return {
        'ok': returncode == 0,
        'returncode': returncode,
        'error': (stderr or '').strip()[-1500:] if returncode else '',
        'notify': notify,
        'summary': summary,
        'script': rel,
        'runner': runner,
    }


def run_standing(state, declaration, log, queue, now_ms=None):
    """Run a script declaration and optionally queue one follow-up card."""
    outcome = run(declaration)
    log(state, 'server', 'standing_task_script', {
        'taskId': declaration.get('taskId'),
        'script': outcome.get('script') or declaration.get('script'),
        'runner': outcome.get('runner'),
        'hostExecution': bool(declaration.get('allowHostExecution')),
        'ok': outcome.get('ok'),
        'returncode': outcome.get('returncode'),
        'notify': outcome.get('notify'),
        'error': outcome.get('error') or '',
        'nowMs': now_ms,
    })
    if outcome.get('notify'):
        queue(state, [{
            'title': f"Standing script report: {declaration.get('name') or declaration.get('taskId')}",
            'room': declaration.get('room') or 'observatory',
            'instructions': ('A scheduled script reported something that needs '
                             'judgment. Review the output below, verify it '
                             'against the named sources, and act only on what '
                             'is real.\n\n' + outcome.get('summary', '')),
            'standingTaskId': declaration.get('taskId'),
            'teamId': declaration.get('team') or None,
            'assignedTo': declaration.get('notifyAgent') or None,
            'kbClass': 'changes_how_we_work',
        }])
    return outcome


def declaration_fields(body):
    """Validate the script-specific fields of a standing-task body.
    Returns (fields, error); unrelated fields are ignored."""
    fields = {}
    script = str(body.get('script') or '').strip()
    if script:
        _path, entry, _source, error = _validate_script(script)
        if error:
            return None, error
        runner = str(entry.get('runner') or '').strip().lower()
        fields['script'] = script
        fields['scriptRunner'] = runner
        fields['scriptSha256'] = str(entry.get('sha256') or '')
        if runner == 'host':
            if body.get('allowHostExecution') is not True:
                return None, 'host-only script requires allowHostExecution: true'
            fields['allowHostExecution'] = True
        timeout = body.get('scriptTimeoutSeconds')
        if timeout not in (None, ''):
            try:
                fields['scriptTimeoutSeconds'] = int(timeout)
            except (TypeError, ValueError):
                return None, '"scriptTimeoutSeconds" must be an integer'
    notify_agent = str(body.get('notifyAgent') or '').strip()
    if notify_agent:
        fields['notifyAgent'] = notify_agent
    return fields, None
