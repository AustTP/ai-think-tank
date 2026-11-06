"""Write (and optionally load) the launchd plist for a village process.

Per-village launchd (operator request, 2026-11-06): a village worker
survives host reboots exactly like the parent does. One plist per
village, label com.austtp.ai-think-tank-village-<id>, KeepAlive so a
crashed village restarts on its own.

  python3 tools/village_plist.py --id north \
      --db postgresql://poole86@/think_tank_north --port 8938 \
      --parent-url http://100.96.104.61:8937 \
      --seed-roster 'ven|Venus|...|Director|mid,...' \
      [--install]

--install writes to ~/Library/LaunchAgents and bootstraps the service
(launchctl bootstrap gui/UID ...; bootout first if already loaded).
Env keys THINK_TANK_* are written into the plist's EnvironmentVariables
so the village's identity graph (SEED_ROSTER override, DIRECTOR_MAP) is
part of its launch definition, not ambient shell state.
"""
import argparse
import os
import plistlib
import subprocess
import sys

HOME = os.path.expanduser('~')
WORLD_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'world')


def build_plist(village_id, db_url, port, host, parent_url, token,
                seed_roster, seed_director_map, seed_admin_ids):
    env = {
        'PATH': '/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin:'
                '/usr/sbin:/sbin',
        'PYTHONUNBUFFERED': '1',
        'THINK_TANK_DB_URL': db_url,
        'THINK_TANK_VILLAGE': village_id,
        'THINK_TANK_PARENT_URL': parent_url,
        'THINK_TANK_PARENT_TOKEN': token,
    }
    if seed_roster:
        env['THINK_TANK_SEED_ROSTER'] = seed_roster
    if seed_director_map:
        env['THINK_TANK_SEED_DIRECTOR_MAP'] = seed_director_map
    if seed_admin_ids is not None:
        env['THINK_TANK_SEED_ADMIN_IDS'] = seed_admin_ids
    return {
        'Label': f'com.austtp.ai-think-tank-village-{village_id}',
        'ProgramArguments': [
            '/opt/anaconda3/bin/python3', 'serve.py', str(port),
            f'--host={host}', '--max-idle-minutes=0',
        ],
        'WorkingDirectory': WORLD_DIR,
        'EnvironmentVariables': env,
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'ProcessType': 'Interactive',
        'StandardOutPath': f'{HOME}/Library/Logs/village-{village_id}.out.log',
        'StandardErrorPath': f'{HOME}/Library/Logs/village-{village_id}.err.log',
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--host', default='100.96.104.61')
    ap.add_argument('--parent-url', default='http://100.96.104.61:8937')
    ap.add_argument('--seed-roster', default='')
    ap.add_argument('--seed-director-map', default='')
    ap.add_argument('--seed-admin-ids', default='')
    ap.add_argument('--token', default=None,
                    help='parent token; defaults to the value in .env')
    ap.add_argument('--install', action='store_true')
    args = ap.parse_args()
    token = args.token
    if not token:
        env_path = os.path.join(os.path.dirname(WORLD_DIR), '.env')
        if os.path.exists(env_path):
            for line in open(env_path, encoding='utf-8'):
                if line.startswith('THINK_TANK_PARENT_TOKEN='):
                    token = line.split('=', 1)[1].strip()
    if not token:
        raise SystemExit('no THINK_TANK_PARENT_TOKEN found (pass --token)')
    plist = build_plist(args.id, args.db, args.port, args.host,
                        args.parent_url, token, args.seed_roster,
                        args.seed_director_map, args.seed_admin_ids)
    path = os.path.join(HOME, 'Library', 'LaunchAgents',
                        plist['Label'] + '.plist')
    with open(path, 'wb') as fh:
        plistlib.dump(plist, fh)
    print(f'wrote {path}')
    if args.install:
        uid = os.getuid()
        label = plist['Label']
        subprocess.run(['launchctl', 'bootout', f'gui/{uid}/{label}'],
                       capture_output=True)
        r = subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', path],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stderr.strip() or 'bootstrap failed', file=sys.stderr)
            raise SystemExit(r.returncode)
        print(f'loaded {label} (gui/{uid}); logs in ~/Library/Logs/')
    else:
        print(f'load with: launchctl bootstrap gui/$(id -u) {path}')


if __name__ == '__main__':
    main()
