"""Worker memory (2026-11-01): agent long-term memory + runbook retrieval.

Two fixes for the same gap: the org's workers never kept what they learned.
(1) A landed, judged deliverable distills typed work-learnings into
agent_ltm (agents/memory.py), cadence-bounded off the tick thread, and the
agent's next task recalls its own relevant learnings beside the coaching
note. (2) The runbook note retrieves the MOST RELEVANT prior incident
instead of always the last one.

Hermetic: DB redirected to a throwaway temp dir; the model call
(memory._chat_call) is mocked so nothing is ever billed; the daemon worker
is disabled and the deterministic seam (_agent_memory_pending_sync) is used.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402
import agents.memory as memory  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-worker-memory-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _reply(*memories):
    """A fake _chat_call reply in the extractor's JSON contract."""
    import json
    return json.dumps({'memories': list(memories)})


class RunbookRetrieval(unittest.TestCase):
    def test_relevant_older_incident_beats_newer_unrelated(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'The API returned nulls under retries; added a guard.'},
            {'ts': 2, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'Font loading raced the carousel; serialized paints.'}]}}
        note = sim._runbook_note_for(
            state, 'prd-1', instructions='Fix the API null handling again')
        self.assertIn('retries', note)

    def test_real_account_preferred_over_filler(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': False,
             'summary': 'Build pipeline flaked -- fixed by Ada.'},
            {'ts': 2, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'The build pipeline raced the cache purge; pinned hashes.'}]}}
        note = sim._runbook_note_for(
            state, 'prd-1', instructions='Build pipeline cache purge flaked')
        self.assertIn('pinned hashes', note)

    def test_verified_fix_outranks_equal_overlap(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': True,
             'restoreVerified': False,
             'summary': 'Webhook retries duplicated orders; deduped by id.'},
            {'ts': 2, 'room': 'pressoffice', 'summaryIsReal': True,
             'restoreVerified': True,
             'summary': 'Webhook retries duplicated orders; deduped by idempotency key.'}]}}
        note = sim._runbook_note_for(
            state, 'prd-1', instructions='Webhook retries duplicate orders')
        self.assertIn('idempotency key', note)

    def test_no_overlap_falls_back_to_last_entry(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'The API returned nulls; added a retry guard.'}]}}
        note = sim._runbook_note_for(
            state, 'prd-1', instructions='Something completely unrelated here')
        self.assertIn('retry guard', note)

    def test_no_instructions_keeps_last_entry(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'First incident.'},
            {'ts': 2, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'Second incident.'}]}}
        self.assertIn('Second incident', sim._runbook_note_for(state, 'prd-1'))

    def test_augment_passes_instructions_to_the_runbook(self):
        state = {'runbooks': {'prd-1': [
            {'ts': 1, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'The API returned nulls under retries; added a guard.'},
            {'ts': 2, 'room': 'pressoffice', 'summaryIsReal': True,
             'summary': 'Font loading raced the carousel; serialized paints.'}]}}
        state['agents'] = {'ben': {'name': 'Ben'}}
        aug = sim._augment_task_instructions(state, 'ben', 'prd-1',
                                             'Fix the API null handling')
        self.assertIn('retries', aug)
        self.assertNotIn('carousel', aug)


class EnqueueGates(unittest.TestCase):
    def setUp(self):
        self._disabled = unittest.mock.patch.object(
            sim, '_AGENT_MEMORY_WORKER_DISABLED', True)
        self._disabled.start()
        self.addCleanup(self._disabled.stop)
        sim._pending_agent_memory.clear()
        sim._agent_last_extract.clear()
        self.addCleanup(sim._pending_agent_memory.clear)
        self.addCleanup(sim._agent_last_extract.clear)

    def _state(self, note='x' * 200):
        return {'agents': {'ada': {'name': 'Ada'}},
                'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'ada',
                                     'title': 'Fix the API retries',
                                     'room': 'pressoffice', 'note': note,
                                     'taskType': 'code'}}}

    def test_long_note_enqueued(self):
        state = self._state()
        task = state['tasks']['task-1']
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ada'}, 1_700_000_000_000)
        self.assertEqual(len(sim._pending_agent_memory), 1)

    def test_short_note_skipped(self):
        state = self._state(note='looks fine')
        task = state['tasks']['task-1']
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ada'}, 1_700_000_000_000)
        self.assertEqual(sim._pending_agent_memory, {})

    def test_cadence_skips_the_second_extraction(self):
        state = self._state()
        task = state['tasks']['task-1']
        base = 1_700_000_000_000
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ada'}, base)
        sim._pending_agent_memory.clear()
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ada'}, base + 60_000)
        self.assertEqual(sim._pending_agent_memory, {})

    def test_off_roster_agent_skipped(self):
        state = self._state()
        state['agents'] = {}
        task = state['tasks']['task-1']
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ghost'}, 1_700_000_000_000)
        self.assertEqual(sim._pending_agent_memory, {})


class AgentMemoryStoreAndRecall(unittest.TestCase):
    def setUp(self):
        memory._ensure_tables()

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM agent_ltm')

    def test_extract_stores_typed_memories(self):
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'fact', 'content': 'The CI gate needs a clean checkout first.',
                     'confidence': 0.9, 'source': 'agent_inferred'})):
            n = memory.agent_ltm_extract('ada', 'Fix the CI retries',
                                         'The CI gate kept failing; a clean checkout fixed it.',
                                         'pressoffice')
        self.assertEqual(n, 1)
        block = memory.agent_ltm_recall('ada', 'the CI gate is failing again')
        self.assertIn('clean checkout', block)

    def test_recall_is_scoped_to_the_agent(self):
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'fact', 'content': 'Ada learned about API retries.',
                     'confidence': 0.9, 'source': 'agent_inferred'})):
            memory.agent_ltm_extract('ada', 't', 'note about API retries', 'r')
        self.assertIsNone(memory.agent_ltm_recall('ben', 'API retries'))

    def test_higher_confidence_recalls_first(self):
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'fact', 'content': 'The deploy needs a warm cache.',
                     'confidence': 0.3, 'source': 'agent_inferred'},
                    {'type': 'fact', 'content': 'A failing deploy needs a clean tree first.',
                     'confidence': 0.95, 'source': 'agent_inferred'})):
            memory.agent_ltm_extract('ada', 'Deploy troubles', 'deploys were flaky', 'bank')
        block = memory.agent_ltm_recall('ada', 'the deploy keeps failing', limit=1)
        self.assertIn('clean tree', block)
        self.assertNotIn('warm cache', block)

    def test_stale_or_low_confidence_not_recalled(self):
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'fact', 'content': 'Ancient uncertain note.',
                     'confidence': 0.1, 'source': 'agent_inferred'})):
            memory.agent_ltm_extract('ada', 'old task', 'something long ago', 'r')
        self.assertIsNone(memory.agent_ltm_recall('ada', 'ancient uncertain note'))

    def test_cap_keeps_newest_forty(self):
        def _varying(system, user, max_tokens=400):
            return _reply({'type': 'fact',
                           'content': f'learning {user[:80]}',
                           'confidence': 0.8, 'source': 'agent_inferred'})
        with unittest.mock.patch.object(memory, '_chat_call', side_effect=_varying):
            for i in range(45):
                memory.agent_ltm_extract('ada', f'task-{i}', f'note {i}', 'r')
        with serve._db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM agent_ltm WHERE agent_id = 'ada' "
                'AND active = 1').fetchone()[0]
        self.assertEqual(n, memory._AGENT_LTM_MAX_PER_AGENT)

    def test_secrets_redacted_before_storage(self):
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'fact',
                     'content': 'the key sk-live-abcdef1234567890abcdef works',
                     'confidence': 0.9, 'source': 'agent_inferred'})):
            memory.agent_ltm_extract('ada', 't', 'note with a leaked key', 'r')
        with serve._db() as conn:
            row = conn.execute(
                "SELECT content FROM agent_ltm WHERE agent_id = 'ada'").fetchone()
        self.assertNotIn('sk-live-abcdef', row[0])

    def test_empty_extraction_writes_nothing(self):
        with unittest.mock.patch.object(memory, '_chat_call', return_value=None):
            n = memory.agent_ltm_extract('ada', 't', 'note', 'r')
        self.assertEqual(n, 0)


class InjectionIntoNextTask(unittest.TestCase):
    def setUp(self):
        self._disabled = unittest.mock.patch.object(
            sim, '_AGENT_MEMORY_WORKER_DISABLED', True)
        self._disabled.start()
        self.addCleanup(self._disabled.stop)
        sim._pending_agent_memory.clear()
        sim._agent_last_extract.clear()
        self.addCleanup(sim._pending_agent_memory.clear)
        self.addCleanup(sim._agent_last_extract.clear)

    def tearDown(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM agent_ltm')

    def test_end_to_end_learn_then_recall(self):
        state = {'agents': {'ada': {'name': 'Ada'}}}
        task = {'id': 'task-1', 'assignedTo': 'ada', 'title': 'Fix the CI retries',
                'room': 'pressoffice', 'taskType': 'code',
                'note': 'The CI gate kept failing on stale checkouts over three runs in a '
                        'row; a clean checkout before the run fixed the '
                        'flakiness for good, and the retry cache was blameless.'}
        sim._maybe_queue_agent_memory(state, task, {'agentId': 'ada'}, 1_700_000_000_000)
        with unittest.mock.patch.object(
                memory, '_chat_call', return_value=_reply(
                    {'type': 'decision',
                     'content': 'A clean checkout before the CI gate run fixes the flakiness.',
                     'confidence': 0.9, 'source': 'agent_inferred'})):
            sim._agent_memory_pending_sync()
        aug = sim._augment_task_instructions(state, 'ada', None,
                                             'The CI gate is flaky again')
        self.assertIn('From your own completed work', aug)
        self.assertIn('clean checkout', aug)

    def test_no_memories_no_injection(self):
        state = {'agents': {'ben': {'name': 'Ben'}}}
        aug = sim._augment_task_instructions(state, 'ben', None, 'Any task')
        self.assertNotIn('From your own completed work', aug)

    def test_merge_drains_the_queue_via_the_grade_path(self):
        state = {'agents': {'ada': {'name': 'Ada'}},
                 'tasks': {'task-1': {'id': 'task-1', 'assignedTo': 'ada',
                                      'title': 'Fix the CI retries',
                                      'room': 'pressoffice', 'taskType': 'code',
                                      'note': 'x' * 200}},
                 'completedDeliverables': [
                     {'id': 'task-1', 'gradePending': True, 'grade': 5.0,
                      'gradeIsReal': False}]}
        with unittest.mock.patch.object(
                sim, '_grade_results', {'task-1': {'grade': 9.0, 'agentId': 'ada',
                                                   'room': 'pressoffice',
                                                   'title': 'Fix the CI retries'}}):
            with unittest.mock.patch.object(
                    sim, '_grade_results_lock', unittest.mock.MagicMock()):
                sim._merge_completed_grades(state, now_ms=1_700_000_000_000)
        self.assertEqual(len(sim._pending_agent_memory), 1)


if __name__ == '__main__':
    unittest.main()
