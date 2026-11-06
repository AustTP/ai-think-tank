"""Move an EXISTING team from the parent into a village -- the HARDENED way.

Lesson of the first real move (2026-11-06): an in-process move-in can be
silently reverted, because a village process's queued saves race its sim's
tick saves (last writer wins). So the surgery happens while the
destination village is STOPPED:

  1. collect the plan from the parent (no mutation)
  2. stop the destination village (launchd bootout)
  3. write the plan straight into the village DB (psycopg2)
  4. start the village again (launchd bootstrap) and wait for its port
  5. commit the parent-side removal through the parent's API

Refusals mirror retire-team: unknown team, an ACTIVE SPRINT, no workers,
unknown village. --dry-run prints the plan and touches nothing.

  python3 tools/village_move_team.py --team eli --village north \
      --village-dsn postgresql://poole86@/think_tank_north [--dry-run]
"""
import argparse
import json
import os
import socket
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = 'com.austtp.ai-think-tank-village-{id}'


def _env(key, default=''):
    v = os.environ.get(key)
    if v:
        return v
    for line in open(os.path.join(ROOT, '.env'), encoding='utf-8'):
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip()
    return default


def _parent_url():
    return os.environ.get('THINK_TANK_PARENT_URL', 'http://100.96.104.61:8937')


def _post(path, payload, timeout=60):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'{_parent_url()}{path}', data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'X-Parent-Token': _env('THINK_TANK_PARENT_TOKEN'),
                 'X-Agent-Key': _env('THINK_TANK_ADMIN_KEY')})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _village_running(village_id):
    r = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/'
                        f'{LABEL.format(id=village_id)}'],
                       capture_output=True, text=True)
    if 'state = running' not in r.stdout:
        return None
    import plistlib
    path = os.path.join(os.path.expanduser('~'), 'Library', 'LaunchAgents',
                        LABEL.format(id=village_id) + '.plist')
    with open(path, 'rb') as fh:
        plist = plistlib.load(fh)
    port = int(plist['ProgramArguments'][2])
    # Villages bind their Tailscale IP, not loopback -- probe the host the
    # village actually serves on, or every stop/start wait lies.
    host = ((plist.get('EnvironmentVariables') or {})
            .get('THINK_TANK_HOST') or '127.0.0.1')
    return port, host


def _stop_village(village_id):
    info = _village_running(village_id)
    if not info:
        raise SystemExit(f'village {village_id} is not a running launchd '
                         'service; start it with village_plist.py first')
    port, host = info
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/'
                    f'{LABEL.format(id=village_id)}'], capture_output=True)
    deadline = time.time() + 30
    while time.time() < deadline and _port_open(host, port):
        time.sleep(1)
    if _port_open(host, port):
        raise SystemExit(f'port {port} still listening after bootout')
    print(f'      village stopped (port {port} free)')
    return port, host


def _start_village(village_id, port, host):
    path = os.path.join(os.path.expanduser('~'), 'Library', 'LaunchAgents',
                        LABEL.format(id=village_id) + '.plist')
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', path],
                   capture_output=True)
    deadline = time.time() + 60
    while time.time() < deadline and not _port_open(host, port):
        time.sleep(2)
    if not _port_open(host, port):
        raise SystemExit(f'village did not come back on port {port}')
    print(f'      village restarted (port {port} serving)')


def _insert_into_village(plan, village_dsn):
    """Direct DB surgery on a STOPPED village: the same writes the
    in-process move-in endpoint would do, with no sim to race."""
    import psycopg2
    conn = psycopg2.connect(village_dsn)
    cur = conn.cursor()
    team = plan['team']
    cur.execute('SELECT blob FROM kv_state WHERE id = 1')
    row = cur.fetchone()
    if not row:
        raise SystemExit('village kv_state is empty; seed it first')
    st = json.loads(row[0])
    if any(isinstance(t, dict) and t.get('id') == team['id']
           for t in (st.get('teams') or [])):
        raise SystemExit('team id already exists in the village DB')
    st.setdefault('teams', []).append(team)
    st.setdefault('tasks', {}).update(plan.get('tasks') or {})
    st.setdefault('workQueue', []).extend(plan.get('queue') or [])
    cur.execute('UPDATE kv_state SET blob = %s, updated_at = %s WHERE id = 1',
                (json.dumps(st), time.time()))
    for row in plan.get('rosterRows') or []:
        cur.execute('INSERT INTO roster_agent (id, seq, blob, updated_at)'
                    ' VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO UPDATE'
                    ' SET blob = EXCLUDED.blob, seq = EXCLUDED.seq,'
                    ' updated_at = EXCLUDED.updated_at',
                    (row['id'], row['seq'], row['blob'], row['updatedAt']))
    for row in plan.get('liveRows') or []:
        cur.execute('INSERT INTO live_agent (id, seq, blob, updated_at)'
                    ' VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO UPDATE'
                    ' SET blob = EXCLUDED.blob, seq = EXCLUDED.seq,'
                    ' updated_at = EXCLUDED.updated_at',
                    (row['id'], row['seq'], row['blob'], row['updatedAt']))
    for row in plan.get('keyRows') or []:
        cur.execute('INSERT INTO agent_keys (agent_id, secret_key,'
                    ' created_at) VALUES (%s, %s, %s) ON CONFLICT'
                    ' (agent_id) DO UPDATE SET secret_key ='
                    ' EXCLUDED.secret_key',
                    (row['agentId'], row['secretKey'], row['createdAt']))
    for row in plan.get('deliverableRows') or []:
        cur.execute('INSERT INTO completed_deliverable (id, seq, blob,'
                    ' updated_at) VALUES (%s, %s, %s, %s) ON CONFLICT (id)'
                    ' DO NOTHING', (row[0], row[1], row[2], row[3]))
    for row in plan.get('backlogRows') or []:
        cur.execute('INSERT INTO backlog_request (id, seq, blob,'
                    ' updated_at) VALUES (%s, %s, %s, %s) ON CONFLICT (id)'
                    ' DO NOTHING', (row[0], row[1], row[2], row[3]))
    cur.execute('CREATE TABLE IF NOT EXISTS agent_ltm (id SERIAL PRIMARY'
                ' KEY, agent_id TEXT NOT NULL, mem_type TEXT, content TEXT,'
                ' created_at DOUBLE PRECISION, active INTEGER DEFAULT 1,'
                ' confidence DOUBLE PRECISION, source_type TEXT)')
    for row in plan.get('ltmRows') or []:
        cur.execute('INSERT INTO agent_ltm (agent_id, mem_type, content,'
                    ' created_at, active, confidence, source_type)'
                    ' VALUES (%s, %s, %s, %s, %s, %s, %s)',
                    (row['agentId'], row['memType'], row['content'],
                     row['createdAt'], row['active'], row['confidence'],
                     row['sourceType']))
    conn.commit()
    cur.close()
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--team', required=True)
    ap.add_argument('--village', required=True)
    ap.add_argument('--village-dsn', required=True,
                    help="the village's postgres DSN")
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    print(f'[1/5] collecting the plan for team {args.team} ...')
    plan = _post('/api/admin/move-team-plan',
                 {'teamId': args.team})['plan']
    print(f"      '{plan['team']['name']}' director "
          f"{plan['team']['directorId']}: {len(plan['members'])} workers, "
          f"{len(plan['tasks'])} team cards, "
          f"{len(plan.get('deliverableRows') or [])} deliverables, "
          f"{len(plan.get('ltmRows') or [])} LTM memories")
    if args.dry_run:
        print(json.dumps(plan, indent=2)[:6000])
        print('[dry-run] nothing moved')
        return
    print(f'[2/5] stopping village {args.village} ...')
    port, host = _stop_village(args.village)
    print('[3/5] writing the team into the village DB ...')
    _insert_into_village(plan, args.village_dsn)
    print('[4/5] restarting the village ...')
    _start_village(args.village, port, host)
    print('[5/5] committing the parent-side removal ...')
    result = _post('/api/admin/move-team-commit',
                   {'villageId': args.village, 'plan': plan})
    print(json.dumps({k: result[k] for k in ('ok', 'team', 'village',
                                             'members', 'tasksMoved')}))
    print(f"\n[done] team {args.team} now lives in village {args.village}; "
          f"director {plan['team']['directorId']} stays homed in the "
          'parent (briefs relay through the control plane).')


if __name__ == '__main__':
    main()
