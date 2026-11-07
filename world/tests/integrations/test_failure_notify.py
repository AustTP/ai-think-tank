import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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


class LowSeverityBatchingTest(unittest.TestCase):
    """2026-11-07: low-severity failure notices keyed per task/agent pushed
    183 Telegram messages in six hours. Low severity now shares one window
    per label; medium and above keep per-key cooldowns."""

    @staticmethod
    def _fake_thread(sent):
        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)
        return FakeThread

    def test_first_low_failure_sends_once_and_rest_accumulate(self):
        sent = []
        serve._FAILURE_NOTICE_STATE.clear()
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time', return_value=1000.0), \
             mock.patch.object(serve.threading, 'Thread',
                               self._fake_thread(sent)):
            for i in range(4):
                serve._maybe_notify_failure(
                    f'agent{i}', 'quality_gate_reject', {'taskId': f'task-{i}'})
        self.assertEqual(len(sent), 1)
        bucket = serve._FAILURE_NOTICE_STATE['__batched__|low|A review or gate rejected work']
        self.assertEqual(bucket['suppressed'], 3)
        self.assertEqual(len(bucket['keys']), 3)

    def test_low_digest_after_window_names_sources(self):
        sent = []
        serve._FAILURE_NOTICE_STATE.clear()
        clock = {'t': 1000.0}
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time',
                               side_effect=lambda: clock['t']), \
             mock.patch.object(serve.threading, 'Thread',
                               self._fake_thread(sent)):
            for i in range(3):
                serve._maybe_notify_failure(
                    f'agent{i}', 'quality_gate_reject', {'taskId': f'task-{i}'})
            self.assertEqual(len(sent), 1)
            clock['t'] = 5000.0  # the 3600s low window has expired
            serve._maybe_notify_failure(
                'agent9', 'quality_gate_reject', {'taskId': 'task-9'})
        self.assertEqual(len(sent), 2)
        subject, body = sent[1]
        self.assertIn('A review or gate rejected work', subject)
        self.assertIn('2 more low-severity', body)
        self.assertIn('task-2', body)
        self.assertIn('task-1', body)

    def test_medium_failures_batch_per_label_not_per_key(self):
        # Medium used to keep per-key cooldowns, but distinct keys per
        # agent/task defeated them (measured 2026-11-07: ~30 pushes/hour).
        # Medium now batches per label like low; high stays per-event.
        sent = []
        serve._FAILURE_NOTICE_STATE.clear()
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time', return_value=1000.0), \
             mock.patch.object(serve.threading, 'Thread',
                               self._fake_thread(sent)):
            serve._maybe_notify_failure('a', 'browse',
                                        {'decision': 'denied', 'taskId': 't1'})
            serve._maybe_notify_failure('b', 'browse',
                                        {'decision': 'denied', 'taskId': 't2'})
        self.assertEqual(len(sent), 1)
        bucket = serve._FAILURE_NOTICE_STATE[
            '__batched__|medium|An operation failed or was blocked']
        self.assertEqual(bucket['suppressed'], 1)
    def test_medium_digest_after_window_reports_total_in_body(self):
        sent = []
        serve._FAILURE_NOTICE_STATE.clear()
        clock = {'t': 1000.0}
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time',
                               side_effect=lambda: clock['t']), \
             mock.patch.object(serve.threading, 'Thread',
                               self._fake_thread(sent)):
            for i in range(3):
                serve._maybe_notify_failure(
                    f'agent{i}', 'tool_use_refused', {'taskId': f'task-{i}'})
            self.assertEqual(len(sent), 1)
            clock['t'] += 901
            serve._maybe_notify_failure(
                'agent9', 'tool_use_refused', {'taskId': 'task-9'})
            self.assertEqual(len(sent), 2)
            # The digest count lives in the body, not the subject.
            self.assertIn('2 more', sent[1][1])

    def test_high_failures_still_send_per_event(self):
        sent = []
        serve._FAILURE_NOTICE_STATE.clear()
        with mock.patch.object(serve, 'send_player_telegram_sync',
                               side_effect=lambda s, b: sent.append((s, b))), \
             mock.patch.object(serve.time, 'time', return_value=1000.0), \
             mock.patch.object(serve.threading, 'Thread',
                               self._fake_thread(sent)):
            for i in range(3):
                serve._maybe_notify_failure(
                    f'agent{i}', 'content_run_abandoned',
                    {'taskId': f'task-{i}'})
            self.assertEqual(len(sent), 3)
