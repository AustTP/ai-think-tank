"""Agent-improvable rule tables (059_agent_rules): additive-only overlay
validation, caps, corrupt-file last-good behavior, and the three consumers
(182 readonly shell, 425 digest internal signals, 70 spike pre-filter).
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import content  # noqa: E402


@contextmanager
def _rules_file(payload):
    """Point serve at a throwaway agent_rules.json and reset the loader
    cache, so every test starts cold. payload None = file absent."""
    fd, path = tempfile.mkstemp(prefix='agent-rules-', suffix='.json')
    os.close(fd)
    os.unlink(path)
    saved = (serve._AGENT_RULES_PATH, serve._agent_rules_cache,
             serve._agent_rules_reject_logged)
    try:
        if payload is not None:
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh)
        serve._AGENT_RULES_PATH = path
        serve._agent_rules_cache = {'mtime': None, 'at': 0.0, 'data': {}}
        serve._agent_rules_reject_logged = False
        yield path
    finally:
        serve._AGENT_RULES_PATH, serve._agent_rules_cache, \
            serve._agent_rules_reject_logged = saved


class LoaderTests(unittest.TestCase):
    def test_missing_file_serves_empty_overlay(self):
        with _rules_file(None):
            self.assertEqual(serve._agent_rules_table('readonlyBinaries'), [])
            self.assertEqual(serve._agent_rules_extra('readonlyBinaries'),
                             set())
            self.assertEqual(serve._agent_rules_forbidden('tar'), ())

    def test_overlay_extends_every_namespace(self):
        with _rules_file({
                'readonlyBinaries': ['mytool'],
                'readonlyGitSubs': ['bisect'],
                'forbiddenFlags': {'tar': ['--absolute-names']},
                'spikeProblemSignals': ['memory leak'],
                'internalSignalActions': ['zzz_custom_kind']}):
            self.assertEqual(serve._agent_rules_table('readonlyBinaries'),
                             ['mytool'])
            self.assertEqual(serve._agent_rules_extra('readonlyGitSubs'),
                             {'bisect'})
            self.assertEqual(serve._agent_rules_forbidden('tar'),
                             ('--absolute-names',))
            self.assertEqual(serve._agent_rules_forbidden('gzip'), ())
            self.assertEqual(serve._agent_rules_table('spikeProblemSignals'),
                             ['memory leak'])
            self.assertEqual(
                serve._agent_rules_table('internalSignalActions'),
                ['zzz_custom_kind'])

    def test_validator_drops_junk_keeps_good(self):
        with _rules_file({
                'readonlyBinaries': ['goodtool', 42, '../etc/passwd',
                                     'a' * 50, 'goodtool'],
                'readonlyGitSubs': ['ok-sub', '/bin/rm'],
                'forbiddenFlags': {'tar': 'not-a-list'},
                'zzzEvilNamespace': ['anything'],
                'spikeProblemSignals': [3]}):
            self.assertEqual(serve._agent_rules_table('readonlyBinaries'),
                             ['goodtool'])
            self.assertEqual(serve._agent_rules_table('readonlyGitSubs'),
                             ['ok-sub'])
            self.assertEqual(serve._agent_rules_table('forbiddenFlags'), [])
            self.assertEqual(serve._agent_rules_table('zzzEvilNamespace'), [])
            self.assertEqual(serve._agent_rules_table('spikeProblemSignals'),
                             [])

    def test_non_dict_file_fails_closed(self):
        with _rules_file(['not', 'a', 'dict']):
            self.assertEqual(serve._agent_rules_table('readonlyBinaries'), [])

    def test_caps_bound_growth(self):
        with _rules_file({
                'readonlyBinaries': ['tool%d' % i for i in range(130)],
                'forbiddenFlags': {
                    't%d' % i: ['--f%d' % i for i in range(80)]
                    for i in range(3)}}):
            self.assertEqual(len(serve._agent_rules_table('readonlyBinaries')),
                             120)
            flags = serve._agent_rules_table('forbiddenFlags')
            self.assertEqual(sum(len(v) for v in flags.values()), 120)

    def test_corrupt_file_keeps_last_good_overlay(self):
        with _rules_file({'readonlyBinaries': ['mytool']}) as path:
            self.assertEqual(serve._agent_rules_extra('readonlyBinaries'),
                             {'mytool'})
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('{broken json')
            serve._agent_rules_cache['at'] = 0.0  # force the stat re-read
            with mock.patch.object(serve, 'log_action') as la:
                # Last good overlay survives: a bad mid-save edit cannot
                # strip live additions or widen anything.
                self.assertEqual(serve._agent_rules_extra('readonlyBinaries'),
                                 {'mytool'})
                self.assertTrue(serve._agent_rules_reject_logged)
                la.assert_called_once()
                # The rejection is logged once, not per read.
                serve._agent_rules_cache['at'] = 0.0
                serve._agent_rules_extra('readonlyBinaries')
                la.assert_called_once()

    def test_first_load_failure_is_just_empty(self):
        with _rules_file(None) as path:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('garbage')
            with mock.patch.object(serve, 'log_action') as la:
                self.assertEqual(
                    serve._agent_rules_table('readonlyBinaries'), [])
                la.assert_called_once()


class ReadonlyShellGateTests(unittest.TestCase):
    def test_overlay_binary_and_git_sub_pass_the_gate(self):
        with _rules_file({
                'readonlyBinaries': ['mytool'],
                'readonlyGitSubs': ['bisect']}):
            self.assertTrue(serve._readonly_shell_command('mytool x.txt'))
            self.assertTrue(serve._readonly_shell_command('git bisect log'))
            # The base floor still decides everything else.
            self.assertFalse(serve._readonly_shell_command('othertool x'))
            self.assertFalse(serve._readonly_shell_command('git push'))

    def test_overlay_forbidden_flag_refuses(self):
        with _rules_file({'readonlyBinaries': ['mytool'],
                          'forbiddenFlags': {'sort': ['--daylight']}}):
            # sort's base flags (-o/--output) never accepted it anyway.
            self.assertTrue(serve._readonly_shell_command('sort -x'))
            self.assertFalse(serve._readonly_shell_command('sort --daylight'))
            # Another tool with the same flag name is untouched.
            self.assertTrue(
                serve._readonly_shell_command('mytool --daylight'))


class DigestInternalSignalTests(unittest.TestCase):
    @staticmethod
    def _digest_only_open(rules_path):
        """Fakes the digest's output file open while the rules loader keeps
        reading the real overlay file."""
        real_open = open

        def fake(path, *a, **k):
            if path == rules_path:
                return real_open(path, *a, **k)
            return FakeFile()

        return fake

    def test_digest_sql_picks_up_overlay_action(self):
        now = datetime.datetime(2026, 11, 21, 5, 0,
                                tzinfo=datetime.timezone.utc).timestamp()
        captured = []

        class FakeFile:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, text):
                pass

        def fake_db():
            conn = mock.MagicMock()

            def execute(sql, *params):
                # DB-API style: the digest passes one parameters tuple per
                # query; flatten it before checking membership.
                if len(params) == 1 and isinstance(params[0], tuple):
                    params = params[0]
                # The digest runs several action_log IN-clause queries; the
                # internal-signals one is parameterized (no GROUP BY, unlike
                # the self-edit counts query, and no literal action names,
                # unlike the telegram scan).
                if ('action_log' in sql and 'action IN' in sql
                        and 'GROUP BY' not in sql and '?' in sql):
                    captured.append(params)
                m = mock.MagicMock()
                m.fetchall.return_value = []
                return m

            conn.execute.side_effect = execute
            return mock.MagicMock(__enter__=lambda s: conn,
                                  __exit__=lambda s, *a: False)

        with _rules_file({'internalSignalActions': ['zzz_custom_kind']}) as \
                rules_path, \
                mock.patch.object(serve, '_db', fake_db), \
                mock.patch.object(serve, '_safe_library_path',
                                  return_value='/tmp/digest.md'), \
                mock.patch.object(serve, 'log_action'), \
                mock.patch('builtins.open',
                           side_effect=self._digest_only_open(rules_path)), \
                mock.patch.object(serve.os.path, 'dirname',
                                  return_value='/tmp'):
            serve.write_telegram_alert_digest(now=now)
        self.assertTrue(captured)
        self.assertIn('zzz_custom_kind', captured[-1])
        self.assertIn('finding_triage_unsure', captured[-1])


class SpikePrefilterTests(unittest.TestCase):
    def test_overlay_signal_reaches_triage(self):
        with mock.patch.object(serve, '_call_openrouter_decision_sync') as d, \
                mock.patch.object(serve, '_jev_choice',
                                  return_value=('none', 0.9, 0.01)):
            # No overlay: a finding matching only the custom signal never
            # reaches Jev at all (the built-in list is the only vocabulary).
            with _rules_file({'spikeProblemSignals': []}):
                self.assertIsNone(content._spike_file_issue_wish(
                    'zzz signal appeared in the trace', {}, 'Ada', 'b'))
                d.assert_not_called()
            # With the overlay signal, the same finding passes the pre-filter
            # and asks Jev for the file/no-file verdict.
            with _rules_file({'spikeProblemSignals': ['zzz signal']}):
                self.assertIsNone(content._spike_file_issue_wish(
                    'zzz signal appeared in the trace', {}, 'Ada', 'b'))
                d.assert_called_once()


if __name__ == '__main__':
    unittest.main()
