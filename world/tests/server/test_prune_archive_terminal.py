"""Retention prune for terminal task_archive rows (2026-11-27).

PruneLogsTest in test_serve_endpoint_contracts.py already covers the
Mock-conn wiring of _prune_logs (the helper's fetch is try/except'd, so a
Mock conn returns 0). These tests exercise _prune_terminal_archive_cards
itself against a real in-memory sqlite conn: age keyed on the card's
createdAt inside the blob, status filter, live-task and kept-row reference
guards, the referencer-first chain rule, the per-run cap, and the days=0
kill switch.
"""
import json
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402

# Ages come from the real clock because the helper's cutoff is
# (time.time() - days*86400)*1000 -- a fixed NOW_MS in the future would
# silently make every 'old' card recent.
OLD_MS = (time.time() - 40 * 86400) * 1000
RECENT_MS = (time.time() - 1 * 86400) * 1000


def _seed(conn, cards):
    conn.execute('CREATE TABLE task_archive (id TEXT PRIMARY KEY, blob TEXT)')
    for card in cards:
        conn.execute('INSERT INTO task_archive (id, blob) VALUES (?, ?)',
                     (card['id'], json.dumps(card)))
    conn.commit()


def _card(cid, status, created_ms, **extra):
    card = {'id': cid, 'status': status, 'createdAt': created_ms}
    card.update(extra)
    return card


def _prune(conn, env=None, live_tasks=None, cap=None):
    env = dict(env or {})
    env.setdefault('ARCHIVE_RETENTION_DAYS', '30')
    with mock.patch.object(serve, '_load_env', return_value=env), \
         mock.patch.object(serve, 'get_state_from_db',
                           return_value={'tasks': dict(live_tasks or {})}):
        return serve._prune_terminal_archive_cards(conn, cap=cap)


class ArchiveTerminalPruneTest(unittest.TestCase):
    def _conn(self, cards):
        conn = sqlite3.connect(':memory:')
        _seed(conn, cards)
        return conn

    def _ids(self, conn):
        return {row[0] for row in
                conn.execute('SELECT id FROM task_archive').fetchall()}

    def test_old_cancelled_deleted_recent_and_done_kept(self):
        conn = self._conn([
            _card('A', 'cancelled', OLD_MS),
            _card('C', 'cancelled', RECENT_MS),
            _card('D', 'done', OLD_MS),
        ])
        self.assertEqual(_prune(conn), 1)
        self.assertEqual(self._ids(conn), {'C', 'D'})

    def test_all_terminal_statuses_eligible(self):
        statuses = ['cancelled', 'failed', 'quarantined',
                    'deferred_conflict', 'rolled_back']
        conn = self._conn([_card('S%d' % i, s, OLD_MS)
                           for i, s in enumerate(statuses)])
        self.assertEqual(_prune(conn), len(statuses))
        self.assertEqual(self._ids(conn), set())

    def test_referencer_dies_first_target_survives_next_run(self):
        conn = self._conn([
            _card('A', 'cancelled', OLD_MS),
            _card('B', 'cancelled', OLD_MS, dependsOn=['A']),
        ])
        self.assertEqual(_prune(conn), 1)
        self.assertEqual(self._ids(conn), {'A'})
        self.assertEqual(_prune(conn), 1)
        self.assertEqual(self._ids(conn), set())

    def test_live_task_reference_protects_card(self):
        conn = self._conn([_card('E', 'cancelled', OLD_MS)])
        self.assertEqual(_prune(conn, live_tasks={
            'live1': {'id': 'live1', 'dependsOn': ['E']},
        }), 0)
        self.assertEqual(self._ids(conn), {'E'})

    def test_kept_row_reference_protects_card(self):
        conn = self._conn([
            _card('D', 'done', OLD_MS, dependsOn=['F']),
            _card('F', 'cancelled', OLD_MS),
        ])
        self.assertEqual(_prune(conn), 0)
        self.assertEqual(self._ids(conn), {'D', 'F'})

    def test_cap_bounds_run(self):
        conn = self._conn([_card('V%d' % i, 'cancelled', OLD_MS)
                           for i in range(5)])
        self.assertEqual(_prune(conn, cap=2), 2)

    def test_days_zero_disables(self):
        conn = self._conn([_card('A', 'cancelled', OLD_MS)])
        self.assertEqual(_prune(conn, env={'ARCHIVE_RETENTION_DAYS': '0'}), 0)
        self.assertEqual(self._ids(conn), {'A'})

    def test_live_guard_failure_falls_back_to_kept_refs(self):
        # get_state_from_db raising must not block the prune (the guard data
        # is best-effort; kept-row refs still protect referenced cards).
        conn = self._conn([
            _card('A', 'cancelled', OLD_MS),
            _card('B', 'cancelled', OLD_MS, dependsOn=['A']),
        ])
        with mock.patch.object(serve, '_load_env',
                               return_value={'ARCHIVE_RETENTION_DAYS': '30'}), \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('no db')):
            self.assertEqual(serve._prune_terminal_archive_cards(conn), 1)
        self.assertEqual(self._ids(conn), {'A'})


if __name__ == '__main__':
    unittest.main()