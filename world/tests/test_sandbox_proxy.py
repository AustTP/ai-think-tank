"""The Work Room sandbox's egress allowlist proxy (sandbox_proxy.py).

Runs as its own standalone Docker container/process -- these tests exercise
its pure logic (is_allowed, rate_limited) directly, without any real network
or Docker involved. `ALLOWED_HOSTS` is built once at import time from
SANDBOX_EGRESS_EXTRA_HOSTS, so tests that need to control it reload the
module under a patched environment rather than mutating the set in place.
"""
import importlib
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sandbox_proxy  # noqa: E402


def _reload_with_env(extra_hosts=''):
    with unittest.mock.patch.dict(os.environ, {'SANDBOX_EGRESS_EXTRA_HOSTS': extra_hosts}, clear=False):
        importlib.reload(sandbox_proxy)
    return sandbox_proxy


class AllowedHosts(unittest.TestCase):
    def tearDown(self):
        importlib.reload(sandbox_proxy)  # restore real-environment state for later tests

    def test_base_package_registry_hosts_are_allowed(self):
        mod = _reload_with_env('')
        self.assertTrue(mod.is_allowed('pypi.org'))
        self.assertTrue(mod.is_allowed('registry.npmjs.org'))
        self.assertTrue(mod.is_allowed('github.com'))

    def test_arbitrary_host_is_not_allowed_by_default(self):
        mod = _reload_with_env('')
        self.assertFalse(mod.is_allowed('evil.example.com'))
        self.assertFalse(mod.is_allowed('dreyx.com'))

    def test_extra_hosts_env_var_extends_the_allowlist(self):
        mod = _reload_with_env('dreyx.com,another-example.com')
        self.assertTrue(mod.is_allowed('dreyx.com'))
        self.assertTrue(mod.is_allowed('another-example.com'))
        # Base allowlist is still intact -- this is additive, not a replacement.
        self.assertTrue(mod.is_allowed('pypi.org'))

    def test_extra_hosts_are_case_insensitive(self):
        mod = _reload_with_env('DreyX.com')
        self.assertTrue(mod.is_allowed('dreyx.com'))
        self.assertTrue(mod.is_allowed('DREYX.COM'))

    def test_empty_or_whitespace_extra_hosts_add_nothing(self):
        mod = _reload_with_env(' , ,')
        self.assertFalse(mod.is_allowed(''))
        self.assertFalse(mod.is_allowed(' '))

    def test_is_allowed_handles_none_host(self):
        mod = _reload_with_env('')
        self.assertFalse(mod.is_allowed(None))


class RateLimit(unittest.TestCase):
    def setUp(self):
        importlib.reload(sandbox_proxy)  # fresh, empty _request_log per test

    def test_requests_under_the_limit_are_not_rate_limited(self):
        for _ in range(sandbox_proxy.RATE_LIMIT_MAX_REQUESTS):
            self.assertFalse(sandbox_proxy.rate_limited('pypi.org'))

    def test_the_request_that_crosses_the_limit_is_blocked(self):
        for _ in range(sandbox_proxy.RATE_LIMIT_MAX_REQUESTS):
            sandbox_proxy.rate_limited('pypi.org')
        self.assertTrue(sandbox_proxy.rate_limited('pypi.org'))

    def test_limit_is_tracked_independently_per_host(self):
        for _ in range(sandbox_proxy.RATE_LIMIT_MAX_REQUESTS):
            sandbox_proxy.rate_limited('pypi.org')
        self.assertTrue(sandbox_proxy.rate_limited('pypi.org'))
        # A different host has its own, untouched budget.
        self.assertFalse(sandbox_proxy.rate_limited('github.com'))

    def test_old_requests_fall_out_of_the_window(self):
        now = 1000.0
        with unittest.mock.patch.object(sandbox_proxy.time, 'time', return_value=now):
            for _ in range(sandbox_proxy.RATE_LIMIT_MAX_REQUESTS):
                sandbox_proxy.rate_limited('pypi.org')
            self.assertTrue(sandbox_proxy.rate_limited('pypi.org'))
        later = now + sandbox_proxy.RATE_LIMIT_WINDOW_S + 1
        with unittest.mock.patch.object(sandbox_proxy.time, 'time', return_value=later):
            self.assertFalse(sandbox_proxy.rate_limited('pypi.org'))


if __name__ == '__main__':
    unittest.main()
