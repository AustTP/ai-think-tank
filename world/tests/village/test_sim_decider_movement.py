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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim

from _sim_social_helpers import SimIsolation


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

    def test_requeue_task_for_agent_inherits_a_non_code_parent_lane(self):
        # A follow-up on a review/research task must not default to the code
        # lane and run the Python gate.
        state = {'workQueue': [], 'tasks': {
            'task-1': {'taskType': 'review', 'room': 'pressoffice',
                       'projectLabel': 'ben'}}}
        self.assertTrue(sim._requeue_task_for_agent(state, 'ada', 'task-1', 1234))
        item = state['workQueue'][0]
        self.assertEqual(item['taskType'], 'review')
        self.assertEqual(item['room'], 'pressoffice')
        self.assertEqual(item['projectLabel'], 'ben')

    def test_requeue_task_for_agent_does_not_mark_an_agent_id_as_a_review(self):
        # help_signal mail carries `about: <agent id>`; reviewOf is for a TASK
        # id only, and the item must be queued ROOM-LESS (the old hardcoded
        # 'townsquare' has no door, which is what wedged it for 38 hours).
        state = {'workQueue': [], 'agents': {'harold': {'id': 'harold'}}}
        self.assertTrue(sim._requeue_task_for_agent(state, 'nora', 'harold', 1234))
        item = state['workQueue'][0]
        self.assertIsNone(item['reviewOf'])
        self.assertIsNone(item['room'])
        self.assertEqual(item['assignedTo'], 'nora')
        self.assertEqual(item['goal'], 'harold')

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
