#!/usr/bin/env python3
"""One-time interactive browser login for a tank agent profile.

The browser lane gives every agent a persistent Chromium profile
(BROWSER_PROFILE_DIR/<agent>) whose cookies survive across browser_act calls,
but an agent cannot type a password: interactive logins (Google, a site's own
form) need the OPERATOR to do the typing once. This tool starts a throwaway
container running the SAME Playwright Chromium build as the browser lane with
that agent's profile mounted, exposes it over noVNC on the tailnet, and the
operator logs in with their own hands. The session (cookies/localStorage)
stays in the profile; the container is torn down and the lane reuses it.

    python3 world/tools/agent_browser_login.py start --agent ben
    python3 world/tools/agent_browser_login.py status
    python3 world/tools/agent_browser_login.py stop --agent ben

The login container runs on the default docker bridge (direct internet, no
egress allowlist) -- it is an operator session, not an agent session -- and
binds noVNC to the tailnet IP only.
"""
import argparse
import os
import secrets
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The browser lane's BROWSER_PROFILE_DIR default is ROOT(world)/.browser-profiles/social
# (serve's ROOT is world/, not the repo root). Mount the SAME tree or the
# operator's login lands in an orphaned profile the lane never reads.
PROFILE_ROOT = os.path.join(ROOT, 'world', '.browser-profiles', 'social')
# Session vault (2026-11-19): operator-deposited per-service browser
# sessions. --service mounts here instead of an agent profile; agents get a
# fresh COPY only while holding a live session grant, so a sign-in here is
# need-based access, never standing access.
SESSION_ROOT = os.path.join(ROOT, 'world', '.browser-sessions')
IMAGE = 'ai-think-tank-work-sandbox:latest'
CONTAINER_PREFIX = 'btank-login-'
DEFAULT_URL = 'https://accounts.google.com'
NOVNC_PORT = 6080


def _slug(agent_id):
    import re
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(agent_id or 'shared'))[:64] or 'shared'


def _tailscale_ip():
    try:
        out = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True,
                             text=True, timeout=10).stdout.strip().splitlines()
        for line in out:
            if line.strip():
                return line.strip()
    except Exception:
        pass
    return None


def _bind_ip(explicit):
    if explicit:
        return explicit
    ts = _tailscale_ip()
    if ts:
        return ts
    print('warning: no tailscale IP found; binding to 127.0.0.1', file=sys.stderr)
    return '127.0.0.1'


def start(args):
    slug = _slug(args.service or args.agent)
    name = CONTAINER_PREFIX + slug
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    profile = (os.path.join(SESSION_ROOT, _slug(args.service)) if args.service
               else os.path.join(PROFILE_ROOT, slug))
    os.makedirs(profile, mode=0o700, exist_ok=True)
    os.chmod(profile, 0o700)
    bind = _bind_ip(args.bind)
    password = secrets.token_urlsafe(9)
    helper = os.path.join(ROOT, 'world', 'tools', 'login_browser_session.py')
    cmd = [
        'docker', 'run', '-d', '--rm', '--name', name,
        '-p', f'{bind}:{args.port}:{NOVNC_PORT}',
        '-v', f'{profile}:/browser-profile',
        '-v', f'{helper}:/opt/login_browser_session.py:ro',
        '-e', f'VNC_PW={password}',
        '-e', f'LOGIN_URL={args.url}',
        '-e', 'HOME=/browser-profile',
        '--shm-size', '256m',
        '--user', f'{os.getuid()}:{os.getgid()}',
        IMAGE,
        'sh', '-c',
        'Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 & '
        'export DISPLAY=:99; '
        'for i in $(seq 1 25); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.2; done; '
        'x11vnc -display :99 -forever -passwd "$VNC_PW" -quiet >/tmp/x11vnc.log 2>&1 & '
        'x11vncpid=$!; '
        f'timeout {args.max_minutes * 60} python3 /opt/login_browser_session.py '
        '>/tmp/login.log 2>&1 & '
        'websockify --web /usr/share/novnc 0.0.0.0:%d localhost:5900 '
        '>/tmp/websockify.log 2>&1 & wait' % NOVNC_PORT,
    ]
    cid = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if cid.returncode != 0:
        print('docker run failed:', cid.stderr.strip(), file=sys.stderr)
        return 1
    what = f'service session {args.service}' if args.service else f'agent {args.agent} (profile {profile})'
    url = f'http://{bind}:{args.port}/vnc.html?autoconnect=1'
    print(f'login session for {what}')
    print(f'  open:  {url}')
    print(f'  vnc password: {password}')
    print(f'  start page: {args.url}')
    print(f'  container: {name} (auto-removed; stops after {args.max_minutes} min)')
    if args.service:
        print(f'  when done: agent_browser_login.py stop --service {args.service}')
        print(f'  agents then request access: request_credential_access(service={args.service!r})')
    else:
        print('  when the login is done: agent_browser_login.py stop --agent '
              + args.agent)
    return 0


def status(_args):
    out = subprocess.run(['docker', 'ps', '--format',
                          '{{.Names}}\t{{.Status}}\t{{.Ports}}'],
                         capture_output=True, text=True).stdout
    rows = [line for line in out.splitlines()
            if line.startswith(CONTAINER_PREFIX)]
    print('\n'.join(rows) if rows else 'no login sessions running')
    return 0


def stop(args):
    target = args.service or args.agent
    name = CONTAINER_PREFIX + _slug(target)
    r = subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
                       text=True)
    print('stopped' if r.returncode == 0 else f'not running: {name}')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_start = sub.add_parser('start')
    p_start.add_argument('--agent', help='log into this AGENT\'s standing profile (standing access)')
    p_start.add_argument('--service', help='log into a vaulted SERVICE session (need-based access)')
    p_start.add_argument('--url', default=DEFAULT_URL)
    p_start.add_argument('--port', type=int, default=6080)
    p_start.add_argument('--bind', default=None)
    p_start.add_argument('--max-minutes', type=int, default=30)
    p_start.set_defaults(fn=start)
    p_status = sub.add_parser('status')
    p_status.set_defaults(fn=status)
    p_stop = sub.add_parser('stop')
    p_stop.add_argument('--agent')
    p_stop.add_argument('--service')
    p_stop.set_defaults(fn=stop)
    args = ap.parse_args()
    if args.cmd == 'start' and not args.agent and not args.service:
        ap.error('start requires --agent <id> or --service <name>')
    if args.cmd == 'stop' and not args.agent and not args.service:
        ap.error('stop requires --agent <id> or --service <name>')
    if args.cmd == 'start' and args.agent and args.service:
        ap.error('choose ONE of --agent or --service')
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
