"""Coverage-push tests for world/sim.py residual branches: the player-ask
director gate, issue-status mirroring, wake-on-mail action routing, and the
SM-committed block/dependency machinery. Uses the temp-DB isolation pattern
from test_sim_gap.py so the real think_tank.db is never touched; serve
network/DB seams are mocked at the module attribute.
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


class _BoomDict(dict):
    """A dict whose setdefault raises, to exercise the _log_block_commit
    except branch without weakening the target code."""

    def setdefault(self, *args, **kwargs):
        raise RuntimeError('boom')


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-simgapD-')
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
    def _issue_state(**over):
        base = {
            'issues': {
                'DEV-1': {'key': 'DEV-1', 'teamId': 'dev',
                          'title': 'Ship reports', 'summary': 'reports',
                          'status': 'open', 'blocked': False},
            },
            'teams': [{'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'sm'}],
            'agents': {
                'sm': {'id': 'sm', 'busy': False, 'offDuty': False},
                'nadia': {'id': 'nadia', 'busy': False, 'offDuty': False},
                'dev': {'id': 'dev', 'busy': False, 'offDuty': False},
            },
            'workQueue': [],
        }
        base.update(over)
        return base


class IssueStatus(SimIsolation):
    def test_set_issue_status_supersedes_pending_input(self):
        # 6150-6153: a terminal status on a needsInput card supersedes the
        # awaiting player-inbox message instead of leaving it waiting forever.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1', 'status': 'open',
                                 'needsInput': True}},
            'playerInbox': [
                {'id': 'ask-1', 'issueKey': 'DEV-1', 'status': 'awaiting_input'},
                {'id': 'ask-2', 'issueKey': 'OTHER-1', 'status': 'awaiting_input'},
            ],
        }
        issue = sim.set_issue_status(state, 'DEV-1', 'closed')
        self.assertIsNotNone(issue)
        self.assertFalse(issue['needsInput'])
        self.assertEqual(state['playerInbox'][0]['status'], 'superseded')
        self.assertEqual(state['playerInbox'][1]['status'], 'awaiting_input')

    def test_issue_owning_team_unknown_issue(self):
        # 6184: an issue_key with no record has no owning team.
        self.assertIsNone(sim._issue_owning_team({}, 'DEV-1'))


class PlayerAskGate(SimIsolation):
    def test_request_player_input_no_director(self):
        # 6203: no owning team/director -> the ask is never surfaced.
        self.assertIsNone(sim.request_player_input({}, 'DEV-1', 'nadia', 'q'))

    def test_request_player_input_already_needs_input(self):
        # 6208: the card already awaits player input -- don't double-ask.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1', 'teamId': 'dev',
                                 'needsInput': True}},
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
        }
        self.assertIsNone(sim.request_player_input(state, 'DEV-1', 'nadia', 'q',
                                                   now_ms=1))
        self.assertNotIn('_pendingPlayerAsk', state)

    def test_request_player_input_already_pending(self):
        # 6212: a gate is already seeded for this issue -- no duplicate ask.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1', 'teamId': 'dev'}},
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
            '_pendingPlayerAsk': {'issueKey': 'DEV-1', 'directed': False, 'at': 0},
        }
        self.assertIsNone(sim.request_player_input(state, 'DEV-1', 'nadia', 'q',
                                                   now_ms=1))

    def test_director_gate_due_for_mismatch(self):
        # 6228-6230: nothing pending for this issue -> not due.
        self.assertFalse(sim._director_gate_due_for({}, 'DEV-1', 10000))

    def test_director_gate_due_for_already_directed(self):
        # 6229-6230: an already-directed ask is not re-gated.
        state = {'_pendingPlayerAsk': {'issueKey': 'DEV-1', 'directed': True}}
        self.assertFalse(sim._director_gate_due_for(state, 'DEV-1', 10000))

    def test_director_gate_due_for_elapsed(self):
        # 6231: an undirected ask old enough for the director to consider.
        state = {'_pendingPlayerAsk': {'issueKey': 'DEV-1', 'directed': False,
                                       'at': 0}}
        self.assertTrue(sim._director_gate_due_for(state, 'DEV-1', 10000))

    def test_player_input_verdict_nothing_pending(self):
        # 6242: no matching pending ask -> nothing to resolve.
        self.assertIsNone(sim.request_player_input_verdict({}, 'DEV-1'))

    def test_player_input_verdict_issue_vanished(self):
        # 6246: the ask is due but its issue/director vanished -> no verdict.
        state = {'_pendingPlayerAsk': {'issueKey': 'DEV-1'}}
        self.assertIsNone(sim.request_player_input_verdict(state, 'DEV-1'))

    def test_pending_ask_sweep_not_due(self):
        # 6447-6448: the ask exists but its gate window hasn't elapsed.
        state = {'_pendingPlayerAsk': {'issueKey': 'DEV-1', 'directed': False,
                                       'at': 0}}
        self.assertIsNone(sim._pending_player_ask_sweep(state, 1000))

    def test_pending_ask_sweep_due_resolves_internally(self):
        # 6449: a due ask spins through the director gate (resolve_internally).
        state = {
            '_pendingPlayerAsk': {'issueKey': 'DEV-1', 'agentId': 'nadia',
                                  'question': 'Which format?', 'context': '',
                                  'at': 0, 'directed': False},
            'issues': {'DEV-1': {'key': 'DEV-1', 'teamId': 'dev'}},
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
            'agents': {'nadia': {'id': 'nadia', 'offDuty': False}},
        }
        with unittest.mock.patch('serve.log_action'):
            res = sim._pending_player_ask_sweep(
                state, 20000, decider=lambda *a: 'resolve_internally')
        self.assertEqual(res, 'internal')
        self.assertNotIn('_pendingPlayerAsk', state)

    def test_resolve_player_ask_unknown_message(self):
        # 6412: an unknown / non-awaiting message id resolves nothing.
        self.assertIsNone(sim.resolve_player_ask({}, 'ask-1', 'answer'))
        state = {'playerInbox': [{'id': 'ask-1', 'status': 'answered'}]}
        self.assertIsNone(sim.resolve_player_ask(state, 'ask-1', 'answer'))

    def test_resolve_player_ask_stamps_profile_notes(self):
        # 6422-6423: the player's answer is stamped onto the agent's profile
        # notes and the card is unblocked.
        state = {
            'playerInbox': [{'id': 'ask-1', 'status': 'awaiting_input',
                             'issueKey': 'DEV-1', 'agentId': 'nadia'}],
            'issues': {'DEV-1': {'key': 'DEV-1', 'summary': 'Ship reports',
                                 'needsInput': True}},
            'agents': {'nadia': {'id': 'nadia', 'profile': {}}},
            'workQueue': [],
        }
        m = sim.resolve_player_ask(state, 'ask-1', 'Use JSON')
        self.assertIsNotNone(m)
        self.assertEqual(m['status'], 'answered')
        self.assertEqual(state['agents']['nadia']['profile']['notes'],
                         ["Player on DEV-1 (Ship reports): Use JSON"])


class BlockChangeRequests(SimIsolation):
    def test_file_block_change_unknown_issue(self):
        # 6485: a block-change can't be filed for an unknown issue.
        self.assertIsNone(sim._file_block_change({}, 'DEV-1', True, 'nadia',
                                                 'stuck_on_agent', 'r'))

    def test_file_block_change_invalid_kind(self):
        # 6487: only BLOCK_CHANGE_KINDS may be filed.
        state = {'issues': {'DEV-1': {'key': 'DEV-1'}}}
        self.assertIsNone(sim._file_block_change(state, 'DEV-1', True, 'nadia',
                                                 'bogus', 'r'))

    def test_file_block_change_dedup(self):
        # 6491: a second live request for the same issue is refused.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1'}},
            '_pendingBlockChanges': [{'issueKey': 'DEV-1', 'state': 'pending'}],
        }
        self.assertIsNone(sim._file_block_change(state, 'DEV-1', True, 'nadia',
                                                 'stuck_on_agent', 'r'))

    def test_request_block_dependency_no_director(self):
        # 6741: no owning team to route the SM commit through.
        self.assertIsNone(sim.request_block_dependency({}, 'DEV-1', 'nadia',
                                                       'task-1'))

    def test_auto_clear_none_task_id(self):
        # 6757: an empty completed-task id clears nothing.
        self.assertEqual(sim._auto_clear_dependency_blocks({}, None), 0)

    def test_auto_clear_committed_block_no_requester(self):
        # 6781: the unblock files but no waiting agent can be recovered.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1', 'blocked': True,
                                 'blockedKind': 'stuck_on_agent',
                                 'dependsOnTask': 'task-5', 'teamId': 'dev'}},
        }
        cleared = sim._auto_clear_dependency_blocks(state, 'task-5', now_ms=1000)
        self.assertEqual(cleared, 1)
        self.assertEqual(len(sim._pending_block_changes(state, 'DEV-1')), 1)

    def test_auto_clear_drops_pending_dependency(self):
        # 6802-6803: a not-yet-committed stuck_on_agent change on the landed
        # task is dropped entirely.
        state = {'_pendingBlockChanges': [
            {'id': 'c1', 'issueKey': 'DEV-2', 'teamId': 'dev', 'wanted': True,
             'requesterId': 'nadia', 'kind': 'stuck_on_agent', 'reason': 'r',
             'dependsOnTask': 'task-5', 'at': 1, 'state': 'pending',
             'committed': False},
        ]}
        cleared = sim._auto_clear_dependency_blocks(state, 'task-5', now_ms=1000)
        self.assertEqual(cleared, 1)
        self.assertEqual(state['_pendingBlockChanges'], [])

    def test_dependency_requester_none(self):
        # 6825: no stuck_on_agent change references this issue+task.
        self.assertIsNone(sim._dependency_requester({}, 'DEV-1', 'task-5'))

    def test_agent_still_blocked_skips_unblocked(self):
        # 6838: only committed stuck_on_agent blocks are considered.
        state = {
            'issues': {
                'DEV-1': {'key': 'DEV-1'},
                'DEV-2': {'key': 'DEV-2', 'blocked': True,
                          'blockedKind': 'stuck_on_agent',
                          'dependsOnTask': 'task-x'},
            },
        }
        self.assertFalse(sim._agent_still_blocked(state, 'nadia'))

    def test_supervisor_vote_mismatch(self):
        # 6864: no claim in flight for this issue -> no vote.
        self.assertFalse(sim._supervisor_block_vote({}, 'DEV-1'))

    def test_supervisor_vote_no_director(self):
        # 6869: the claim's director (or issue) vanished -> rejected.
        state = {'_pendingBlockClaim': {'issueKey': 'DEV-1'}}
        self.assertFalse(sim._supervisor_block_vote(state, 'DEV-1'))

    def test_request_block_claim_met_no_director(self):
        # 6915: a claim needs an owning team's director.
        self.assertFalse(sim.request_block_claim_met({}, 'DEV-1', 'nadia'))

    def test_request_block_claim_met_already_blocked(self):
        # 6924-6927: an already-blocked card files an unblock instead of a claim.
        state = {
            'issues': {'DEV-1': {'key': 'DEV-1', 'teamId': 'dev',
                                 'blocked': True}},
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
        }
        self.assertFalse(sim.request_block_claim_met(state, 'DEV-1', 'nadia',
                                                     now_ms=1))
        self.assertEqual(len(sim._pending_block_changes(state, 'DEV-1')), 1)
        self.assertEqual(sim._pending_block_changes(state, 'DEV-1')[0]['kind'],
                         'unblock_resolved')


class MailAction(SimIsolation):
    def test_mail_action_step_clears_when_busy(self):
        # 6560-6562: a woken agent already acting hands off to normal flow.
        state = {'agents': {'nadia': {'id': 'nadia', 'busy': True,
                                      '_mailAwake': {'kind': 'player_answer',
                                                     'entry': {}}}}}
        routed = sim._mail_action_step(state, 0, 1000)
        self.assertEqual(routed, 1)
        self.assertNotIn('_mailAwake', state['agents']['nadia'])

    def test_queue_mail_work_task_id(self):
        # 6583-6584: a task-referencing mail re-arms that task for the agent.
        state = {'workQueue': []}
        self.assertTrue(sim._queue_mail_work(state, 'nadia', 'player_answer',
                                             {'taskId': 'task-1'}, 1000))
        self.assertEqual(state['workQueue'][0]['goal'], 'task-1')
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'nadia')

    def test_queue_mail_work_empty_payload(self):
        # 6585: no issueKey or taskId -> nothing queued.
        self.assertFalse(sim._queue_mail_work({}, 'nadia', 'x', {}, 1000))

    def test_requeue_card_task_unknown_issue(self):
        # 6594: a mail referencing a vanished issue queues nothing.
        self.assertFalse(sim._requeue_card_task({}, 'nadia', 'DEV-1', 1000))

    def test_requeue_card_task_queue_not_list(self):
        # 6597: a non-list workQueue can't take the resume item.
        state = {'issues': {'DEV-1': {'key': 'DEV-1'}}, 'workQueue': None}
        self.assertFalse(sim._requeue_card_task(state, 'nadia', 'DEV-1', 1000))


class BlockStep(SimIsolation):
    def test_block_step_no_team(self):
        # 6650: a pending change with no owning team is deferred.
        state = {'_pendingBlockChanges': [
            {'id': 'c1', 'issueKey': 'DEV-1', 'teamId': 'ghost',
             'state': 'pending'}]}
        self.assertIsNone(sim._block_step(state, 0, 100000))

    def test_block_step_no_scrum_master(self):
        # 6656: no effective scrum master for the team yet -- defer.
        state = {
            'teams': [{'id': 'dev'}],
            '_pendingBlockChanges': [
                {'id': 'c1', 'issueKey': 'DEV-1', 'teamId': 'dev',
                 'state': 'pending', 'wanted': True,
                 'kind': 'requirements_met', 'requesterId': 'nadia'}],
        }
        self.assertIsNone(sim._block_step(state, 0, 100000))

    def test_block_step_issue_vanished(self):
        # 6664: the change's issue vanished before the SM could commit it.
        state = {
            'teams': [{'id': 'dev', 'scrumMasterId': 'sm', 'directorId': 'dev'}],
            'agents': {'sm': {'id': 'sm', 'busy': False, 'offDuty': False}},
            'teamBlockAt': {},
            'issues': {},
            '_pendingBlockChanges': [
                {'id': 'c1', 'issueKey': 'ghost', 'teamId': 'dev',
                 'state': 'pending', 'wanted': True,
                 'kind': 'requirements_met', 'requesterId': 'nadia',
                 'reason': 'r'}],
        }
        self.assertIsNone(sim._block_step(state, 0, 100000))

    def test_log_block_commit_setdefault_raises(self):
        # 6727-6728: a failing actionLog write must never abort the commit.
        sim._log_block_commit(_BoomDict(), {'committedBy': 'sm'},
                              {'blocked': True}, 1)


if __name__ == '__main__':
    unittest.main()