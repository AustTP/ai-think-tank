"""The hourly village scan's tool realism (2026-11-24).

The scan template once directed api_call against internal loopback
endpoints (/api/ops/*) that no agent transport can reach: api_call is
SSRF-gated against private hosts and loopback browsing is rejected
('not_discovered'). Every scan burned 2 tool_policy_denied refusals plus
MEDIUM/BROWSE telegram alerts before finishing via library tools alone
(task-35908..35913). Rules: the instructions may only direct tools the
report executor actually has (search_library/read_library_file/
project_board/read_pulse), and must not command api_call."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import sim  # noqa: E402  (sim wires the world/village path)

SCAN = sim._SCHEDULED_VILLAGE_SCAN_ITEM


class ScanInstructionsUseRealTools(unittest.TestCase):
    def test_seed_item_has_no_api_call_grant(self):
        self.assertNotIn('api_call', SCAN.get('approvedTools') or [])

    def test_instructions_do_not_command_api_call(self):
        text = (SCAN.get('instructions') or '').lower()
        # The tool may be MENTIONED (the anti-retry note); it may not be
        # COMMANDED. No call imperative, no endpoint path.
        for bad in ('api_call get', 'api_call post',
                    'call api_call', '/api/ops/', '/api/intent/'):
            self.assertNotIn(bad, text)

    def test_instructions_name_native_tools(self):
        text = SCAN.get('instructions') or ''
        for tool in ('search_library', 'read_library_file', 'project_board',
                     'read_pulse'):
            self.assertIn(tool, text)

    def test_scan_is_report_task(self):
        self.assertEqual(SCAN.get('deliverable'), 'report')
        self.assertEqual(SCAN.get('ownerRole'), 'regional-vp')


if __name__ == '__main__':
    unittest.main()
