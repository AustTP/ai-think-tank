import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve


class NotificationDelivery(unittest.TestCase):
    def test_escalation_records_actual_email_outcome(self):
        for sent in (False, True):
            records = {}
            def save(data):
                records.clear()
                records.update(copy.deepcopy(data))
            with self.subTest(sent=sent), \
                    patch.object(serve, '_load_escalations', side_effect=lambda: copy.deepcopy(records)), \
                    patch.object(serve, '_save_escalations', side_effect=save), \
                    patch.object(serve, '_send_escalation_email_sync', return_value=sent):
                eid = serve.create_escalation('allowlist request', 'Approve example.com?', on_approve_note='example.com')
                self.assertEqual(records[eid]['emailSent'], sent)
                self.assertEqual(records[eid]['status'], 'pending')

    def test_player_notifications_can_use_configured_smtp(self):
        with patch.object(serve, '_credential_token', return_value=None), \
                patch.multiple(serve, PLAYER_EMAIL_ENABLED=True, SMTP_HOST='smtp.example.com',
                               SMTP_USER='player@example.com', SMTP_PASSWORD='test-password',
                               ESCALATION_EMAIL_TO='player@example.com'), \
                patch.object(serve, '_send_escalation_email_sync', return_value=True) as send:
            self.assertTrue(serve._send_player_email_sync('Test', 'Body'))
            send.assert_called_once_with('Test', 'Body')

    def test_escalations_can_use_vault_notification_channel(self):
        with patch.object(serve, 'SMTP_PASSWORD', None), \
                patch.object(serve, '_send_player_email_sync', return_value=False) as send:
            self.assertFalse(serve._send_escalation_email_sync('Approval', 'Body'))
            send.assert_called_once_with('Approval', 'Body')
