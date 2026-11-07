# Docker-down fallback for /api/execute (2026-11-07): when the local Docker
# sandbox cannot start (daemon down, binary missing, network setup failed),
# agent shell commands would previously just error, stalling every code task.
# This module reroutes the SAME command to the player's Google Colab free
# tier -- with the SAME guards the direct Colab path already enforces:
#   1. The /api/execute endpoint has already Jev-classified the command
#      (this fallback only runs for commands Jev allowed).
#   2. _colab_denied re-checks the command text against the account-killer
#      and offensive-security classes -- bug bounty / scan / exploit work and
#      anything that could get the Google account cancelled is refused here,
#      exactly as the user asked. It stays a local-sandbox-only activity.
#   3. The sandbox workspace is shipped as a bounded tar.gz, the command runs
#      in bash on the runtime, and changed files come back the same way.
#   4. The result says plainly WHERE it ran, so the agent is never misled
#      about its execution backend (same honesty rule as the DigitalOcean
#      branch: a silent fall-through would be a security lie).
# Nothing here bypasses Jev: refuse -> honest error; no fallback for denied
# work; oversized workspaces fail closed instead of shipping a truncated one.
import json
import os
import tarfile
import tempfile

_COLAB_FALLBACK_WORKSPACE_BUDGET = 24 * 1024 * 1024   # tar.gz bytes, either way
_COLAB_FALLBACK_SKIP_DIRS = {
    '.git', '__pycache__', '.pytest_cache', '.mypy_cache', 'node_modules',
    '.venv', 'venv',
}
_COLAB_FALLBACK_SKIP_SUFFIX = ('.pyc', '.pyo', '.log')
_DOCKER_DOWN_MARKERS = (
    'cannot connect to the docker daemon',
    'daemon is not running',
    'is the docker daemon running',
    'docker: error during connect',
    'error while creating mount source path',
    'creating shim',
    'no such file or directory: docker',   # CLI missing variant text
)


def _docker_unavailable(returncode=None, stderr=''):
    text = (stderr or '').lower()
    if returncode == 125 and ('docker' in text or 'daemon' in text):
        return True
    return any(marker in text for marker in _DOCKER_DOWN_MARKERS)


def _colab_fallback_workspace_tar(sandbox_dir):
    """tar.gz the workspace (skipping caches/VCS) up to the byte budget.
    Returns (temp_path, bytes) or (None, reason) when over budget."""
    fd, path = tempfile.mkstemp(prefix='colab-fb-', suffix='.tar.gz')
    os.close(fd)
    try:
        with tarfile.open(path, 'w:gz') as tar:
            for root, dirs, files in os.walk(sandbox_dir):
                dirs[:] = [d for d in dirs if d not in _COLAB_FALLBACK_SKIP_DIRS
                           and not os.path.islink(os.path.join(root, d))]
                for name in files:
                    fp = os.path.join(root, name)
                    if name.endswith(_COLAB_FALLBACK_SKIP_SUFFIX) \
                            or os.path.islink(fp):
                        continue
                    try:
                        if os.path.getsize(fp) > _COLAB_FALLBACK_WORKSPACE_BUDGET:
                            continue  # one oversized file never blocks the rest
                        tar.add(fp, arcname=os.path.relpath(fp, sandbox_dir))
                    except OSError:
                        continue
        size = os.path.getsize(path)
        if size > _COLAB_FALLBACK_WORKSPACE_BUDGET:
            os.remove(path)
            return None, f'workspace tar is {size} bytes (budget {_COLAB_FALLBACK_WORKSPACE_BUDGET})'
        return path, None
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        return None, f'could not tar the workspace: {e}'


_COLAB_FALLBACK_RUNNER = r'''
import os, subprocess, tarfile, json
ws = '/content/workspace'
with tarfile.open('/content/__fallback_in.tar.gz') as t:
    t.extractall(ws)
os.makedirs(ws, exist_ok=True)
proc = subprocess.run(['bash', '-c', os.environ['tank_fb_command']],
                      cwd=ws, capture_output=True, text=True,
                      timeout=int(os.environ.get('tank_fb_timeout', '300')),
                      env={**os.environ, 'HOME': ws})
out = {'exitCode': proc.returncode,
       'stdout': (proc.stdout or '')[-16000:],
       'stderr': (proc.stderr or '')[-8000:],
       'timedOut': False}
with tarfile.open('/content/__fallback_out.tar.gz', 'w:gz') as t:
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__')]
        for name in files:
            fp = os.path.join(root, name)
            if fp.endswith(('.pyc', '.pyo', '.log')):
                continue
            try:
                t.add(fp, arcname=os.path.relpath(fp, ws))
            except OSError:
                pass
print(json.dumps(out))
'''


def _run_on_colab_sandbox_sync(agent_id, sandbox_dir, command, purpose, timeout_s):
    """Docker-down fallback: run the same bash command on the Colab CPU
    runtime with the workspace shipped around it. Returns the standard
    _run_in_sandbox_sync result shape plus ranOn/colabNote. Never raises."""
    colab_run = globals().get('_colab_compute_run')
    colab_cli = globals().get('_colab_cli')
    colab_enabled = globals().get('COLAB_ENABLED')
    colab_denied = globals().get('_colab_denied')
    colab_cpu = globals().get('COLAB_CPU_SESSION') or 'tank-cpu'
    note_refuse = (
        'Local Docker is unavailable AND this command is off-limits on the '
        'player\'s Colab account (bug bounty / scan / exploit tooling, mining, '
        'bulk media, torrents, exfil hosts, or Drive/GCP access are all '
        'refused there). Retry when Docker is back, or reshape the work so it '
        'needs neither.')
    if not (colab_enabled and colab_run and colab_cli and colab_denied):
        return {'exitCode': None, 'stdout': '', 'timedOut': False,
                'stderr': 'Local Docker is unavailable and the Colab fallback '
                          'is not enabled (COLAB_ENABLED=0 or colab CLI missing). '
                          'No command was run anywhere. Retry when Docker returns.'}
    if len(command) > 20000:
        return {'exitCode': None, 'stdout': '', 'timedOut': False,
                'stderr': 'Local Docker is unavailable and this command is too '
                          'large for the Colab fallback. No command was run anywhere.'}
    blocked = colab_denied(command) or colab_denied(purpose or '')
    if blocked:
        log_action(agent_id, 'execute_colab_fallback',
                   {'sandboxId': os.path.basename(sandbox_dir), 'refused': blocked},
                   authorized=False)
        return {'exitCode': None, 'stdout': '', 'timedOut': False,
                'stderr': note_refuse}
    tar_path, err = _colab_fallback_workspace_tar(sandbox_dir)
    if err:
        return {'exitCode': None, 'stdout': '', 'timedOut': False,
                'stderr': f'Local Docker is unavailable and the Colab fallback '
                          f'cannot ship the workspace: {err}. Retry when Docker '
                          f'returns.'}
    out_tar = None
    session = None
    try:
        timeout = max(1, min(int(timeout_s or 300), 900))
        ok, msg = globals()['_colab_compute_provision'](colab_cpu, kind='cpu')
        if not ok:
            return {'exitCode': None, 'stdout': '', 'timedOut': False,
                    'stderr': f'Local Docker is unavailable and no Colab runtime '
                              f'could be provisioned: {msg}. Retry when Docker returns.'}
        session = colab_cpu
        rc, up = colab_cli('upload', '-s', session, tar_path,
                           '/content/__fallback_in.tar.gz', timeout=300)
        if rc != 0:
            return {'exitCode': None, 'stdout': '', 'timedOut': False,
                    'stderr': f'Colab fallback upload failed: {up[-600:]}. '
                              f'Retry when Docker returns.'}
        code = ('import os, json\n'
                "os.environ['tank_fb_command'] = "
                + repr(command) + '\n'
                "os.environ['tank_fb_timeout'] = '" + str(timeout) + "'\n"
                + _COLAB_FALLBACK_RUNNER)
        rc, out = colab_cli('exec', '-s', session, '--timeout', str(timeout + 60),
                            timeout=timeout + 120, input=code)
        if '__COLAB_DONE__' in out:
            out = '\n'.join(l for l in out.splitlines() if '__COLAB_DONE__' not in l)
        # parse the runner's json line
        parsed = None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith('{') and '"exitCode"' in line:
                try:
                    parsed = json.loads(line)
                    break
                except Exception:
                    continue
        if parsed is None:
            return {'exitCode': rc if rc else None, 'stdout': '', 'timedOut': False,
                    'stderr': ('Colab fallback run did not produce a result. '
                               f'Tail: {out[-800:]}'),
                    'ranOn': 'colab'}
        # bring changed files home (best effort)
        fd, out_tar = tempfile.mkstemp(prefix='colab-fb-out-', suffix='.tar.gz')
        os.close(fd)
        rc2, dl = colab_cli('download', '-s', session,
                            '/content/__fallback_out.tar.gz',
                            out_tar, timeout=300)
        if rc2 == 0 and os.path.getsize(out_tar) <= _COLAB_FALLBACK_WORKSPACE_BUDGET:
            try:
                with tarfile.open(out_tar) as t:
                    t.extractall(sandbox_dir)
            except Exception:
                pass  # output already captured; file sync is best effort
        elif rc2 != 0:
            parsed['stderr'] = (parsed.get('stderr') or '') + \
                '\n[changed files could not be pulled back from Colab]'
        parsed['ranOn'] = 'colab'
        parsed['colabNote'] = (
            'Local Docker was unavailable, so this command ran on the player\'s '
            'Colab CPU runtime with the workspace shipped to it. Colab denies '
            'bug bounty / scan / exploit and account-risk work by policy.')
        log_action(agent_id, 'execute_colab_fallback',
                   {'sandboxId': os.path.basename(sandbox_dir),
                    'exitCode': parsed.get('exitCode'), 'session': session},
                   authorized=True)
        return parsed
    except Exception as e:
        return {'exitCode': None, 'stdout': '', 'timedOut': False,
                'stderr': f'Colab fallback failed: {e}. Retry when Docker returns.',
                'ranOn': 'colab'}
    finally:
        for p in (tar_path, out_tar):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
