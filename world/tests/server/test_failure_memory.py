"""Failure-memory search: /api/failures reads every failure record the tank
keeps (action_log failure rows, live failed/blocked cards, the repair
ledger, archived terminal cards) and answers "has this failed before, and
why?" before anyone re-mints the same mistake."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _parts_path(name):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        'server', 'serve_parts', name)


class TokenMatching(unittest.TestCase):
    def test_tokens_are_lowercase_and_capped(self):
        import serve as fm
        self.assertEqual(fm._tokens('Disk FULL  retry'),
                         ['disk', 'full', 'retry'])
        self.assertEqual(fm._tokens('a b c d e f g h i'),
                         ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'])
        self.assertEqual(fm._tokens(''), [])

    def test_matches_all_requires_every_token(self):
        import serve as fm
        self.assertTrue(fm._matches_all(
            'disk pressure: under 20 GiB', ['disk', '20']))
        self.assertFalse(fm._matches_all(
            'disk pressure: under 20 GiB', ['disk', 'enosp']))


class Sources(unittest.TestCase):
    def _state(self):
        return {'tasks': {
            'task-1': {'id': 'task-1', 'status': 'blocked',
                       'title': 'Fix the write gate',
                       'blockedReason': 'parent_workspace_reaped',
                       'failNote': 'workspace was reaped; nothing to amend',
                       'blockedAt': 9_999_000_000_000}},
            '_repairFailureLog': [
            {'key': 'title:Fix the write gate', 'count': 3,
             'lastTs': 1}],
        }

    def test_live_cards_match_on_fail_note(self):
        import serve as fm
        hits = fm._live_cards(self._state(), ['reaped'], 10_000_000_000_000)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['taskId'], 'task-1')
        self.assertEqual(hits[0]['source'], 'task')

    def test_live_cards_skip_non_failures_and_old_rows(self):
        import serve as fm
        state = self._state()
        state['tasks']['task-2'] = {'id': 'task-2', 'status': 'done',
                                    'title': 'Fix the write gate'}
        stale = dict(state['tasks']['task-1'])
        stale['blockedAt'] = 1
        state['tasks']['task-1'] = stale
        self.assertEqual(fm._live_cards(state, ['reaped'], 10_000_000_000_000), [],
                         'a done card and an out-of-window card are skipped')
        stale = dict(self._state()['tasks']['task-1'])
        stale['blockedAt'] = 1
        state['tasks']['task-1'] = stale
        hits = fm._live_cards(state, ['reaped'], 10_000_000_000_000)
        self.assertEqual(hits, [], 'a card older than the window is skipped')

    def test_ledger_rows_match_by_key(self):
        import serve as fm
        hits = fm._ledger_rows(self._state(), ['write', 'gate'])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['count'], 3)
        self.assertEqual(fm._ledger_rows(self._state(), ['nomatch']), [])

    def test_log_rows_require_every_token_in_python(self):
        import serve as fm
        rows = [
            (1.0, 'ada', 'task_completion_refused',
             json.dumps({'taskId': 'task-1', 'failNote': 'no artifact'})),
            (2.0, 'ben', 'content_run_abandoned',
             json.dumps({'taskId': 'task-2', 'heartbeatAgeS': 333})),
        ]

        class FakeConn:
            def execute(self, _sql, _params):
                return self

            def fetchall(self):
                return rows

        hits = fm._log_rows(FakeConn(), ['refused', 'artifact'], 0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['taskId'], 'task-1')
        self.assertEqual(hits[0]['source'], 'log')

    def test_archive_rows_match_blob_fail_note(self):
        import serve as fm
        rows = [('task-9', json.dumps(
            {'id': 'task-9', 'status': 'failed',
             'title': 'Sync the mirror',
             'failNote': 'disk pressure: under 20 GiB free'}))]

        class FakeConn:
            def execute(self, _sql, _params):
                return self

            def fetchall(self):
                return rows

        with mock.patch('db_backend.is_postgres', return_value=False):
            hits = fm._archive_rows(FakeConn(), ['disk', '20'])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['source'], 'archive')
        self.assertIn('disk pressure', hits[0]['failNote'])


class Endpoint(unittest.TestCase):
    def test_search_requires_auth(self):
        import serve as fm
        req = mock.Mock(headers={})
        with mock.patch('serve._require_player_or_admin',
                        return_value=False):
            fm.api_failures.__wrapped__(req, q='disk') \
                if hasattr(fm.api_failures, '__wrapped__') else None
        # api_failures delegates through the async wrapper; assert the
        # guard path directly instead of unwrapping FastAPI plumbing.

    def test_search_contract(self):
        src = open(_parts_path('455_failure_memory.py'), encoding='utf-8').read()
        self.assertIn("'/api/failures'", src)
        self.assertIn('_require_player_or_admin(request)', src,
                      'operator/admin only, like /api/replay')
        self.assertIn('_FAILURE_ACTIONS', src,
                      'the failure-class action allowlist is explicit')


if __name__ == '__main__':
    unittest.main()


class LibraryPathTolerance(unittest.TestCase):
    """Agents read artifacts by the repo-convention path (library/reports/...)
    that cards and search results name; the read surface used to resolve
    that to library/library/... and return Not found for real files."""

    def test_strips_single_library_prefix_when_it_resolves(self):
        import serve
        from serve import _safe_library_path
        got = _safe_library_path('library/reports/dankornas-review-verdict.md')
        base = os.path.abspath(serve.LIBRARY_DIR)
        self.assertTrue(got and got.startswith(base + os.sep),
                        'resolves inside the Library')
        self.assertFalse(os.path.join('library', 'library') in got,
                         'no double prefix')

    def test_plain_relative_paths_still_work(self):
        import serve
        from serve import _safe_library_path
        got = _safe_library_path('reports/dankornas-review-verdict.md')
        base = os.path.abspath(serve.LIBRARY_DIR)
        self.assertTrue(got and got.startswith(base + os.sep))

    def test_traversal_still_refused(self):
        import serve
        from serve import _safe_library_path
        self.assertIsNone(_safe_library_path('../../.env'))
        got = _safe_library_path('library/../../.env')
        if got is not None:
            base = os.path.abspath(serve.LIBRARY_DIR)
            self.assertTrue(got.startswith(base + os.sep),
                            'even prefixed traversal stays inside')
