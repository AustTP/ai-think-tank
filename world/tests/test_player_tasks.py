"""Tests for the player's human-in-the-loop tasks (2026-10-06).

The think tank can hand the PLAYER a card (a queue item with assignedTo
'player' -- a spike or a one-off). It is delivered as a durable state['tasks']
record in a 'needs_player' status with a player-inbox card + email; the player
marks it done (complete_player_task), and the EXISTING dependency machinery
releases anything queued behind it in BOTH directions:
  - an agent card depends_on_task on the player's task stays unassigned until
    the player completes it (_dependency_landed is 'done'-based);
  - the player's own task can dependsOn an agent story, holding it undelivered
    until that story lands.
Hermetic: DB redirected to a throwaway temp dir (the pattern every other sim
test uses) so log_action writes are harmless.
"""
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


class PlayerTaskBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-player-task-test-')
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

    def _state(self):
        return {
            'agentRoster': [
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'isAdmin': False,
                 'director': 'faye'},
                {'id': 'faye', 'name': 'Faye', 'role': 'Admin', 'isAdmin': True},
            ],
            'agents': {'ada': {'id': 'ada', 'offDuty': False},
                       'faye': {'id': 'faye', 'offDuty': False}},
            'teams': [{'id': 'ada', 'directorId': 'ada', 'scrumMasterId': 'ada',
                       'prefix': 'DEV', 'name': 'Dev Team'}],
            'workQueue': [],
            'tasks': {},
            'issues': {},
        }

    def _assign_one_player_card(self, state, title='Player hands-on step',
                                room='pressoffice'):
        """Queue a player spike, pop it like the task cycle would, assign it.
        Returns the player task dict."""
        sim.queue_spike(state, title, room, 60_000, assigned_to='player')
        pick = state['workQueue'].pop(0)
        return sim._assign_due_item(state, pick, False, {}, {},
                                    int(time.time() * 1000), [0])


class PlayerTaskAssignment(PlayerTaskBase):
    def test_queue_spike_accepts_assigned_to_player(self):
        state = self._state()
        sim.queue_spike(state, 'Write the release blurb', 'pressoffice',
                        60_000, assigned_to='player')
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'player')

    def test_queue_once_accepts_assigned_to_player_and_composes_with_dependency(self):
        state = self._state()
        item = sim.queue_once(state, 'Draft the Q3 narrative', 10 ** 12,
                              assigned_to='player', depends_on_task='task-42')
        self.assertEqual(item['assignedTo'], 'player')
        self.assertEqual(item['dependsOn'], 'task-42')

    def test_player_card_is_assigned_to_the_player_not_an_agent(self):
        state = self._state()
        task = self._assign_one_player_card(state)
        self.assertIsNotNone(task)
        self.assertEqual(task['assignedTo'], 'player')
        self.assertEqual(task['status'], 'needs_player')
        self.assertIs(state['tasks'][task['id']], task)
        # Delivered to the player: inbox card + real email, no agent touched.
        inbox = state['playerInbox']
        self.assertTrue(any(m.get('kind') == 'player_task'
                            and m.get('taskId') == task['id'] for m in inbox))
        self.assertTrue(any(e.get('kind') == 'player_task'
                            for e in state['emailOutbox']))
        self.assertIsNone(state['agents']['ada'].get('task'),
                          'the player card must never land on an agent')

    def test_agent_card_depending_on_a_player_task_waits_for_the_player(self):
        state = self._state()
        player_task = self._assign_one_player_card(state)
        player_task_id = player_task['id']
        sim.queue_work(state, [{'title': 'agent follow-up', 'room': 'pressoffice',
                                'dependsOn': player_task_id}])
        now_ms = int(time.time() * 1000)
        # Gated: not landed, not assignable, picker skips it entirely.
        self.assertFalse(sim._dependency_landed(state, player_task_id))
        self.assertFalse(sim._work_item_dependency_met(state, state['workQueue'][0]))
        self.assertEqual(sim.pick_next_due_index(state['workQueue'], now_ms, set(), state), -1)
        # The player completes it -> the agent card is now assignable.
        done = sim.complete_player_task(state, player_task_id, now_ms=now_ms)
        self.assertIsNotNone(done)
        self.assertEqual(done['status'], 'done')
        self.assertTrue(sim._dependency_landed(state, player_task_id))
        self.assertTrue(sim._work_item_dependency_met(state, state['workQueue'][0]))
        self.assertEqual(sim.pick_next_due_index(state['workQueue'], now_ms, set(), state), 0)

    def test_complete_player_task_validates_ownership_and_status(self):
        state = self._state()
        now_ms = int(time.time() * 1000)
        self.assertIsNone(sim.complete_player_task(state, 'task-999', now_ms=now_ms))
        # Not player-owned.
        state['tasks']['task-7'] = {'id': 'task-7', 'title': 't',
                                    'assignedTo': 'ada', 'status': 'working'}
        self.assertIsNone(sim.complete_player_task(state, 'task-7', now_ms=now_ms))
        # Already done.
        state['tasks']['task-8'] = {'id': 'task-8', 'title': 't',
                                    'assignedTo': 'player', 'status': 'done'}
        self.assertIsNone(sim.complete_player_task(state, 'task-8', now_ms=now_ms))

    def test_completion_marks_the_inbox_card_done(self):
        state = self._state()
        task = self._assign_one_player_card(state)
        sim.complete_player_task(state, task['id'])
        card = next(m for m in state['playerInbox'] if m.get('id') == task['playerInboxId'])
        self.assertEqual(card['status'], 'done')


class PlayerTaskDependencyRelease(PlayerTaskBase):
    def test_completion_auto_clears_an_issue_blocked_on_the_player_task(self):
        state = self._state()
        player_task = self._assign_one_player_card(state)
        pid = player_task['id']
        # An issue committed blocked on the player's task (agents waiting).
        issue = sim.file_issue(state, 'ada', 'story', 'Follow-up story',
                               'feature', 'ben')
        self.assertIsNotNone(issue)
        issue['blocked'] = True
        issue['blockedKind'] = 'stuck_on_agent'
        issue['dependsOnTask'] = pid
        sim.complete_player_task(state, pid)
        self.assertTrue(
            any(c.get('kind') == 'unblock_landed' and c.get('issueKey') == issue['key']
                for c in state.get('_pendingBlockChanges', [])),
            'completing the player task must queue the SM auto-unblock, the same '
            'release an agent\'s finish_task triggers')


class PlayerTaskCreationChannels(PlayerTaskBase):
    def test_file_issue_can_mark_a_card_for_the_player(self):
        state = self._state()
        issue = sim.file_issue(state, 'ada', 'story', 'Draft the copy',
                               'marketing', 'ben', assigned_to='player')
        self.assertEqual(issue['assignedTo'], 'player')
        self.assertEqual(state['backlogRequests'][0]['assignedTo'], 'player')

    def test_refinement_hands_a_player_card_to_the_player(self):
        state = self._state()
        sim.file_issue(state, 'ada', 'spike', 'Investigate pricing',
                       'research', 'ben', assigned_to='player')
        req = state['backlogRequests'][0]
        pending = {'scrumMasterId': 'ada', 'reqIds': [req['id']], 'teamId': 'ada'}

        def decider(instructions, criteria):
            return 'accept'

        sim._resolve_refinement(state, pending, int(time.time() * 1000), decider=decider)
        item = state['workQueue'][-1]
        self.assertEqual(item['assignedTo'], 'player')
        self.assertEqual(item['issueKey'], req['issueKey'])

    def test_a_player_card_counts_as_work_for_the_idle_gate(self):
        state = self._state()
        sim.queue_spike(state, 'Player hands-on step', 'pressoffice',
                        60_000, assigned_to='player')
        # The generic due-item clause already counts it: the tank must not
        # treat a waiting player card as a reason to idle out.
        self.assertTrue(sim.think_tank_has_work(state, int(time.time() * 1000)))


if __name__ == '__main__':
    unittest.main()