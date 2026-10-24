import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402


class TelegramChunkTest(unittest.TestCase):
    def test_long_body_is_split_without_loss(self):
        text = ('line\n' * 2500).strip()
        chunks = notify._telegram_chunks(text, limit=4000)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))


class TelegramSuppressionTest(unittest.TestCase):
    def test_flag_off_suppresses_proactive_push(self):
        with mock.patch.object(serve, 'TELEGRAM_NOTIFICATIONS_ENABLED', False), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'token'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1'}), \
             mock.patch.object(serve, '_telegram_api_sync') as api:
            self.assertFalse(notify.send_player_telegram_sync('subject', 'body'))
        api.assert_not_called()

    def test_flag_off_suppresses_captcha_text(self):
        with mock.patch.object(serve, 'TELEGRAM_NOTIFICATIONS_ENABLED', False), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'token'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1'}):
            self.assertEqual(serve._telegram_send_text_sync('challenge'), 0)

    def test_test_process_suppresses_even_when_global_flag_is_on(self):
        with mock.patch.object(serve, 'TELEGRAM_NOTIFICATIONS_ENABLED', True), \
             mock.patch.object(serve, 'TELEGRAM_SUPPRESS_DURING_TESTS', True), \
             mock.patch.object(serve, '_in_test_process', return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'token'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1'}), \
             mock.patch.object(serve, '_telegram_api_sync') as api:
            self.assertFalse(notify.send_player_telegram_sync('subject', 'body'))
        api.assert_not_called()

    def test_test_suppression_can_be_disabled(self):
        with mock.patch.object(serve, 'TELEGRAM_NOTIFICATIONS_ENABLED', True), \
             mock.patch.object(serve, 'TELEGRAM_SUPPRESS_DURING_TESTS', False), \
             mock.patch.object(serve, '_in_test_process', return_value=True), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', 'token'), \
             mock.patch.object(serve, 'TELEGRAM_ALLOWED_CHAT_IDS', {'1'}), \
             mock.patch.object(serve, '_telegram_api_sync',
                               return_value={'ok': True}) as api:
            self.assertTrue(notify.send_player_telegram_sync('subject', 'body'))
        api.assert_called_once()


class SpikeNotificationTest(unittest.TestCase):
    def test_successful_spike_is_silent_but_failure_is_not(self):
        state = {}
        sim._queue_player_email(
            state, 'spike_done', '[AI Think Tank] Spike done: X', 'done')
        self.assertFalse(state.get('emailOutbox'))
        sim._queue_player_email(
            state, 'spike_done', '[AI Think Tank] Spike stalled: X', 'stalled')
        self.assertEqual(len(state['emailOutbox']), 1)


class CardFailureNotificationTest(unittest.TestCase):
    def test_card_failure_is_telegram_only_and_deduped(self):
        state = {}
        task = {'id': 'task-1', 'title': 'Fix the report',
                'room': 'pressoffice', 'taskType': 'story',
                'issueKey': 'NORA-1', 'assignedTo': 'leo'}
        sim._notify_card_failure(
            state, task, 'the pipeline did not pass', now_ms=1_000_000_000)
        self.assertEqual(len(state['emailOutbox']), 1)
        entry = state['emailOutbox'][0]
        self.assertEqual(entry['channels'], ['telegram'])
        self.assertIn('Card failed: Fix the report', entry['subject'])
        self.assertIn('the pipeline did not pass', entry['body'])
        self.assertIn('Issue: NORA-1', entry['body'])
        sim._notify_card_failure(
            state, task, 'the pipeline did not pass', now_ms=1_000_000_100)
        self.assertEqual(len(state['emailOutbox']), 1,
                         'the same card is deduped for 30 minutes')

    def test_run_abandoned_does_not_claim_queue_removal(self):
        state = {}
        task = {'id': 'task-3', 'title': 'Act on mail', 'room': 'pressoffice',
                'taskType': 'code'}
        sim._notify_card_failure(
            state, task, 'the content run stopped heartbeating', category='run-abandoned',
            now_ms=1_000_000_000)
        body = state['emailOutbox'][0]['body']
        self.assertIn('stopped reporting liveness', body)
        self.assertNotIn('removed from the queue', body)

    def test_spike_specific_notice_wins_over_generic_card_failure(self):
        state = {}
        task = {'id': 'task-2', 'title': 'Spike X', 'room': 'observatory',
                'taskType': 'spike', '_notifyQueued': 'spike_done'}
        sim._notify_card_failure(state, task, 'came up empty', now_ms=1_000_000_000)
        self.assertFalse(state.get('emailOutbox'))


class AdminEscalationTest(unittest.TestCase):
    def test_admin_resolves_after_director_gives_up(self):
        state = {'agentRoster': [
            {'id': 'theo', 'name': 'Theo', 'isAdmin': True},
            {'id': 'dir', 'name': 'Dir', 'isDirector': True},
        ]}
        esc = {'kind': 'allowlist request', 'question': 'q',
               'status': 'pending', 'askCount': 3, 'note': 'example.com'}
        with mock.patch.object(serve, '_admin_agent_id', return_value='theo'), \
             mock.patch.object(serve, '_jev_quorum_choice_sync',
                               return_value=('approve', 0.9, 0.0)), \
             mock.patch.object(serve, '_escalation_floor', return_value=0.5), \
             mock.patch.object(serve, '_jev_directory_score', return_value=0.9), \
             mock.patch.object(serve, '_effective_safety_confidence', return_value=0.6), \
             mock.patch.object(serve, '_jev_judge_model', return_value=None), \
             mock.patch.object(serve, '_grant_allowlist') as grant, \
             mock.patch.object(serve, 'log_action'):
            self.assertTrue(serve._escalation_admin_attempt(
                state, 'esc-1', esc, 'allowlist request', 'dir',
                {d['id']: d for d in state['agentRoster']}))
        self.assertEqual(esc['status'], 'approved')
        self.assertIn('Theo', esc['resolvedBy'])
        grant.assert_called_once_with(
            'example.com', actor='theo', via='admin escalation')
