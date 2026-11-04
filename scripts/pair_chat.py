#!/usr/bin/env python3
"""Pairing CLI: sit down with Theo in a live terminal conversation.

Usage:
    python scripts/pair_chat.py [--url URL] [--key PAIR_KEY] [--thread ID]

The key must match PAIR_KEY in the server's .env (browser sessions also work
if you log in first and pass the cookie, but the key path is the CLI path).
Type a line, get Theo's reply. When Theo hands work off to the coordinator
(ruth), the CLI prints the handoff line explicitly so you can see what was
passed on. Ctrl-C or an empty line exits.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_URL = os.environ.get('SELF_BASE_URL', 'http://127.0.0.1:8936')


def post(url, key, thread_id, text, timeout=180):
    payload = {'text': text}
    if thread_id:
        payload['threadId'] = thread_id
    req = urllib.request.Request(
        url.rstrip('/') + '/api/pair/message',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'X-Pair-Key': key},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, {'error': f'HTTP {e.code}'}
    except Exception as e:
        return 0, {'error': str(e)}


def main():
    ap = argparse.ArgumentParser(description='Pairing CLI for Theo.')
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--key', default=os.environ.get('PAIR_KEY'),
                    help='must equal PAIR_KEY in the server .env')
    ap.add_argument('--thread', default=None,
                    help='resume an existing thread id (default: new thread)')
    args = ap.parse_args()
    if not args.key:
        print('error: no --key and no PAIR_KEY in the environment; '
              'set PAIR_KEY in .env first.', file=sys.stderr)
        return 1

    thread_id = args.thread
    if thread_id:
        req = urllib.request.Request(
            args.url.rstrip('/') + '/api/pair/history?threadId=' + thread_id,
            headers={'X-Pair-Key': args.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                hist = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print('error: cannot reach thread: %s' % e, file=sys.stderr)
            return 1
        for m in (hist.get('messages') or [])[-6:]:
            who = 'you' if m['role'] == 'player' else 'theo'
            print(f'[{who}] {m["text"]}')
        if hist.get('messages'):
            print('--- resumed above history ---')
    else:
        thread_id = None
    print(f'pairing with theo (thread={thread_id or "new"}), '
          'empty line or Ctrl-C to exit.')

    while True:
        try:
            line = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        status, data = post(args.url, args.key, thread_id, line)
        if status != 200 or not data.get('ok'):
            print('server> ' + json.dumps(data), file=sys.stderr)
            continue
        thread_id = data.get('threadId', thread_id)
        reply = (data.get('reply') or '').strip()
        if reply:
            print(f'theo> {reply}')
        handoff = data.get('handoff')
        if handoff:
            if handoff.get('mailed'):
                print(f"  -> passed to {handoff['to']}: "
                      f"{handoff['request']}")
            else:
                print(f"  -> HANDOFF FAILED ({handoff.get('error')}): "
                      f"{handoff['request']}", file=sys.stderr)
                print('     (re-sending manually may be needed; the request '
                      'is also kept in the thread.)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
