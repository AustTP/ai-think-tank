"""Cross-session search: /api/session-search reads every action_log row
plus agent mailboxes (live + archive) and the player inbox, newest first,
answering "what did we do about X last time?" -- the gap the 2026-11-18
dankornas round-3 review adopted (aise's idea on the tank's rails, one
increment past /api/failures' failure-only lane)."""

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
        import serve as ss
        self.assertEqual(ss._ss_tokens('Agent MAIL  reaped'),
                         ['agent', 'mail', 'reaped'])
        self.assertEqual(ss._ss_tokens('a b c d e f g h i'),
                         ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'])
        self.assertEqual(ss._ss_tokens(''), [])

    def test_matches_all_requires_every_token(self):
        import serve as ss
        self.assertTrue(ss._ss_matches_all(
            'chief directive: fix the write gate', ['write', 'gate']))
        self.assertFalse(ss._ss_matches_all(
            'chief directive: fix the write gate', ['write', 'fail']))


class Snippet(unittest.TestCase):
    def test_short_text_passes_through(self):
        import serve as ss
        self.assertEqual(ss._ss_snippet('short note', ['note']), 'short note')

    def test_long_text_windows_around_first_token(self):
        import serve as ss
        text = ('filler ' * 60) + 'the write gate jammed at dawn ' + \
            ('tail ' * 60)
        got = ss._ss_snippet(text, ['gate'])
        self.assertIn('write gate', got)
        self.assertLessEqual(len(got), ss._SS_SNIPPET + 10)

    def test_no_hit_falls_back_to_head(self):
        import serve as ss
        text = 'abc ' * 200
        got = ss._ss_snippet(text, ['zzz'])
        self.assertTrue(got.startswith('abc'))
        self.assertLessEqual(len(got), ss._SS_SNIPPET + 10)


class LogRows(unittest.TestCase):
    ROWS = [
        (100.0, 'ada', 'selfedit_applied',
         json.dumps({'taskId': 'task-1', 'title': 'Tighten the write gate',
                     'note': 'patched fs.py'})),
        (200.0, 'ben', 'browser_fetch',
         json.dumps({'taskId': 'task-2', 'url': 'https://x.example/post',
                     'bytes': 4200})),
        (300.0, 'cid', 'gate_review_passed',
         json.dumps({'taskId': 'task-3', 'reviewer': 'ada',
                     'note': 'write gate change verified by evidence'})),
    ]

    class FakeConn:
        def execute(self, _sql, _params):
            return self

        def fetchall(self):
            return list(LogRows.ROWS)

    def test_matches_across_all_actions_and_tokens(self):
        import serve as ss
        hits = ss._ss_log_rows(self.FakeConn(), ['write', 'gate'], 0)
        self.assertEqual({h['taskId'] for h in hits},
                         {'task-1', 'task-3'},
                         'non-failure actions are searchable, every token must hit')
        self.assertTrue(all(h['source'] == 'log' for h in hits))

    def test_ts_becomes_epoch_ms_and_snippet_present(self):
        import serve as ss
        hits = ss._ss_log_rows(self.FakeConn(), ['gate'], 0)
        by_task = {h['taskId']: h for h in hits}
        self.assertEqual(by_task['task-1']['atMs'], 100_000)
        self.assertIn('write gate', by_task['task-1']['snippet'])

    def test_details_prefilter_is_passed_to_sql(self):
        import serve as ss
        seen = {}

        class SpyConn(LogRows.FakeConn):
            def execute(self, sql, params):
                seen['sql'] = sql
                seen['params'] = params
                return self

        ss._ss_log_rows(SpyConn(), ['needle'], 55.0)
        self.assertIn('LOWER(details) LIKE', seen['sql'])
        self.assertEqual(seen['params'][0], 55.0)
        self.assertEqual(seen['params'][1], '%needle%')

    def test_db_errors_return_empty_not_raise(self):
        import serve as ss

        class BoomConn:
            def execute(self, *_a):
                raise RuntimeError('db down')

        self.assertEqual(ss._ss_log_rows(BoomConn(), ['x'], 0), [])


class MailHits(unittest.TestCase):
    NOW_MS = 1_800_000_000_000  # well inside the 90d mail window

    def _state(self):
        return {'agents': {
            'ada': {'mailbox': [
                {'kind': 'supervisor_brief', 'about': 'team-1',
                 'title': 'Supervisor brief: the write gate',
                 'text': 'gate reviews refuse incomplete work',
                 'createdAt': self.NOW_MS - 1000}],
                    'mailboxArchive': [
                {'kind': 'chief_directive', 'about': 'DEV-153',
                 'title': 'Chief of staff directive: write gate',
                 'text': 'filed for your team immediately',
                 'ts': self.NOW_MS - 2000}]},
            'player-shaped': {'mailbox': 'not-a-list'}},
            'playerInbox': [
                {'id': 'card-1', 'agentId': 'system',
                 'question': '[card] write gate', 'answer': 'groomed into the sprint',
                 'ts': self.NOW_MS - 3000}],
        }

    def test_mailbox_and_archive_both_searched(self):
        import serve as ss
        hits = ss._ss_mail_hits(self._state(), ['write', 'gate'], self.NOW_MS)
        sources = {(h['agent'], h['source']) for h in hits}
        self.assertIn(('ada', 'mail'), sources)
        self.assertEqual(len(hits), 3,
                         'one live mail, one archive mail, one inbox note')

    def test_inbox_hits_carry_inbox_source(self):
        import serve as ss
        hits = ss._ss_mail_hits(self._state(), ['groomed'], self.NOW_MS)
        self.assertEqual([h['source'] for h in hits], ['inbox'])
        self.assertEqual(hits[0]['agent'], 'system')

    def test_out_of_window_entries_dropped(self):
        import serve as ss
        state = self._state()
        state['agents']['ada']['mailbox'][0]['createdAt'] = 1
        state['agents']['ada']['mailboxArchive'][0]['ts'] = 1
        state['playerInbox'][0]['ts'] = 1
        self.assertEqual(
            ss._ss_mail_hits(state, ['write', 'gate'], self.NOW_MS), [],
            'entries older than the 90-day mail window are skipped')

    def test_malformed_entries_ignored(self):
        import serve as ss
        state = {'agents': {'ada': {'mailbox': ['nope', None, 3]}},
                 'playerInbox': [42, 'also nope']}
        self.assertEqual(ss._ss_mail_hits(state, ['anything'], self.NOW_MS), [])


class SearchFunction(unittest.TestCase):
    def test_empty_query_refused(self):
        import serve as ss
        self.assertEqual(ss._ss_session_search('   ', 0, 7),
                         {'error': 'empty query'})

    def test_days_window_clamped_and_results_sorted(self):
        import serve as ss
        captured = {}
        log_rows = [
            {'source': 'log', 'atMs': 5_000, 'agent': 'ben', 'action': 'x',
             'taskId': None, 'title': '', 'note': '', 'snippet': 'x'},
            {'source': 'log', 'atMs': 9_000, 'agent': 'ada', 'action': 'y',
             'taskId': None, 'title': '', 'note': '', 'snippet': 'y'},
        ]
        mail_rows = [
            {'source': 'mail', 'atMs': 7_000, 'agent': 'cid', 'kind': 'k',
             'title': 'm', 'snippet': 'm'},
        ]

        def fake_log_rows(_conn, toks, since_ts):
            captured['since_ts'] = since_ts
            return list(log_rows)

        fake_state = {'agents': {}, 'playerInbox': []}

        class NullDb:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def execute(self, *_a):
                raise RuntimeError('unused')

        with mock.patch.object(ss, '_ss_log_rows', fake_log_rows), \
             mock.patch.object(ss, '_ss_mail_hits', return_value=mail_rows), \
             mock.patch.object(ss, 'get_state_from_db',
                               return_value=fake_state), \
             mock.patch.object(ss, '_db', return_value=NullDb()), \
             mock.patch.object(ss, 'time') as fake_time:
            fake_time.time.return_value = 1800.0
            out = ss._ss_session_search('needle', 0, 500)
        self.assertEqual(out['counts'], {'log': 2, 'mail': 1, 'inbox': 0})
        self.assertEqual([m['atMs'] for m in out['matches']],
                         [9_000, 7_000, 5_000], 'newest first across sources')
        self.assertFalse(out['truncated'])
        # days=500 clamps to the 90-day ceiling
        self.assertAlmostEqual(captured['since_ts'],
                               1800.0 - 90 * 24 * 3600)

    def test_limit_applied_with_truncation_flag(self):
        import serve as ss
        many = [{'source': 'log', 'atMs': i, 'agent': 'a', 'action': 'x',
                 'taskId': None, 'title': '', 'note': '', 'snippet': 's'}
                for i in range(5)]

        class NullDb:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        with mock.patch.object(ss, '_ss_log_rows', return_value=many), \
             mock.patch.object(ss, '_ss_mail_hits', return_value=[]), \
             mock.patch.object(ss, 'get_state_from_db', return_value={}), \
             mock.patch.object(ss, '_db', return_value=NullDb()), \
             mock.patch.object(ss, 'time') as fake_time:
            fake_time.time.return_value = 1800.0
            out = ss._ss_session_search('needle', 2, 7)
        self.assertEqual(len(out['matches']), 2)
        self.assertTrue(out['truncated'])

    def test_namespace_hygiene(self):
        """Serve parts are exec'd into ONE namespace; these helpers must
        stay _ss_-prefixed so /api/failures' _tokens/_matches_all and its
        2000-row prefilter are never clobbered by this module's 4000."""
        src455 = open(_parts_path('455_failure_memory.py'), encoding='utf-8').read()
        src465 = open(_parts_path('465_session_search.py'), encoding='utf-8').read()
        self.assertIn('_SQL_PREFILTER_LIMIT = 2000', src455)
        self.assertIn('_SS_PREFILTER = 4000', src465)
        self.assertNotIn('def _tokens(', src465)
        self.assertNotIn('def _matches_all(', src465)
        self.assertIn("'/api/session-search'", src465)
        self.assertIn('_require_player_or_admin(request)', src465,
                      'operator/admin only, like /api/replay and /api/failures')
        # The endpoint dispatches the _ss_-prefixed searcher; an unprefixed
        # call would NameError at request time (exec'd-namespace hazard the
        # first deploy actually hit).
        self.assertIn('to_thread(\n        _ss_session_search', src465)
        self.assertNotIn('to_thread(\n        _session_search', src465)


if __name__ == '__main__':
    unittest.main()
