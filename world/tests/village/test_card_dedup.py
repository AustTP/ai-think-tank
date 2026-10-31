"""Anti-entropy card dedup (scale slice 2).

Same-title re-cards are the dominant duplicate waste: chief backlog
directives minting issues for already-open work, re-graded observatory
cards, re-requests. queue_work / queue_once refuse to open a second card
while an OPEN twin (queued / walking / working / needs_review / blocked)
exists under the same normalized key, and log the refusal. Wrapper titles
('Review: X', 'Fix issues found in review testing of "X"') deliberately do
NOT collapse to X: those are lifecycle edges (the gate reviews open work)
that dedup must never block. A failed/recovering original never blocks its
own recovery chain.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-card-dedup-')
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


def _state_with_open_task(title, status='queued'):
    return {
        'workQueue': [],
        'tasks': {'task-1': {'id': 'task-1', 'title': title,
                             'status': status}},
        'agentRoster': [], 'agents': {}, 'playerInbox': [],
    }


class DedupKeyTest(unittest.TestCase):
    def test_normalization_collapses_case_spacing_and_re_markers(self):
        self.assertEqual(sim.card_dedup_key('Fix the observatory route'),
                         sim.card_dedup_key('  FIX   the  Observatory  Route '))
        self.assertEqual(sim.card_dedup_key('Re: Fix the observatory route'),
                         sim.card_dedup_key('fix the observatory route'))
        self.assertEqual(sim.card_dedup_key('Re-Request: [STORY] Fix it'),
                         sim.card_dedup_key('fix it'))

    def test_wrapper_titles_do_not_collapse_to_their_target(self):
        # Lifecycle edges: a review of open work and a review-fix card are
        # legitimate cards in their own right, never dedup-equal to the work
        # they reference.
        base = sim.card_dedup_key('Fix the observatory route')
        self.assertNotEqual(sim.card_dedup_key('Review: Fix the observatory route'), base)
        self.assertNotEqual(
            sim.card_dedup_key('Fix issues found in review testing of "Review: Fix the observatory route"'),
            base)
        self.assertNotEqual(
            sim.card_dedup_key('Fix issues found in review testing of "Review: Fix the observatory route"'),
            sim.card_dedup_key('Review: Fix the observatory route'))

    def test_empty_key_never_dedups(self):
        self.assertIsNone(sim.card_dedup_key(''))
        self.assertIsNone(sim.card_dedup_key('re: '))
        self.assertIsNone(sim.card_dedup_key(None))


class QueueDedupTest(unittest.TestCase):
    def setUp(self):
        self.state = {'workQueue': [], 'tasks': {}, 'agentRoster': [],
                      'agents': {}, 'playerInbox': []}

    def test_queue_once_refuses_open_twin_and_logs(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Fix the observatory route',
            'status': 'working'}
        at = int(time.time() * 1000) + 60000
        item = sim.queue_once(self.state, 'Fix the observatory route', at)
        self.assertIsNone(item)
        self.assertEqual(self.state['workQueue'], [])

    def test_queue_once_force_bypasses(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Fix the observatory route',
            'status': 'working'}
        at = int(time.time() * 1000) + 60000
        item = sim.queue_once(self.state, 'Fix the observatory route', at,
                              force=True)
        self.assertIsNotNone(item)
        self.assertEqual(len(self.state['workQueue']), 1)

    def test_terminal_twin_does_not_block(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Fix the observatory route',
            'status': 'done'}
        at = int(time.time() * 1000) + 60000
        item = sim.queue_once(self.state, 'Fix the observatory route', at)
        self.assertIsNotNone(item)

    def test_failed_original_does_not_block_its_recovery(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Land the norms brief',
            'status': 'failed'}
        at = int(time.time() * 1000) + 60000
        item = sim.queue_once(self.state, 'Land the norms brief', at)
        self.assertIsNotNone(item)

    def test_work_queue_twin_blocks(self):
        self.state['workQueue'] = [{'title': 'Distill recent think tank knowledge'}]
        at = int(time.time() * 1000) + 60000
        self.assertIsNone(sim.queue_once(
            self.state, 'distill  recent think tank KNOWLEDGE', at))

    def test_queue_work_skips_twin_keeps_rest(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Fix the observatory route',
            'status': 'blocked'}
        added = sim.queue_work(self.state, [
            {'title': 'Fix the observatory route'},
            {'title': 'Fix the observatory route', 'force': True},
            {'title': 'An unrelated task'},
        ])
        titles = [w['title'] for w in self.state['workQueue']]
        self.assertEqual(added, 2)
        self.assertEqual(titles, ['Fix the observatory route',
                                  'An unrelated task'])

    def test_case_and_punctuation_normalization_matches(self):
        self.state['tasks']['task-1'] = {
            'id': 'task-1', 'title': 'Fix  the Observatory route!',
            'status': 'queued'}
        at = int(time.time() * 1000) + 60000
        self.assertIsNone(sim.queue_once(
            self.state, '[STORY] fix the observatory route', at))


if __name__ == '__main__':
    unittest.main()
