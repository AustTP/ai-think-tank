"""Create a new village process: its own DB, its own roster seed.

The operator's architecture (2026-11-06): one parent DB/process for the
org (chief of staff, regional VP, bank, registry, operator console) and
one DB + process per village. This tool performs the creation side:

  1. initializes the village's OWN database (full schema via serve.init_db
     in a subprocess with THINK_TANK_DB_URL pointed at the new DSN);
  2. registers the village in the parent's registry (id, name, api base);
  3. prints the exact launch command for the village process.

Nothing is migrated: a new village starts EMPTY and hires/grows inside
its own process (its own .env SEED_ROSTER decides who exists there).
Moving EXISTING teams between villages is deliberate surgery, not this
tool's job.

Usage:
  python3 tools/village_new.py --id north --name "North Village" \
      --dsn postgresql://.../think_tank_north --api-base http://127.0.0.1:8938
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _init_village_db(dsn):
    env = dict(os.environ)
    env['THINK_TANK_DB_URL'] = dsn
    code = (
        'import sys; sys.path.insert(0, %r); '
        'import serve; serve.init_db(); print("schema ok")' % os.path.join(
            ROOT, 'world'))
    out = subprocess.run([sys.executable, '-c', code], env=env,
                         capture_output=True, text=True, cwd=ROOT,
                         timeout=120)
    if 'schema ok' not in out.stdout:
        raise SystemExit(f'village DB init failed:\n{out.stdout}\n{out.stderr}')
    return out.stdout.strip()


def _register_in_parent(parent_url, token, vid, name, api_base):
    body = json.dumps({'id': vid, 'name': name, 'apiBase': api_base}).encode()
    req = urllib.request.Request(
        f'{parent_url}/api/villages/registry', data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'X-Parent-Token': token,
                 'Cookie': os.environ.get('THINK_TANK_SESSION_COOKIE', '')})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f'[warn] parent registration failed ({exc}); the village '
              'process still runs, register it later with the same call.')
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--dsn', required=True,
                    help='e.g. postgresql://user:pass@host/db_name')
    ap.add_argument('--api-base', required=True,
                    help='the village process URL, e.g. http://127.0.0.1:8938')
    ap.add_argument('--parent-url',
                    default=os.environ.get('THINK_TANK_PARENT_URL',
                                           'http://100.96.104.61:8937'))
    args = ap.parse_args()
    print(f'[1/3] initializing DB {args.dsn.rsplit("/", 1)[-1]} ...')
    _init_village_db(args.dsn)
    print('[2/3] registering in the parent registry ...')
    token = ''
    env_path = os.path.join(ROOT, '.env')
    if os.path.exists(env_path):
        for line in open(env_path, encoding='utf-8'):
            if line.startswith('THINK_TANK_PARENT_TOKEN='):
                token = line.split('=', 1)[1].strip()
    result = _register_in_parent(args.parent_url, token,
                                 args.id, args.name, args.api_base)
    print(json.dumps(result) if result else '(not registered)')
    print('[3/3] launch the village process with:\n')
    print(f'  cd {ROOT}/world && THINK_TANK_DB_URL={args.dsn} '
          f'THINK_TANK_VILLAGE={args.id} '
          f'THINK_TANK_PARENT_URL={args.parent_url} '
          f'THINK_TANK_PARENT_TOKEN=<token> '
          f'{sys.executable} serve.py <port> --host=<ip> '
          f'--max-idle-minutes=0\n')
    print('Reminder: the village .env needs its own SEED_ROSTER entries '
          'before first boot, or the village starts empty by design.')
    print(f'[done] {time.strftime("%Y-%m-%d %H:%M:%S")}')


if __name__ == '__main__':
    main()
