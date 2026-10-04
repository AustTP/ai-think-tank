"""Coverage-push tests for world/sim.py gap-H cluster: the affinity-scoring
branches (_feature_affinity product match, _description_affinity roster/string/
no-token paths), the pair-recruiter candidate/eligibility branches
(_recruit_pair_navigator), the sprint-staffing default-timestamp guard
(_sprint_staffing_step), and the _assign_due_item router's pin-drop / on-call
re-derive / sprint-pool-invariant branches.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapH-')
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

    @staticmethod
    def _free_grid(cols=16, rows=16):
        return {'cols': cols, 'rows': rows, 'cell': 8,
                'grid': [[0] * cols for _ in range(rows)]}

    @staticmethod
    def _doors():
        return {'observatory': {'x': 40, 'y': 20, 'w': 40, 'h': 10}}


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


class BlockCluster(SimIsolation):
    def _issue_state(self, **issue_overrides):
        issue = {'key': 'ISS-1', 'title': 'Fix the thing', 'summary': 'ship it',
                 'teamId': 'team-1', 'type': 'story'}
        issue.update(issue_overrides)
        return {
            'issues': {'ISS-1': issue},
            'teams': [{'id': 'team-1', 'directorId': 'dir-1',
                       'scrumMasterId': 'sm-1'}],
            'agents': {'sm-1': {'id': 'sm-1'}},
            'agentRoster': [],
        }

    def test_file_block_change_unknown_issue(self):
        # 6485: unknown issue -> no request filed.
        self.assertIsNone(sim._file_block_change({}, 'ISS-9', True, 'ada',
                                                 'stuck_on_agent', 'why'))

    def test_file_block_change_bad_kind(self):
        # 6487: unknown kind -> no request filed.
        state = {'issues': {'ISS-1': {'key': 'ISS-1'}}}
        self.assertIsNone(sim._file_block_change(state, 'ISS-1', True, 'ada',
                                                 'bogus', 'why'))

    def test_file_block_change_duplicate_pending(self):
        # 6491: a live pending request already exists for this issue.
        state = {'issues': {'ISS-1': {'key': 'ISS-1'}},
                 '_pendingBlockChanges': [{'issueKey': 'ISS-1',
                                           'state': 'pending'}]}
        self.assertIsNone(sim._file_block_change(state, 'ISS-1', True, 'ada',
                                                 'stuck_on_agent', 'why'))

    def test_mail_action_step_clears_acting_marker(self):
        # 6560-6562: the woken mail agent is already busy/working -> clear the
        # marker and count her as routed (she acts via the normal flow).
        state = {'agents': {'ada': {'id': 'ada', 'busy': True, '_mailAwake': {
            'kind': 'player_answer', 'entry': {'issueKey': 'ISS-1'}}}}}
        self.assertEqual(sim._mail_action_step(state, 1_000_000, 1_000_000), 1)
        self.assertNotIn('_mailAwake', state['agents']['ada'])

    def test_queue_mail_work_task_branch(self):
        # 6583/6584: a payload carrying a taskId (no issueKey) re-arms the task.
        state = {'workQueue': []}
        self.assertTrue(sim._queue_mail_work(
            state, 'ada', 'peer_review_request', {'taskId': 't9'}, 1_000_000))
        self.assertEqual(state['workQueue'][0]['reviewOf'], 't9')

    def test_queue_mail_work_empty_payload(self):
        # 6585: neither issueKey nor taskId -> nothing queued.
        self.assertFalse(sim._queue_mail_work(state := {'workQueue': []},
                                              'ada', 'x', {}, 1_000_000))

    def test_requeue_card_task_unknown_issue(self):
        # 6594: the referenced issue is gone -> nothing re-armed.
        self.assertFalse(sim._requeue_card_task({'workQueue': []}, 'ada',
                                                'ISS-9', 1_000_000))

    def test_requeue_card_task_no_queue(self):
        # 6597: workQueue isn't a list -> nothing re-armed.
        state = {'issues': {'ISS-1': {'key': 'ISS-1'}}, 'workQueue': {}}
        self.assertFalse(sim._requeue_card_task(state, 'ada', 'ISS-1',
                                                1_000_000))

    def test_block_step_no_team(self):
        # 6650: the change's team record is gone -> defer.
        state = {'_pendingBlockChanges': [
            {'issueKey': 'ISS-1', 'teamId': 'ghost-team', 'state': 'pending',
             'wanted': True}],
            'issues': {'ISS-1': {'key': 'ISS-1', 'teamId': 'ghost-team'}},
            'teams': []}
        self.assertIsNone(sim._block_step(state, 1_000_000, 1_000_000))

    def test_block_step_no_scrum_master(self):
        # 6656: the team has no effective scrum master -> defer.
        state = {'_pendingBlockChanges': [
            {'issueKey': 'ISS-1', 'teamId': 'team-1', 'state': 'pending',
             'wanted': True}],
            'issues': {'ISS-1': {'key': 'ISS-1', 'teamId': 'team-1'}},
            'teams': [{'id': 'team-1'}], 'agents': {}}
        self.assertIsNone(sim._block_step(state, 1_000_000, 1_000_000))

    def test_block_step_no_issue(self):
        # 6664: scrum master is free but the issue record is gone -> defer.
        state = {'_pendingBlockChanges': [
            {'issueKey': 'ISS-9', 'teamId': 'team-1', 'state': 'pending',
             'wanted': True}],
            'teams': [{'id': 'team-1', 'directorId': 'dir-1',
                       'scrumMasterId': 'sm-1'}],
            'agents': {'sm-1': {'id': 'sm-1'}}, 'issues': {}}
        self.assertIsNone(sim._block_step(state, 1_000_000, 1_000_000))

    def test_log_block_commit_raises(self):
        # 6727/6728: a corrupt actionLog (not a list) is swallowed.
        state = {'actionLog': {}}
        sim._log_block_commit(state, {'issueKey': 'i1', 'committedBy': 'sm',
                                      'kind': 'x', 'requesterId': 'a',
                                      'reason': 'r'}, {'blocked': True}, 1_000)
        self.assertEqual(state['actionLog'], {})

    def test_request_block_dependency_no_team(self):
        # 6741: no owning team to route the SM commit through.
        state = {'issues': {'ISS-1': {'key': 'ISS-1'}}}
        self.assertIsNone(sim.request_block_dependency(state, 'ISS-1', 'ada',
                                                       't9'))

    def test_request_block_dependency_missing_issue(self):
        # 6744: the issue record is gone between the director check and fetch.
        with unittest.mock.patch.object(sim, '_issue_director',
                                        return_value='dir-1'):
            self.assertIsNone(sim.request_block_dependency({}, 'ISS-1', 'ada',
                                                           't9'))

    def test_auto_clear_no_completed_task(self):
        # 6757: no completed task id -> nothing to clear.
        self.assertEqual(sim._auto_clear_dependency_blocks({}, None), 0)

    def test_auto_clear_committed_block_no_requester(self):
        # 6781: the committed block cleared but no requester could be recovered
        # (no block-change record) -> the wake is skipped, count still advances.
        state = {'issues': {'ISS-1': {'key': 'ISS-1', 'blocked': True,
                                      'blockedKind': 'stuck_on_agent',
                                      'dependsOnTask': 't9',
                                      'teamId': 'team-1'}},
                 'tasks': {'t9': {'title': 'landed'}},
                 '_pendingBlockChanges': []}
        self.assertEqual(sim._auto_clear_dependency_blocks(state, 't9',
                                                           now_ms=1_000_000), 1)

    def test_auto_clear_drops_pending(self):
        # 6802/6803: a pending (uncommitted) stuck_on_agent change keyed to the
        # completed task is dropped entirely, no field flip needed.
        state = {'issues': {}, '_pendingBlockChanges': [
            {'id': 'b1', 'issueKey': 'ISS-1', 'state': 'pending',
             'kind': 'stuck_on_agent', 'dependsOnTask': 't9', 'wanted': True},
            {'id': 'b2', 'issueKey': 'ISS-1', 'state': 'committed',
             'kind': 'stuck_on_agent', 'dependsOnTask': 't9'}]}
        self.assertEqual(sim._auto_clear_dependency_blocks(state, 't9',
                                                           now_ms=1_000_000), 1)
        self.assertEqual(len(state['_pendingBlockChanges']), 1)

    def test_dependency_requester_none(self):
        # 6825: no matching stuck_on_agent block-change -> no requester.
        state = {'_pendingBlockChanges': [
            {'issueKey': 'ISS-1', 'kind': 'stuck_on_agent',
             'dependsOnTask': 't8', 'requesterId': 'ben'}]}
        self.assertIsNone(sim._dependency_requester(state, 'ISS-1', 't9'))

    def test_agent_still_blocked_no_dep(self):
        # 6838: an issue blocked for another reason is skipped.
        state = {'issues': {'ISS-1': {'key': 'ISS-1', 'blocked': True,
                                      'blockedKind': 'requirements_met',
                                      'dependsOnTask': 't9'}},
                 '_pendingBlockChanges': []}
        self.assertFalse(sim._agent_still_blocked(state, 'ada', 't9'))

    def test_supervisor_block_vote_no_claim(self):
        # 6864: no claim pending for this issue -> False.
        self.assertFalse(sim._supervisor_block_vote({}, 'ISS-1'))

    def test_supervisor_block_vote_missing_issue(self):
        # 6869: the claim matches but its issue is gone -> False (claim popped).
        state = {'_pendingBlockClaim': {'issueKey': 'ISS-1', 'agentId': 'ada',
                                        'reason': 'r'}}
        self.assertFalse(sim._supervisor_block_vote(state, 'ISS-1',
                                                    decider=lambda *a, **k: True))
        self.assertNotIn('_pendingBlockClaim', state)

    def test_request_block_claim_no_team(self):
        # 6915: no owning director -> the claim is not recorded.
        state = {'issues': {'ISS-1': {'key': 'ISS-1'}}}
        self.assertFalse(sim.request_block_claim_met(state, 'ISS-1', 'ada'))

    def test_request_block_claim_missing_issue(self):
        # 6918: the issue record is gone.
        with unittest.mock.patch.object(sim, '_issue_director',
                                        return_value='dir-1'):
            self.assertFalse(sim.request_block_claim_met({}, 'ISS-1', 'ada'))

    def test_request_block_claim_already_blocked(self):
        # 6924/6927: a card already blocked wants an unblock, not another
        # met-claim -- an unblock_resolved change is filed and the claim dropped.
        state = {'issues': {'ISS-1': {'key': 'ISS-1', 'blocked': True,
                                      'teamId': 'team-1'}},
                 'teams': [{'id': 'team-1', 'directorId': 'dir-1'}],
                 '_pendingBlockChanges': []}
        self.assertFalse(sim.request_block_claim_met(state, 'ISS-1', 'ada'))
        self.assertEqual(state['_pendingBlockChanges'][0]['kind'],
                         'unblock_resolved')


class RefinementCluster(SimIsolation):
    def _ref_state(self):
        return {
            'teams': [{'id': 'team-1', 'directorId': 'd1'}],
            'agentRoster': [{'id': 'd1', 'director': 'd1'},
                            {'id': 'ada', 'director': 'd1'}],
            'agents': {'d1': {'id': 'd1'}, 'ada': {'id': 'ada'}},
        }

    def test_refinement_team_none(self):
        # 7036: a filer who belongs to no team has no owning team.
        state = {'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'agentRoster': [{'id': 'ada', 'director': 'd1'}]}
        self.assertIsNone(sim._refinement_team(state, {'filedBy': 'ghost'}))

    def test_refinement_scrum_master_no_team(self):
        # 7054: no team matches the id -> no effective scrum master.
        self.assertIsNone(sim._refinement_scrum_master_for_team(
            {'teams': [{'id': 'team-1'}]}, 'nope'))

    def test_refinement_attendees_busy_sm(self):
        # 7106: the facilitator must be free to run the meeting.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}}}
        self.assertIsNone(sim._refinement_attendees(state, ['r1'], 'sm-1'))

    def test_start_refinement_no_attendees(self):
        # 7129: no healthy attendees -> defer to a later pass.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}}}
        self.assertFalse(sim._start_refinement(state, 'team-1', ['r1'], 'sm-1',
                                               1_000_000))

    def test_restore_refinement_agent_unknown(self):
        # 7163: the attendee is gone -> nothing to restore (no raise).
        sim._restore_refinement_agent({'agents': {}}, 'ghost', {}, 1_000_000,
                                      1_000)

    def test_reassign_rolled_over_wrong_sprint(self):
        # 7208/7215: an item tagged to a non-closed sprint is skipped; nothing
        # is picked for re-assignment.
        state = self._ref_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed',
                                   'teamIds': ['team-1']}}
        state['tasks'] = {}
        state['workQueue'] = [{'title': 'carried', 'room': 'observatory',
                               'sprintId': 's2'}]
        self.assertEqual(sim._reassign_rolled_over_cards(state, 'team-1'), 0)

    def test_reassign_rolled_over_already_done(self):
        # 7212: an equivalent card already landed (done task, same title+room).
        state = self._ref_state()
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed',
                                   'teamIds': ['team-1']}}
        state['tasks'] = {'t1': {'status': 'done', 'title': 'carried',
                                 'room': 'observatory'}}
        state['workQueue'] = [{'title': 'carried', 'room': 'observatory',
                               'sprintId': 's1'}]
        self.assertEqual(sim._reassign_rolled_over_cards(state, 'team-1'), 0)

    def test_reassign_rolled_over_no_free_member(self):
        # 7219: every member is busy/off-duty -> nothing re-planned.
        state = self._ref_state()
        state['agents'] = {'d1': {'id': 'd1', 'busy': True},
                           'ada': {'id': 'ada', 'offDuty': True}}
        state['sprints'] = {'s1': {'id': 's1', 'status': 'closed',
                                   'teamIds': ['team-1']}}
        state['tasks'] = {}
        state['workQueue'] = [{'title': 'carried', 'room': 'observatory',
                               'sprintId': 's1'}]
        self.assertEqual(sim._reassign_rolled_over_cards(state, 'team-1'), 0)

    def test_resolve_refinement_skips_non_pending(self):
        # 7250: a request that is no longer pending is not groomed.
        state = {
            'agents': {'sm-1': {'id': 'sm-1'}},
            'agentRoster': [{'id': 'sm-1', 'name': 'Sam'}],
            'backlogRequests': [{'id': 'r1', 'status': 'accepted',
                                 'filedBy': 'ada', 'room': 'observatory',
                                 'title': 't', 'reason': 'r'}],
            'workQueue': [], 'issues': {}, 'tasks': {},
        }
        pending = {'scrumMasterId': 'sm-1', 'reqIds': ['r1'], 'teamId': 'team-1',
                   'people': {}}
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=lambda i, c: 'accept')
        self.assertEqual(state['workQueue'], [])

    def test_resolve_refinement_depends_on(self):
        # 7302: an accepted card whose issue is blocked on another task carries
        # that dependency onto its real task.
        state = {
            'agents': {'sm-1': {'id': 'sm-1'}},
            'agentRoster': [{'id': 'sm-1', 'name': 'Sam'}],
            'backlogRequests': [{'id': 'r1', 'status': 'pending',
                                 'filedBy': 'ada', 'room': 'observatory',
                                 'title': 'Story A', 'reason': 'need it',
                                 'issueKey': 'ISS-1', 'teamId': 'team-1'}],
            'issues': {'ISS-1': {'key': 'ISS-1', 'dependsOnTask': 't9'}},
            'workQueue': [], 'tasks': {},
        }
        pending = {'scrumMasterId': 'sm-1', 'reqIds': ['r1'], 'teamId': 'team-1',
                   'people': {}}
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=lambda i, c: 'accept')
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['workQueue'][0]['dependsOn'], 't9')

    def test_resolve_refinement_rollover_logged(self):
        # 7330: carried-over cards from this team's closed sprint are re-assigned
        # to a fresh owner and the rollover is logged.
        state = {
            'agents': {'sm-1': {'id': 'sm-1'}, 'ada': {'id': 'ada'}},
            'agentRoster': [{'id': 'sm-1', 'name': 'Sam'},
                            {'id': 'ada', 'director': 'd1'}],
            'teams': [{'id': 'team-1', 'directorId': 'd1'}],
            'sprints': {'s1': {'id': 's1', 'status': 'closed',
                               'teamIds': ['team-1']}},
            'backlogRequests': [{'id': 'r1', 'status': 'pending',
                                 'filedBy': 'ada', 'room': 'observatory',
                                 'title': 'Carried card', 'reason': 'r',
                                 'teamId': 'team-1'}],
            'workQueue': [{'title': 'Carried card', 'room': 'observatory',
                           'sprintId': 's1'}],
            'issues': {}, 'tasks': {},
        }
        pending = {'scrumMasterId': 'sm-1', 'reqIds': ['r1'], 'teamId': 'team-1',
                   'people': {}}
        sim._resolve_refinement(state, pending, 1_000_000,
                                decider=lambda i, c: 'accept')
        carried = next(it for it in state['workQueue']
                       if it.get('sprintId') == 's1')
        self.assertIn('_reassignedTo', carried)

    def test_refinement_cadence_far_future_sentinel(self):
        # 7359: a far-future stale TEST stamp is treated as unset (due now).
        now = 1_000_000_000_000
        state = {'teamRefinementAt': {'team-1': now + 400_000_000_000_000}}
        self.assertTrue(sim._refinement_cadence_due_for(state, 'team-1', now))

    def test_kick_refinement_no_team(self):
        # 7372: no team id -> no kick.
        self.assertFalse(sim.kick_refinement_now({}, ''))

    def test_kick_refinement_in_flight(self):
        # 7376: an in-flight ceremony already covers the team.
        self.assertFalse(sim.kick_refinement_now(
            {'pendingRefinements': {'team-1': {}}}, 'team-1'))

    def test_refinement_step_defers_unembarked(self):
        # 7408: no free facilitator for the in-flight ceremony -> defer a pass.
        state = {'pendingRefinements': {'team-1': {'embarked': False,
                                                   'reqIds': [], 'people': {},
                                                   'at': 0}},
                 'teams': [{'id': 'team-1'}], 'agents': {}}
        sim._refinement_step(state, 1_000_000, 1_000_000)
        self.assertFalse(state['pendingRefinements']['team-1']['embarked'])

    def test_refinement_step_no_team_facilitator(self):
        # 7423: a team with no free+on-duty facilitator isn't convened.
        state = {'pendingRefinements': {}, 'teams': [{'id': 'team-1'}],
                 'agents': {}, 'sprints': {}}
        sim._refinement_step(state, 1_000_000, 1_000_000)
        self.assertEqual(state['pendingRefinements'], {})

    def test_queue_work_repairs_non_list(self):
        # 1858: a non-list workQueue is replaced with a fresh list.
        state = {'workQueue': {}}
        sim.queue_work(state, [{'title': 'Story A'}])
        self.assertEqual(len(state['workQueue']), 1)


class BreakdownCluster(SimIsolation):
    def test_breakdown_attendees_busy(self):
        # 7496: a busy attendee defers the whole ceremony.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}]}
        self.assertIsNone(sim._breakdown_attendees(state, ['r1'], 'sm-1',
                                                   'team-1'))

    def test_start_breakdown_no_attendees(self):
        # 7507: no healthy attendees -> defer.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}]}
        self.assertFalse(sim._start_breakdown(state, 'team-1', ['r1'], 'sm-1',
                                              1_000_000))

    def test_breakdown_decider_import_failure(self):
        # 7541/7542: a serve import failure degrades to no plan (single card).
        def fake_import(name, *a, **k):
            if name == 'serve':
                raise ImportError('boom')
            return builtins.__import__(name, *a, **k)
        with unittest.mock.patch.object(builtins, '__import__',
                                        side_effect=fake_import):
            self.assertIsNone(sim._breakdown_decider_default({}, 'instr', 'goal'))

    def test_resolve_breakdown_skips_foreign_requests(self):
        # 7594/7596: requests outside the ceremony's reqIds / no longer pending
        # are skipped.
        state = {
            'agents': {'sm-1': {'id': 'sm-1'}},
            'agentRoster': [{'id': 'sm-1', 'name': 'Sam'}],
            'backlogRequests': [
                {'id': 'r1', 'status': 'pending', 'filedBy': 'ada', 'goal': 'g',
                 'title': 't', 'teamId': 'team-1'},
                {'id': 'r2', 'status': 'pending', 'filedBy': 'ada', 'goal': 'g2',
                 'title': 't2', 'teamId': 'team-1'},
                {'id': 'r3', 'status': 'accepted', 'filedBy': 'ada', 'goal': 'g3',
                 'title': 't3', 'teamId': 'team-1'},
            ],
            'workQueue': [],
        }
        pending = {'scrumMasterId': 'sm-1', 'reqIds': ['r1', 'r3'],
                   'teamId': 'team-1', 'people': {}}
        sim._resolve_breakdown(state, pending, 1_000_000,
                               decider=lambda *a, **k: {'items': [{'title': 'Card A'}]})
        self.assertEqual(state['backlogRequests'][0]['status'], 'accepted')
        self.assertEqual(state['backlogRequests'][1]['status'], 'pending')
        self.assertEqual(state['backlogRequests'][2]['status'], 'accepted')

    def test_breakdown_step_defers_no_facilitator(self):
        # 7663-7665: an unembarked breakdown with no free facilitator defers.
        state = {'pendingBreakdowns': {'team-1': {'embarked': False,
                                                  'reqIds': ['r1'], 'people': {},
                                                  'at': 0}},
                 'teams': [{'id': 'team-1'}], 'agents': {}}
        sim._breakdown_step(state, 1_000_000, 1_000_000)
        self.assertFalse(state['pendingBreakdowns']['team-1']['embarked'])

    def test_breakdown_step_embarks(self):
        # 7666: an unembarked breakdown with a free facilitator convenes.
        state = {'pendingBreakdowns': {'team-1': {'embarked': False,
                                                  'reqIds': ['r1'], 'people': {},
                                                  'at': 0}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'agents': {'d1': {'id': 'd1'}},
                 'agentRoster': [{'id': 'd1', 'director': 'd1'}]}
        sim._breakdown_step(state, 1_000_000, 1_000_000)
        self.assertTrue(state['pendingBreakdowns']['team-1']['embarked'])

    def test_breakdown_step_skips_in_flight(self):
        # 7674: a team with an in-flight ceremony isn't double-convened.
        state = {'pendingBreakdowns': {'team-1': {'embarked': True, 'reqIds': [],
                                                  'people': {}, 'at': 0}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'agents': {'d1': {'id': 'd1'}},
                 'agentRoster': [{'id': 'd1', 'director': 'd1'}]}
        sim._breakdown_step(state, 1_000_000, 1_000_000,
                            decider=lambda *a, **k: {'items': []})
        self.assertEqual(state['pendingBreakdowns']['team-1']['embarked'], True)


class RetroCluster(SimIsolation):
    def test_retro_scrum_master_no_director(self):
        # 7724: a sprint team with no director id is skipped.
        self.assertIsNone(sim._retro_scrum_master({'teams': [{'id': 'team-1'}]},
                                                  ['team-1']))

    def test_retro_attendees_missing_team(self):
        # 7753: a sprint team with no matching record is skipped.
        state = {'agents': {'sm-1': {'id': 'sm-1'}}}
        self.assertEqual(sim._retro_attendees(state, ['ghost-team'], 'sm-1'),
                         ['sm-1'])

    def test_retro_attendees_busy(self):
        # 7761: a busy attendee defers the whole meeting.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}}}
        self.assertIsNone(sim._retro_attendees(state, [], 'sm-1'))

    def test_start_retrospective_no_attendees(self):
        # 7772: no healthy attendees -> defer.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}}}
        self.assertFalse(sim._start_retrospective(state, 'sprint-1', [], 'sm-1',
                                                  1_000_000))

    def test_retro_decider_import_failure(self):
        # 7860/7861: a serve import failure records an empty retro.
        def fake_import(name, *a, **k):
            if name == 'serve':
                raise ImportError('boom')
            return builtins.__import__(name, *a, **k)
        with unittest.mock.patch.object(builtins, '__import__',
                                        side_effect=fake_import):
            self.assertEqual(sim._retro_decider_default({}, 'instr', 'sprint-1',
                                                        []), {})

    def test_retro_step_embarks(self):
        # 7903: an unembarked pending retro convenes via _start_retrospective.
        state = {'pendingRetrospectives': {'sprint-1': {'embarked': False,
                                                        'teamIds': ['team-1'],
                                                        'scrumMasterId': 'sm-1',
                                                        'people': {}, 'at': 0}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'agents': {'sm-1': {'id': 'sm-1'}},
                 'agentRoster': [{'id': 'sm-1', 'director': 'd1'}]}
        sim._retro_step(state, 1_000_000, 1_000_000)
        self.assertTrue(state['pendingRetrospectives']['sprint-1']['embarked'])

    def test_retro_step_skips_in_flight(self):
        # 7911: a sprint already retro-ing isn't re-convened from the queue.
        state = {'pendingRetrospectives': {'sprint-1': {'embarked': False,
                                                        'teamIds': ['team-1'],
                                                        'scrumMasterId': 'sm-1',
                                                        'people': {}, 'at': 0}},
                 'pendingSprintRetros': ['sprint-1'],
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'agents': {'sm-1': {'id': 'sm-1'}},
                 'agentRoster': [{'id': 'sm-1', 'director': 'd1'}]}
        sim._retro_step(state, 1_000_000, 1_000_000,
                        decider=lambda *a, **k: {'start': [], 'stop': [],
                                                 'continue': []})
        self.assertEqual(len(state['pendingRetrospectives']), 1)


class EscalationCluster(SimIsolation):
    def test_escalation_decider_import_failure(self):
        # 8380/8381: a serve import failure degrades to no decision (spike).
        def fake_import(name, *a, **k):
            if name == 'serve':
                raise ImportError('boom')
            return builtins.__import__(name, *a, **k)
        with unittest.mock.patch.object(builtins, '__import__',
                                        side_effect=fake_import):
            self.assertIsNone(sim._escalation_decider_default({}, 'instr', 'p1',
                                                              't'))

    def test_escalation_convener_busy(self):
        # 8452: the scrum master must be free + on-duty to convene.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'offDuty': True}}}
        self.assertIsNone(sim._escalation_convener(state, 'p1', 'sm-1'))

    def test_embark_escalation_no_convener(self):
        # 8461: no free convenor -> defer the embark.
        state = {'agents': {'sm-1': {'id': 'sm-1', 'busy': True}}}
        self.assertFalse(sim._embark_escalation(state, 'sm-1', 1_000_000))

    def test_log_escalation_digest_raises(self):
        # 8567/8568: a digest write failure is swallowed (best-effort).
        with unittest.mock.patch.object(serve, 'log_escalation_digest',
                                        side_effect=RuntimeError('boom')):
            sim._log_escalation_digest({}, {}, 'spike')

    def test_escalation_step_skips_nonactive_bug(self):
        # 8599: only walking/working bugs are swept.
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'done'}}}
        sim._escalation_step(state, 1_000_000, 1_000_000)

    def test_escalation_step_no_opened(self):
        # 8603: a working bug with no opened marker is skipped.
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working'}}}
        sim._escalation_step(state, 1_000_000, 1_000_000)

    def test_escalation_step_no_product(self):
        # 8608: a working bug with no productId is skipped.
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working',
                                  'openedAt': 0}}}
        sim._escalation_step(state, 1_000_000, 1_000_000)

    def test_escalation_step_no_director(self):
        # 8612: a product with no owning team is skipped.
        state = {'tasks': {'t1': {'taskType': 'bug', 'status': 'working',
                                  'openedAt': 0, 'productId': 'p1'}}}
        sim._escalation_step(state, 1_000_000, 1_000_000)

    def test_governance_pass_not_server(self):
        # 8636: a non-server-owned state short-circuits governance.
        state = {'sim': {'owner': 'browser'}}
        self.assertIs(sim._governance_pass(state, now=1_000, now_ms=1_000_000),
                      state)


class CoachingCluster(SimIsolation):
    def test_team_health_review_empty(self):
        # 8077: no teams to review -> nothing done.
        self.assertEqual(sim._team_health_review({}, [], 1_000_000), 0)

    def test_escalate_coaching_loop_director_known(self):
        # 8177: the agent's roster director is resolved for the escalation.
        state = {'agentRoster': [{'id': 'ada', 'director': 'd1'}],
                 'agents': {'ada': {'id': 'ada'}},
                 'teams': [], 'backlogRequests': []}
        sim._escalate_coaching_loop(state, 'ada', 'observatory', 3, 1_000_000,
                                    'title')

    def test_escalate_coaching_loop_room_fallback(self):
        # 8178/8189: an unknown agent falls back to the room-keyed team; with no
        # scrum master the team director files the request.
        state = {'agentRoster': [],
                 'agents': {'ada': {'id': 'ada'}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1',
                            'room': 'observatory'}],
                 'backlogRequests': []}
        sim._escalate_coaching_loop(state, 'ada', 'observatory', 3, 1_000_000,
                                    'title')
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_runbook_task_not_bug(self):
        # 8211: a non-bug or product-less task writes nothing.
        sim._runbook_task({'runbooks': {}}, {'taskType': 'code'}, 1_000_000)
        sim._runbook_task({'runbooks': {}}, {'taskType': 'bug'}, 1_000_000)

    def test_runbook_task_trims(self):
        # 8226: the per-product runbook is trimmed to the max entries.
        state = {'runbooks': {'prod-1': [{'ts': i} for i in range(21)]}}
        with unittest.mock.patch.object(sim, '_runbook_decider',
                                        return_value=None):
            sim._runbook_task(state, {'taskType': 'bug', 'productId': 'prod-1',
                                      'title': 'boom', 'assignedTo': 'ada',
                                      'room': 'observatory'}, 1_000_000)
        self.assertEqual(len(state['runbooks']['prod-1']), 20)

    def test_sm_replan_stale_work_bad_room(self):
        # 8951: a card in a non-valued room isn't SM-routed.
        self.assertFalse(sim._sm_replan_stale_work({}, {'title': 'x',
                                                        'room': 'nowhere'},
                                                   1_000_000))

    def test_sm_replan_stale_work_routes(self):
        # 8954/8962: a product-derived team + director filer route the re-plan.
        state = {'products': {'prod-1': {'teamId': 'team-1'}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'backlogRequests': []}
        task = {'title': 'stale', 'room': 'observatory', 'productId': 'prod-1'}
        self.assertTrue(sim._sm_replan_stale_work(state, task, 1_000_000))
        self.assertEqual(state['backlogRequests'][0]['filedBy'], 'd1')

    def test_sm_replan_stale_work_dup_pending(self):
        # 8968: a duplicate pending request -> no re-plan filed.
        state = {'products': {'prod-1': {'teamId': 'team-1'}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'backlogRequests': [{'status': 'pending', 'room': 'observatory',
                                      'title': 'stale'}]}
        self.assertFalse(sim._sm_replan_stale_work(state, {'title': 'stale',
                                                           'room': 'observatory',
                                                           'productId': 'prod-1'},
                                                   1_000_000))

    def test_sm_help_stuck_worker_bad_room(self):
        # 8981: a card in a non-valued room isn't routed.
        self.assertFalse(sim._sm_help_stuck_worker({}, {'title': 'x',
                                                        'room': 'nowhere'},
                                                   1_000_000))

    def test_sm_help_stuck_worker_routes(self):
        # 8984/8990: a product-derived team + director filer route the signal.
        state = {'products': {'prod-1': {'teamId': 'team-1'}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'backlogRequests': []}
        task = {'title': 'hard', 'room': 'observatory', 'productId': 'prod-1'}
        self.assertTrue(sim._sm_help_stuck_worker(state, task, 1_000_000))
        self.assertEqual(state['backlogRequests'][0]['filedBy'], 'd1')

    def test_sm_help_stuck_worker_dup_pending(self):
        # 8996: a duplicate pending request -> not routed.
        state = {'products': {'prod-1': {'teamId': 'team-1'}},
                 'teams': [{'id': 'team-1', 'directorId': 'd1'}],
                 'backlogRequests': [{'status': 'pending', 'room': 'observatory',
                                      'title': 'hard'}]}
        self.assertFalse(sim._sm_help_stuck_worker(state, {'title': 'hard',
                                                           'room': 'observatory',
                                                           'productId': 'prod-1'},
                                                   1_000_000))

    def test_worker_stuck_help_signal_unknown_agent(self):
        # 9017: no agent record for the failing worker -> no signal.
        sim._worker_stuck_help_signal({'agents': {}}, 'ghost', {'id': 't1'},
                                      False, 1_000_000)

    def test_stale_work_skips_non_dict(self):
        # 8870: a malformed task entry is skipped.
        state = {'lastStaleWorkSweep': 0, 'tasks': {'t1': 'junk'}, 'agents': {}}
        self.assertEqual(sim._stale_work_step(state, 1_000, 1_000_000), 0)

    def test_stale_work_skips_bug(self):
        # 8874: bugs own their restore alarm -- not swept here.
        state = {'lastStaleWorkSweep': 0,
                 'tasks': {'t1': {'status': 'working', 'taskType': 'bug'}},
                 'agents': {}}
        self.assertEqual(sim._stale_work_step(state, 1_000, 1_000_000), 0)

    def test_stale_work_recent_walking(self):
        # 8882: a walking task younger than the timeout isn't swept.
        state = {'lastStaleWorkSweep': 0,
                 'tasks': {'t1': {'status': 'walking',
                                  'openedAt': 1_000_000 - 1}},
                 'agents': {}}
        self.assertEqual(sim._stale_work_step(state, 1_000, 1_000_000), 0)

    def test_coaching_loop_skips_non_list_plans(self):
        # 9049: a malformed growth-plan entry is skipped.
        state = {'growthPlans': {'ada': 'notalist'}, 'agents': {},
                 'completedDeliverables': []}
        self.assertEqual(sim._coaching_loop_step(state, 1_000_000_000), 0)

    def test_coaching_loop_skips_unrelated_kinds(self):
        # 9051: plans with no coaching kind aren't re-coached.
        state = {'growthPlans': {'ada': [{'kind': 'adopt'}]}, 'agents': {},
                 'completedDeliverables': []}
        self.assertEqual(sim._coaching_loop_step(state, 1_000_000_000), 0)

    def test_coaching_loop_pressoffice_fallback(self):
        # 9058: an agent in a non-valued room is re-coached at pressoffice.
        state = {'growthPlans': {'ada': [{'kind': 'low_grade'}]},
                 'agents': {'ada': {'id': 'ada', 'room': 'basement'}},
                 'completedDeliverables': [{'agentId': 'ada', 'grade': 3,
                                            'gradeIsReal': True}],
                 'sim': {}, 'emailOutbox': [], 'teams': []}
        self.assertEqual(sim._coaching_loop_step(state, 1_000_000_000), 1)


class GovernanceCluster(SimIsolation):
    def test_log_governance_raises(self):
        # 8800/8803: a failed governance log is swallowed.
        with unittest.mock.patch.object(serve, 'log_action',
                                        side_effect=RuntimeError('boom')):
            sim._log_governance({}, 'admin', 'test', {})

    def test_revoke_agent_credentials_raises(self):
        # 8816/8817: a failed credential revocation is swallowed.
        with unittest.mock.patch.object(serve, 'revoke_agent_credentials',
                                        side_effect=RuntimeError('boom')):
            sim._revoke_agent_credentials('ghost')


if __name__ == '__main__':
    unittest.main()