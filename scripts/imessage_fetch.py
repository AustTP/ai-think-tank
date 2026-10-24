#!/usr/bin/env python3
"""On-demand iMessage fetcher -> POST /api/sms/inbound.

The think tank's SMS inbox is receive-only: texts enter via the shared-secret
webhook and are read through /api/sms/grant. This script is the other half on
a Mac: it reads NEW incoming iMessages out of Messages.app's chat.db and
forwards each one to the webhook. It is meant to run on demand -- right after
an action that will produce a code by text -- not as a poller:

    python3 scripts/imessage_fetch.py                 # one sweep
    python3 scripts/imessage_fetch.py --wait 90       # block until one arrives
    python3 scripts/imessage_fetch.py --dry-run       # show what would send

Why chat.db and not AppleScript: macOS 14's Messages.sdef no longer exposes a
`messages` element on chat, so AppleScript cannot read bodies at all. Reading
chat.db requires Full Disk Access for whatever runs this script (System
Settings -> Privacy & Security -> Full Disk Access -> add Terminal or the
python3 binary). The db is opened read-only; nothing here writes to it.

First run sets the watermark to "now" and forwards nothing, so existing
history never floods the inbox. State lives in .secret_keys/imessage_state.json
(gitignored). Env (repo-root .env, same parser as serve.py): SMS_FORWARD_SECRET
(required), IMESSAGE_FORWARD_URL (default: the local think-tank server),
IMESSAGE_CHAT_DB / IMESSAGE_STATE_FILE (test overrides).
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.expanduser('~/Library/Messages/chat.db')
DEFAULT_STATE = os.path.join(ROOT, '.secret_keys', 'imessage_state.json')
DEFAULT_URL = 'http://100.96.104.61:8937/api/sms/inbound'
APPLE_EPOCH_OFFSET = 978307200  # unix seconds of 2001-01-01 00:00:00 UTC
_MAX_SENT_GUIDS = 200
_SWEEP_LIMIT = 50


def _load_env():
    env = {}
    env_path = os.path.join(ROOT, '.env')
    if os.path.exists(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k] = v
    return env


def _apple_now():
    """Current time as an Apple-epoch float (seconds since 2001), matching
    chat.db's message.date units (modern macOS stores nanoseconds; the
    comparison in _fetch_new normalizes both sides)."""
    return time.time() - APPLE_EPOCH_OFFSET


def _to_unix(apple_date):
    """chat.db message.date: seconds (older macOS) or nanoseconds (newer)
    since 2001-01-01. Returns unix seconds as float."""
    v = float(apple_date)
    if v > 1e12:  # nanoseconds
        v /= 1e9
    return v + APPLE_EPOCH_OFFSET


def _norm_apple(apple_date):
    """Normalize a raw message.date to seconds-since-2001 for comparison
    against the stored watermark."""
    v = float(apple_date)
    return v / 1e9 if v > 1e12 else v


def _load_state(path):
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
            if isinstance(state.get('appleWatermark'), (int, float)):
                state.setdefault('sentGuids', [])
                return state
        except (OSError, ValueError):
            pass
    return None


def _save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def _db_date_scale(conn):
    """chat.db message.date is seconds (older macOS) or nanoseconds (Big Sur+)
    since 2001. Probed from the data: values above 1e12 cannot be seconds
    (that is the year 33000+). Returns the multiplier from our seconds-based
    watermark to the db's native unit."""
    row = conn.execute('SELECT max(date) FROM message').fetchone()
    if row and row[0] is not None and float(row[0]) > 1e12:
        return 1e9
    return 1.0


def _fetch_new(db_path, watermark, sent_guids, include_sms):
    """Read incoming iMessages newer than the watermark. Returns a list of
    (guid, apple_date, sender, text) oldest-first. Read-only; raises
    sqlite3.Error with the original message on FDA denial."""
    services = ('iMessage', 'SMS') if include_sms else ('iMessage',)
    placeholders = ','.join('?' * len(services))
    # message.service carries the transport directly ('iMessage' / 'SMS'), so
    # no chat join is needed; handle gives the sender address.
    sql = f'''
        SELECT m.guid, m.date, h.id AS sender, m.text
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.is_from_me = 0
          AND m.text IS NOT NULL AND trim(m.text) != ''
          AND m.service IN ({placeholders})
          AND m.date > ?
        ORDER BY m.date ASC
        LIMIT ?
    '''
    uri = f'file:{os.path.abspath(db_path)}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        native_watermark = watermark * _db_date_scale(conn)
        rows = conn.execute(sql, (*services, native_watermark, _SWEEP_LIMIT)).fetchall()
    finally:
        conn.close()
    out, seen = [], set(sent_guids)
    for guid, raw_date, sender, text in rows:
        if guid in seen:
            continue
        seen.add(guid)
        out.append((guid, _norm_apple(raw_date), (sender or '').strip(), text.strip()))
    return out


def _forward(url, secret, sender, text):
    """POST one message to the webhook. Returns the server JSON dict."""
    payload = json.dumps({'from': sender, 'body': text}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, method='POST', headers={
        'Content-Type': 'application/json',
        'X-SMS-Forward-Secret': secret,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:200]
        raise RuntimeError(f'webhook returned {e.code}: {detail}') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'webhook unreachable at {url}: {e.reason}') from None


def _sweep(db_path, state, url, secret, include_sms, dry_run):
    """One check-and-forward pass. Returns how many messages were forwarded
    (dry runs count as 0). Advances the watermark only past messages the
    webhook accepted, so a server hiccup retries on the next sweep."""
    msgs = _fetch_new(db_path, state['appleWatermark'], state['sentGuids'], include_sms)
    forwarded = 0
    for guid, apple_date, sender, text in msgs:
        if dry_run:
            print(f'[imessage] dry-run: from {sender} ({len(text)} chars) '
                  f'at {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_to_unix(apple_date)))}')
            continue
        result = _forward(url, secret, sender, text)
        forwarded += 1
        state['sentGuids'].append(guid)
        state['sentGuids'] = state['sentGuids'][-_MAX_SENT_GUIDS:]
        state['appleWatermark'] = max(state['appleWatermark'], apple_date)
        _save_state(STATE_PATH, state)
        print(f'[imessage] forwarded from {sender} ({len(text)} chars) -> message #{result.get("messageId")}')
    return forwarded


STATE_PATH = DEFAULT_STATE  # rebound in main(); module attr so tests can patch


def main(argv=None):
    import argparse
    global STATE_PATH
    p = argparse.ArgumentParser(description='Forward new iMessages to /api/sms/inbound (on demand).')
    p.add_argument('--wait', type=int, default=0, metavar='SEC',
                   help='poll every 4s until a new message is forwarded or SEC elapses')
    p.add_argument('--dry-run', action='store_true', help='list new messages without forwarding')
    p.add_argument('--include-sms', action='store_true',
                   help='also forward SMS texts relayed from iPhone (default: iMessage only)')
    args = p.parse_args(argv)

    env = _load_env()
    secret = (env.get('SMS_FORWARD_SECRET') or '').strip()
    if not secret:
        print('SMS_FORWARD_SECRET is not set in .env -- the webhook would refuse anyway.', file=sys.stderr)
        return 2
    db_path = os.environ.get('IMESSAGE_CHAT_DB') or env.get('IMESSAGE_CHAT_DB') or DEFAULT_DB
    STATE_PATH = os.environ.get('IMESSAGE_STATE_FILE') or env.get('IMESSAGE_STATE_FILE') or DEFAULT_STATE
    url = env.get('IMESSAGE_FORWARD_URL') or DEFAULT_URL

    if not os.path.exists(db_path):
        print(f'{db_path} not found -- this script only runs on the Mac where Messages.app lives.',
              file=sys.stderr)
        return 2

    state = _load_state(STATE_PATH)
    if state is None:
        state = {'appleWatermark': _apple_now(), 'sentGuids': []}
        _save_state(STATE_PATH, state)
        print('[imessage] first run: watermark set to now; existing history is NOT forwarded. '
              'Rerun after the expected text arrives.', flush=True)

    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        try:
            n = _sweep(db_path, state, url, secret, args.include_sms, args.dry_run)
        except sqlite3.OperationalError as e:
            print(f'cannot read {db_path}: {e}\n'
                  f'Grant Full Disk Access to the app running this script: '
                  f'System Settings -> Privacy & Security -> Full Disk Access.', file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f'{e}', file=sys.stderr)
            return 2
        if args.dry_run:
            return 0
        if n > 0:
            return 0
        if time.monotonic() >= deadline:
            if args.wait:
                print(f'[imessage] no new message within {args.wait}s', file=sys.stderr)
            return 1
        time.sleep(4)


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
