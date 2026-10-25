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
import sys
import urllib.error
import urllib.request
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base-url', default=os.environ.get('TANK_URL', 'http://127.0.0.1:8936'))
    parser.add_argument('--agent-key', default=os.environ.get('TANK_AGENT_KEY', ''))
    parser.add_argument('--agent-id', default=os.environ.get('TANK_AGENT_ID', 'admin'))
    parser.add_argument('--model', default=os.environ.get('TANK_EVAL_MODEL', 'openrouter/auto'))
    parser.add_argument('--cases', type=Path, default=HERE / 'cases.json')
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
    for case in cases:
        body = {
            'model': args.model,
            'agentId': args.agent_id,
            'max_tokens': 120,
            'messages': [{'role': 'user', 'content': case['message']}],
        }
        try:
            reply = ask(args.base_url, args.agent_key, body)
        except Exception as error:
            failures += 1
            print(f'FAIL {case["name"]}: {error}')
            continue
        missing = [pattern for pattern in case['expect']
                   if not re.search(pattern, reply, re.IGNORECASE)]
        if missing:
            failures += 1
            print(f'FAIL {case["name"]}: missing {missing}; reply={reply[:160]!r}')
        else:
            print(f'PASS {case["name"]}')
    print(f'{len(cases) - failures}/{len(cases)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
