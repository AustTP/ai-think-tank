#!/usr/bin/env python3
"""Fail when the two monolith files grow past their committed line budget.

New code belongs in a focused module. If a change genuinely needs these files
to grow, raise the budget in scripts/file_budgets.json in the same commit and
explain why in the commit message.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGETS = Path(__file__).with_name('file_budgets.json')


def line_count(path: Path) -> int:
    text = path.read_text(encoding='utf-8')
    return text.count('\n') + (1 if text and not text.endswith('\n') else 0)


def main() -> int:
    budgets = json.loads(BUDGETS.read_text(encoding='utf-8'))
    failed = False
    for name, limit in sorted(budgets.items()):
        path = ROOT / name
        if not path.exists():
            print(f'[growth] missing: {name}')
            failed = True
            continue
        current = line_count(path)
        status = 'ok' if current <= limit else 'OVER BUDGET'
        print(f'[growth] {name}: {current}/{limit} {status}')
        if current > limit:
            failed = True
    if failed:
        print('[growth] move the new code into its own module, or raise the '
              'budget explicitly in scripts/file_budgets.json.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
