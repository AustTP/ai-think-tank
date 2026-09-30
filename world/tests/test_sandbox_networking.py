"""ensure_sandbox_networking() -- the Work Room sandbox's Docker network/proxy
setup. Player-vetted data sites: BROWSE_ALLOWLIST_DOMAINS is now
passed into the egress proxy container (SANDBOX_EGRESS_EXTRA_HOSTS) so agent-
run scripts can do real systematic crawling against them, not just one-URL-
at-a-time browse_page calls. These tests cover the create/recreate-on-drift
decision; sandbox_proxy.py's own allowlist/rate-limit logic is covered by
test_sandbox_proxy.py.

Hermetic: subprocess.run is mocked throughout -- no real Docker is ever
touched.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class EnsureSandboxNetworking(unittest.TestCase):
    def _run(self, network_exists=True, container_running=False, current_extra_hosts=None,
             allowlist=frozenset()):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = unittest.mock.Mock()
            result.returncode = 0
            result.stdout = ''
            return result

        # Reflects real Docker's actual behavior: running unless/until an
        # `rm -f` for this container has actually been issued.
        def fake_running(name):
            if any(c[:3] == ['docker', 'rm', '-f'] and c[-1] == name for c in calls):
                return False
            return container_running

        with unittest.mock.patch.object(serve, 'BROWSE_ALLOWLIST_DOMAINS', allowlist), \
             unittest.mock.patch.object(serve, '_docker_network_exists', return_value=network_exists), \
             unittest.mock.patch.object(serve, '_docker_container_running', side_effect=fake_running), \
             unittest.mock.patch.object(serve, '_docker_container_env_value', return_value=current_extra_hosts), \
             unittest.mock.patch.object(serve.subprocess, 'run', side_effect=fake_run):
            serve.ensure_sandbox_networking()
        return calls

    def test_creates_proxy_with_the_allowlist_as_an_env_var(self):
        calls = self._run(container_running=False, allowlist=frozenset({'dreyx.com'}))
        run_cmds = [c for c in calls if c[:2] == ['docker', 'run']]
        self.assertEqual(len(run_cmds), 1)
        self.assertIn('-e', run_cmds[0])
        idx = run_cmds[0].index('-e')
        self.assertEqual(run_cmds[0][idx + 1], 'SANDBOX_EGRESS_EXTRA_HOSTS=dreyx.com')

    def test_multiple_domains_are_sorted_and_comma_joined(self):
        calls = self._run(container_running=False, allowlist=frozenset({'zebra.com', 'dreyx.com'}))
        run_cmds = [c for c in calls if c[:2] == ['docker', 'run']]
        idx = run_cmds[0].index('-e')
        self.assertEqual(run_cmds[0][idx + 1], 'SANDBOX_EGRESS_EXTRA_HOSTS=dreyx.com,zebra.com')

    def test_running_proxy_with_matching_allowlist_is_left_alone(self):
        calls = self._run(container_running=True, current_extra_hosts='dreyx.com',
                          allowlist=frozenset({'dreyx.com'}))
        self.assertFalse(any(c[:2] == ['docker', 'run'] for c in calls))
        self.assertFalse(any('rm' in c for c in calls))

    def test_running_proxy_with_stale_allowlist_is_recreated(self):
        # Real scenario this protects: BROWSE_ALLOWLIST_DOMAINS changed (a
        # new site vetted) since the proxy container was last created -- it
        # must not keep silently running with the OLD allowlist forever.
        calls = self._run(container_running=True, current_extra_hosts='old-site.com',
                          allowlist=frozenset({'dreyx.com'}))
        self.assertTrue(any(c[:3] == ['docker', 'rm', '-f'] for c in calls))
        run_cmds = [c for c in calls if c[:2] == ['docker', 'run']]
        self.assertEqual(len(run_cmds), 1)
        idx = run_cmds[0].index('-e')
        self.assertEqual(run_cmds[0][idx + 1], 'SANDBOX_EGRESS_EXTRA_HOSTS=dreyx.com')

    def test_empty_allowlist_still_creates_a_working_proxy(self):
        calls = self._run(container_running=False, allowlist=frozenset())
        run_cmds = [c for c in calls if c[:2] == ['docker', 'run']]
        idx = run_cmds[0].index('-e')
        self.assertEqual(run_cmds[0][idx + 1], 'SANDBOX_EGRESS_EXTRA_HOSTS=')


if __name__ == '__main__':
    unittest.main()
