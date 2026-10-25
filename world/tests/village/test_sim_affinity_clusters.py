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

    def test_help_signal_mail_is_advisory_not_a_work_card(self):
        # "Act on mail (<agent>)" was queued into the code room for every help
        # signal, ran the Python gate, and looped. The mail is advisory.
        state = {'workQueue': [], 'agents': {'harold': {'id': 'harold'}}}
        self.assertTrue(sim._queue_mail_work(
            state, 'nora', 'help_signal', {'about': 'harold'}, 1_000_000))
        self.assertEqual(state['workQueue'], [],
                         'a personnel signal must not create a deliverable card')

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
