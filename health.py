"""Print a read-only summary of a think tank database."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time


DEFAULT_DATABASE = Path(__file__).resolve().parent / 'think_tank.db'
TERMINAL_STATUSES = {'done', 'failed', 'cancelled', 'abandoned', 'stopped'}


def load_state(database):
    """Read an existing database without creating an empty one on a typo."""
    connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT blob FROM kv_state WHERE id=1').fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError('The database has no saved simulation state.')
    state = json.loads(row[0])
    if not isinstance(state, dict):
        raise ValueError('Saved simulation state must be an object.')
    return state


def print_summary(state):
    tasks = state.get('tasks') or {}
    inflight = [task for task in tasks.values() if task.get('status') not in TERMINAL_STATUSES]
    titles = Counter(task.get('title') for task in inflight)
    sim = state.get('sim') or {}
    epoch = sim.get('lastTickEpochS')
    last_tick = time.strftime('%m-%d %H:%M UTC', time.gmtime(epoch)) if epoch else 'not recorded'
    print('last tick   :', last_tick, '| tick', sim.get('tick', 'not recorded'))
    print('statuses    :', Counter(task.get('status') for task in tasks.values()))
    print('queue depth :', len(state.get('workQueue') or []))
    top = titles.most_common(1)
    share = f'= {100 * top[0][1] // len(inflight)}% of in-flight' if top else ''
    print('top title   :', top, share)
    print('roles       :', Counter(agent.get('role') or 'seed' for agent in (state.get('agents') or {}).values()))
    print('pendingOnb  :', len(state.get('_pendingOnboard') or {}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        state = load_state(args.database)
    except (sqlite3.Error, ValueError, OSError) as error:
        print(f'Cannot read {args.database}: {error}', file=sys.stderr)
        return 1
    print_summary(state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
