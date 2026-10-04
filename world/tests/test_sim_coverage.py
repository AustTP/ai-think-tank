"""Coverage-push tests for world/sim.py: the social ceremony pipeline, the
Jev-backed decider defaults, the movement stuck/replan/respawn/cancel branches,
and the small never-called helpers. Uses the temp-DB isolation pattern from
test_sim.py so the real think_tank.db is never touched; serve network helpers
are mocked at the module attribute (deciders late-import serve)."""
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think tank-sim-cov-')
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


class SocialCeremony(SimIsolation):
    @staticmethod
    def _social_state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'x': 10, 'y': 20, 'dir': 'south',
                        'busy': True, 'task': 't1', 'inRoom': 'office',
                        'offDuty': False, 'visible': True, 'weekApprovals': 3,
                        'name': 'Ada'},
                'ben': {'id': 'ben', 'x': 30, 'y': 40, 'dir': 'north',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': True, 'visible': False, 'weekApprovals': 2,
                        'name': 'Ben'},
                'cat': {'id': 'cat', 'x': 50, 'y': 60, 'dir': 'south',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': False, 'visible': True, 'weekApprovals': 0,
                        'name': 'Cat'},
                'dan': {'id': 'dan', 'x': 70, 'y': 80, 'dir': 'south',
                        'busy': False, 'task': None, 'inRoom': None,
                        'offDuty': False, 'visible': True, 'weekApprovals': 5,
                        'handoff': {'with': 'ada'}, 'name': 'Dan'},
            },
            'tasks': {'t1': {'status': 'walking', 'workUntil': 1000}},
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada'}, {'id': 'ben', 'name': 'Ben'},
                {'id': 'cat', 'name': 'Cat'}, {'id': 'dan', 'name': 'Dan'},
            ],
        }

    def test_attendees_filters_by_work_handoff_pair(self):
        state = self._social_state()
        state['agents']['eve'] = {'id': 'eve', 'weekApprovals': 4,
                                  'pairWith': 'ada', 'offDuty': False}
        out = sim._social_attendees(state, {'embarked': False})
        self.assertEqual(out, {'ada': True, 'ben': True})
        self.assertNotIn('cat', out)   # weekApprovals <= 0
        self.assertNotIn('dan', out)   # mid-handoff
        self.assertNotIn('eve', out)   # mid-pair

    def test_convene_social_snapshots_workers_and_off_duty(self):
        state = self._social_state()
        pending = {'embarked': False, 'at': 0, 'people': {}}
        with unittest.mock.patch('serve.log_action'):
            sim._convene_social(state, pending, 1234)
        self.assertTrue(pending['embarked'])
        self.assertEqual(pending['at'], 1234 + sim.SOCIAL_MEET_MS)
        self.assertEqual(set(pending['people']), {'ada', 'ben'})
        ada = state['agents']['ada']
        self.assertEqual(ada['inRoom'], 'hangout')
        self.assertEqual(ada['task'], None)
        self.assertTrue(ada['busy'])
        self.assertTrue(ada['visible'])
        self.assertFalse(ada['offDuty'])
        self.assertEqual(state['tasks']['t1']['_inSocial'], 1234)
        ben = state['agents']['ben']
        self.assertTrue(ben['visible'])      # brought into the Hangout
        self.assertFalse(ben['offDuty'])
        self.assertEqual(pending['people']['ben']['offDuty'], True)

    def test_restore_worker_extends_workuntil(self):
        state = self._social_state()
        snapshot = {'offDuty': False, 'visible': True, 'x': 10, 'y': 20,
                    'dir': 'south', 'task': 't1', 'busy': True,
                    'inRoom': 'office', 'workUntil': 1000}
        sim._restore_social_agent(state, 'ada', snapshot, 1234, 60000)
        a = state['agents']['ada']
        self.assertEqual(a['task'], 't1')
        self.assertTrue(a['busy'])
        self.assertEqual(a['inRoom'], 'office')
        self.assertTrue(a['visible'])
        self.assertNotIn('_inSocial', state['tasks']['t1'])
        self.assertEqual(state['tasks']['t1']['workUntil'], 1060)

    def test_restore_off_duty_and_idle_and_vanished(self):
        state = self._social_state()
        sim._restore_social_agent(state, 'ben',
                                  {'offDuty': True, 'task': None, 'visible': False},
                                  1234, 60000)
        self.assertTrue(state['agents']['ben']['offDuty'])
        self.assertFalse(state['agents']['ben']['visible'])
        sim._restore_social_agent(state, 'cat',
                                  {'offDuty': False, 'task': None, 'visible': True},
                                  1234, 60000)
        self.assertFalse(state['agents']['cat']['busy'])
        self.assertTrue(state['agents']['cat']['visible'])
        sim._restore_social_agent(state, 'ghost', {'offDuty': False, 'task': None},
                                  1234, 60000)  # vanished -> no-op

    def test_resolve_social_writes_tape_and_growth_plans(self):
        state = self._social_state()
        state['agents']['ben']['offDuty'] = False
        pending = {
            'people': {
                'ada': {'offDuty': False, 'visible': True, 'x': 10, 'y': 20,
                        'dir': 'south', 'task': 't1', 'busy': True,
                        'inRoom': 'office', 'workUntil': 1000},
                'ben': {'offDuty': False, 'visible': False, 'x': 30, 'y': 40,
                        'dir': 'north', 'task': None, 'busy': False,
                        'inRoom': None},
            },
        }
        choices = iter([('adopt', 0.9), ('note', 0.5)])
        decider = lambda instructions, criteria: next(choices)
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_social_digest') as digest:
            sim._resolve_social(state, pending, 5000, decider=decider)
        self.assertNotIn('_pendingSocial', state)
        self.assertEqual(state['agents']['ada']['weekApprovals'], 0)
        self.assertEqual(state['agents']['ben']['weekApprovals'], 0)
        self.assertEqual(len(state.get('_socialDecisions') or []), 2)
        plans = (state.get('growthPlans') or {}).get('ada') or []
        self.assertEqual(len(plans), 1)          # adopt + high confidence
        self.assertEqual(plans[0]['kind'], 'social_adopt')
        self.assertTrue(plans[0]['applied'] is False)
        self.assertEqual((state.get('growthPlans') or {}).get('ben'), None)
        digest.assert_called_once()

    def test_resolve_social_survives_digest_outage(self):
        state = self._social_state()
        pending = {'people': {}}
        with unittest.mock.patch('serve.log_action'):
            with unittest.mock.patch('serve.log_social_digest',
                                     side_effect=RuntimeError('boom')):
                sim._resolve_social(state, pending, 5000, decider=lambda *a: ('skip', 0.1))
        self.assertNotIn('_pendingSocial', state)

    def test_social_step_schedules_then_convenes_then_resolves(self):
        state = self._social_state()
        state['agents']['ben']['offDuty'] = False
        now_ms = sim.SOCIAL_CADENCE_MS + 1000
        with unittest.mock.patch('serve.log_action'):
            sim._social_step(state, now_ms / 1000, now_ms)
            pending = state['_pendingSocial']
            self.assertFalse(pending['embarked'])
            self.assertEqual(state['lastSocialAt'], now_ms)
            sim._social_step(state, now_ms / 1000, now_ms)
            self.assertTrue(state['_pendingSocial']['embarked'])
            self.assertEqual(set(state['_pendingSocial']['people']), {'ada', 'ben'})
            sim._social_step(state, now_ms / 1000, now_ms + sim.SOCIAL_MEET_MS + 1,
                             decider=lambda *a: ('skip', 0.1))
        self.assertNotIn('_pendingSocial', state)

    def test_social_step_in_flight_before_cadence_is_noop(self):
        state = {'agents': {}, 'lastSocialAt': sim.SOCIAL_CADENCE_MS}
        sim._social_step(state, 0, sim.SOCIAL_CADENCE_MS - 1)
        self.assertNotIn('_pendingSocial', state)

    def test_social_decider_default_returns_choice(self):
        data = {'answers': {'q': {'choice': 'adopt', 'confidence': 0.9}},
                'usage': {'cost': 0.01}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            choice, conf = sim._social_decider_default(
                'instr', [{'id': 'adopt', 'description': 'x'},
                          {'id': 'skip', 'description': 'y'}])
        self.assertEqual((choice, conf), ('adopt', 0.9))

    def test_social_decider_default_rejects_unknown_and_outage(self):
        data = {'answers': {'q': {'choice': 'bogus'}}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            choice, conf = sim._social_decider_default('instr', [])
        self.assertEqual(choice, None)
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('offline')):
            choice, conf = sim._social_decider_default('instr', [])
        self.assertEqual((choice, conf), (None, 1.0))


class DeciderDefaults(SimIsolation):
    def test_governance_decider_choice_and_outage(self):
        candidates = [{'id': 'a', 'description': 'A'}, {'id': 'b', 'description': 'B'}]
        data = {'answers': {'q': {'choice': 'b'}}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertEqual(sim._governance_decider_default({}, 'i', candidates), 'b')
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'nope'}}}):
            self.assertEqual(sim._governance_decider_default({}, 'i', candidates), None)
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._governance_decider_default({}, 'i', candidates), None)

    def test_refinement_decider_choice_and_outage(self):
        criteria = [{'id': 'accept', 'description': 'yes'},
                    {'id': 'reject', 'description': 'no'}]
        data = {'answers': {'q': {'choice': 'accept'}}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertEqual(sim._refinement_decider_default('i', criteria), 'accept')
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._refinement_decider_default('i', criteria), None)

    def test_breakdown_decider_parses_and_accrues(self):
        data = {'usage': {'cost': 0.02},
                'choices': [{'message': {'content': '{"items":[{"title":"card"}]}'}}]}
        with unittest.mock.patch('serve._resolve_model_tier', return_value='high'), \
             unittest.mock.patch('serve._call_openrouter_sync', return_value=data), \
             unittest.mock.patch('serve._accrue_spend') as accrue:
            out = sim._breakdown_decider_default({}, 'instr', 'build a website')
        self.assertEqual(out['items'][0]['title'], 'card')
        accrue.assert_called_with('__breakdowns__', 0.02)

    def test_breakdown_decider_no_model_and_bad_json(self):
        with unittest.mock.patch('serve._resolve_model_tier', return_value=None):
            self.assertEqual(sim._breakdown_decider_default({}, 'i', 'g'), None)
        data = {'choices': [{'message': {'content': 'no json here'}}]}
        with unittest.mock.patch('serve._resolve_model_tier', return_value='m'), \
             unittest.mock.patch('serve._call_openrouter_sync', return_value=data):
            self.assertEqual(sim._breakdown_decider_default({}, 'i', 'g'), None)

    def test_retro_decider_parses_and_outage(self):
        data = {'usage': {'cost': 0.01},
                'choices': [{'message': {'content': '{"start":"x","stop":"y","continue":"z"}'}}]}
        with unittest.mock.patch('serve._low_tier_slug', return_value='low'), \
             unittest.mock.patch('serve._call_openrouter_sync', return_value=data), \
             unittest.mock.patch('serve._accrue_spend') as accrue:
            out = sim._retro_decider_default({}, 'instr', 'sprint-7', [])
        self.assertEqual(out['start'], 'x')
        accrue.assert_called_with('__retrospectives__', 0.01)
        with unittest.mock.patch('serve._call_openrouter_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._retro_decider_default({}, 'i', 's', []), {})

    def test_grading_decider_score_and_fallback(self):
        data = {'answers': {'q': {'choice': '8'}}}
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value=data), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertEqual(sim._grading_decider_default({}, 'i', 't', 'room'), 8.0)
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'bogus'}}}):
            self.assertEqual(sim._grading_decider_default({}, 'i', 't', 'room'), None)

    def test_runbook_decider_reply_and_failures(self):
        with unittest.mock.patch('serve.SELF_BASE_URL', 'http://x'), \
             unittest.mock.patch('serve.get_or_create_agent_key', return_value='k'), \
             unittest.mock.patch('serve._resolve_model_tier', return_value='low'), \
             unittest.mock.patch('serve._http_json',
                                 return_value={'reply': 'db connection dropped and restored'}):
            out = sim._runbook_decider_default({}, 'instr', 'db', 'room', 'title', agent_id='ada')
        self.assertEqual(out, 'db connection dropped and restored')
        with unittest.mock.patch('serve._resolve_model_tier', return_value=None):
            self.assertEqual(sim._runbook_decider_default({}, 'i', 'db', 'r', 't'), None)
        with unittest.mock.patch('serve._resolve_model_tier', return_value='low'), \
             unittest.mock.patch('serve._http_json', return_value={'error': 'nope'}):
            self.assertEqual(sim._runbook_decider_default({}, 'i', 'db', 'r', 't'), None)

    def test_escalation_decider_story_spike_and_outage(self):
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'story'}}}), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertEqual(sim._escalation_decider_default({}, 'i', 'p', 't'), 'story')
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._escalation_decider_default({}, 'i', 'p', 't'), None)

    def test_supervisor_vote_decider_approve_reject_outage(self):
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'approve'}}}), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertTrue(sim._supervisor_vote_decider_default({}, 'k', 'd', 'a', 'q', 'c'))
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'reject'}}}):
            self.assertFalse(sim._supervisor_vote_decider_default({}, 'k', 'd', 'a', 'q', 'c'))
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('down')):
            self.assertFalse(sim._supervisor_vote_decider_default({}, 'k', 'd', 'a', 'q', 'c'))

    def test_player_ask_decider_choice_and_outage(self):
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 return_value={'answers': {'q': {'choice': 'ask_player'}}}), \
             unittest.mock.patch('serve._jev_model', return_value='m'):
            self.assertEqual(sim._player_ask_decider_default({}, 'k', 'd', 'q', 'c'), 'ask_player')
        with unittest.mock.patch('serve._call_openrouter_decision_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._player_ask_decider_default({}, 'k', 'd', 'q', 'c'),
                             'resolve_internally')

    def test_name_chooser_accepts_valid_and_rejects_taken(self):
        data = {'usage': {'cost': 0.01},
                'choices': [{'message': {'content': ' Zoe '}}]}
        with unittest.mock.patch('serve._low_tier_slug', return_value='low'), \
             unittest.mock.patch('serve._call_openrouter_sync', return_value=data), \
             unittest.mock.patch('serve._accrue_spend') as accrue:
            name = sim._name_chooser_default({}, 'director', set(), 'member')
        self.assertEqual(name, 'Zoe')
        accrue.assert_called_with('__hire_names__', 0.01)
        with unittest.mock.patch('serve._call_openrouter_sync', return_value=data):
            self.assertEqual(sim._name_chooser_default({}, 'd', {'zoe'}, 'member'), None)
        data_bad = {'choices': [{'message': {'content': '123!!'}}]}
        with unittest.mock.patch('serve._call_openrouter_sync', return_value=data_bad):
            self.assertEqual(sim._name_chooser_default({}, 'd', set(), 'member'), None)
        with unittest.mock.patch('serve._call_openrouter_sync',
                                 side_effect=RuntimeError('down')):
            self.assertEqual(sim._name_chooser_default({}, 'd', set(), 'member'), None)


class MovementEdges(SimIsolation):
    @staticmethod
    def _grid():
        return {'cols': 20, 'rows': 20, 'cell': 8,
                'grid': [[0] * 20 for _ in range(20)]}

    def test_slide_east_when_diagonal_blocked(self):
        grid = self._grid()
        for (gx, gy) in ((4, 4), (5, 4), (4, 5), (5, 5)):
            grid['grid'][gy][gx] = 1
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'pathTarget': {'x': 100, 'y': 100}}
        agents = {'a': a}
        events = sim.step_agent_movement(1.0, agents, grid)
        self.assertEqual(events, [])
        self.assertGreater(a['x'], 32)
        self.assertEqual(a['y'], 32)  # x-only slide

    def test_slide_south_when_diagonal_and_x_blocked(self):
        grid = self._grid()
        for (gx, gy) in ((4, 4), (5, 4), (4, 5), (5, 5), (4, 2), (5, 2)):
            grid['grid'][gy][gx] = 1
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'pathTarget': {'x': 100, 'y': 100}}
        agents = {'a': a}
        events = sim.step_agent_movement(1.0, agents, grid)
        self.assertEqual(events, [])
        self.assertEqual(a['x'], 32)
        self.assertGreater(a['y'], 32)  # y-only slide

    @staticmethod
    def _wall_around_agent(grid):
        for (gx, gy) in ((4, 4), (5, 4), (4, 5), (5, 5), (4, 2), (5, 2),
                         (2, 4), (3, 4), (2, 5), (3, 5)):
            grid['grid'][gy][gx] = 1

    def test_stuck_replans_via_find_path(self):
        grid = self._grid()
        self._wall_around_agent(grid)
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'pathTarget': {'x': 100, 'y': 100}}
        agents = {'a': a}
        sim.step_agent_movement(1.0, agents, grid)   # stuckTimer 1.0
        events = sim.step_agent_movement(1.0, agents, grid)  # 2.0 > 1.2 -> replan
        self.assertEqual(events, [])
        self.assertEqual(a['replanCount'], 1)
        self.assertEqual(a['pathIndex'], 0)
        self.assertTrue(a['path'])

    def test_stuck_respawns_via_pick_free_when_replan_count_capped(self):
        grid = self._grid()
        self._wall_around_agent(grid)
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'pathTarget': {'x': 100, 'y': 100}, 'replanCount': 3}
        agents = {'a': a}
        pick_free = lambda avoid, ag: {'x': 300, 'y': 300}
        sim.step_agent_movement(1.0, agents, grid, pick_free=pick_free)  # stuckTimer 1.0
        events = sim.step_agent_movement(1.0, agents, grid, pick_free=pick_free)
        self.assertEqual(events, [])
        self.assertTrue(a.get('respawnedForTask'))
        self.assertEqual(a['x'], 300)
        self.assertEqual(a['y'], 300)

    def test_stuck_respawn_fails_then_cancel_task(self):
        grid = self._grid()
        self._wall_around_agent(grid)
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'pathTarget': {'x': 5000, 'y': 5000}, 'replanCount': 3}
        agents = {'a': a}
        pick_free = lambda avoid, ag: {'x': 300, 'y': 300}
        sim.step_agent_movement(1.0, agents, grid, pick_free=pick_free)
        events = sim.step_agent_movement(1.0, agents, grid, pick_free=pick_free)
        self.assertEqual(events, [('cancel', 'task', 'a')])

    def test_stuck_no_target_cancels_handoff(self):
        grid = self._grid()
        self._wall_around_agent(grid)
        a = {'x': 32, 'y': 32, 'visible': True, 'path': [{'x': 100, 'y': 100}],
             'pathIndex': 0, 'handoff': {'with': 'b'}, 'replanCount': 3}
        agents = {'a': a}
        sim.step_agent_movement(1.0, agents, grid)
        events = sim.step_agent_movement(1.0, agents, grid)
        self.assertEqual(events, [('cancel', 'handoff', 'a')])


class SmallHelpers(SimIsolation):
    def test_add_research_topic_rejects_and_adds(self):
        state = {'researchTopics': []}
        self.assertEqual(sim.add_research_topic(state, '', 'http://x.com', 1000), None)
        self.assertEqual(sim.add_research_topic(state, 't', '', 1000), None)
        self.assertEqual(sim.add_research_topic(state, 't', 'not-a-url', 1000), None)
        rec = sim.add_research_topic(state, 'follow widgets', 'https://example.com',
                                     1000, link_keyword='widget')
        self.assertIsNotNone(rec)
        self.assertEqual(rec['id'], 'topic-1')
        self.assertEqual(rec['cadenceMs'], sim.MIN_RESEARCH_CADENCE_MS)
        self.assertEqual(rec['lastRunAt'], 0)
        self.assertEqual(rec['linkKeyword'], 'widget')
        self.assertEqual(state['researchTopicCounter'], 1)
        self.assertEqual(sim.next_topic_id(state), 'topic-2')

    def test_estimate_employees_for_request_bounds(self):
        self.assertEqual(sim._estimate_employees_for_request(''), 1)
        self.assertEqual(sim._estimate_employees_for_request('short ask'), 1)
        self.assertEqual(sim._estimate_employees_for_request(' '.join(['w'] * 40)), 2)
        self.assertEqual(sim._estimate_employees_for_request(
            'build create develop platform website app system'), 3)
        self.assertEqual(sim._estimate_employees_for_request(' '.join(['w'] * 130)), 3)

    def test_requeue_task_for_agent(self):
        state = {'workQueue': []}
        self.assertTrue(sim._requeue_task_for_agent(state, 'ada', 'task-1', 1234))
        self.assertEqual(state['workQueue'][0]['reviewOf'], 'task-1')
        self.assertTrue(state['workQueue'][0]['_mailResume'])
        self.assertFalse(sim._requeue_task_for_agent({'workQueue': None}, 'a', 't', 1))

    def test_next_new_team_name(self):
        state = {'agentRoster': [], '_usedNames': []}
        self.assertEqual(sim._next_new_team_name(state), sim._NEW_TEAM_NAME_POOL[0])
        state['_usedNames'] = list(sim._NEW_TEAM_NAME_POOL)
        self.assertEqual(sim._next_new_team_name(state), None)

    def test_write_growth_plan_dedup_and_repeat(self):
        state = {}
        sim._write_growth_plan(state, 'ada', 'room', 'low_grade', 1, 'note one')
        sim._write_growth_plan(state, 'ada', 'room', 'low_grade', 2, 'note two')  # dedup
        self.assertEqual(len(state['growthPlans']['ada']), 1)
        sim._write_growth_plan(state, 'ada', 'room', 'low_grade', 3, 'note three', repeat=True)
        self.assertEqual(len(state['growthPlans']['ada']), 2)
        self.assertEqual(sim._coaching_note_for(state, 'ada'), 'note one')
        self.assertEqual(sim._coaching_note_for(state, 'ada'), 'note three')
        self.assertEqual(sim._coaching_note_for(state, 'ada'), None)
        self.assertEqual(sim._coaching_note_for(state, 'ghost'), None)

    def test_drain_emails_from_db_no_state(self):
        with unittest.mock.patch('serve.get_state_from_db', return_value=None):
            self.assertEqual(sim._drain_emails_from_db(), [])

    def test_drain_emails_from_db_sends_and_saves(self):
        state = {'emailOutbox': [{'kind': 'reply', 'subject': 'hi', 'body': 'yo'},
                                 {'kind': 'ask', 'subject': 's', 'body': 'b'}]}
        with unittest.mock.patch('serve.get_state_from_db', return_value=state), \
             unittest.mock.patch('serve.save_state_to_db') as save, \
             unittest.mock.patch('serve.send_player_email_sync', return_value=True), \
             unittest.mock.patch('serve.send_player_telegram_sync', return_value=False):
            out = sim._drain_emails_from_db()
        self.assertEqual(out, [('reply', True, False), ('ask', True, False)])
        self.assertEqual(state['emailOutbox'], [])
        save.assert_called_once()

    def test_drain_email_outbox_sync_send_raises(self):
        state = {'emailOutbox': [{'kind': 'x', 'subject': 's', 'body': 'b'}]}
        with unittest.mock.patch('serve.send_player_email_sync',
                                 side_effect=RuntimeError('smtp down')), \
             unittest.mock.patch('serve.send_player_telegram_sync', return_value=True):
            out = sim._drain_email_outbox_sync(state)
        self.assertEqual(out, [('x', False, True)])
        self.assertEqual(state['emailOutbox'], [])

    def test_drain_email_outbox_sync_empty(self):
        self.assertEqual(sim._drain_email_outbox_sync({'emailOutbox': []}), [])
        self.assertEqual(sim._drain_email_outbox_sync({}), [])

    def test_sim_loop_pass_dormant_and_no_state(self):
        with unittest.mock.patch('serve._expire_handles'), \
             unittest.mock.patch('serve._dormant', return_value=True):
            self.assertEqual(sim._sim_loop_pass(), None)
        with unittest.mock.patch('serve._expire_handles'), \
             unittest.mock.patch('serve._dormant', return_value=False), \
             unittest.mock.patch('serve.get_state_from_db', return_value=None):
            self.assertEqual(sim._sim_loop_pass(), None)

    def test_sim_loop_pass_full_round_trip(self):
        state = {'sim': {}, 'agents': {}}
        fake_engine = unittest.mock.Mock()
        fake_engine.tick.return_value = state
        with unittest.mock.patch('serve._expire_handles'), \
             unittest.mock.patch('serve._dormant', return_value=False), \
             unittest.mock.patch('serve.get_state_from_db', return_value=state), \
             unittest.mock.patch('serve._apply_pending_ask_results'), \
             unittest.mock.patch('serve._peer_review_tick'), \
             unittest.mock.patch('serve.save_state_to_db') as save, \
             unittest.mock.patch.object(sim, '_engine', fake_engine):
            out = sim._sim_loop_pass()
        self.assertIs(out, state)
        save.assert_called_once()

    def test_sim_loop_runs_iterations_and_cancels(self):
        state = {'sim': {}, 'agents': {}}
        fake_engine = unittest.mock.Mock()
        fake_engine.tick.return_value = state
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise asyncio.CancelledError()

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        fake_async = unittest.mock.Mock()
        fake_async.sleep = fake_sleep
        fake_async.to_thread = fake_to_thread
        with unittest.mock.patch.object(sim, 'asyncio', fake_async), \
             unittest.mock.patch('serve._expire_handles'), \
             unittest.mock.patch('serve._dormant', return_value=False), \
             unittest.mock.patch('serve.get_state_from_db', return_value=state), \
             unittest.mock.patch('serve._apply_pending_ask_results'), \
             unittest.mock.patch('serve._peer_review_tick'), \
             unittest.mock.patch('serve.save_state_to_db'), \
             unittest.mock.patch.object(sim, '_engine', fake_engine):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sim._sim_loop())
        self.assertGreaterEqual(calls['n'], 2)

    def test_sim_loop_error_branches_are_swallowed(self):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise asyncio.CancelledError()

        async def fake_to_thread(fn, *args, **kwargs):
            raise RuntimeError('thread boom')

        fake_async = unittest.mock.Mock()
        fake_async.sleep = fake_sleep
        fake_async.to_thread = fake_to_thread
        with unittest.mock.patch.object(sim, 'asyncio', fake_async):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sim._sim_loop())
        self.assertGreaterEqual(calls['n'], 2)


class CeremonyBranches(SimIsolation):
    @staticmethod
    def _team_state():
        return {
            'agents': {
                'sm': {'id': 'sm', 'busy': True, 'offDuty': False, 'visible': True},
                'dir': {'id': 'dir', 'busy': False, 'offDuty': False, 'visible': True},
                'dir2': {'id': 'dir2', 'busy': False, 'offDuty': False, 'visible': True},
                'worker': {'id': 'worker', 'busy': False, 'offDuty': False, 'visible': True},
            },
            'teams': [
                {'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'},
                {'id': 't2', 'directorId': 'dir2', 'scrumMasterId': 'sm'},
            ],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
                {'id': 'dir2', 'name': 'Dir2', 'director': 'admin'},
                {'id': 'worker', 'name': 'Worker', 'director': 'dir'},
            ],
        }

    def test_ceremony_facilitator_director_fallback(self):
        state = self._team_state()
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'dir')

    def test_ceremony_facilitator_borrows_other_director(self):
        state = self._team_state()
        state['agents']['dir']['busy'] = True
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'dir2')

    def test_ceremony_facilitator_returns_none_when_all_busy(self):
        state = self._team_state()
        state['agents']['dir']['busy'] = True
        state['agents']['dir2']['busy'] = True
        state['agents']['sm']['busy'] = True
        self.assertIsNone(sim._ceremony_facilitator(state, 't1'))

    def test_ceremony_facilitator_uses_free_scrum_master(self):
        state = self._team_state()
        state['agents']['sm']['busy'] = False
        self.assertEqual(sim._ceremony_facilitator(state, 't1'), 'sm')

    def test_refinement_scrum_master_small_team_director_standin(self):
        state = self._team_state()
        state['teams'][0]['scrumMasterId'] = None
        # 1 direct report (< SCRUM_MASTER_MIN_TEAM_SIZE=4) -> director stands in.
        self.assertEqual(sim._refinement_scrum_master_for_team(state, 't1'), 'dir')

    def test_refinement_scrum_master_big_team_without_sm(self):
        state = self._team_state()
        state['teams'][0]['scrumMasterId'] = None
        # A large team (>= 4 members) with no designated SM -> None.
        state['agentRoster'].extend([
            {'id': 'w2', 'name': 'W2', 'director': 'dir'},
            {'id': 'w3', 'name': 'W3', 'director': 'dir'},
            {'id': 'w4', 'name': 'W4', 'director': 'dir'},
        ])
        for wid in ('w2', 'w3', 'w4'):
            state['agents'][wid] = {'id': wid, 'busy': False, 'offDuty': False}
        self.assertIsNone(sim._refinement_scrum_master_for_team(state, 't1'))

    def test_restore_escalation_agent_off_duty(self):
        state = {'agents': {'sm': {'id': 'sm', 'offDuty': False, 'visible': True,
                                   'task': 't1', 'busy': True, 'inRoom': 'commandcenter'}}}
        sim._restore_escalation_agent(state, 'sm', {'offDuty': True, 'task': 't1'}, 1000)
        a = state['agents']['sm']
        self.assertTrue(a['offDuty'])
        self.assertFalse(a['visible'])
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])

    def test_restore_escalation_agent_mid_task_extends_budget(self):
        state = {'agents': {'sm': {'id': 'sm', 'offDuty': False, 'visible': True,
                                   'task': 't1', 'busy': True, 'inRoom': 'office'}},
                 'tasks': {'t1': {'status': 'working', 'workUntil': 1000}}}
        sim._restore_escalation_agent(
            state, 'sm',
            {'offDuty': False, 'visible': True, 'task': 't1', 'inRoom': 'office'},
            1000)
        a = state['agents']['sm']
        self.assertEqual(a['task'], 't1')
        self.assertTrue(a['busy'])
        self.assertEqual(state['tasks']['t1']['workUntil'],
                         1000 + sim.ESCALATION_MEET_MS // 1000)

    def test_restore_escalation_agent_idle(self):
        state = {'agents': {'sm': {'id': 'sm', 'task': 't1', 'busy': True,
                                   'inRoom': 'commandcenter', 'visible': True}}}
        sim._restore_escalation_agent(state, 'sm',
                                      {'offDuty': False, 'visible': True}, 1000)
        a = state['agents']['sm']
        self.assertIsNone(a['task'])
        self.assertFalse(a['busy'])
        self.assertTrue(a['visible'])

    def test_restore_escalation_agent_vanished_is_noop(self):
        sim._restore_escalation_agent({'agents': {}}, 'ghost',
                                      {'offDuty': False, 'task': None}, 1000)

    def test_resolve_refinement_accept_and_reject(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'},
                       'worker': {'id': 'worker', 'name': 'Worker', 'role': 'Dev'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [
                {'id': 'r1', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Ship docs', 'reason': 'gap', 'status': 'pending'},
                {'id': 'r2', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Trivial task', 'reason': 'dupe', 'status': 'pending'},
            ],
            'pendingRefinements': {'t1': {'scrumMasterId': 'sm', 'reqIds': ['r1', 'r2'],
                                          'teamId': 't1', 'people': {}}},
        }
        choices = iter(['accept', 'reject'])
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(state, state['pendingRefinements']['t1'], 1000,
                                    decider=lambda i, c: next(choices))
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'rejected')
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['teamRefinementAt']['t1'], 1000)
        self.assertNotIn('t1', state['pendingRefinements'])

    def test_resolve_refinement_outage_fallback_accepts_valued_room(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'},
                       'worker': {'id': 'worker', 'name': 'Worker'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [
                {'id': 'r1', 'filedBy': 'worker', 'room': 'pressoffice',
                 'title': 'Ship docs', 'reason': 'gap', 'status': 'pending'},
                {'id': 'r2', 'filedBy': 'worker', 'room': 'notaroom',
                 'title': 'Junk', 'reason': 'x', 'status': 'pending'},
            ],
            '_pendingRefinement': {'scrumMasterId': 'sm', 'reqIds': ['r1', 'r2'],
                                   'people': {}},
        }
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest'):
            sim._resolve_refinement(state, state['_pendingRefinement'], 1000,
                                    decider=lambda i, c: None)
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'rejected')
        self.assertEqual(state['lastBacklogRefinementAt'], 1000)
        self.assertNotIn('_pendingRefinement', state)

    def test_resolve_refinement_survives_digest_outage(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'name': 'SM'}},
            'agentRoster': [{'id': 'sm', 'name': 'SM'}],
            'backlogRequests': [],
            '_pendingRefinement': {'scrumMasterId': 'sm', 'reqIds': [],
                                   'people': {}},
        }
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_refinement_digest',
                                 side_effect=RuntimeError('boom')):
            sim._resolve_refinement(state, state['_pendingRefinement'], 1000,
                                    decider=lambda i, c: 'reject')
        self.assertNotIn('_pendingRefinement', state)

    def test_breakdown_step_in_flight_resolve_and_convene(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'busy': False, 'offDuty': False, 'visible': True},
                       'dir': {'id': 'dir', 'busy': False, 'offDuty': False, 'visible': True},
                       'worker': {'id': 'worker', 'busy': False, 'offDuty': False, 'visible': True}},
            'teams': [{'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'}],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
                {'id': 'worker', 'name': 'Worker', 'director': 'dir'},
            ],
            'backlogRequests': [
                {'id': 'br1', 'filedBy': 'worker', 'status': 'pending', 'breakdown': True,
                 'teamId': 't1', 'title': 'Build the platform', 'goal': 'Build the platform',
                 'room': 'pressoffice'},
            ],
        }
        with unittest.mock.patch('serve.log_action'):
            sim._breakdown_step(state, 1.0, 1000,
                                decider=lambda s, i, g: {'items': [
                                    {'title': 'story one', 'type': 'story'},
                                    {'title': 'spike one', 'type': 'spike'}]})
        # Convened a ceremony (in-flight).
        self.assertIn('t1', state['pendingBreakdowns'])
        pend = state['pendingBreakdowns']['t1']
        self.assertTrue(pend['embarked'])
        # Advance it on the next pass -> resolves into queued stories.
        sim._breakdown_step(state, 1.0, 1000 + sim.BREAKDOWN_MEET_MS + 1,
                            decider=lambda s, i, g: {'items': [
                                {'title': 'story one', 'type': 'story'},
                                {'title': 'spike one', 'type': 'spike'}]})
        self.assertNotIn('t1', state['pendingBreakdowns'])
        titles = [it['title'] for it in state['workQueue']]
        self.assertEqual(titles, ['story one', 'spike one'])
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertTrue(state['backlogRequests'][0]['brokenDown'])

    def test_breakdown_step_defers_no_facilitator_and_active_sprint(self):
        state = {
            'agents': {'sm': {'id': 'sm', 'busy': True, 'offDuty': False},
                       'dir': {'id': 'dir', 'busy': True, 'offDuty': False}},
            'teams': [{'id': 't1', 'directorId': 'dir', 'scrumMasterId': 'sm'}],
            'agentRoster': [
                {'id': 'sm', 'name': 'SM', 'director': 'dir'},
                {'id': 'dir', 'name': 'Dir', 'director': 'admin'},
            ],
            'backlogRequests': [
                {'id': 'br1', 'filedBy': 'sm', 'status': 'pending', 'breakdown': True,
                 'teamId': 't1', 'title': 'Big ask', 'goal': 'Big ask', 'room': 'pressoffice'},
            ],
        }
        sim._breakdown_step(state, 1.0, 1000)
        self.assertNotIn('t1', state['pendingBreakdowns'])  # no facilitator
        # With a free facilitator but an active sprint, still defer.
        state['agents']['sm']['busy'] = False
        state['agents']['dir']['busy'] = False
        state['sprints'] = {'s1': {'status': 'active', 'teamIds': ['t1']}}
        sim._breakdown_step(state, 1.0, 1000)
        self.assertNotIn('t1', state['pendingBreakdowns'])


class StuckGateSweep(SimIsolation):
    @staticmethod
    def _gated_state():
        return {
            'agents': {'a': {'id': 'a', 'busy': False},
                       'r1': {'id': 'r1', 'busy': True, 'task': 'other'},
                       'r2': {'id': 'r2', 'busy': True, 'task': 'other'},
                       'r3': {'id': 'r3', 'busy': False},
                       'r4': {'id': 'r4', 'busy': False}},
            'agentRoster': [
                {'id': 'a', 'name': 'A'},
                {'id': 'r1', 'name': 'R1'},
                {'id': 'r2', 'name': 'R2'},
                {'id': 'r3', 'name': 'R3'},
                {'id': 'r4', 'name': 'R4'},
            ],
            'tasks': {'t1': {'id': 't1', 'status': 'needs_review', 'assignedTo': 'a',
                             'room': 'pressoffice', 'title': 'story',
                             '_peerGate': {'reviewerIds': ['r1', 'r2'], 'enteredMs': 0,
                                           'stuckRescueTs': 0, 'approvals': 0}}},
            'workQueue': [{'reviewOf': 't1', 'assignedTo': 'r3', 'title': 'Review: story'}],
        }

    def test_sweep_young_gate_skipped(self):
        state = self._gated_state()
        sim._sweep_stuck_gates(state, sim.STUCK_GATE_GRACE_MS // 2)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_rescued_recently_skipped(self):
        state = self._gated_state()
        sim._sweep_stuck_gates(state, sim.STUCK_GATE_GRACE_MS + 1)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_reachable_pair_left_alone(self):
        state = self._gated_state()
        for rid in ('r1', 'r2'):
            state['agents'][rid]['busy'] = False
            state['agents'][rid]['task'] = None
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        sim._sweep_stuck_gates(state, now)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_widens_unreachable_pair(self):
        state = self._gated_state()
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        with unittest.mock.patch('serve.log_action'):
            sim._sweep_stuck_gates(state, now)
        gate = state['tasks']['t1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['r3', 'r4'])
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'r3')
        self.assertEqual(gate['stuckRescueTs'], now)

    def test_sweep_no_fresh_reviewer_noop(self):
        state = self._gated_state()
        state['agents'].pop('r3')
        state['agents'].pop('r4')
        state['agentRoster'] = [d for d in state['agentRoster']
                                if d['id'] not in ('r3', 'r4')]
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        sim._sweep_stuck_gates(state, now)
        self.assertEqual(state['tasks']['t1']['_peerGate']['reviewerIds'], ['r1', 'r2'])

    def test_sweep_reenters_lost_review(self):
        state = self._gated_state()
        state['workQueue'] = []
        state['agents']['r1']['busy'] = False
        state['agents']['r1']['task'] = None
        now = sim.STUCK_GATE_GRACE_MS + sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1
        with unittest.mock.patch('serve.log_action'):
            sim._sweep_stuck_gates(state, now)
        gate = state['tasks']['t1']['_peerGate']
        self.assertEqual(gate['stuckRescueTs'], now)
        self.assertTrue(any(it.get('reviewOf') == 't1' for it in state['workQueue']))


class FiringReview(SimIsolation):
    @staticmethod
    def _state(candidate=None, severe=True):
        candidate = candidate or {
            'id': 'c', 'name': 'Cara', 'role': 'Dev',
            'approvedCount': 1, 'droppedCount': 5, 'hiredAt': 1000,
            'busy': False, 'task': None, 'pairWith': None, 'handoff': None,
        }
        reports = []
        if severe:
            reports = [{'aboutId': 'c', 'fromId': 'r', 'severity': 'severe',
                        'quote': 'bad', 'note': 'worse'}]
        return {
            'agents': {
                'c': candidate,
                'r1': {'id': 'r1', 'name': 'R1', 'busy': False, 'visible': True},
                'r2': {'id': 'r2', 'name': 'R2', 'busy': False, 'visible': True},
            },
            'agentRoster': [
                {'id': 'r1', 'name': 'R1'}, {'id': 'r2', 'name': 'R2'},
                {'id': 'c', 'name': 'Cara'},
            ],
            'reports': reports,
        }

    def _pending(self):
        return {'reviewer1Id': 'r1', 'reviewer2Id': 'r2', 'candidateId': 'c',
                'at': 1000}

    def test_resolve_firing_review_candidate_vanished(self):
        state = self._state()
        state['agents'].pop('c')
        state['_pendingFiringReview'] = self._pending()
        sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                   decider=lambda *a: 'fire')
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_candidate_def_missing(self):
        state = self._state()
        state['agentRoster'] = [d for d in state['agentRoster'] if d['id'] != 'c']
        state['_pendingFiringReview'] = self._pending()
        sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                   decider=lambda *a: 'fire')
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_deferred_for_consultation(self):
        # No severe report -> consultation blocks the fire.
        state = self._state(severe=False)
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertIn('c', state['agents'])
        self.assertNotIn('_pendingFiringReview', state)
        self.assertIsNone(state['agents']['c']['lastFiringReview'])

    def test_resolve_firing_review_busy_candidate_deferred(self):
        state = self._state()
        state['agents']['c']['busy'] = True
        state['agents']['c']['task'] = 't9'
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertIn('c', state['agents'])
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_fires(self):
        state = self._state()
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.revoke_agent_credentials'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'fire')
        self.assertNotIn('c', state['agents'])
        self.assertEqual([d['id'] for d in state['agentRoster']], ['r1', 'r2'])
        self.assertIn('cara', state['_usedNames'])
        self.assertNotIn('_pendingFiringReview', state)

    def test_resolve_firing_review_keeps(self):
        state = self._state()
        state['_pendingFiringReview'] = self._pending()
        with unittest.mock.patch('serve.log_action'):
            sim._resolve_firing_review(state, state['_pendingFiringReview'], 2000,
                                       decider=lambda *a: 'keep')
        self.assertIn('c', state['agents'])
        self.assertEqual(state['agents']['c']['lastFiringReview']['verdict'], 'keep')
        self.assertNotIn('_pendingFiringReview', state)

    def test_fire_decision_outage_fallback(self):
        state = self._state()
        info = {'reporters': [{'id': 'r', 'severity': 'severe'}], 'coworkers': []}
        with unittest.mock.patch('sim._firing_consultation', return_value=info):
            decision, _ = sim._fire_decision(state, [{'name': 'R1'}, {'name': 'R2'}],
                                             {'id': 'c'}, 2000, decider=lambda *a: None)
        self.assertEqual(decision, 'fire')  # severe report + dropped > approved*0.3


class AssignDueItemEdges(SimIsolation):
    @staticmethod
    def _state():
        return {
            'agents': {
                'ada': {'id': 'ada', 'busy': False, 'task': None, 'offDuty': False,
                        'x': 600, 'y': 340},
                'ben': {'id': 'ben', 'busy': False, 'task': None, 'offDuty': False,
                        'x': 640, 'y': 340},
                'faye': {'id': 'faye', 'busy': False, 'task': None, 'offDuty': False,
                         'isAdmin': True, 'x': 680, 'y': 340},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 't1'},
                {'id': 'ben', 'name': 'Ben', 'director': 't2'},
                {'id': 'faye', 'name': 'Faye', 'isAdmin': True, 'director': 'admin'},
            ],
            'sim': {'rr': {'task': 0}},
            'workQueue': [],
        }

    def test_assign_due_item_resolves_room_for_spike(self):
        state = self._state()
        pick = {'title': 'Investigate', 'taskType': 'spike', 'notBefore': None,
                'pair': False}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(pick['room'], 'observatory')
        self.assertEqual(task['taskType'], 'spike')
        self.assertEqual(state['workQueue'], [])

    def test_assign_due_item_empty_sprint_pool_returns_none(self):
        state = self._state()
        state['sprints'] = {'s1': {'status': 'active', 'workerCount': 2}}
        pick = {'title': 'Sprint card', 'room': 'pressoffice', 'sprintId': 's1'}
        grid, doors = sim._load_outdoor_geometry()
        self.assertIsNone(sim._assign_due_item(state, pick, True, grid, doors, 1000))

    def test_assign_due_item_pinned_reviewer_wins(self):
        state = self._state()
        state['agents']['ben']['offDuty'] = True
        pick = {'title': 'Review: story', 'room': 'pressoffice', 'reviewOf': 't1',
                'assignedTo': 'ben', 'taskType': 'review'}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ben')
        self.assertFalse(state['agents']['ben']['offDuty'])

    def test_assign_due_item_no_candidates_returns_none(self):
        state = self._state()
        for a in state['agents'].values():
            a['busy'] = True
        pick = {'title': 'Story', 'room': 'pressoffice'}
        grid, doors = sim._load_outdoor_geometry()
        self.assertIsNone(sim._assign_due_item(state, pick, True, grid, doors, 1000))

    def test_assign_due_item_team_preference_narrows_pick(self):
        state = self._state()
        pick = {'title': 'Team story', 'room': 'pressoffice', 'teamId': 't1'}
        grid, doors = sim._load_outdoor_geometry()
        task = sim._assign_due_item(state, pick, True, grid, doors, 1000,
                                    task_id_holder=[0])
        self.assertEqual(task['assignedTo'], 'ada')


class ReclaimAndEscalation(SimIsolation):
    def test_reclaim_orphaned_walking_tasks_requeues(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'taskType': 'code',
                                  'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': []}
        count = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(count, 1)
        self.assertNotIn('t1', state['tasks'])
        self.assertEqual(state['workQueue'][0]['title'], 'x')

    def test_reclaim_orphaned_walking_tasks_skips_in_social(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ghost',
                                  '_inSocial': 1234}},
                 'agents': {}, 'workQueue': []}
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)
        self.assertIn('t1', state['tasks'])

    def test_reclaim_orphaned_walking_tasks_held_task_skipped(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ada'}},
                 'agents': {'ada': {'id': 'ada', 'task': 't1', 'busy': False}},
                 'workQueue': []}
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)
        self.assertIn('t1', state['tasks'])

    def test_reclaim_orphaned_walking_tasks_abandons_over_cap(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'walking', 'title': 'x',
                                  'room': 'pressoffice', 'taskType': 'code',
                                  'attempts': sim.WORK_ITEM_MAX_ATTEMPTS - 1,
                                  'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': []}
        with unittest.mock.patch('serve.log_action'):
            count = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(count, 1)
        self.assertNotIn('t1', state['tasks'])
        self.assertEqual(state['workQueue'], [])

    def test_reclaim_orphaned_walking_tasks_work_queue_not_list(self):
        state = {'tasks': {'t1': {'id': 't1', 'status': 'working', 'title': 'x',
                                  'room': 'pressoffice', 'assignedTo': 'ghost'}},
                 'agents': {}, 'workQueue': None}
        sim._reclaim_orphaned_walking_tasks(state)
        self.assertIsInstance(state['workQueue'], list)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['title'], 'x')

    def test_start_escalation_dedups_by_product(self):
        state = {'_escalatedProducts': {'p1': {'resolved': False}},
                 'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        self.assertIsNone(sim._start_escalation(state, 'p1', 'd1', 't', 'src', 1000))

    def test_start_escalation_no_scrum_master(self):
        state = {'teams': [{'directorId': 'd1'}]}
        self.assertIsNone(sim._start_escalation(state, 'p1', 'd1', 't', 'src', 1000))

    def test_start_escalation_caps_concurrent(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        state['_escalatedProducts'] = {
            f'p{i}': {'directorId': 'd1', 'resolved': False}
            for i in range(sim.ESCALATION_MAX_OPEN)}
        self.assertIsNone(sim._start_escalation(state, 'p-new', 'd1', 't', 'src', 1000))

    def test_start_escalation_opens(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}]}
        sm = sim._start_escalation(state, 'p1', 'd1', 'Broken thing', 'unrestored', 1000)
        self.assertEqual(sm, 'sm')
        self.assertEqual(state['_pendingEscalation']['productId'], 'p1')
        self.assertFalse(state['_pendingEscalation']['embarked'])
        self.assertIn('p1', state['_escalatedProducts'])

    def test_resolve_escalation_story_and_spike_and_outage(self):
        state = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                   'visible': True}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                 '_escalatedProducts': {'p1': {'resolved': False}}}
        pending = {'scrumMasterId': 'sm', 'productId': 'p1', 'title': 'Broken',
                   'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state, pending, 1000, decider=lambda *a: 'story')
        self.assertEqual(state['backlogRequests'][0]['origin'], 'oncall_escalation')
        self.assertEqual(state['_escalatedProducts']['p1']['outcome'], 'story')
        self.assertIsNone(state['_pendingEscalation'])
        # Spike path.
        state2 = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                    'visible': True}},
                  'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                  '_escalatedProducts': {'p2': {'resolved': False}}}
        pending2 = {'scrumMasterId': 'sm', 'productId': 'p2', 'title': 'Broken',
                    'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state2, pending2, 1000, decider=lambda *a: 'spike')
        self.assertEqual(state2['workQueue'][0]['taskType'], 'spike')
        # Outage -> spike fallback.
        state3 = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                    'visible': True}},
                  'agentRoster': [{'id': 'sm', 'name': 'SM'}],
                  '_escalatedProducts': {'p3': {'resolved': False}}}
        pending3 = {'scrumMasterId': 'sm', 'productId': 'p3', 'title': 'Broken',
                    'source': 'unrestored', 'people': {}}
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._resolve_escalation(state3, pending3, 1000, decider=lambda *a: None)
        self.assertEqual(state3['workQueue'][0]['taskType'], 'spike')

    def test_escalation_step_advances_and_sweeps(self):
        state = {'agents': {'sm': {'id': 'sm', 'name': 'SM', 'busy': False,
                                   'visible': True, 'offDuty': False}},
                 'agentRoster': [{'id': 'sm', 'name': 'SM'}]}
        state['_pendingEscalation'] = {'scrumMasterId': 'sm', 'embarked': False,
                                       'productId': 'p1', 'title': 'B',
                                       'source': 'unrestored', 'at': 0, 'people': {}}
        with unittest.mock.patch('serve.log_action'):
            sim._escalation_step(state, 1.0, 1000)
        self.assertTrue(state['_pendingEscalation']['embarked'])
        with unittest.mock.patch('serve.log_action'), \
             unittest.mock.patch('serve.log_escalation_digest'):
            sim._escalation_step(state, 1.0, 1000 + sim.ESCALATION_MEET_MS + 1,
                                 decider=lambda *a: 'spike')
        self.assertIsNone(state['_pendingEscalation'])

    def test_escalation_step_sweeps_unrestored_bug(self):
        state = {'agents': {'sm': {'id': 'sm', 'busy': False, 'offDuty': False}},
                 'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}],
                 'products': {'p1': {'teamId': 'd1'}},
                 'tasks': {'b1': {'id': 'b1', 'taskType': 'bug', 'status': 'walking',
                                  'openedAt': 1, 'productId': 'p1',
                                  'title': 'Broken'}}}
        with unittest.mock.patch('serve.log_action'):
            sim._escalation_step(state, 1.0, sim.RESTORE_TIMEOUT_MS + 1)
        self.assertIn('p1', state['_escalatedProducts'])
        self.assertEqual(state['_pendingEscalation']['scrumMasterId'], 'sm')

    def test_on_work_item_abandoned_incident_routes(self):
        state = {'teams': [{'directorId': 'd1', 'scrumMasterId': 'sm'}],
                 'products': {'p1': {'teamId': 'd1'}}}
        with unittest.mock.patch('serve.log_action'):
            sim._on_work_item_abandoned(
                state, {'taskType': 'bug', 'productId': 'p1', 'title': 'B'},
                int(1000))
        self.assertIn('p1', state['_escalatedProducts'])
        # Non-bug, no product, no director -> no-op.
        state2 = {}
        sim._on_work_item_abandoned(state2, {'taskType': 'code', 'productId': 'p1'}, 1)
        sim._on_work_item_abandoned(state2, {'taskType': 'bug'}, 1)
        sim._on_work_item_abandoned(state2, {'taskType': 'bug', 'productId': 'nope'}, 1)
        self.assertEqual(state2, {})


if __name__ == '__main__':
    unittest.main()