"""Long-lived, player-viewable browser sessions.

A browser_act run normally starts one Chromium, does one op, and exits. That
is right for an agent working alone, but it leaves the player nothing to
attach to when the agent hits a wall (a challenge page, a form it cannot
finish, a login that needs a human).

This module keeps ONE browser alive per agent, plus a tiny viewer container:

  browser container (internal network, no published port)
    Xvfb :99 -> headful Chromium (CDP on 9222, profile at /browser-profile)
             -> x11vnc on localhost:5900

  viewer container (default bridge + internal network, one loopback port)
    websockify/noVNC on 6080 -> browser-container:5900

The split is deliberate. Docker does not publish ports from a container whose
only network is `internal`, and the browser must stay on the internal network
so it can only egress through the allowlist proxy. The viewer has no browser
and no credentials; it just relays VNC frames. serve.py proxies the viewer's
static assets and websocket through the player session, so takeover works from
any machine that can reach the app.

The driver connects to the session Chromium over CDP instead of launching its
own (see browser_driver._launch_cdp), so the player and the agent see the same
page. Takeover is a state flag, not a second browser: while it is set, agent
ops come back as "waiting for operator", and handback clears it so the next
read sees whatever the human changed.

Nothing here disguises the browser. It is a window on loopback, reached with
the player session plus a per-session token, so a human can step in.
"""
import os
import re
import secrets
import socket
import subprocess
import threading
import time

SESSION_IMAGE = os.environ.get('BROWSER_SESSION_IMAGE') or 'ai-think-tank-work-sandbox'
SESSION_NETWORK = os.environ.get('SANDBOX_NETWORK') or 'ai-think-tank-sandbox-net'
SESSION_IDLE_STOP_S = int(os.environ.get('BROWSER_SESSION_IDLE_STOP_S') or 900)
SESSION_CDP_URL = 'http://127.0.0.1:9222'
SESSION_VNC_PORT = 5900
SESSION_WEB_PORT = 6080
SESSION_CONTAINER_WORKSPACE = '/workspace'
# A private, internal network for VNC traffic. Only our browser and viewer
# containers join it, never an agent sandbox, so x11vnc can bind its interface
# without exposing the display to other agents.
SESSION_VIEW_NETWORK = 'ai-think-tank-browser-view'

_LOCK = threading.RLock()
_SESSIONS = {}


class BrowserSessionError(Exception):
    pass


def _slug(agent_id):
    slug = re.sub(r'[^A-Za-z0-9_.-]', '_', str(agent_id or ''))[:64]
    if not slug or slug in ('.', '..'):
        raise BrowserSessionError('agentId is required')
    return slug


def container_name(agent_id):
    return 'tt-browser-' + _slug(agent_id)


def viewer_container_name(agent_id):
    return 'tt-browser-view-' + _slug(agent_id)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def viewer_url(port, password):
    base = f'http://127.0.0.1:{int(port)}/vnc.html?autoconnect=1&resize=scale'
    return (base + '&password=' + password) if password else base


def _container_script():
    """Shell for PID 1 in the browser container. One string keeps the image
    free of an extra entrypoint file."""
    return (
        'set -e; '
        'Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 & '
        'for i in $(seq 1 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.2; done; '
        'export DISPLAY=:99; '
        'CHROME=$(ls -d /root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome '
        '2>/dev/null | head -1); '
        '[ -n "$CHROME" ] || { echo "no chromium in image" >&2; exit 1; }; '
        '"$CHROME" --remote-debugging-port=9222 --remote-allow-origins=* '
        '--user-data-dir=/browser-profile --no-sandbox --disable-gpu '
        '--disable-dev-shm-usage --no-first-run --no-default-browser-check '
        'about:blank >/tmp/chrome.log 2>&1 & '
        'wait'
    )


def _docker(args, timeout=60, input_text=None):
    return subprocess.run(['docker'] + list(args), capture_output=True,
                          text=True, timeout=timeout,
                          input=input_text)


def _running(container):
    try:
        out = _docker(['inspect', '-f', '{{.State.Running}}', container], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and (out.stdout or '').strip() == 'true'


def _public(session):
    return {
        'agentId': session['agentId'],
        'status': session['status'],
        'takeover': session['takeover'],
        'port': session['port'],
        'startedAt': session['startedAt'],
        'lastUsed': session['lastUsed'],
        'viewerUrl': session['viewerUrl'],
        'token': session['token'],
        'cdpUrl': SESSION_CDP_URL,
    }


def _build_run_command(name, password, profile_dir, sandbox_dir, proxy_url,
                       cpus, memory):
    """Browser container: internal network only, no published port."""
    cmd = [
        'run', '-d', '--rm', '--name', name,
        '--network', SESSION_NETWORK,
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--memory', memory, '--cpus', cpus,
        '--pids-limit', '256', '--shm-size', '512m',
        '-e', f'VNC_PASS={password}',
    ]
    if proxy_url:
        for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            cmd += ['-e', f'{key}={proxy_url}']
    cmd += [
        '-v', f'{profile_dir}:/browser-profile',
        '-v', f'{sandbox_dir}:{SESSION_CONTAINER_WORKSPACE}',
        '-w', SESSION_CONTAINER_WORKSPACE,
        SESSION_IMAGE,
        'sh', '-c', _container_script(),
    ]
    return cmd


def _build_viewer_command(name, browser_name, port):
    """Viewer container: default bridge (so the port publishes) plus the
    internal network (so it can reach the browser's x11vnc). It runs only
    websockify."""
    return [
        'run', '-d', '--rm', '--name', name,
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--memory', '256m', '--cpus', '0.5',
        '-p', f'127.0.0.1:{port}:{SESSION_WEB_PORT}',
        SESSION_IMAGE,
        'websockify', '--web=/usr/share/novnc', str(SESSION_WEB_PORT),
        f'{browser_name}:{SESSION_VNC_PORT}',
    ]


def _container_ip(container, network, attempts=20):
    """IP a container holds on `network`. Docker assigns it shortly after
    `network connect`, so poll briefly."""
    tmpl = '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % network
    for _ in range(attempts):
        try:
            out = _docker(['inspect', '-f', tmpl, container], timeout=15)
            ip = (out.stdout or '').strip()
        except (OSError, subprocess.SubprocessError):
            ip = ''
        if ip and re.match(r'^[0-9a-fA-F:.]+$', ip):
            return ip
        time.sleep(0.25)
    return ''


def _start_x11vnc(container, ip, allow_ip=''):
    """Start x11vnc bound to the VIEW network address only.

    The browser is also on the sandbox network for egress. Binding 0.0.0.0
    would put the display on that network where agent sandboxes live, so the
    listen address is the private view IP, and the viewer is the only peer
    that can reach it."""
    if not ip:
        raise BrowserSessionError('browser has no address on the view network')
    cmd = [
        'exec', '-d', container, 'sh', '-c',
        'x11vnc -storepasswd "$VNC_PASS" /tmp/vncpw >/dev/null 2>&1; '
        f'exec x11vnc -display :99 -no6 -listen {ip} '
        + (f'-allow {allow_ip} ' if allow_ip else '')
        + f'-rfbauth /tmp/vncpw -shared -forever -rfbport {SESSION_VNC_PORT} '
        '>/tmp/x11vnc.log 2>&1',
    ]
    out = _docker(cmd, timeout=20)
    if out.returncode != 0:
        raise BrowserSessionError(
            'x11vnc failed to start: ' + (out.stderr or out.stdout or '').strip()[:300])


def _teardown(container):
    try:
        _docker(['rm', '-f', container], timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def start(agent_id, *, sandbox_dir, profile_dir, proxy_url='', cpus='1',
          memory='1g'):
    """Start a session for `agent_id`, or return the running one."""
    slug = _slug(agent_id)
    with _LOCK:
        current = _SESSIONS.get(slug)
        if current and _running(current['container']):
            current['lastUsed'] = time.time()
            return _public(current)
        if current:
            _SESSIONS.pop(slug, None)
        os.makedirs(profile_dir, mode=0o700, exist_ok=True)
        os.makedirs(sandbox_dir, exist_ok=True)
        browser_name = container_name(agent_id)
        viewer_name = viewer_container_name(agent_id)
        _teardown(viewer_name)
        _teardown(browser_name)
        port = _free_port()
        password = secrets.token_urlsafe(6)[:8]
        # Shared VNC-only network, created once. Not internal-only publishing
        # problem here: the viewer also joins the default bridge for its port.
        try:
            _docker(['network', 'create', '--internal', SESSION_VIEW_NETWORK],
                    timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

        def _must_attach(container, network):
            joined = _docker(['network', 'connect', network, container], timeout=30)
            if joined.returncode != 0:
                raise BrowserSessionError(
                    f'network attach failed ({network}): '
                    + (joined.stderr or joined.stdout or '').strip()[:300])

        try:
            result = _docker(
                _build_run_command(browser_name, password, profile_dir,
                                   sandbox_dir, proxy_url, cpus, memory),
                timeout=60)
            if result.returncode != 0:
                raise BrowserSessionError(
                    'docker run failed: '
                    + (result.stderr or result.stdout or '').strip()[:400])
            _must_attach(browser_name, SESSION_VIEW_NETWORK)
            viewer = _docker(_build_viewer_command(viewer_name, browser_name, port),
                             timeout=60)
            if viewer.returncode != 0:
                raise BrowserSessionError(
                    'viewer failed: '
                    + (viewer.stderr or viewer.stdout or '').strip()[:300])
            _must_attach(viewer_name, SESSION_VIEW_NETWORK)
            # x11vnc starts last: it binds the browser's view address and
            # accepts ONLY the viewer container's address, so even a stray
            # listener on another interface is not usable.
            _start_x11vnc(browser_name,
                          _container_ip(browser_name, SESSION_VIEW_NETWORK),
                          allow_ip=_container_ip(viewer_name, SESSION_VIEW_NETWORK))
        except (OSError, subprocess.SubprocessError) as e:
            _teardown(viewer_name)
            _teardown(browser_name)
            raise BrowserSessionError(f'could not start browser session: {e}')
        except Exception:
            _teardown(viewer_name)
            _teardown(browser_name)
            raise
        session = {
            'agentId': slug,
            'container': browser_name,
            'viewerContainer': viewer_name,
            'containerId': (result.stdout or '').strip()[:64],
            'port': port,
            'password': password,
            'token': secrets.token_urlsafe(24),
            'status': 'starting',
            'takeover': False,
            'startedAt': time.time(),
            'lastUsed': time.time(),
            'viewerUrl': viewer_url(port, password),
            'sandboxDir': sandbox_dir,
        }
        _SESSIONS[slug] = session
        return _public(session)


def _reap_if_idle(slug, session):
    if session['takeover'] or session['status'] != 'running':
        return
    if SESSION_IDLE_STOP_S <= 0:
        return
    if time.time() - session['lastUsed'] < SESSION_IDLE_STOP_S:
        return
    _teardown(session.get('viewerContainer') or '')
    _teardown(session['container'])
    _SESSIONS.pop(slug, None)


def get(agent_id, *, touch=True):
    """Return the live public session for `agent_id`, or None. Marks a
    container that died on its own as stopped, and reaps idle ones."""
    slug = _slug(agent_id)
    with _LOCK:
        session = _SESSIONS.get(slug)
        if not session:
            return None
        if (session['status'] in ('running', 'takeover', 'starting')
                and not _running(session['container'])):
            session['status'] = 'stopped'
            session['takeover'] = False
        _reap_if_idle(slug, session)
        session = _SESSIONS.get(slug)
        if not session:
            return None
        if session['status'] == 'starting':
            session['status'] = 'running'
        if touch:
            session['lastUsed'] = time.time()
        return _public(session)


def list_sessions():
    with _LOCK:
        return [s for s in (get(a) for a in list(_SESSIONS)) if s]


def stop(agent_id):
    slug = _slug(agent_id)
    with _LOCK:
        session = _SESSIONS.pop(slug, None)
    if not session:
        return False
    _teardown(session.get('viewerContainer') or '')
    _teardown(session['container'])
    return True


def takeover(agent_id):
    slug = _slug(agent_id)
    with _LOCK:
        session = _SESSIONS.get(slug)
        if not session or not _running(session['container']):
            raise BrowserSessionError('no running browser session for that agent')
        session['takeover'] = True
        session['status'] = 'takeover'
        session['lastUsed'] = time.time()
        return _public(session)


def handback(agent_id):
    slug = _slug(agent_id)
    with _LOCK:
        session = _SESSIONS.get(slug)
        if not session:
            raise BrowserSessionError('no browser session for that agent')
        session['takeover'] = False
        session['status'] = 'running'
        session['lastUsed'] = time.time()
        return _public(session)


def session_by_token(token):
    """Look up a session by its viewer token. Touches lastUsed: viewing keeps
    the session alive. Returns the fields the proxy needs, or None."""
    token = str(token or '')
    if not token:
        return None
    with _LOCK:
        for session in _SESSIONS.values():
            if secrets.compare_digest(session['token'], token):
                session['lastUsed'] = time.time()
                return {
                    'agentId': session['agentId'],
                    'port': session['port'],
                    'password': session['password'],
                    'status': session['status'],
                    'takeover': session['takeover'],
                    'token': session['token'],
                }
    return None


def session_sandbox_dir(agent_id):
    """Host path the session mounts at /workspace, or None."""
    with _LOCK:
        session = _SESSIONS.get(_slug(agent_id))
        return session.get('sandboxDir') if session else None


def is_waiting_for_operator(agent_id):
    """True while a human holds the session. Agent ops should pause, not race
    the pointer."""
    with _LOCK:
        session = _SESSIONS.get(_slug(agent_id))
        return bool(session and session['takeover'])


def exec_driver(agent_id, action, timeout_s=180):
    """Run one driver op inside the browser container against its Chromium.

    The action document goes over stdin (`docker exec -i`), never to a file:
    the session mount is writable, so a credential-bearing fill must not sit
    on disk where another process could read it. The driver writes its result
    to `action['resultPath']`, which the endpoint set under the session's own
    /workspace mount."""
    import json as _json
    name = container_name(agent_id)
    try:
        return _docker([
            'exec', '-i', '-e', f'BROWSER_CDP_URL={SESSION_CDP_URL}',
            name, 'python3', '/opt/browser_driver.py',
        ], timeout=timeout_s, input_text=_json.dumps(action))
    except (OSError, subprocess.SubprocessError) as e:
        raise BrowserSessionError(f'could not run op in session: {e}')
