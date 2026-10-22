"""Host-run cron scripts are operator-owned and allowlisted. A standing task
must not be able to name an arbitrary script under scripts/."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron_scripts  # noqa: E402


class CronAllowlist(unittest.TestCase):
    def test_allowlisted_script_resolves(self):
        path, error = cron_scripts.resolve_script('scripts/check_models.py')
        self.assertIsNone(error)
        self.assertTrue(path and path.endswith('scripts/check_models.py'))

    def test_non_allowlisted_script_is_refused(self):
        with mock.patch.object(cron_scripts, '_allowed_scripts',
                               return_value={'scripts/check_models.py'}):
            path, error = cron_scripts.resolve_script('scripts/browser_record.py')
        self.assertIsNone(path)
        self.assertIn('allowlist', error)

    def test_run_refuses_non_allowlisted_without_executing(self):
        with mock.patch.object(cron_scripts, '_allowed_scripts',
                               return_value={'scripts/check_models.py'}):
            result = cron_scripts.run({'script': 'scripts/browser_record.py'})
        self.assertFalse(result['ok'])
        self.assertIn('allowlist', result['error'])

    def test_missing_allowlist_fails_closed(self):
        with mock.patch.object(cron_scripts, '_allowed_scripts',
                               return_value=None):
            path, error = cron_scripts.resolve_script('scripts/check_models.py')
        self.assertIsNone(path)
        self.assertIn('allowlist', error)


if __name__ == '__main__':
    unittest.main()
