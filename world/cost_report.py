"""Spend per completed card, from the action log.

The Bank shows totals by service. This shows the other half: which cards the
tank actually spent money on, and what a completed card costs on average.
Usage: python3 world/cost_report.py [--days 7] [--db path]
"""

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def summarize(db_path, days=7):
    since = time.time() - max(1, days) * 86400
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    by_task = defaultdict(lambda: {'cost': 0.0, 'title': '', 'calls': 0})
    total = 0.0
    completed = set()
    for row in con.execute(
            'SELECT action, details FROM action_log WHERE ts >= ? ORDER BY ts',
            (since,)):
        try:
            d = json.loads(row['details'] or '{}')
        except ValueError:
            d = {}
        if row['action'] == 'task_completed':
            tid = d.get('taskId')
            if tid:
                completed.add(tid)
            continue
        cost = d.get('cost')
        if not isinstance(cost, (int, float)) or cost <= 0:
            continue
        total += cost
        tid = d.get('taskId') or d.get('task_id')
        if not tid:
            continue
        item = by_task[tid]
        item['cost'] += float(cost)
        item['calls'] += 1
        item['title'] = item['title'] or d.get('title') or d.get('service') or ''
    con.close()
    cards = sorted(by_task.items(), key=lambda kv: kv[1]['cost'], reverse=True)
    return {
        'days': days,
        'totalCost': round(total, 4),
        'completedCards': len(completed),
        'avgCostPerCompletedCard': round(total / len(completed), 4) if completed else 0.0,
        'cards': [
            {'taskId': tid, 'title': v['title'][:120], 'cost': round(v['cost'], 4),
             'calls': v['calls']}
            for tid, v in cards if tid in completed
        ][:25],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--db', default=os.path.join(ROOT, 'think_tank.db'))
    args = ap.parse_args()
    report = summarize(args.db, args.days)
    print(f"Window: {report['days']} day(s)")
    print(f"Total model/Jev cost: ${report['totalCost']:.4f}")
    print(f"Completed cards: {report['completedCards']}")
    print(f"Average per completed card: ${report['avgCostPerCompletedCard']:.4f}")
    print()
    print('Top completed cards:')
    for item in report['cards']:
        print(f"  ${item['cost']:.4f}  {item['calls']:>3} calls  "
              f"{item['taskId']}  {item['title']}")


if __name__ == '__main__':  # pragma: no cover
    main()
