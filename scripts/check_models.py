#!/usr/bin/env python3
"""Print the selected model tiers and fail on inconsistent model config."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'world'))

import model_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(ROOT / 'think_tank.db'))
    parser.add_argument('--config', default=str(ROOT / 'world' / 'model_bands.json'))
    args = parser.parse_args()
    config = model_config.load_bands(Path(args.config))
    problems, rows = model_config.validate(config, Path(args.db))
    if rows:
        print(model_config.table(rows))
    else:
        print('(no selected tiers in the database yet)')
    if problems:
        print('\nproblems:')
        for problem in problems:
            print(f'- {problem}')
        return 1
    print('\nmodel configuration is consistent')
    return 0


if __name__ == '__main__':
    sys.exit(main())
