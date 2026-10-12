import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import serve
import content


class PipelineDeadlines(unittest.TestCase):
    def test_deadline_covers_all_steps(self):
        results = [{'name': step['name'], 'exitCode': 0, 'stdout': 'passed'}
                   for step in content._quality_pipeline_steps()]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(serve, 'SANDBOXES_DIR', directory), \
             patch.object(serve, '_http_json', return_value={'results': results}) as http:
            outcome = content._run_quality_pipeline_locked('http://local', 'key', 'agent', 'test', 'validation')
        self.assertTrue(outcome['ok'])
        self.assertGreater(http.call_args.kwargs['timeout'], len(results) * serve.SANDBOX_TIMEOUT_S)

    def test_partial_response_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(serve, 'SANDBOXES_DIR', directory), \
             patch.object(serve, '_http_json', return_value={'results': [{'name': 'flake8', 'exitCode': 0}]}):
            outcome = content._run_quality_pipeline_locked('http://local', 'key', 'agent', 'test', 'validation')
        self.assertFalse(outcome['ok'])
        self.assertIn('incomplete', outcome['note'])
