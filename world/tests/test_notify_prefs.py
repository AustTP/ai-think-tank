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


class SpikeNotificationTest(unittest.TestCase):
    def test_successful_spike_is_silent_but_failure_is_not(self):
        state = {}
        sim._queue_player_email(
            state, 'spike_done', '[AI Think Tank] Spike done: X', 'done')
        self.assertFalse(state.get('emailOutbox'))
        sim._queue_player_email(
            state, 'spike_done', '[AI Think Tank] Spike stalled: X', 'stalled')
        self.assertEqual(len(state['emailOutbox']), 1)


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
