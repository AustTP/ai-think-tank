#!/usr/bin/env python3
"""CRON watcher for bot-protection laws and regulations.

Runs without waking an agent. Collects new official items, dedupes them by URL
in library/research/bot-law-watch/seen.json, and writes a dated brief. When it
finds something new it prints one JSON line with notify=true so the standing
task can ask for judgment; otherwise it stays quiet.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'library', 'research', 'bot-law-watch')
SEEN_PATH = os.path.join(OUT_DIR, 'seen.json')
LATEST_PATH = os.path.join(OUT_DIR, 'latest.md')
QUERIES = [
    'automated decision system regulation',
    'bot disclosure law',
    'algorithmic accountability regulation',
    'robotics liability law',
]
MAX_RESULTS = 8
TIMEOUT_S = 20


def _load_seen():
    try:
        with open(SEEN_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return set(data if isinstance(data, list) else [])
    except (OSError, ValueError):
        return set()


def _save_seen(seen):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = SEEN_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(sorted(seen), fh, indent=2)
    os.replace(tmp, SEEN_PATH)


def _get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'think-tank-bot-law-watch/1.0'})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def _federal_register():
    items = []
    for query in QUERIES:
        params = urllib.parse.urlencode({
            'per_page': MAX_RESULTS,
            'order': 'newest',
            'conditions[term]': query,
        })
        data = _get_json('https://www.federalregister.gov/api/v1/documents.json?' + params)
        for row in data.get('results') or []:
            items.append({
                'title': row.get('title') or '',
                'url': row.get('html_url') or '',
                'date': row.get('publication_date') or '',
                'source': 'Federal Register',
                'summary': (row.get('abstract') or '')[:400],
            })
    return items


def _tavily():
    key = os.environ.get('TAVILY_API_KEY')
    if not key:
        return []
    items = []
    for query in QUERIES:
        body = json.dumps({
            'api_key': key,
            'query': query + ' law regulation official source',
            'search_depth': 'basic',
            'max_results': MAX_RESULTS,
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.tavily.com/search', data=body,
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'think-tank-bot-law-watch/1.0'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        for row in data.get('results') or []:
            items.append({
                'title': row.get('title') or '',
                'url': row.get('url') or '',
                'date': row.get('published_date') or '',
                'source': 'web search',
                'summary': (row.get('content') or '')[:400],
            })
    return items


def main():
    try:
        items = _federal_register()
    except Exception as exc:  # noqa: BLE001 -- a watcher failure is data
        items = []
        print(f'federal register fetch failed: {exc}', file=sys.stderr)
    try:
        items.extend(_tavily())
    except Exception as exc:  # noqa: BLE001 -- one source failing is not fatal
        print(f'tavily fetch failed: {exc}', file=sys.stderr)
    seen = _load_seen()
    fresh = []
    for item in items:
        url = item.get('url') or ''
        if not url or url in seen:
            continue
        seen.add(url)
        fresh.append(item)
    _save_seen(seen)
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if fresh:
        os.makedirs(OUT_DIR, exist_ok=True)
        lines = [f'# Bot-protection law watch, {stamp}', '']
        for item in fresh:
            lines.append(f"- [{item['title']}]({item['url']}) "
                         f"({item['source']}, {item['date']})")
            if item.get('summary'):
                lines.append(f"  {item['summary']}")
        lines.append('')
        with open(LATEST_PATH, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
        summary = (f'{len(fresh)} new bot-protection law/regulation item(s) '
                   f'on {stamp}. Brief: {os.path.relpath(LATEST_PATH, ROOT)}')
        print(json.dumps({'notify': True, 'summary': summary}))
    else:
        print(json.dumps({'notify': False,
                          'summary': f'No new bot-protection items on {stamp}.'}))


if __name__ == '__main__':
    main()
