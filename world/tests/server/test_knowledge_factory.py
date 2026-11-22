# Tests for the knowledge factory (394 flags store + 396 curator): gap flags
# recorded/listed/resolved, deterministic lesson path, mock-LLM triage, wiki
# proposal filed via the quarantine lane. No network, no real model calls.
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


def _flag(**kw):
    defaults = {'agent_id': 'ruth', 'kind': 'wrong',
                'about': 'stale path convention', 'note': 'tests/ vs world/tests/'}
    defaults.update(kw)
    return serve.flag_knowledge_gap(**defaults)


class KnowledgeGapStore(unittest.TestCase):
    def setUp(self):
        serve._KG_DIR_OVERRIDE = tempfile.mkdtemp()
        self.addCleanup(setattr, serve, '_KG_DIR_OVERRIDE', None)

    def test_flag_roundtrip_bumps_duplicates(self):
        fid = _flag()
        self.assertTrue(fid)
        fid2 = serve._kg_record('ruth', 'wrong', 'stale path convention', '')
        self.assertEqual(fid['id'], fid2)
        flags = serve._kg_list(status='open')
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['seen'], 2)

    def test_resolve_and_filters(self):
        fid = _flag()['id']
        self.assertTrue(serve._kg_resolve(fid, by='player', note='done'))
        self.assertEqual(serve._kg_list(status='open'), [])
        self.assertEqual(len(serve._kg_list(status='resolved')), 1)
        self.assertEqual(serve._kg_list(kind='missing'), [])

    def test_invalid_kind_rejected(self):
        out = serve.flag_knowledge_gap('ruth', 'bogus', 'x')
        self.assertFalse(out['ok'])

    def test_best_effort_on_bad_store(self):
        serve._KG_DIR_OVERRIDE = '/proc/nonexistent/nope'
        serve._kg_record('ruth', 'wrong', 'x', '')
        self.assertEqual(serve._kg_list(), [])


class KnowledgeCurator(unittest.TestCase):
    def setUp(self):
        serve._KG_DIR_OVERRIDE = tempfile.mkdtemp()

        self.addCleanup(setattr, serve, '_KG_DIR_OVERRIDE', None)
        sleep_patcher = mock.patch.object(serve.time, 'sleep', lambda s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_verdict_flag_becomes_lesson_without_llm(self):
        fid = serve._kg_record('ruth', 'wrong', 'SMOKETEST host', 'note',
                               verdict='deny', verdict_action='curl',
                               verdict_target='http://smoketest.example.org/x')
        with mock.patch.object(serve, '_jev_lesson_record',
                               return_value='deny|curl|smoketest.example.org') as rec, \
                mock.patch.object(serve, '_jev_quorum_choice_sync') as quorum:
            serve._kc_triage_once()
        rec.assert_called_once()
        quorum.assert_not_called()
        self.assertEqual(serve._kg_list(status='open'), [])

    def test_deny_triage_resolves_open_flag(self):
        _flag()
        with mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('deny', 0.95, 0.01)):
            serve._kc_triage_once()
        self.assertEqual(serve._kg_list(status='open'), [])

    def test_approve_triage_files_wiki_proposal(self):
        _flag(about='how to rotate api keys', kind='missing')
        with mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0.01)):
            serve._kc_triage_once()
        self.assertEqual(serve._kg_list(status='open'), [])
        found = []
        base = os.path.join(serve._KG_DIR_OVERRIDE)  # not the real wiki path
        # The proposal lands in the real Library quarantine; assert via the
        # mocked _safe_library_path target instead:
        with mock.patch.object(serve, '_safe_library_path',
                               return_value=os.path.join(base, 'p.json')) as sl:
            self.assertIsNotNone(serve._kc_file_wiki_proposal(
                {'id': 'kg-1', 'kind': 'missing', 'about': 'a', 'note': 'n'}))
        self.assertTrue(sl.called)

    def test_attempt_cap_stops_retriage(self):
        _flag()
        with mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=(None, None, None)) as quorum:
            serve._kc_triage_once()
            serve._kc_triage_once()
        self.assertEqual(quorum.call_count, 2)  # cap = 2, then stop
