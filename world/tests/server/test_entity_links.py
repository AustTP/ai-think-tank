"""Shared entity extraction over the think tank's own id grammar.

The calibration and audit layers join prose (tape prompts, action_log
details) against structured entities; the per-site regexes that did this
before drifted (task-123 vs task-1234 boundary bugs). One extractor,
tested once.
DB-isolated (setUpModule below, the test_vpn_exit_endpoints pattern).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-entity-links-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class TaskRefs(unittest.TestCase):
    def test_basic_and_order(self):
        text = 'task-42 blocked on spike-task-7; see task-42 again'
        self.assertEqual(serve._extract_task_refs(text),
                         ['task-42', 'spike-task-7'])

    def test_digit_boundary(self):
        # Greedy: task-1234 is one full ref, never task-123. Anything glued
        # to another token (9task-12, task-12x) is rejected as ambiguous --
        # stricter than the old digit-only guards, which is the safe
        # direction for outcome joins (false negatives beat false
        # condemnations).
        self.assertEqual(serve._extract_task_refs('task-1234'), ['task-1234'])
        self.assertEqual(serve._extract_task_refs('9task-12'), [])
        self.assertEqual(serve._extract_task_refs('task-12x'), [])

    def test_empty(self):
        self.assertEqual(serve._extract_task_refs(''), [])
        self.assertEqual(serve._extract_task_refs(None), [])


class AgentRefs(unittest.TestCase):
    def test_name_to_id(self):
        roster = {'Ada': 'ada', 'nora': 'nora'}
        self.assertEqual(serve._extract_agent_refs('Ada and nora signed off', roster),
                         ['ada', 'nora'])

    def test_word_boundary(self):
        roster = {'ada': 'ada'}
        self.assertEqual(serve._extract_agent_refs('the adaptive stack', roster), [])

    def test_no_roster_no_guess(self):
        self.assertEqual(serve._extract_agent_refs('Ada was here'), [])


class RoomRefs(unittest.TestCase):
    def test_rooms(self):
        out = serve._extract_room_refs('the observatory and bank rooms')
        self.assertIn('observatory', out)
        self.assertIn('bank', out)

    def test_no_substring_false_positive(self):
        self.assertEqual(serve._extract_room_refs('the libraries are closed'), [])


class RefPresent(unittest.TestCase):
    def test_exact_vs_superset(self):
        self.assertTrue(serve._entity_ref_present('task-123', 'requeued task-123 today'))
        self.assertFalse(serve._entity_ref_present('task-123', 'requeued task-1234 today'))
        self.assertFalse(serve._entity_ref_present('task-123', 'no refs at all'))


class ExtractAll(unittest.TestCase):
    def test_families(self):
        out = serve._extract_entities('Ada finished task-77 in the observatory',
                                      roster={'Ada': 'ada'})
        self.assertEqual(out['tasks'], ['task-77'])
        self.assertEqual(out['agents'], ['ada'])
        self.assertEqual(out['rooms'], ['observatory'])

    def test_skipped_family_is_empty(self):
        out = serve._extract_entities('task-77 in the observatory')
        self.assertEqual(out['agents'], [])


if __name__ == '__main__':
    unittest.main()
