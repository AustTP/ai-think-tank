"""Operator-owned, allowlisted scripts for CRON-style standing tasks.

These scripts run on the HOST, not inside the sandbox, so they have the host
environment and full network access. Only scripts explicitly listed in
`scripts/cron_allowlist.json` may be referenced by a standing task, and only
the player session may create a script-backed standing task. That keeps an
agent from writing a URL into a script and scheduling it around the normal
sandbox egress allowlist.

A standing task normally queues an agent card. When its declaration names a
`script`, the due window instead runs that script and does not wake an agent.
The script is the default action; it can print a JSON object with
{"notify": true, "summary": "..."} to request a follow-up card, but that is an
exception, not the normal path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SCRIPT_DIR = 'scripts'
MAX_SCRIPT_BYTES = 2_000_000
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _allowed_scripts():
    """Operator-vetted cron scripts. Fail closed when the manifest is missing,
    unreadable, or malformed: host-run scripts are not sandboxed."""
    path = os.path.join(_repo_root(), SCRIPT_DIR, 'cron_allowlist.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('scripts'), list):
        return None
    return {str(s).strip().replace('\\', '/')
            for s in data['scripts'] if str(s).strip()}


def resolve_script(relpath):
    """Resolve one repo-relative scripts/*.py path. Returns (path, error)."""
    raw = str(relpath or '').strip().replace('\\', '/')
    if not raw:
        return None, 'script path is required'
    if raw.startswith('/') or '..' in raw.split('/'):
        return None, 'script must be a repo-relative path under scripts/'
    if not raw.startswith(SCRIPT_DIR + '/') or not raw.endswith('.py'):
        return None, 'script must be a .py file under scripts/'
    allowed = _allowed_scripts()
    if allowed is None:
        return None, 'cron script allowlist is missing or unreadable'
    if raw not in allowed:
        return None, 'script is not on the operator cron allowlist'
    root = os.path.realpath(_repo_root())
    path = os.path.realpath(os.path.join(root, raw))
    scripts = os.path.join(root, SCRIPT_DIR)
    if not path.startswith(scripts + os.sep):
        return None, 'script resolves outside scripts/'
    if not os.path.isfile(path):
        return None, f'script not found: {raw}'
    if os.path.getsize(path) > MAX_SCRIPT_BYTES:
        return None, 'script is too large'
    return path, None


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


def run(declaration):
    """Run one declaration's script. Never raises for a normal failure."""
    path, error = resolve_script(declaration.get('script'))
    if error:
        return {'ok': False, 'error': error, 'notify': False, 'summary': error}
    root = os.path.realpath(_repo_root())
    env = dict(os.environ)
    world = os.path.join(root, 'world')
    env['PYTHONPATH'] = os.pathsep.join(
        p for p in (root, world, env.get('PYTHONPATH')) if p)
    try:
        # Fixed argv, no shell; the path was validated under scripts/.
        proc = subprocess.run(  # nosec B603
            [sys.executable, path],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_timeout(declaration),
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'script timed out', 'notify': False,
                'summary': 'script timed out'}
    except OSError as exc:
        return {'ok': False, 'error': str(exc), 'notify': False,
                'summary': str(exc)}
    notify, summary = _parse_output(proc.stdout)
    return {
        'ok': proc.returncode == 0,
        'returncode': proc.returncode,
        'error': (proc.stderr or '').strip()[-1500:] if proc.returncode else '',
        'notify': notify,
        'summary': summary,
        'script': declaration.get('script'),
    }


def run_standing(state, declaration, log, queue, now_ms=None):
    """Run a script declaration and optionally queue one follow-up card."""
    outcome = run(declaration)
    log(state, 'server', 'standing_task_script', {
        'taskId': declaration.get('taskId'),
        'script': outcome.get('script') or declaration.get('script'),
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
        _, error = resolve_script(script)
        if error:
            return None, error
        fields['script'] = script
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
