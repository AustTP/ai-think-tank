"""Tests for the deterministic agent-readiness surface scan."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content


class AgentReadiness(unittest.TestCase):
    def test_full_page_scores_all_checks(self):
        page = ('Price $10. Shipping and returns. Bot and agent policy. '
                'API and MCP feed. Approval before payment. JSON-LD schema.org SKU.')
        out = json.loads(content.audit_agent_readiness(lambda n, a: page, {'url': 'https://example.com'}))
        self.assertTrue(out['ok'])
        self.assertEqual(out['score'], out['maxScore'])
        self.assertEqual(out['missing'], [])
        self.assertEqual(len(out['questions']), 10)

    def test_missing_signals_have_next_actions(self):
        out = json.loads(content.audit_agent_readiness(
            lambda n, a: 'Welcome to our site. Contact us.', {'url': 'https://example.com'}))
        self.assertTrue(out['ok'])
        self.assertEqual(out['score'], 0)
        self.assertTrue(out['missing'])
        self.assertTrue(out['nextActions'])

    def test_requires_url_and_reports_fetch_failure(self):
        self.assertFalse(json.loads(content.audit_agent_readiness(lambda n, a: 'x', {}))['ok'])
        out = json.loads(content.audit_agent_readiness(
            lambda n, a: 'Could not fetch the page', {'url': 'https://example.com'}))
        self.assertFalse(out['ok'])


if __name__ == '__main__':
    unittest.main()
