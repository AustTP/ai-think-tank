"""Host-run cron scripts are operator-owned and allowlisted. A standing task
must not be able to name an arbitrary script under scripts/."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron_scripts  # noqa: E402


def test_allowlisted_script_resolves():
    path, error = cron_scripts.resolve_script('scripts/check_models.py')
    assert error is None
    assert path and path.endswith('scripts/check_models.py')


def test_non_allowlisted_script_is_refused():
    with mock.patch.object(cron_scripts, '_allowed_scripts',
                           return_value={'scripts/check_models.py'}):
        path, error = cron_scripts.resolve_script('scripts/browser_record.py')
    assert path is None
    assert 'allowlist' in error


def test_run_refuses_non_allowlisted_without_executing():
    with mock.patch.object(cron_scripts, '_allowed_scripts',
                           return_value={'scripts/check_models.py'}):
        result = cron_scripts.run({'script': 'scripts/browser_record.py'})
    assert result['ok'] is False
    assert 'allowlist' in result['error']


def test_missing_allowlist_fails_closed():
    with mock.patch.object(cron_scripts, '_allowed_scripts', return_value=None):
        path, error = cron_scripts.resolve_script('scripts/check_models.py')
    assert path is None
    assert 'allowlist' in error
