import json
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import serve
import notify


class ReviewNotifications(unittest.TestCase):
    def test_review_failures_do_not_email_and_deduplicate_changed_text(self):
        records = {}
        with patch.object(serve,'_load_escalations',return_value=records), \
             patch.object(serve,'_save_escalations'), \
             patch.object(serve,'_send_escalation_email_sync') as send:
            first=notify.create_escalation('stuck review loop','failure',json.dumps({'taskId':'p'}),json.dumps({'fingerprint':'same'}))
            second=notify.create_escalation('stuck review loop','different wording',json.dumps({'taskId':'p'}),json.dumps({'fingerprint':'same'}))
            self.assertEqual(first,second)
            send.assert_not_called()
            self.assertEqual(records[first]['notificationChannel'],'board')

    def test_completed_alert_resolved_without_approving_or_sending(self):
        records={'a':{'kind':'stuck review loop','status':'pending','note':'{"taskId":"p"}'}}
        state={'tasks':{'p':{'status':'done'}}}
        with patch.object(serve,'_load_escalations',return_value=records),patch.object(serve,'_save_escalations'):
            notify.reconcile_review_escalations(state)
        self.assertEqual(records['a']['status'],'resolved')
        self.assertEqual(state['tasks']['p'],{'status':'done'})
