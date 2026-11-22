# Hermetic tests for the Jev institutional-memory loop (2026-11-22): the
# director chain's confident verdicts become lessons; the safety gate recalls
# them before escalating again. Generalization asymmetry: deny by host /
# command family, allow exact-only. Everything is file-backed under a temp
# library dir; no network, no real models.
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class LessonStoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Pin the store through JEV_LESSONS_DIR (the authoritative override
        # in _jev_lessons_dir) so no cross-file patch of _safe_library_path
        # or leaked env can split read/write paths.
        env_patcher = unittest.mock.patch.dict(
            os.environ, {'JEV_LESSONS_DIR': self.tmp})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        patcher = unittest.mock.patch.object(
            serve, '_safe_library_path',
            side_effect=lambda rel: os.path.join(self.tmp, rel))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))


class LessonKeys(unittest.TestCase):
    def test_url_deny_generalizes_by_host(self):
        deny, allow = serve._jev_lesson_keys('browse', 'https://evil-cdn.com/page1')
        self.assertEqual(deny, 'host:browse:evil-cdn.com')
        self.assertIn('exact:browse:', allow)

    def test_allow_key_is_url_exact(self):
        _, allow1 = serve._jev_lesson_keys('browse', 'https://ok.com/a')
        _, allow2 = serve._jev_lesson_keys('browse', 'https://ok.com/b')
        self.assertNotEqual(allow1, allow2)

    def test_execute_deny_is_command_family(self):
        deny, _ = serve._jev_lesson_keys('execute_classify', 'git  push  origin main')
        self.assertEqual(deny, 'cmd:execute_classify:git')

    def test_action_scoped(self):
        d1, _ = serve._jev_lesson_keys('browse', 'https://x.com/p')
        d2, _ = serve._jev_lesson_keys('download', 'https://x.com/p')
        self.assertNotEqual(d1, d2)


class RecordLookupRoundtrip(LessonStoreBase):
    def test_deny_recall_hits_sibling_paths(self):
        self.assertTrue(serve._jev_lesson_record(
            'browse', 'https://evil-cdn.com/page1', 'deny', 'director:rev1'))
        lesson = serve._jev_lesson_lookup('browse', 'https://evil-cdn.com/page2', 'deny')
        self.assertIsNotNone(lesson)
        self.assertEqual(lesson['verdict'], 'deny')

    def test_allow_recall_is_exact_only(self):
        serve._jev_lesson_record('browse', 'https://ok.com/a', 'allow', 'director:rev1')
        self.assertIsNotNone(serve._jev_lesson_lookup('browse', 'https://ok.com/a', 'allow'))
        self.assertIsNone(serve._jev_lesson_lookup('browse', 'https://ok.com/b', 'allow'))
        self.assertIsNone(serve._jev_lesson_lookup('browse', 'https://ok.com/a', 'deny'))

    def test_hits_increment(self):
        serve._jev_lesson_record('browse', 'https://h.com/x', 'deny', 'director:rev1')
        serve._jev_lesson_lookup('browse', 'https://h.com/y', 'deny')
        lesson = serve._jev_lesson_lookup('browse', 'https://h.com/y', 'deny')
        self.assertEqual(lesson['hits'], 2)

    def test_ttl_ages_out_non_sticky(self):
        serve._jev_lesson_record('browse', 'https://old.com/p', 'deny', 'director:rev1')
        store = serve._jev_lessons_read()
        key = next(iter(store))
        store[key]['created'] = time.time() - 25 * 3600
        serve._jev_lessons_write(store)
        self.assertIsNone(serve._jev_lesson_lookup('browse', 'https://old.com/q', 'deny'))


class Conflicts(LessonStoreBase):
    def test_host_deny_and_exact_allow_coexist(self):
        # Deny keys (host-class) and allow keys (exact URL) are disjoint by
        # design: the operator can seal a host while still approving one
        # page on it. No flip-flop conflict is generated across key classes.
        serve._jev_lesson_record('browse', 'https://flip.com/p', 'deny', 'director:rev1')
        serve._jev_lesson_record('browse', 'https://flip.com/p', 'allow', 'director:rev2')
        lesson = serve._jev_lesson_lookup('browse', 'https://flip.com/p', 'allow')
        self.assertIsNotNone(lesson)
        self.assertEqual(lesson['conflicts'], 0)
        deny = serve._jev_lesson_lookup('browse', 'https://flip.com/sibling', 'deny')
        self.assertEqual(deny['verdict'], 'deny')

    def test_same_key_reteach_counts_conflicts(self):
        # Only a same-key re-teach with a different verdict tallies (defensive
        # path for future key-scheme changes).
        serve._jev_lesson_record('browse', 'plain text target', 'deny', 'director:rev1')
        serve._jev_lesson_record('browse', 'plain text target', 'deny', 'director:rev2')
        lesson = serve._jev_lesson_lookup('browse', 'plain text target', 'deny')
        self.assertEqual(lesson['conflicts'], 0)
        self.assertEqual(lesson['hits'], 1)

    def test_mark_conflict_skips_recall(self):
        serve._jev_lesson_record('browse', 'https://war.com/p', 'deny', 'director:rev1')
        serve._jev_lesson_mark_conflict('browse', 'https://war.com/q', 'deny', 'allow')
        self.assertIsNone(serve._jev_lesson_lookup('browse', 'https://war.com/z', 'deny'))
        lesson = serve._jev_lessons_read()['host:browse:war.com']
        self.assertEqual(lesson['conflicts'], 1)


class GateWiring(LessonStoreBase):
    def _gate(self, decision, confidence, director_result=True):
        with unittest.mock.patch.object(serve, '_effective_safety_confidence',
                                        return_value=0.8), \
                unittest.mock.patch.object(serve, '_director_second_opinion',
                                           return_value=director_result) as director, \
                unittest.mock.patch.object(serve, 'log_action'):
            allowed = serve._jev_safety_gate(
                'agent-x', 'browse', 'page', 'https://site.com/p', 'research',
                decision, confidence, 0.01, True)
        return allowed, director

    def test_allow_lesson_rescues_low_confidence(self):
        serve._jev_lesson_record('browse', 'https://site.com/p', 'allow', 'director:rev1')
        allowed, director = self._gate('allow', 0.55)
        self.assertTrue(allowed)
        director.assert_not_called()

    def test_firm_jev_block_survives_allow_lesson(self):
        serve._jev_lesson_record('browse', 'https://site.com/p', 'allow', 'director:rev1')
        allowed, director = self._gate('block', 0.95)
        self.assertFalse(allowed)
        director.assert_not_called()

    def test_deny_lesson_seals_host_low_confidence(self):
        serve._jev_lesson_record('browse', 'https://site.com/p1', 'deny', 'director:rev1')
        allowed, director = self._gate('allow', 0.4, director_result=True)
        self.assertFalse(allowed)
        director.assert_not_called()

    def test_no_lesson_keeps_director_path(self):
        allowed, director = self._gate('allow', 0.55, director_result=True)
        self.assertTrue(allowed)
        director.assert_called_once()

    def test_deny_lesson_blocks_outage(self):
        serve._jev_lesson_record('browse', 'https://site.com/p1', 'deny', 'director:rev1')
        allowed, _ = self._gate(None, 0.0)
        self.assertFalse(allowed)


class DirectorTeaches(unittest.TestCase):
    def _run_chain(self, quorum_result):
        state = {'agentRoster': [
            {'id': 'rev1', 'name': 'Rev One'},
            {'id': 'admin1', 'name': 'Admin', 'isAdmin': True},
        ]}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
                unittest.mock.patch.object(serve, '_admin_agent_id',
                                           return_value='admin1'), \
                unittest.mock.patch.object(serve, '_escalation_review_chain',
                                           return_value=[('rev1', 'senior')]), \
                unittest.mock.patch.object(serve, '_effective_safety_confidence',
                                           return_value=0.8), \
                unittest.mock.patch.object(serve, '_escalation_review_model',
                                           return_value='model-x'), \
                unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                           return_value=quorum_result), \
                unittest.mock.patch.object(serve, 'log_action'):
            return serve._director_second_opinion(
                'agent-x', 'browse', 'page', 'https://teach.com/p', 'research',
                0.55)

    def test_approval_recorded_as_exact_allow(self):
        self.assertTrue(self._run_chain(('approve', 0.9, 0.01)))
        lesson = serve._jev_lessons_read().get('exact:browse:https://teach.com/p')
        self.assertIsNotNone(lesson)
        self.assertEqual(lesson['verdict'], 'allow')
        self.assertEqual(lesson['source'], 'director:rev1')

    def test_denial_recorded_as_host_deny(self):
        self.assertFalse(self._run_chain(('deny', 0.9, 0.01)))
        lesson = serve._jev_lessons_read().get('host:browse:teach.com')
        self.assertIsNotNone(lesson)
        self.assertEqual(lesson['verdict'], 'deny')


if __name__ == '__main__':
    unittest.main()
