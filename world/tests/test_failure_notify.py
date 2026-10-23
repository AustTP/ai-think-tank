import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selfedit  # noqa: E402
import serve  # noqa: E402


class FailureSpecTest(unittest.TestCase):
    def test_operational_failures_are_classified(self):
        self.assertEqual(serve._failure_spec(
            'content_run_abandoned', {})[1], 'high')
        self.assertEqual(serve._failure_spec(
            'self_edit_review_unavailable', {})[1], 'high')
        self.assertEqual(serve._failure_spec(
            'api_key_auth_failure', {})[1], 'critical')
        self.assertEqual(serve._failure_spec(
            'quality_gate_reject', {})[1], 'low')
        self.assertIsNone(serve._failure_spec('task_assigned', {}))

    def test_browse_failure_decision_is_classified(self):
        self.assertIsNotNone(serve._failure_spec(
            'browse', {'decision': 'allowed_but_fetch_failed'}))


class FailureMessageTest(unittest.TestCase):
    def test_message_explains_and_redacts(self):
        subject, body = serve._failure_notice_message(
            'leo', 'content_run_abandoned',
            {'taskId': 'task-9', 'reason': 'heartbeat stopped',
             'error': 'sk-test-abcdefghijklmnopqrstuvwxyz'},
            'A content run was abandoned', 'high')
        self.assertIn('Failure: A content run was abandoned', subject)
        self.assertIn('task-9', body)
        self.assertIn('heartbeat stopped', body)
        self.assertNotIn('sk-test-abcdefghijklmnopqrstuvwxyz', body)

    def test_dedup_cools_down_identical_failures(self):
        sent = []

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        serve._FAILURE_NOTICE_STATE.clear()
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time', return_value=1000.0), \
             mock.patch.object(serve.threading, 'Thread', FakeThread):
            serve._maybe_notify_failure('leo', 'content_run_abandoned',
                                        {'taskId': 'task-9', 'reason': 'x'})
            serve._maybe_notify_failure('leo', 'content_run_abandoned',
                                        {'taskId': 'task-9', 'reason': 'x'})
        self.assertEqual(len(sent), 1)
        self.assertEqual(serve._FAILURE_NOTICE_STATE[
            'content_run_abandoned|taskId|task-9']['suppressed'], 1)


class ChangeRequestOutcomeTest(unittest.TestCase):
    def test_end_message_names_the_outcome(self):
        sent = []
        with mock.patch.object(selfedit, '_window_row',
                               return_value={'validator': 'ben'}), \
             mock.patch.object(selfedit, '_notify_player_telegram',
                               side_effect=lambda s, b: sent.append((s, b))):
            selfedit._telegram_change_request(
                'win-1', 'successful_with_issues', 'one file had a warning',
                sha='abc123', n_edits=1)
        subject, body = sent[0]
        self.assertIn('Change request ended: SUCCESSFUL WITH ISSUES', subject)
        self.assertIn('Outcome: SUCCESSFUL WITH ISSUES', body)
        self.assertIn('Explanation: one file had a warning', body)
