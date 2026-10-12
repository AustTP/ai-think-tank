import datetime
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import content
import review_grounding
import serve
import sim


class GroundedReview(unittest.TestCase):
    def test_saved_report_and_actual_dataset_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b'timestamp,close\n2026-10-09,100\n'
            (root / 'data.csv').write_bytes(data)
            (root / 'data.json').write_text(json.dumps({'sha256': hashlib.sha256(data).hexdigest()}))
            (root / 'report.md').write_text('Actual collected dataset: data.csv')
            packet = review_grounding.packet({'libraryPath': 'report.md',
                'instructions': 'Collect historical dataset', 'acceptanceCriteria': 'Save actual CSV'},
                'x = 1', {'ok': True}, lambda path: str(root / path))
            self.assertIn('Actual collected dataset', packet['text'])
            self.assertIn('"hashMatches": true', packet['text'])
            self.assertIn('2026-10-09', packet['text'])
            self.assertEqual(packet['currentUtcDate'], datetime.datetime.now(datetime.timezone.utc).date().isoformat())

    def test_unknown_evidence_reference_cannot_create_repairs(self):
        text = json.dumps({'verdict': 'send_back', 'repairStories': [{'title': 'fix', 'criterionId': 'C2',
            'evidenceId': 'E999', 'instructions': 'fix', 'acceptanceCriteria': 'works'}]})
        self.assertEqual(content._document_review_repairs(text, 'x = 1', 'Works',
            {'criteria': {'C2': 'Works'}, 'evidence': {'E1': 'x = 1'}}), [])

    def test_valid_references_keep_exact_requirement_and_quote(self):
        text = json.dumps({'verdict': 'send_back', 'repairStories': [{'title': 'fix', 'criterionId': 'C2',
            'evidenceId': 'E1', 'instructions': 'fix x', 'acceptanceCriteria': 'x = 2'}]})
        repairs = content._document_review_repairs(text, 'x = 1', 'Works',
            {'criteria': {'C2': 'Works'}, 'evidence': {'E1': 'x = 1'}})
        self.assertEqual(repairs[0]['criterion'], 'Works')
        self.assertEqual(repairs[0]['evidenceQuote'], 'x = 1')

    def test_literal_fields_cannot_bypass_unknown_reference_ids(self):
        text = json.dumps({'verdict': 'send_back', 'repairStories': [{'title': 'fix', 'criterionId': 'C999',
            'evidenceId': 'E999', 'criterion': 'Works', 'evidenceQuote': 'x = 1',
            'instructions': 'fix', 'acceptanceCriteria': 'works'}]})
        self.assertEqual(content._document_review_repairs(text, 'x = 1', 'Works',
            {'criteria': {'C2': 'Works'}, 'evidence': {'E1': 'x = 1'}}), [])

    def test_unanchored_code_rejection_does_not_queue_a_rewrite(self):
        parent = {'id': 'p', 'room': 'pressoffice', 'instructions': 'Implement code',
                  'acceptanceCriteria': 'Correct calculation'}
        review = {'id': 'r', 'reviewOf': 'p', 'taskType': 'review'}
        response = json.dumps({'verdict': 'send_back', 'summary': 'Something is wrong',
                               'repairStories': []})
        with patch('code_workspaces.prepare', return_value='test-workspace'), \
             patch.object(serve, 'get_or_create_agent_key', return_value='test'), \
             patch.object(serve, '_coding_tier_slug', return_value='test-model'), \
             patch.object(content, '_gather_unified_context', return_value='x = 1'), \
             patch.object(content, '_run_quality_pipeline', return_value={'ok': True, 'note': 'passed', 'results': []}), \
             patch.object(content, '_review_screenshot', return_value={'ok': False, 'note': 'No page'}), \
             patch.object(serve, '_http_json', return_value={'reply': response}) as http, \
             patch.object(sim, '_store_content_result') as store, \
             patch.object(serve, '_call_openrouter_decision_sync') as judge:
            content._run_review_content({'tasks': {'p': parent}}, 'reviewer', review)
            result = store.call_args.args[1]
            self.assertTrue(result['reviewFormatError'])
            self.assertIsNone(result['peerVerdict'])
            self.assertNotIn('queueFix', result)
            judge.assert_not_called()
            prompt = next(c.args[3]['messages'][0]['content'] for c in http.call_args_list if c.args[2] == '/api/chat')
            self.assertIn('Current UTC date:', prompt)
            self.assertIn('Actual quality pipeline:', prompt)
            self.assertIn('criterionId', prompt)
