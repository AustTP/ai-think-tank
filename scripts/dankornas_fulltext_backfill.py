#!/usr/bin/env python3
"""Backfill full tweet text for the dankornas corpus parent tweets.

The original extraction was guest-tier and capped parent_text at 300
chars; the promotion threads carry the tool feature details, so the
triage ran on openings. This backfill fetches each parent tweet's FULL
text through the public fxtwitter mirror (no auth, one request per
tweet, throttled) and writes a sidecar file; the original corpus is
never modified.

Usage: python3 scripts/dankornas_fulltext_backfill.py [--rps 1.0]
Input:  library/analysis/dankornas-promotions-2026-full.json
Output: library/analysis/dankornas-parents-fulltext.json
        {tweet_id: {text, author, likes, created_at, fetched_at}}
Resume: existing entries in the output file are skipped, so re-running
        retries only the misses.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, 'library', 'analysis',
                   'dankornas-promotions-2026-full.json')
OUT = os.path.join(HERE, 'library', 'analysis',
                   'dankornas-parents-fulltext.json')
ENDPOINT = 'https://api.fxtwitter.com/status/{id}'


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def parent_ids(corpus):
    ids = []
    for row in corpus:
        for pair in (row.get('pairs') or []):
            pid = str(pair.get('parent_id') or '')
            if pid and pid not in ids:
                ids.append(pid)
    return ids


def fetch_one(pid, timeout=20):
    url = ENDPOINT.format(id=pid)
    req = urllib.request.Request(url, headers={'User-Agent': 'tank-backfill/1'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    tweet = payload.get('tweet') or {}
    text = tweet.get('text') or ''
    if not text:
        return None
    return {
        'text': text,
        'author': (tweet.get('author') or {}).get('screen_name') or '',
        'likes': tweet.get('likes'),
        'created_at': tweet.get('created_at'),
        'fetched_at': time.time(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rps', type=float, default=1.0,
                    help='max requests per second (default 1.0)')
    args = ap.parse_args()
    corpus = load_json(SRC, [])
    if not corpus:
        print('no corpus at', SRC, file=sys.stderr)
        return 1
    store = load_json(OUT, {})
    ids = parent_ids(corpus)
    todo = [i for i in ids if not (store.get(i) or {}).get('text')]
    print(f'corpus parents: {len(ids)} | already fetched: '
          f'{len(ids) - len(todo)} | to fetch: {len(todo)}', flush=True)
    delay = 1.0 / max(0.1, args.rps)
    ok = miss = 0
    try:
        for i, pid in enumerate(todo):
            try:
                rec = fetch_one(pid)
            except Exception as exc:
                rec = None
                miss += 1
                if miss <= 3 or miss % 50 == 0:
                    print(f'[{i + 1}/{len(todo)}] miss {pid}: {exc}',
                          flush=True)
            if rec:
                store[pid] = rec
                ok += 1
            if (i + 1) % 100 == 0:
                json.dump(store, open(OUT, 'w', encoding='utf-8'))
                print(f'[{i + 1}/{len(todo)}] ok={ok} miss={miss}',
                      flush=True)
            time.sleep(delay)
    except KeyboardInterrupt:
        print('interrupted; progress saved', flush=True)
    json.dump(store, open(OUT, 'w', encoding='utf-8'))
    lens = [len(r['text']) for r in store.values()]
    print(f'done: ok={ok} miss={miss} | stored={len(store)} | '
          f'text chars avg={sum(lens) // max(1, len(lens))} '
          f'max={max(lens) if lens else 0}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
