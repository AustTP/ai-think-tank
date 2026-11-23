# Tool-policy read-only-set tests (operator, 2026-11-23): the price
# question is the anti-waste tool -- a worker denied it could not check
# the cost before spending. service_prices must be replayable/read-only.
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.tool_policy import (task_tool_policy, tool_replay_safety,
                              _READ_ONLY_TOOLS)  # noqa: E402


class ServicePricesIsReadOnly(unittest.TestCase):
    def test_service_prices_in_read_only_set(self):
        self.assertIn('service_prices', _READ_ONLY_TOOLS)

    def test_service_prices_is_replayable(self):
        self.assertEqual(tool_replay_safety('service_prices'), 'safe')


if __name__ == '__main__':
    unittest.main()
