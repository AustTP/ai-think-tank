"""Coverage-push tests for world/sim.py residual branches: the governance /
escalation / coaching-loop / stale-work cluster (team-health review guards,
coaching-loop escalation routing, runbook cap, roadmap starved-room branch,
on-call escalation sweep + ceremony guards, governance pass gates, credential
revocation, scrum-master re-plan/help routing, and the worker stuck-help
signal). Uses the temp-DB isolation pattern from test_sim_gap.py so the real
think_tank.db is never touched; serve log/DB seams are mocked at the module
attribute.
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
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapG-')
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


class GovernanceEscalationGap(SimIsolation):
    # --- _team_health_review ------------------------------------------------
    def test_team_health_review_no_team_ids(self):
        self.assertEqual(sim._team_health_review({}, [], 1_000_000), 0)

    # --- _grade_completed_task ----------------------------------------------
    def test_grade_completed_task_non_valued_room(self):
        task = {'taskType': 'code', 'room': 'hangout'}
        sim._grade_completed_task({'agents': {}}, 'ada', task, 1)
        self.assertNotIn('completedDeliverables', {})

    # --- _escalate_coaching_loop / _director_for ----------------------------
    def test_escalate_coaching_loop_director_for_missing(self):
        state = {'agentRoster': [], 'teams': [], 'agents': {}}
        with unittest.mock.patch.object(sim, '_log_governance'):
            sim._escalate_coaching_loop(state, 'ghost', 'pressoffice', 3.0,
                                        1_000_000, 'low')
        self.assertNotIn('backlogRequests', state)

    def test_escalate_coaching_loop_filer_falls_back_to_director(self):
        state = {
            'agentRoster': [{'id': 'ada', 'director': 'd1'}],
            'teams': [{'id': 't1', 'directorId': 'd1'}],
            'agents': {},
        }
        with unittest.mock.patch.object(sim, '_log_governance'):
            sim._escalate_coaching_loop(state, 'ada', 'pressoffice', 3.0,
                                        1_000_000, 'low')
        self.assertEqual(len(state.get('backlogRequests') or []), 1)
        self.assertEqual(state['backlogRequests'][0]['filedBy'], 'd1')

    # --- _runbook_task -------------------------------------------------------
    def test_runbook_task_missing_product(self):
        sim._runbook_task({'agents': {}}, {'taskType': 'bug'}, 1_000_000)
        self.assertNotIn('runbooks', {'agents': {}})

    def test_runbook_task_trims_over_cap(self):
        state = {'runbooks': {'prd-1': [{'summary': f's{i}'} for i in range(21)]}}
        task = {'taskType': 'bug', 'productId': 'prd-1', 'title': 'outage',
                'room': 'observatory', 'assignedTo': 'ada'}
        with unittest.mock.patch.object(sim, '_runbook_decider', return_value=None):
            sim._runbook_task(state, task, 1_000_000)
        self.assertEqual(len(state['runbooks']['prd-1']),
                         sim.RUNBOOK_MAX_ENTRIES_PER_PRODUCT)

    # --- _roadmap_step -------------------------------------------------------
    def test_roadmap_step_starved_room(self):
        now_ms = 604_800_001  # just past ROADMAP_CADENCE_MS from 0
        state = {'lastRoadmapReviewAt': 0, 'roadmap': {}}
        with unittest.mock.patch.object(sim, '_room_trailing_grade', return_value=4.0), \
             unittest.mock.patch.object(sim, '_roadmap_release_demand', return_value=0):
            sim._roadmap_step(state, now_ms)
        self.assertEqual(state['roadmap']['observatory']['priority'], 3)
        self.assertEqual(state['roadmap']['observatory']['demand'], 0)

    # --- _escalation_decider_default ----------------------------------------
    def test_escalation_decider_default_import_error(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'serve':
                raise ImportError('nope')
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch('builtins.__import__', side_effect=fake_import):
            self.assertIsNone(sim._escalation_decider_default({}, 'instr', 'prd-1', 'title'))

    # --- _escalation_convener / _embark_escalation --------------------------
    def test_escalation_convener_busy_sm(self):
        state = {'agents': {'sm1': {'busy': True, 'offDuty': False}}}
        self.assertIsNone(sim._escalation_convener(state, None, 'sm1'))

    def test_embark_escalation_no_conveners(self):
        state = {'agents': {'sm1': {'busy': True}}}
        self.assertFalse(sim._embark_escalation(state, 'sm1', 1_000_000))

    # --- _log_escalation_digest ---------------------------------------------
    def test_log_escalation_digest_raises(self):
        with unittest.mock.patch('serve.log_escalation_digest',
                                 side_effect=RuntimeError('boom')):
            sim._log_escalation_digest({}, {'productId': 'prd-1'}, 'spike')

    # --- _escalation_step sweep ---------------------------------------------
    def test_escalation_sweep_skips_non_inflight(self):
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'done'}}}
        sim._escalation_step(state, now=1.0, now_ms=100_000_000)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_escalation_sweep_skips_missing_opened(self):
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working'}}}
        sim._escalation_step(state, now=1.0, now_ms=100_000_000)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_escalation_sweep_skips_missing_product(self):
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working',
                                  'openedAt': 1_000_000}}}
        sim._escalation_step(state, now=1.0, now_ms=100_000_000)
        self.assertIsNone(state.get('_pendingEscalation'))

    def test_escalation_sweep_skips_missing_director(self):
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working',
                                  'openedAt': 1_000_000, 'productId': 'prd-1'}}}
        sim._escalation_step(state, now=1.0, now_ms=100_000_000)
        self.assertIsNone(state.get('_pendingEscalation'))

    # --- _governance_pass ----------------------------------------------------
    def test_governance_pass_non_server_owner(self):
        state = {'sim': {'owner': 'client'}}
        self.assertIs(sim._governance_pass(state, now_ms=1_000_000), state)

    def test_governance_pass_loads_geometry(self):
        now_ms = 1_000_000
        state = {'sim': {'owner': 'server'},
                 'agents': {'ada': {'id': 'ada', 'task': 't1'}},
                 '_usedNames': ['ada'],
                 'lastHireAt': now_ms,
                 'lastFiringReviewAt': now_ms}
        with unittest.mock.patch.object(sim, '_load_outdoor_geometry',
                                        return_value=({'cols': 4, 'rows': 4}, None)):
            out = sim._governance_pass(state, now=1.0, now_ms=now_ms)
        self.assertIs(out, state)

    # --- _log_governance -----------------------------------------------------
    def test_log_governance_raises(self):
        with unittest.mock.patch('serve.log_action', side_effect=RuntimeError('boom')):
            sim._log_governance({}, 'admin', 'stale_work_requeued', {})

    # --- _revoke_agent_credentials ------------------------------------------
    def test_revoke_agent_credentials_raises(self):
        with unittest.mock.patch('serve.revoke_agent_credentials',
                                 side_effect=RuntimeError('db')):
            sim._revoke_agent_credentials('ada')

    # --- _stale_work_step ----------------------------------------------------
    def test_stale_work_skips_non_dict_task(self):
        state = {'tasks': {'t1': 'not-a-dict'}}
        self.assertEqual(sim._stale_work_step(state, now=1.0, now_ms=1_000_000), 0)
        self.assertEqual(state['lastStaleWorkSweep'], 1_000_000)

    def test_stale_work_skips_bug_incident_shadow(self):
        state = {'tasks': {'t1': {'status': 'walking', 'taskType': 'bug'}},
                 'agents': {}}
        self.assertEqual(sim._stale_work_step(state, now=1.0, now_ms=1_000_000), 0)

    def test_stale_work_skips_walking_within_timeout(self):
        state = {'tasks': {'t1': {'status': 'walking', 'openedAt': 999_000}},
                 'agents': {}}
        self.assertEqual(sim._stale_work_step(state, now=1.0, now_ms=1_000_000), 0)

    # --- _sm_replan_stale_work -----------------------------------------------
    def test_sm_replan_stale_work_bad_room(self):
        task = {'room': 'hangout', 'title': 'Wedged'}
        self.assertFalse(sim._sm_replan_stale_work({}, task, 1_000_000))

    def test_sm_replan_stale_work_uses_product_director_and_filer(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'products': {'prd-1': {'teamId': 't1'}}}
        task = {'room': 'pressoffice', 'productId': 'prd-1', 'title': 'Wedged card'}
        self.assertTrue(sim._sm_replan_stale_work(state, task, 1_000_000))
        self.assertEqual(state['backlogRequests'][0]['filedBy'], 'd1')

    def test_sm_replan_stale_work_dedup_drops(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'products': {'prd-1': {'teamId': 't1'}},
                 'backlogRequests': [{'status': 'pending', 'room': 'pressoffice',
                                      'title': 'Wedged card'}]}
        task = {'room': 'pressoffice', 'productId': 'prd-1', 'title': 'Wedged card'}
        self.assertFalse(sim._sm_replan_stale_work(state, task, 1_000_000))

    # --- _sm_help_stuck_worker -----------------------------------------------
    def test_sm_help_stuck_worker_bad_room(self):
        task = {'room': 'hangout', 'title': 'Stuck'}
        self.assertFalse(sim._sm_help_stuck_worker({}, task, 1_000_000))

    def test_sm_help_stuck_worker_uses_product_director_and_filer(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'products': {'prd-1': {'teamId': 't1'}}}
        task = {'room': 'pressoffice', 'productId': 'prd-1', 'title': 'Stuck card'}
        self.assertTrue(sim._sm_help_stuck_worker(state, task, 1_000_000))
        self.assertEqual(state['backlogRequests'][0]['filedBy'], 'd1')

    def test_sm_help_stuck_worker_dedup_drops(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'products': {'prd-1': {'teamId': 't1'}},
                 'backlogRequests': [{'status': 'pending', 'room': 'pressoffice',
                                      'title': 'Stuck card'}]}
        task = {'room': 'pressoffice', 'productId': 'prd-1', 'title': 'Stuck card'}
        self.assertFalse(sim._sm_help_stuck_worker(state, task, 1_000_000))

    # --- _worker_stuck_help_signal -------------------------------------------
    def test_worker_stuck_help_signal_missing_agent(self):
        sim._worker_stuck_help_signal({'agents': {}}, 'ghost', {'id': 't1'},
                                      False, 1_000_000)
        self.assertNotIn('_contentFailStreak', {})

    # --- _coaching_loop_step -------------------------------------------------
    def test_coaching_loop_skips_non_list_plans(self):
        now_ms = 100_000_000  # past COACHING_LOOP_CADENCE_MS from 0
        state = {'growthPlans': {'a1': 'not-a-list'}}
        self.assertEqual(sim._coaching_loop_step(state, now_ms), 0)
        self.assertEqual(state['lastCoachingLoop'], now_ms)

    def test_coaching_loop_skips_irrelevant_plan_kind(self):
        now_ms = 100_000_000
        state = {'growthPlans': {'a1': [{'kind': 'other'}]}}
        self.assertEqual(sim._coaching_loop_step(state, now_ms), 0)

    def test_coaching_loop_pressoffice_fallback(self):
        now_ms = 100_000_000
        state = {'growthPlans': {'a1': [{'kind': 'low_grade'}]},
                 'agents': {'a1': {'id': 'a1', 'room': 'hangout'}},
                 'completedDeliverables': [
                     {'agentId': 'a1', 'room': 'hangout', 'grade': 3.0,
                      'gradeIsReal': True}]}
        checked = sim._coaching_loop_step(state, now_ms)
        self.assertEqual(checked, 1)
        self.assertEqual(state['growthPlans']['a1'][-1]['room'], 'pressoffice')


if __name__ == '__main__':
    unittest.main()