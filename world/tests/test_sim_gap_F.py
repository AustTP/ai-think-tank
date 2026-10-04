"""Coverage-push tests for world/sim.py residual branches: pipeline intake
(add_pipeline offset coercion), the distill/schedule/pipeline sweeps' guard
branches, social attendance edge cases, large-request intake, description
normalization, and the grading/runbook decider fallback paths.
Uses the temp-DB isolation pattern from test_sim_gap.py so the real
think_tank.db is never touched; serve network/DB seams are mocked.
"""
import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapF-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        sim._content_results.clear()


class PipelineIntake(SimIsolation):
    def test_add_pipeline_coerces_bad_offset_ms(self):
        state = {}
        rec = sim.add_pipeline(state, 'release', 1000,
                               [{'title': 'build', 'room': 'observatory',
                                 'offsetMs': 'not-a-number'}])
        self.assertIsNotNone(rec)
        self.assertEqual(rec['steps'][0]['offsetMs'], 0)


class DistillGate(SimIsolation):
    def test_distill_has_new_archives_skips_subdirs_and_old_files(self):
        archive = tempfile.mkdtemp(prefix='simgapF-archive-')
        try:
            os.makedirs(os.path.join(archive, 'nested'))
            with open(os.path.join(archive, 'old.md'), 'w') as f:
                f.write('stale')
            with unittest.mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR', archive):
                self.assertFalse(sim._distill_has_new_archives(2 ** 62))
        finally:
            shutil.rmtree(archive, ignore_errors=True)

    def test_distill_has_new_archives_oserror(self):
        archive = tempfile.mkdtemp(prefix='simgapF-archive-')
        try:
            with unittest.mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR', archive), \
                 unittest.mock.patch('os.listdir', side_effect=OSError('boom')):
                self.assertFalse(sim._distill_has_new_archives(0))
        finally:
            shutil.rmtree(archive, ignore_errors=True)


class ScheduleAndPipelineSweeps(SimIsolation):
    def test_check_schedules_fires_due_research_topic(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'cryptography', 'startUrl': 'http://x',
             'lastRunAt': 0, 'cadenceMs': 0}]}
        with unittest.mock.patch.object(sim, '_check_pipelines'), \
             unittest.mock.patch.object(sim, '_pending_player_ask_sweep'), \
             unittest.mock.patch.object(sim, '_supervisor_block_vote_sweep'), \
             unittest.mock.patch.object(sim, '_skill_review_has_pending',
                                        return_value=False), \
             unittest.mock.patch.object(sim, '_distill_has_new_archives',
                                        return_value=False):
            sim._check_schedules(state, 1000.0, 1000000)
        self.assertEqual(state['researchTopics'][0]['lastRunAt'], 1000000)
        self.assertTrue(any('Scheduled research' in w['title']
                            for w in state['workQueue']))

    def test_check_pipelines_skips_pipeline_without_steps(self):
        state = {'pipelines': [{'id': 'p1', 'steps': []}]}
        sim._check_pipelines(state, 1000000)
        self.assertNotIn('runStepIndex', state['pipelines'][0])
        self.assertEqual(state.get('workQueue'), None)

    def test_check_pipelines_holds_on_queued_next_step(self):
        state = {
            'pipelines': [{'id': 'p1', 'cadenceMs': 1000, 'lastRunAt': 0,
                           'runId': 1, 'runStepIndex': 0,
                           'steps': [{'title': 's0', 'room': 'r'},
                                     {'title': 's1', 'room': 'r'}]}],
            'tasks': {
                't0': {'id': 't0', 'status': 'done',
                       'pipelineStep': {'pipelineId': 'p1', 'runId': 1,
                                        'stepIndex': 0}},
                't1': {'id': 't1', 'status': 'working',
                       'pipelineStep': {'pipelineId': 'p1', 'runId': 1,
                                        'stepIndex': 1}},
            },
        }
        sim._check_pipelines(state, 1000)
        self.assertEqual(state['pipelines'][0]['runStepIndex'], 1)
        self.assertEqual(state.get('workQueue'), None)


class SocialEdgeCases(SimIsolation):
    def test_social_attendees_skips_non_dict_agent(self):
        state = {'agents': {'ghost': 'not-a-dict',
                            'ada': {'id': 'ada', 'weekApprovals': 2}}}
        out = sim._social_attendees(state, {'embarked': False})
        self.assertEqual(out, {'ada': True})

    def test_convene_social_skips_vanished_attendee(self):
        state = {'agents': {}, 'tasks': {}}
        pending = {'embarked': False, 'at': 0, 'people': {}}
        with unittest.mock.patch.object(sim, '_social_attendees',
                                        return_value={'ghost': True}), \
             unittest.mock.patch('serve.log_action'):
            sim._convene_social(state, pending, 1234)
        self.assertTrue(pending['embarked'])
        self.assertEqual(pending['people'], {})

    def test_resolve_social_skips_vanished_attendee(self):
        state = {'agents': {}, 'agentRoster': [], '_pendingSocial': True}
        pending = {'people': {'ghost': {'offDuty': False, 'visible': True,
                                        'x': 0, 'y': 0, 'dir': 'south',
                                        'task': None, 'busy': False,
                                        'inRoom': None}}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_social_digest'):
            sim._resolve_social(state, pending, 5000,
                                decider=lambda *a: ('skip', 0.1))
        self.assertNotIn('_pendingSocial', state)


class RequestAndDescription(SimIsolation):
    def test_file_large_request_rejects_blank_goal(self):
        self.assertIsNone(sim.file_large_request({}, 'authority', '', 'team-1'))

    def test_normalize_gwt_block_missing_markers(self):
        self.assertIsNone(sim.normalize_gwt_block('given something when something'))

    def test_normalize_description_plain_gwt_string(self):
        self.assertEqual(
            sim._normalize_description('given foo when bar then baz'),
            (None, 'given foo, when bar, then baz'))


class DeciderFallbacks(SimIsolation):
    def _fake_import_raises_serve(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'serve':
                raise ImportError('offline')
            return real_import(name, *args, **kwargs)

        return unittest.mock.patch('builtins.__import__', side_effect=fake_import)

    def test_grading_decider_default_import_fails(self):
        with self._fake_import_raises_serve():
            self.assertIsNone(
                sim._grading_decider_default({}, 'instructions', 'title', 'room'))

    def test_runbook_decider_default_import_fails(self):
        with self._fake_import_raises_serve():
            self.assertIsNone(
                sim._runbook_decider_default({}, 'instr', 'p1', 'room', 'title'))

    def test_runbook_decider_default_call_raises(self):
        with unittest.mock.patch('serve._resolve_model_tier',
                                 side_effect=RuntimeError('boom')):
            self.assertIsNone(
                sim._runbook_decider_default({}, 'instr', 'p1', 'room', 'title'))


if __name__ == '__main__':
    unittest.main()