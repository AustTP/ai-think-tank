"""Standing-task scheduling wiring (ask 5): the cron sweep.

schedule.py is pure and already tested on its own. These tests
cover the WIRING in sim._standing_task_sweep: every
state['standingTasks'] declaration is ticked through the
module's StandingScheduler, a 'start' verdict queues a real
queue_work card (with standingTaskId so a completion can be
read back off the durable task mirror), a 'stop' is logged as
governance, and continue/idle are silent. Malformed
declarations are skipped, never wedging the rest of the sweep.
The final test proves the sweep actually runs from
_check_schedules, the same pass as every other cadence.

The scheduler's per-task runtime state lives in-process
(sim._STANDING_SCHEDULER.states) and is deliberately NOT
persisted: a restart re-anchors hours-mode tasks and forgets
occurrence counts. Each test clears it in setUp.
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402


class SimIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-standing-')
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
        # The scheduler owns its runtime state in-process; clear it
        # so one test's ticks never leak into the next.
        sim._STANDING_SCHEDULER.states.clear()
        self.governance = []
        self._gov = unittest.mock.patch.object(
            sim, '_log_governance',
            side_effect=lambda state, agent_id, action, details:
                self.governance.append((agent_id, action, details)))
        self._gov.start()
        self.addCleanup(self._gov.stop)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        sim._STANDING_SCHEDULER.states.clear()

    def _sweep(self, state, when):
        sim._standing_task_sweep(state, when,
                                 int(when.timestamp() * 1000))

    def _declaration(self, **overrides):
        declaration = {'taskId': 'standup', 'name': 'Daily standup',
                       'mode': 'always', 'agentCount': 2,
                       'room': 'observatory', 'team': 't1'}
        declaration.update(overrides)
        return declaration

    def _state(self, declarations):
        return {'sim': {'owner': 'server'},
                'standingTasks': declarations,
                'workQueue': []}


class StandingTaskSweep(SimIsolation):
    def test_always_start_queues_a_real_card_and_logs_governance(self):
        state = self._state([self._declaration()])
        self._sweep(state, datetime.datetime(2025, 6, 16, 9, 0))
        self.assertEqual(len(state['workQueue']), 1)
        card = state['workQueue'][0]
        self.assertEqual(card['title'], 'Standing task: Daily standup')
        self.assertEqual(card['room'], 'observatory')
        self.assertEqual(card['standingTaskId'], 'standup')
        self.assertEqual(card['teamId'], 't1')
        self.assertEqual(card['kbClass'], 'changes_how_we_work')
        self.assertIn('crew', card['instructions'])
        self.assertIn('director', card['instructions'])
        self.assertEqual(self.governance, [(
            'server', 'standing_task_tick', {
                'action': 'start', 'taskId': 'standup',
                'reason': 'active window opened',
                'agentCount': 2, 'leadRole': 'director'})])

    def test_the_second_tick_continues_silently(self):
        state = self._state([self._declaration()])
        when = datetime.datetime(2025, 6, 16, 9, 0)
        self._sweep(state, when)
        self._sweep(state, when + datetime.timedelta(minutes=5))
        # The running work is already the card the start queued; a
        # continue must not queue a second one or log again.
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(len(self.governance), 1)

    def test_a_window_opens_and_closes(self):
        state = self._state([self._declaration(
            mode='window', startTime='09:00', endTime='10:00')])
        self._sweep(state, datetime.datetime(2025, 6, 16, 9, 30))
        self.assertEqual(len(state['workQueue']), 1)
        self._sweep(state, datetime.datetime(2025, 6, 16, 10, 30))
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(len(self.governance), 2)
        self.assertEqual(self.governance[1][2]['action'], 'stop')
        self.assertEqual(self.governance[1][2]['reason'],
                         'active window closed')

    def test_hours_mode_runs_its_duration_then_finishes(self):
        # A crew of 4 needs a scrum master (mirrors
        # SCRUM_MASTER_MIN_TEAM_SIZE); hours mode is one-shot: the
        # first tick anchors the clock, the period ends when the
        # duration elapses, and the task is finished after that.
        state = self._state([self._declaration(
            taskId='health', name='Sprint health check', mode='hours',
            durationHours=1, agentCount=4)])
        base = datetime.datetime(2025, 6, 16, 9, 0)
        self._sweep(state, base)                       # start
        self._sweep(state, base + datetime.timedelta(minutes=30))
        self._sweep(state, base + datetime.timedelta(hours=2))   # stop
        self._sweep(state, base + datetime.timedelta(hours=3))   # done
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(len(self.governance), 2)
        self.assertEqual(self.governance[0][2]['leadRole'],
                         'scrum master')
        self.assertEqual(self.governance[1][2]['action'], 'stop')

    def test_a_declaration_with_an_unknown_mode_is_skipped(self):
        state = self._state([self._declaration(
            taskId='bad', name='Bad', mode='bogus'),
            self._declaration(taskId='good', name='Good')])
        self._sweep(state, datetime.datetime(2025, 6, 16, 9, 0))
        # The malformed declaration never wedges the good one.
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['standingTaskId'], 'good')
        self.assertEqual(self.governance[0][2]['taskId'], 'good')

    def test_a_declaration_without_a_task_id_is_skipped(self):
        state = self._state([{'name': 'No id', 'mode': 'always'}])
        self._sweep(state, datetime.datetime(2025, 6, 16, 9, 0))
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(self.governance, [])

    def test_a_window_without_times_is_skipped(self):
        state = self._state([{'taskId': 'w', 'name': 'W',
                              'mode': 'window'}])
        self._sweep(state, datetime.datetime(2025, 6, 16, 9, 0))
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(self.governance, [])

    def test_check_schedules_ticks_standing_tasks(self):
        # The sweep must run from the same _check_schedules pass as
        # every other cadence (research topics, skill review,
        # distillation, pipelines, player-ask/supervisor-vote).
        state = self._state([self._declaration()])
        state['researchTopics'] = []
        with unittest.mock.patch.object(sim, '_check_pipelines'), \
             unittest.mock.patch.object(sim, '_pending_player_ask_sweep'), \
             unittest.mock.patch.object(sim, '_supervisor_block_vote_sweep'), \
             unittest.mock.patch.object(sim, '_skill_review_has_pending',
                                        return_value=False), \
             unittest.mock.patch.object(sim, '_distill_has_new_archives',
                                        return_value=False):
            sim._check_schedules(state, 1000.0, 1000000)
        self.assertTrue(any(w.get('standingTaskId') == 'standup'
                            for w in state['workQueue']))


if __name__ == '__main__':
    unittest.main()
