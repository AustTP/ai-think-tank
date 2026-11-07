#!/usr/bin/env python3
"""Prompt-contract smoke evals for a running think tank.

This is a small behavioral harness, not an outcome benchmark. It sends each
case to /api/chat with an agent key and checks the reply against explicit
patterns. Run it after a model or prompt change.

    python3 evals/run_evals.py --dry-run
    TANK_AGENT_KEY=... python3 evals/run_evals.py --base-url http://127.0.0.1:8936
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases must be a non-empty list')
    for case in cases:
        if not case.get('name') or not case.get('message') or not case.get('expect'):
            raise ValueError(f'invalid case: {case!r}')
        for pattern in case['expect']:
            re.compile(pattern)
    return cases


def ask(base_url: str, agent_key: str, body: dict) -> str:
    request = urllib.request.Request(
        base_url.rstrip('/') + '/api/chat',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-Agent-Key': agent_key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code}: {error.read().decode("utf-8", "replace")}') from error
    reply = payload.get('reply')
    if not isinstance(reply, str):
        raise RuntimeError(f'response has no reply: {payload!r}')
    return reply


def ask(base_url: str, agent_key: str, body: dict) -> tuple[str, float | None]:
    """Return (reply, reported_cost_usd). The cost rides the OpenRouter
    usage payload when the server priced the call; None when unpriced or
    when the server predates includeUsage."""
    request = urllib.request.Request(
        base_url.rstrip('/') + '/api/chat',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-Agent-Key': agent_key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code}: {error.read().decode("utf-8", "replace")}') from error
    reply = payload.get('reply')
    if not isinstance(reply, str):
        raise RuntimeError(f'response has no reply: {payload!r}')
    usage = payload.get('usage') or {}
    cost = usage.get('cost') if isinstance(usage, dict) else None
    return reply, (float(cost) if isinstance(cost, (int, float)) else None)


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'], cwd=HERE.parent,
            capture_output=True, text=True, timeout=10)
        sha = (out.stdout or '').strip()
        return sha or None
    except Exception:
        return None


def record_run(path: Path, record: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-url', default=os.environ.get('TANK_URL', 'http://127.0.0.1:8936'))
    parser.add_argument('--agent-key', default=os.environ.get('TANK_AGENT_KEY', ''))
    parser.add_argument('--agent-id', default=os.environ.get('TANK_AGENT_ID', 'admin'))
    parser.add_argument('--model', default=os.environ.get('TANK_EVAL_MODEL', 'openrouter/auto'))
    parser.add_argument('--cases', type=Path, default=HERE / 'cases.json')
    parser.add_argument('--results-dir', type=Path, default=HERE / 'results')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.dry_run:
        print(f'{len(cases)} eval cases valid')
        return 0
    if not args.agent_key:
        print('TANK_AGENT_KEY or --agent-key is required', file=sys.stderr)
        return 2

    failures = 0
    case_rows = []
    total_cost = 0.0
    priced_calls = 0
    started = time.time()
    for case in cases:
        body = {
            'model': args.model,
            'agentId': args.agent_id,
            'max_tokens': 120,
            # The endpoint already accrues and logs this cost; asking for it
            # back lets the benchmark report real spend per run.
            'includeUsage': True,
            'messages': [{'role': 'user', 'content': case['message']}],
        }
        try:
            reply, cost = ask(args.base_url, args.agent_key, body)
        except Exception as error:
            failures += 1
            case_rows.append({'name': case['name'], 'ok': False,
                              'error': str(error)[:300]})
            print(f'FAIL {case["name"]}: {error}')
            continue
        if cost is not None:
            total_cost += cost
            priced_calls += 1
        missing = [pattern for pattern in case['expect']
                   if not re.search(pattern, reply, re.IGNORECASE)]
        if missing:
            failures += 1
            case_rows.append({'name': case['name'], 'ok': False,
                              'missing': missing, 'costUsd': cost})
            print(f'FAIL {case["name"]}: missing {missing}; reply={reply[:160]!r}')
        else:
            case_rows.append({'name': case['name'], 'ok': True, 'costUsd': cost})
            print(f'PASS {case["name"]}')
    duration_ms = int((time.time() - started) * 1000)
    # Provenance without leaks: the endpoint host never enters the record
    # (a public benchmark page is rendered from these files), and reply
    # text is not stored -- pass/fail plus the missed patterns is enough
    # to audit a case.
    record = {
        'runAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'model': args.model,
        'endpoint': 'local-tank',
        'agentId': args.agent_id,
        'gitSha': git_sha(),
        'cases': case_rows,
        'passed': len(cases) - failures,
        'total': len(cases),
        'durationMs': duration_ms,
        'costUsd': round(total_cost, 6) if priced_calls else None,
        'pricedCalls': priced_calls,
    }
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    slug = re.sub(r'[^A-Za-z0-9._-]', '_', args.model)
    path = record_run(args.results_dir / f'{stamp}-{slug}.json', record)
    print(f'{record["passed"]}/{record["total"]} passed; recorded {path.name}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
