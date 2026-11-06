"""Move an EXISTING team from the parent into a village (the CLI).

The surgery itself lives in the server (196_village_move.py): the PARENT
process collects the team + its workers' rows and open cards, the
DESTINATION VILLAGE process inserts them through its own token-authenticated
move-in endpoint, and only then does the parent remove its side -- each
process mutates only its own state, so a running sim can never clobber
the move. This CLI is a thin caller with a dry-run that inspects the
plan without touching anything.

  python3 tools/village_move_team.py --team eli --village north [--dry-run]
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parent_url():
    return os.environ.get('THINK_TANK_PARENT_URL', 'http://100.96.104.61:8937')


def _token():
    for line in open(os.path.join(ROOT, '.env'), encoding='utf-8'):
        if line.startswith('THINK_TANK_PARENT_TOKEN='):
            return line.split('=', 1)[1].strip()
    return ''


def _admin_key():
    for line in open(os.path.join(ROOT, '.env'), encoding='utf-8'):
        if line.startswith('THINK_TANK_ADMIN_KEY='):
            return line.split('=', 1)[1].strip()
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--team', required=True)
    ap.add_argument('--village', required=True)
    ap.add_argument('--parent-url', default=None)
    ap.add_argument('--dry-run', action='store_true',
                    help='inspect the plan (HTTP GET) without moving')
    args = ap.parse_args()
    base = args.parent_url or _parent_url()
    headers = {'Content-Type': 'application/json',
               'X-Parent-Token': _token()}
    if args.dry_run:
        # The dry-run reuses the plan collection via a query flag.
        url = f'{base}/api/admin/move-team-to-village'
        body = json.dumps({'teamId': args.team, 'villageId': args.village,
                           'dryRun': True}).encode()
        req = urllib.request.Request(url, headers=headers, method='POST',
                                     data=body)
        with urllib.request.urlopen(req, timeout=30) as resp:
            plan = json.loads(resp.read())
        print(json.dumps(plan, indent=2)[:5000])
        return
    body = json.dumps({'teamId': args.team, 'villageId': args.village}).encode()
    req = urllib.request.Request(
        f'{base}/api/admin/move-team-to-village', data=body, method='POST',
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode()[:300], file=sys.stderr)
        raise SystemExit(exc.code)
    print(json.dumps(result, indent=2))
    print(f"\n[done] team {args.team} now lives in village {args.village}.")


if __name__ == '__main__':
    main()
