# Operator sign-in escalation (2026-11-19): the need-based path for auth
# walls only a human can pass -- a federated sign-in with a device-prompt 2FA,
# an email magic link, a human puzzle. The agent calls this mid-task; the
# server starts the operator login browser on the service's VAULT profile and
# notifies the player (Telegram + inbox) with the one-click URL, the site,
# and the VNC password. The player signs in once; the vault session then
# propagates to every agent run afterwards (session grants + per-run copies).
# The agent NEVER handles the 2FA itself -- possession factor stays with the
# operator, by design.
import json as _json


def _svc_slug(value):
    import re as _re
    return _re.sub(r'[^A-Za-z0-9_.-]', '_', str(value or '').strip())[:64]


def _docker_run_cmd(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _tailscale_ip_cached():
    cached = globals().get('_TS_IP_CACHE')
    if cached:
        return cached
    try:
        out = _docker_run_cmd(['tailscale', 'ip', '-4'], timeout=10)
        ip = (out.stdout or '').strip().splitlines()
        if ip:
            globals()['_TS_IP_CACHE'] = ip[0].strip()
            return globals()['_TS_IP_CACHE']
    except Exception:
        pass
    return '127.0.0.1'


def _login_container_running(name):
    out = _docker_run_cmd(['docker', 'inspect', '-f',
                           '{{.State.Running}}', name], timeout=15)
    return (out.returncode == 0 and (out.stdout or '').strip() == 'true')


def _login_container_info(name):
    """(vnc_password, host_port) for a running login container, or None."""
    out = _docker_run_cmd(['docker', 'inspect', '-f',
                           '{{range .Config.Env}}{{println .}}{{end}}|'
                           '{{range $k, $v := .NetworkSettings.Ports}}'
                           '{{(index $v 0).HostPort}} {{end}}', name],
                          timeout=15)
    if out.returncode != 0:
        return None
    env_part, _, port_part = (out.stdout or '').partition('|')
    pw = ''
    for line in env_part.splitlines():
        if line.startswith('VNC_PW='):
            pw = line[len('VNC_PW='):]
            break
    port = (port_part.split() or [''])[0].strip()
    if not pw or not port:
        return None
    return pw, int(port)


def _free_login_port(used_ports):
    for candidate in range(6081, 6100):
        if candidate not in used_ports:
            return candidate
    return None


def _used_login_ports():
    out = _docker_run_cmd(['docker', 'ps', '--format', '{{.Ports}}'], timeout=15)
    used = set()
    import re as _re
    for match in _re.findall(r':(\d{4,5})->', out.stdout or ''):
        used.add(int(match))
    return used


def _start_operator_login_container(service, start_url, max_minutes=30):
    """Start (or reuse) the operator login browser for a vault session.
    Returns (vnc_url, vnc_password, container_name) or (None, None, reason)."""
    name = 'btank-login-' + _svc_slug(service)
    profile_dir = os.path.join(SESSION_VAULT_DIR, _svc_slug(service))
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(profile_dir, 0o700)
    except OSError:
        pass
    if _login_container_running(name):
        info = _login_container_info(name)
        if info:
            pw, port = info
            return f'http://{_tailscale_ip_cached()}:{port}/vnc.html?autoconnect=1', pw, name
        return None, None, 'login container is running but could not be inspected; stop it first'
    _docker_run_cmd(['docker', 'rm', '-f', name], timeout=20)
    port = _free_login_port(_used_login_ports())
    if not port:
        return None, None, 'no free local port in 6081..6099 for a login session'
    bind = _tailscale_ip_cached()
    password = secrets.token_urlsafe(9)
    helper = os.path.join(THINK_TANK_DIR, 'world', 'tools', 'login_browser_session.py')
    cmd = [
        'docker', 'run', '-d', '--rm', '--name', name,
        '-p', f'{bind}:{port}:6080',
        '-v', f'{profile_dir}:/browser-profile',
        '-v', f'{helper}:/opt/login_browser_session.py:ro',
        '-e', f'VNC_PW={password}',
        '-e', f'LOGIN_URL={start_url}',
        '-e', 'HOME=/browser-profile',
        '--shm-size', '256m',
        '--user', f'{os.getuid()}:{os.getgid()}',
        SANDBOX_IMAGE,
        'sh', '-c',
        'Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 & '
        'export DISPLAY=:99; '
        'for i in $(seq 1 25); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.2; done; '
        'x11vnc -display :99 -forever -passwd "$VNC_PW" -quiet >/tmp/x11vnc.log 2>&1 & '
        'x11vncpid=$!; '
        f'timeout {int(max_minutes) * 60} python3 /opt/login_browser_session.py '
        '>/tmp/login.log 2>&1 & '
        'websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 '
        '>/tmp/websockify.log 2>&1 & wait',
    ]
    started = _docker_run_cmd(cmd, timeout=90)
    if started.returncode != 0:
        return None, None, ('could not start the login browser: '
                            + (started.stderr or started.stdout or '')[:200])
    return f'http://{bind}:{port}/vnc.html?autoconnect=1', password, name


@app.post('/api/browser-auth/request-login')
@_state_writer
async def request_operator_login(request: Request):
    body = await request.json()
    agent_id = (body.get('agentId') or '').strip()
    service = (body.get('service') or '').strip().lower()
    task_id = (body.get('taskId') or '').strip()
    start_url = (body.get('url') or '').strip()
    # Same gate as every other agent endpoint: a player session may act as
    # any agent; an agent key may act only as the agent it belongs to.
    if not _actor_is_authorized(request, agent_id):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    if not check_rate_limit(agent_id):
        return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
    if not service:
        return JSONResponse({'error': 'service is required'}, status_code=400)
    spec = (_credential_access_cfg().get('autoGrant') or {}).get(service)
    if not (spec and spec.get('session')):
        return JSONResponse({'error': 'that service has no operator sign-in '
                                      'surface; only session-backed services '
                                      'can be escalated'}, status_code=403)
    # Spam guard: one escalation per agent+service per 15 minutes. The login
    # browser stays up for 30; repeated calls within the window are noise.
    try:
        with _db() as conn:
            recent = conn.execute(
                'SELECT COUNT(*) FROM action_log WHERE action = ? '
                'AND agent_id = ? AND ts > ?',
                ('operator_login_requested', agent_id,
                 time.time() - 900)).fetchone()[0]
        if recent >= 2:
            return JSONResponse({'error': 'an operator sign-in was already '
                                          'requested recently; wait for the '
                                          'operator to finish it'}, status_code=429)
    except Exception:
        pass
    if not start_url:
        hosts = list(spec.get('hosts') or [])
        start_url = f'https://{hosts[0]}' if hosts else f'https://{service}.com'
    login_url, password, container = _start_operator_login_container(service, start_url)
    if not login_url:
        return JSONResponse({'error': container}, status_code=500)
    state = _state_begin()
    if state is None:
        return JSONResponse({'error': 'state unavailable'}, status_code=503)
    now_ms = int(time.time() * 1000)
    question = (f'{agent_id} needs your sign-in for {service} ({start_url})'
                + (f' for task {task_id}' if task_id else '')
                + f'. Open {login_url} -- VNC password {password}. '
                'Finish the sign-in (including the Google number-tap if it '
                'appears); the vaulted session reaches every agent run. '
                'The browser auto-closes in 30 minutes.')
    msg_id = f'auth-{now_ms}'
    state.setdefault('playerInbox', []).append({
        'id': msg_id,
        'agentId': agent_id,
        'question': question,
        'service': service,
        'taskId': task_id or None,
        'vncUrl': login_url,
        'status': 'awaiting_input',
        'createdAt': now_ms,
        'queued': False,
    })
    save_state_to_db(state)
    log_action(agent_id, 'operator_login_requested',
               {'service': service, 'taskId': task_id, 'url': start_url,
                'vncUrl': login_url}, authorized=True)
    _append_passport_decision('operator_login_requested', agent_id, {
        'service': service, 'taskId': task_id, 'site': start_url})
    telegram_text = (f'[auth-escalation] {agent_id} needs your sign-in\n'
                     f'site: {start_url}\n'
                     f'open: {login_url}\n'
                     f'VNC password: {password}\n'
                     f'auto-closes in 30 min')
    try:
        await asyncio.to_thread(_telegram_send_text_sync, telegram_text)
    except Exception:
        pass  # Telegram is best-effort; the inbox message is the record
    return JSONResponse({'ok': True, 'escalated': True, 'service': service,
                         'vncUrl': login_url, 'vncPassword': password,
                         'inboxMessageId': msg_id, 'expiresInSec': 1800,
                         'note': 'the operator has been notified (Telegram + '
                                 'inbox). Do NOT retry the sign-in for at '
                                 'least 15 minutes; continue anonymously if '
                                 'the task allows and retry signed-in later.'})
