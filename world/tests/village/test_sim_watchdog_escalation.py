"""Tests for the R6 sim-loop death escalation ladder.

Invariants:
1. A watchdog failure event stamped on sim._SIM_WATCHDOG_EVENT is consumed
   ONCE by the next non-dormant _sim_loop_pass (clear-on-read inside the
   single read-modify-write); while DORMANT it waits for the wake tick.
2. Delivery mails the SENIOR DIRECTOR (never the admin first) and pins a thin
   investigate work item to her -- directly when she is on-duty (no marker),
   via _mail_action_step when she was off-duty.
3. Only once the streak reaches 2 restarts is the admin ALSO mailed (distinct
   kind `sim_watchdog_escalated`, which must never queue work: isAdmin is
   excluded from assignment) and an audit row filed.
4. serve._sim_watchdog_note_failure stamps the side channel, counts the
   streak, and alerts the PLAYER (Telegram) only when the restart itself
   failed or the streak hits _SIM_WATCHDOG_PLAYER_ALERT_AFTER -- once per
   streak; _sim_watchdog_note_healthy resets the ladder.
5. An unroutable system mail (no issueKey/taskId/about) clears its
   _mailAwake marker instead of holding the agent awake forever.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000


def _state(**over):
    base = {
        'sim': {'owner': 'server'},
        'teamBlockAt': {},
        'agents': {
            'nora': {'id': 'nora', 'name': 'Nora', 'busy': False, 'offDuty': False},
            'theo': {'id': 'theo', 'name': 'Theo', 'busy': False, 'offDuty': False},
            'worker': {'id': 'worker', 'name': 'Worker', 'busy': False, 'offDuty': False},
        },
        'teams': [],
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'nora', 'isAdmin': False, 'isDirector': True},
            {'id': 'worker', 'isAdmin': False, 'isDirector': False},
        ],
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
        'issues': {},
    }
    base.update(over)
    return base


def _evt(**over):
    e = {'kind': 'dead', 'detail': 'exception SystemExit', 'restarts': 1,
         'at': time.time(), 'player_alert': False}
    e.update(over)
    return e


class DeliverWatchdogEvent(unittest.TestCase):
    def test_on_duty_senior_gets_mail_and_pinned_item_admin_gets_nothing(self):
        st = _state()
        sim._deliver_watchdog_event(st, _evt(restarts=1))
        mailbox = st['agents']['nora']['mailbox']
        self.assertEqual(len(mailbox), 1)
        self.assertEqual(mailbox[0]['kind'], 'sim_watchdog')
        self.assertIn('dead', mailbox[0]['text'])
        self.assertFalse(st['agents']['nora'].get('_mailAwake'))
        queued = [w for w in st['workQueue'] if w.get('_mailResume')]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['assignedTo'], 'nora')
        self.assertEqual(queued[0]['goal'], 'sim-watchdog')
        self.assertEqual(queued[0]['taskType'], 'research')
        self.assertIsNone(queued[0].get('reviewOf'))
        # Admin is NOT mailed at restart #1.
        self.assertEqual(st['agents']['theo'].get('mailbox', []), [])
        self.assertEqual(
            [w for w in st['workQueue'] if w.get('assignedTo') == 'theo'], [])

    def test_off_duty_senior_routes_via_mail_action_step(self):
        st = _state()
        st['agents']['nora']['offDuty'] = True
        st['agents']['nora']['visible'] = False
        sim._deliver_watchdog_event(st, _evt(restarts=1))
        # Marker set, item NOT yet queued (routing is deferred to the step).
        self.assertTrue(st['agents']['nora'].get('_mailAwake'))
        self.assertEqual(st['workQueue'], [])
        routed = sim._mail_action_step(st, 0, _NOW_MS)
        self.assertEqual(routed, 1)
        self.assertFalse(st['agents']['nora'].get('_mailAwake'))
        queued = [w for w in st['workQueue'] if w.get('_mailResume')]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['assignedTo'], 'nora')
        self.assertEqual(queued[0]['taskType'], 'research')

    def test_senior_director_with_a_team_is_still_nora(self):
        st = _state(teams=[
            {'id': 'nora', 'directorId': 'nora', 'members': ['worker']},
            {'id': 'ben', 'directorId': 'ben', 'members': []},
        ])
        sim._deliver_watchdog_event(st, _evt(restarts=1))
        self.assertEqual(len(st['agents']['nora'].get('mailbox') or []), 1)
        queued = [w for w in st['workQueue'] if w.get('_mailResume')]
        self.assertEqual(queued[0]['assignedTo'], 'nora')
        self.assertEqual(queued[0]['taskType'], 'research')

    @mock.patch.object(serve, 'log_action')
    def test_admin_mailed_and_logged_only_at_two_restarts(self, mock_log):
        st = _state()
        sim._deliver_watchdog_event(st, _evt(restarts=2))
        mailbox = st['agents']['theo']['mailbox']
        self.assertEqual(len(mailbox), 1)
        self.assertEqual(mailbox[0]['kind'], 'sim_watchdog_escalated')
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args[0][1], 'sim_watchdog_escalated')
        # The admin copy must never become a work item (isAdmin is excluded
        # from assignment -- an unassignable item would clog the queue).
        self.assertEqual(
            [w for w in st['workQueue'] if w.get('assignedTo') == 'theo'], [])

    @mock.patch.object(serve, 'log_action')
    def test_no_senior_director_still_logs_without_crashing(self, mock_log):
        st = _state()
        st['agentRoster'] = [
            {'id': 'theo', 'isAdmin': True, 'isDirector': True},
            {'id': 'worker', 'isAdmin': False, 'isDirector': False},
        ]
        sim._deliver_watchdog_event(st, _evt(restarts=2))
        self.assertEqual(st['workQueue'], [])
        mock_log.assert_called_once()


class MailActionStepUnroutable(unittest.TestCase):
    def test_unroutable_system_mail_clears_marker_without_queueing(self):
        st = _state()
        st['agents']['theo']['offDuty'] = True
        sim._deliver_mail(st, 'theo', 'sim_watchdog_escalated',
                          {'text': 'watchdog escalation'})
        self.assertTrue(st['agents']['theo'].get('_mailAwake'))
        routed = sim._mail_action_step(st, 0, _NOW_MS)
        self.assertEqual(routed, 0)
        self.assertNotIn('_mailAwake', st['agents']['theo'])
        self.assertEqual(st['workQueue'], [])


class SimLoopPassConsumesEvent(unittest.TestCase):
    def setUp(self):
        serve._set_dormant(False)
        sim._SIM_WATCHDOG_EVENT = None

    def tearDown(self):
        serve._set_dormant(False)
        sim._SIM_WATCHDOG_EVENT = None

    @mock.patch.object(serve, '_adversarial_village_tick')
    @mock.patch.object(serve, '_peer_review_tick')
    @mock.patch.object(serve, '_apply_pending_ask_results')
    @mock.patch.object(serve, 'save_state_to_db')
    @mock.patch.object(serve, 'get_state_from_db')
    @mock.patch.object(serve, 'expire_jev_grants')
    @mock.patch.object(serve, 'expire_sublow_grants')
    @mock.patch.object(serve, '_expire_handles')
    def test_event_consumed_once_and_delivered(self, _h, _s, _j, mock_get,
                                               mock_save, _a, _p, _v):
        st = _state()
        mock_get.return_value = st
        sim._SIM_WATCHDOG_EVENT = _evt(restarts=1)
        # Patch the CURRENT engine: an earlier test may have reloaded sim.
        with mock.patch.object(sim._engine, 'tick', return_value=st) as mock_tick:
            result = sim._sim_loop_pass()
        mock_tick.assert_called_once()
        self.assertIsNotNone(result)
        self.assertIsNone(sim._SIM_WATCHDOG_EVENT, 'clear-on-read: consumed')
        self.assertEqual(len(st['agents']['nora']['mailbox']), 1)
        self.assertEqual(len(st['workQueue']), 1)
        mock_save.assert_called_once()

    @mock.patch.object(serve, 'save_state_to_db')
    @mock.patch.object(sim._engine, 'tick')
    @mock.patch.object(serve, 'get_state_from_db')
    @mock.patch.object(serve, 'expire_jev_grants')
    @mock.patch.object(serve, 'expire_sublow_grants')
    @mock.patch.object(serve, '_expire_handles')
    def test_event_waits_while_dormant(self, _h, _s, _j, mock_get, mock_tick,
                                       mock_save):
        serve._set_dormant(True)
        evt = _evt(restarts=1)
        sim._SIM_WATCHDOG_EVENT = evt
        self.assertIsNone(sim._sim_loop_pass())
        self.assertIs(sim._SIM_WATCHDOG_EVENT, evt, 'stamped event survives')
        mock_tick.assert_not_called()
        mock_save.assert_not_called()


class WatchdogNoteFailure(unittest.TestCase):
    def setUp(self):
        self._saved = (serve._SIM_WATCHDOG_RESTARTS,
                       serve._SIM_WATCHDOG_PLAYER_ALERTED,
                       serve._SIM_WATCHDOG_LAST_EVENT_AT)
        serve._SIM_WATCHDOG_RESTARTS = 0
        serve._SIM_WATCHDOG_PLAYER_ALERTED = False
        serve._SIM_WATCHDOG_LAST_EVENT_AT = 0.0
        sim._SIM_WATCHDOG_EVENT = None

    def tearDown(self):
        (serve._SIM_WATCHDOG_RESTARTS, serve._SIM_WATCHDOG_PLAYER_ALERTED,
         serve._SIM_WATCHDOG_LAST_EVENT_AT) = self._saved
        sim._SIM_WATCHDOG_EVENT = None

    @mock.patch('integrations.notify.send_player_telegram_sync', return_value=True)
    def test_stamps_side_channel_counts_and_no_alert_yet(self, mock_tg):
        n = serve._sim_watchdog_note_failure('dead', 'boom', True)
        self.assertEqual(n, 1)
        evt = sim._SIM_WATCHDOG_EVENT
        self.assertEqual(evt['kind'], 'dead')
        self.assertEqual(evt['restarts'], 1)
        self.assertFalse(evt['player_alert'])
        mock_tg.assert_not_called()

    @mock.patch('integrations.notify.send_player_telegram_sync', return_value=True)
    def test_failed_restart_alerts_player_immediately(self, mock_tg):
        n = serve._sim_watchdog_note_failure('dead', 'boom', False)
        self.assertEqual(n, 1)
        evt = sim._SIM_WATCHDOG_EVENT
        self.assertEqual(evt['kind'], 'dead_restart_failed')
        self.assertTrue(evt['player_alert'])
        mock_tg.assert_called_once()
        self.assertIn('server logs', mock_tg.call_args[0][1])

    @mock.patch('integrations.notify.send_player_telegram_sync', return_value=True)
    def test_threshold_alerts_once_per_streak(self, mock_tg):
        for _ in range(serve._SIM_WATCHDOG_PLAYER_ALERT_AFTER - 1):
            serve._sim_watchdog_note_failure('stale', 'frozen', True)
        mock_tg.assert_not_called()
        serve._sim_watchdog_note_failure('stale', 'frozen', True)
        mock_tg.assert_called_once()
        serve._sim_watchdog_note_failure('stale', 'frozen', True)
        mock_tg.assert_called_once()  # latch: one alert per streak

    @mock.patch('integrations.notify.send_player_telegram_sync', return_value=True)
    def test_healthy_tick_resets_ladder_and_latch(self, mock_tg):
        for _ in range(serve._SIM_WATCHDOG_PLAYER_ALERT_AFTER):
            serve._sim_watchdog_note_failure('dead', 'boom', True)
        mock_tg.assert_called_once()
        serve._sim_watchdog_note_healthy(time.time() + 5)
        self.assertEqual(serve._SIM_WATCHDOG_RESTARTS, 0)
        self.assertFalse(serve._SIM_WATCHDOG_PLAYER_ALERTED)
        for _ in range(serve._SIM_WATCHDOG_PLAYER_ALERT_AFTER):
            serve._sim_watchdog_note_failure('dead', 'boom', True)
        self.assertEqual(mock_tg.call_count, 2, 'new streak may alert again')

    def test_stale_tick_does_not_reset(self):
        serve._sim_watchdog_note_failure('dead', 'boom', True)
        serve._sim_watchdog_note_healthy(time.time() - 60)
        self.assertEqual(serve._SIM_WATCHDOG_RESTARTS, 1)


if __name__ == '__main__':
    unittest.main()
