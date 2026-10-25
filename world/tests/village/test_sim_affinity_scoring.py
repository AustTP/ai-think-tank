"""Coverage-push tests for world/sim.py gap-H cluster: the affinity-scoring
branches, the pair-recruiter candidate/eligibility branches, the sprint-staffing
default-timestamp guard, and the _assign_due_item router branches.

Uses the temp-DB isolation pattern from test_sim.py / test_sim_gap.py so the
real think_tank.db is never touched; serve network/DB seams are mocked at the
module attribute.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve
import sim

from _sim_affinity_helpers import SimIsolation


class FeatureAffinity(SimIsolation):
    def test_feature_affinity_counts_matching_product(self):
        # 9488: the product-id score increment. A done task by this agent with a
        # matching productId bumps the affinity counter; non-done and other-agent
        # tasks are skipped.
        state = {'tasks': {
            't1': {'status': 'done', 'assignedTo': 'ada', 'room': 'observatory',
                   'projectLabel': 'feature-1', 'productId': 'prod-1'},
            't2': {'status': 'working', 'assignedTo': 'ada', 'productId': 'prod-1'},
            't3': {'status': 'done', 'assignedTo': 'ben', 'productId': 'prod-1'},
            't4': {'status': 'done', 'assignedTo': 'ada', 'productId': 'prod-9'},
        }}
        pick = {'room': 'observatory', 'projectLabel': 'feature-1',
                'productId': 'prod-1'}
        score = sim._feature_affinity(state, 'ada', pick)
        self.assertEqual(score, 3)


class DescriptionAffinity(SimIsolation):
    def test_description_affinity_no_roster_def(self):
        # 9515: agent_id has no roster entry -> zero affinity.
        state = {'agentRoster': []}
        self.assertEqual(sim._description_affinity(state, 'ghost', {'title': 'x'}), 0)

    def test_description_affinity_string_instructions(self):
        # 9528: profile.instructions is a non-empty STRING (not a list), so it
        # is appended as a single part and tokenized.
        state = {'agentRoster': [
            {'id': 'ada', 'role': 'backend engineer',
             'profile': {'instructions': 'write clean code'}}]}
        pick = {'title': 'backend cleanup', 'room': 'observatory'}
        score = sim._description_affinity(state, 'ada', pick)
        self.assertGreater(score, 0)

    def test_description_affinity_no_task_tokens(self):
        # 9539: the picked work's fields yield no meaningful tokens (stopwords /
        # too-short words) -> zero affinity despite a real description.
        state = {'agentRoster': [{'id': 'ada', 'role': 'engineer'}]}
        pick = {'title': 'the', 'instructions': '', 'goal': '', 'projectLabel': '',
                'room': 'a'}
        self.assertEqual(sim._description_affinity(state, 'ada', pick), 0)


class PairRecruit(SimIsolation):
    def _pair_state(self, off_duty=False):
        return {
            'sim': {'rr': {'pair': 0}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True,
                        **({'offDuty': True} if off_duty else {})},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada'},
                {'id': 'ben', 'name': 'Ben'},
            ],
        }

    def test_recruit_no_other_candidate(self):
        # 9569: the driver is the only eligible hand -> no navigator.
        state = {'sim': {}, 'agents': {'ada': {'id': 'ada', 'x': 0, 'y': 0}},
                 'agentRoster': [{'id': 'ada', 'name': 'Ada'}]}
        self.assertIsNone(sim._recruit_pair_navigator(
            state, 'ada', {'id': 'task-1', 'room': 'observatory'},
            False, self._free_grid(), {}, 1))

    def test_recruit_wakes_off_duty_navigator(self):
        # 9580: the recruited navigator is off-duty, so she is woken via
        # appear_from_outskirts before walking to the driver's door.
        state = self._pair_state(off_duty=True)
        with unittest.mock.patch.object(sim, 'appear_from_outskirts') as wake:
            nav_id = sim._recruit_pair_navigator(
                state, 'ada', {'id': 'task-1', 'room': 'observatory'},
                True, self._free_grid(), self._doors(), 1)
        self.assertEqual(nav_id, 'ben')
        wake.assert_called_once_with(state, 'ben', self._doors())
        self.assertEqual(state['agents']['ben']['pairWith'], 'ada')
        self.assertEqual(state['agents']['ben']['pairTaskId'], 'task-1')

    def test_recruit_no_door_for_room(self):
        # 9584: the pair card's room has no door geometry -> no navigator.
        state = self._pair_state()
        self.assertIsNone(sim._recruit_pair_navigator(
            state, 'ada', {'id': 'task-1', 'room': 'observatory'},
            True, self._free_grid(), {}, 1))

    def test_recruit_no_path_falls_through(self):
        # 9602: every door-front offset is unwalkable (patched find_path) ->
        # the loop exhausts and the recruiter degrades to solo.
        state = self._pair_state()
        with unittest.mock.patch.object(sim, 'find_path', return_value=None):
            self.assertIsNone(sim._recruit_pair_navigator(
                state, 'ada', {'id': 'task-1', 'room': 'observatory'},
                True, self._free_grid(), self._doors(), 1))

    def test_recruit_phantom_candidate(self):
        # 9578: the round-robin picked an id that is NOT in state['agents'] at
        # wake time (defensive guard) -> no navigator, driver proceeds solo.
        state = {'sim': {}, 'agents': {}, 'agentRoster': []}
        with unittest.mock.patch.object(sim, '_eligible_candidates',
                                        return_value=['ghost']):
            self.assertIsNone(sim._recruit_pair_navigator(
                state, 'ada', {'id': 'task-1', 'room': 'observatory'},
                True, self._free_grid(), self._doors(), 1))


class SprintStaffing(SimIsolation):
    def test_staffing_step_defaults_now_ms(self):
        # 9671: now_ms is None -> the current wall-clock timestamp is used.
        # Empty sprints make the pass a pure no-op, deterministic.
        state = {'sprints': {}, 'agents': {}}
        self.assertEqual(sim._sprint_staffing_step(state), [])


class AssignDueItem(SimIsolation):
    def _grid(self):
        return self._free_grid()

    def test_drops_pin_outside_sprint_pool(self):
        # 9750: a review pin to someone outside the sprint's worker pool is
        # dropped; assignment falls back to the pool-narrowed round-robin.
        state = {
            'sim': {'rr': {'task': 0}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True},
                'cat': {'id': 'cat', 'x': 0, 'y': 0, 'visible': True, 'busy': True},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 'd1'},
                {'id': 'ben', 'name': 'Ben', 'director': 'd1'},
                {'id': 'cat', 'name': 'Cat', 'director': 'd1'},
            ],
            'sprints': {'sprint-1': {'id': 'sprint-1', 'status': 'active',
                                     'teamIds': ['team-1'], 'workerCount': 2}},
            'teams': [{'id': 'team-1', 'directorId': 'd1'}],
            'tasks': {},
            'workQueue': [],
        }
        pick = {'room': 'observatory', 'title': 'Review the change',
                'reviewOf': 'task-9', 'assignedTo': 'cat',
                'sprintId': 'sprint-1'}
        task = sim._assign_due_item(state, pick, True, self._grid(),
                                    self._doors(), 1_000_000, task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ada')

    def test_incident_backup_rederive_off_duty(self):
        # 9781: the pinned on-call is mid-work, so the incident re-derives the
        # owning team's current on-call and wakes her at the room door.
        state = {
            'sim': {'rr': {}},
            'products': {'prod-1': {'teamId': 'd1', 'name': 'Prod', 'summary': 's'}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': True, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'offDuty': True, 'visible': True},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 'd2'},
                {'id': 'ben', 'name': 'Ben', 'director': 'd1'},
            ],
            'sprints': {},
            'tasks': {},
            'workQueue': [],
        }
        pick = {'room': 'observatory', 'title': 'Fix the outage',
                'incident': True, 'productId': 'prod-1',
                'assignedTo': 'ada', 'taskType': 'bug'}
        task = sim._assign_due_item(state, pick, True, self._grid(),
                                    self._doors(), 1_000_000, task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'ben')

    def test_team_preference_borrows_loaned_agent(self):
        # 9810: a team-filed card soft-preferences the team's own free members
        # PLUS any agent loaned to that team (roster `loan` tag). With the
        # team's direct reports busy, the loaned agent is the one chosen.
        state = {
            'sim': {'rr': {'task': 0}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': True, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'busy': True, 'visible': True},
                'cat': {'id': 'cat', 'x': 0, 'y': 0, 'visible': True},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 'd1'},
                {'id': 'ben', 'name': 'Ben', 'director': 'd1'},
                {'id': 'cat', 'name': 'Cat', 'director': 'd2',
                 'loan': {'teamId': 'd1'}},
            ],
            'sprints': {},
            'tasks': {},
            'workQueue': [],
        }
        pick = {'room': 'observatory', 'title': 'Backlog story', 'teamId': 'd1'}
        task = sim._assign_due_item(state, pick, True, self._grid(),
                                    self._doors(), 1_000_000, task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'cat')

    def test_sprint_chosen_outside_pool_returns_none(self):
        # 9855: an incident backup re-derive picks an on-call OUTSIDE the
        # sprint's strict worker pool -> the card waits (None) rather than
        # leaking to a non-staffed worker.
        state = {
            'sim': {'rr': {}},
            'products': {'prod-1': {'teamId': 'd1'}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': True, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'offDuty': True, 'visible': True},
                'zed': {'id': 'zed', 'x': 0, 'y': 0, 'visible': True},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'director': 'd9'},
                {'id': 'ben', 'name': 'Ben', 'director': 'd1'},
                {'id': 'zed', 'name': 'Zed', 'director': 'd9'},
            ],
            'sprints': {'sprint-1': {'id': 'sprint-1', 'status': 'active',
                                     'teamIds': ['other-team'], 'workerCount': 2}},
            'teams': [{'id': 'other-team', 'directorId': 'd9'}],
            'tasks': {},
            'workQueue': [],
        }
        pick = {'room': 'observatory', 'title': 'Fix the outage',
                'incident': True, 'productId': 'prod-1',
                'assignedTo': 'ada', 'sprintId': 'sprint-1', 'taskType': 'bug'}
        self.assertIsNone(sim._assign_due_item(
            state, pick, True, self._grid(), self._doors(), 1_000_000,
            task_id_holder=[0]))

    def test_pinned_agent_busy_but_candidate(self):
        # 9766: the pinned agent is busy (fast wake branch skipped) but is STILL
        # among the eligible candidates -> the pin is honored directly as the
        # chosen id. (The busy agent is then refused by assign_task, so the call
        # returns None -- the point is exercising the `pinned in candidates`
        # branch, a defensive path only reachable when candidate semantics
        # change.)
        state = {
            'sim': {'rr': {'task': 0}},
            'agents': {
                'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': True, 'visible': True},
                'ben': {'id': 'ben', 'x': 0, 'y': 0, 'visible': True},
            },
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada'},
                {'id': 'ben', 'name': 'Ben'},
            ],
            'tasks': {},
            'workQueue': [],
        }
        pick = {'room': 'observatory', 'title': 'Judge the change',
                'reviewOf': 'task-9', 'assignedTo': 'ada'}
        with unittest.mock.patch.object(sim, '_eligible_candidates',
                                        return_value=['ada', 'ben']):
            task = sim._assign_due_item(state, pick, True, self._grid(),
                                        self._doors(), 1_000_000,
                                        task_id_holder=[0])
        self.assertIsNone(task)


    def test_all_candidates_vanish(self):
        # 9815: every eligible candidate id is absent from state['agents'] at
        # assignment time -> no card is handed out.
        state = {'sim': {'rr': {'task': 0}}, 'agents': {}, 'agentRoster': [],
                 'tasks': {}, 'workQueue': []}
        pick = {'room': 'observatory', 'title': 'Backlog story'}
        with unittest.mock.patch.object(sim, '_eligible_candidates',
                                        return_value=['ghost']):
            self.assertIsNone(sim._assign_due_item(
                state, pick, True, self._grid(), self._doors(), 1_000_000,
                task_id_holder=[0]))


class PlayerInput(SimIsolation):
    def _issue_state(self, **issue_overrides):
        issue = {'key': 'ISS-1', 'title': 'Fix the thing', 'summary': 'ship it',
                 'teamId': 'team-1', 'type': 'story'}
        issue.update(issue_overrides)
        return {
            'issues': {'ISS-1': issue},
            'teams': [{'id': 'team-1', 'directorId': 'dir-1'}],
            'agentRoster': [],
            'agents': {},
        }

    def test_set_issue_status_clears_needs_input(self):
        # 6150-6153: a terminal 'done' status supersedes an awaiting player-ask:
        # needsInput is cleared and the inbox message flips to 'superseded'.
        state = self._issue_state(needsInput=True)
        state['playerInbox'] = [
            {'id': 'm1', 'issueKey': 'ISS-1', 'status': 'awaiting_input',
             'question': 'q', 'agentId': 'ada'}]
        issue = sim.set_issue_status(state, 'ISS-1', 'done')
        self.assertFalse(issue['needsInput'])
        self.assertEqual(state['playerInbox'][0]['status'], 'superseded')

    def test_issue_owning_team_unknown(self):
        # 6184: no such issue -> owning team is None.
        self.assertIsNone(sim._issue_owning_team({'issues': {}}, 'nope'))

    def test_request_player_input_no_owning_team(self):
        # 6203: the issue has no owning team -> nothing is recorded.
        self.assertIsNone(sim.request_player_input(
            {'issues': {'ISS-1': {'key': 'ISS-1'}}}, 'ISS-1', 'ada', 'q'))

    def test_request_player_input_unknown_issue(self):
        # 6206: the owning team resolves but the issue record is gone.
        with unittest.mock.patch.object(sim, '_issue_director',
                                        return_value='dir-1'):
            self.assertIsNone(sim.request_player_input({}, 'ISS-9', 'ada', 'q'))

    def test_request_player_input_already_needs_input(self):
        # 6208: already awaiting input -- don't double-ask.
        state = self._issue_state(needsInput=True)
        self.assertIsNone(sim.request_player_input(state, 'ISS-1', 'ada', 'q'))

    def test_request_player_input_duplicate_pending(self):
        # 6212: a gate for this issue is already pending -- don't re-file.
        state = self._issue_state()
        state['_pendingPlayerAsk'] = {'issueKey': 'ISS-1', 'at': 0}
        self.assertIsNone(sim.request_player_input(state, 'ISS-1', 'ada', 'q'))

    def test_request_player_input_queues_instead_of_overwriting(self):
        # A second issue waits in the backlog; it is promoted when the first
        # ask resolves, so no owner-facing question is silently discarded.
        state = self._issue_state()
        state['issues']['ISS-2'] = {'key': 'ISS-2', 'title': 'Second', 'summary': 's',
                                    'teamId': 'team-1', 'type': 'story'}
        with unittest.mock.patch.object(sim, '_issue_director',
                                        return_value='dir-1'):
            sim.request_player_input(state, 'ISS-1', 'ada', 'First?', now_ms=1_000)
            sim.request_player_input(state, 'ISS-2', 'ada', 'Second?', now_ms=2_000)
        self.assertEqual(state['_pendingPlayerAsk']['issueKey'], 'ISS-1')
        self.assertEqual([a['issueKey'] for a in state['_pendingPlayerAskBacklog']],
                         ['ISS-2'])
        sim.request_player_input_verdict(state, 'ISS-1',
                                         decider=lambda *a: 'resolve_internally')
        self.assertEqual(state['_pendingPlayerAsk']['issueKey'], 'ISS-2')
        self.assertEqual(state['_pendingPlayerAskBacklog'], [])

    def test_director_gate_not_due(self):
        # 6229/6230: no pending ask (issueKey mismatch) or an already-directed
        # ask -> not due.
        self.assertFalse(sim._director_gate_due_for({}, 'ISS-1', 1_000))
        self.assertFalse(sim._director_gate_due_for(
            {'_pendingPlayerAsk': {'issueKey': 'ISS-1', 'at': 0,
                                   'directed': True}}, 'ISS-1', 1_000))

    def test_director_gate_due(self):
        # 6231: an undirected ask old enough for the director -> due.
        pend = {'issueKey': 'ISS-1', 'at': 1_000, 'directed': False}
        self.assertTrue(sim._director_gate_due_for(
            {'_pendingPlayerAsk': pend}, 'ISS-1', 10_000, gate_ms=8_000))

    def test_verdict_no_pending_for_key(self):
        # 6242: nothing pending for this issue -> no verdict.
        self.assertIsNone(sim.request_player_input_verdict({}, 'ISS-1'))

    def test_verdict_missing_issue(self):
        # 6246: pending ask exists but its issue record is gone.
        state = {'_pendingPlayerAsk': {'issueKey': 'ISS-1', 'agentId': 'ada',
                                       'question': 'q', 'at': 0}}
        self.assertIsNone(sim.request_player_input_verdict(state, 'ISS-1'))

    def test_sweep_not_due(self):
        # 6447/6448: a pending ask younger than the gate window -> nothing due.
        state = {'_pendingPlayerAsk': {'issueKey': 'ISS-1', 'at': 0,
                                       'directed': False}}
        self.assertIsNone(sim._pending_player_ask_sweep(state, 1_000))

    def test_sweep_due_runs_gate(self):
        # 6449: a due ask is spun through the (injected) director gate -> the
        # ask_player verdict delivers the inbox message.
        state = self._issue_state()
        state['_pendingPlayerAsk'] = {'issueKey': 'ISS-1', 'agentId': 'ada',
                                      'question': 'q', 'at': 0,
                                      'directed': False}
        state['agents'] = {'ada': {'id': 'ada', 'profile': {}}}
        state['workQueue'] = []
        out = sim._pending_player_ask_sweep(
            state, 50_000, decider=lambda *a, **k: 'ask_player')
        self.assertTrue(isinstance(out, str) and out.startswith('ask-'))
        self.assertTrue(state['issues']['ISS-1']['needsInput'])

    def test_resolve_player_ask_unknown(self):
        # 6412: unknown id or a non-awaiting message -> None.
        self.assertIsNone(sim.resolve_player_ask({}, 'zzz', 'yes'))
        self.assertIsNone(sim.resolve_player_ask(
            {'playerInbox': [{'id': 'm1', 'status': 'answered'}]}, 'm1', 'yes'))

    def test_resolve_player_ask_happy(self):
        # 6422/6423: the answer is stamped onto the agent's profile notes so the
        # resumed task carries the player's context.
        state = self._issue_state()
        state['playerInbox'] = [
            {'id': 'm1', 'issueKey': 'ISS-1', 'agentId': 'ada',
             'status': 'awaiting_input', 'question': 'q', 'createdAt': 0}]
        state['agents'] = {'ada': {'id': 'ada', 'profile': {'name': 'Ada'}}}
        state['workQueue'] = []
        m = sim.resolve_player_ask(state, 'm1', '  do it  ')
        self.assertEqual(m['status'], 'answered')
        self.assertEqual(m['answer'], 'do it')
        notes = state['agents']['ada']['profile']['notes']
        self.assertIn('Player on ISS-1', notes[0])
