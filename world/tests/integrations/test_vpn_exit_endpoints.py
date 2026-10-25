"""Operator-gated VPN exit pool endpoints (ask 13).

vpn_pool.py is pure and already tested on its own. These tests
cover the WIRING in serve: the operator-gated
POST /api/vpn-exits/start and /api/vpn-exits/stop endpoints
(starting a container burns one of the account's 5 Mullvad
devices and runs docker --privileged, so agents never start
exits -- an operator does, then agents USE the exit through the
ordinary viaVpnExit path in /api/browse), the merged registry a
started exit immediately appears in, and the browse fetch routed
through a pool exit's own proxy.

DB-isolated (setUpModule below, the test_browse_allowlist
pattern) since /api/browse's every path calls log_action, a real
DB write. The pool is a scripted fake: no docker, no Mullvad
account, no network.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-vpn-exits-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # BROWSE_TRAIL_PATH is a module-level constant computed once
        # from the REAL THINK_TANK_DIR at import time -- patching
        # THINK_TANK_DIR alone does not recompute it.
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        # The CLI-run Laya standby appends a provider to every
        # decision chain when enabled; pin it off so decision chains
        # stay single-slug in the test process.
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class FakeExit:
    """One running pool exit: the attributes
    vpn_location.describe_exit reads off a registry record."""

    def __init__(self, name, relay):
        self.name = name
        self.relay = relay
        self.proxy = 'http://127.0.0.1:18803'
        self.proxy_port = 18803


class FakePool:
    """A scripted stand-in for serve.VPN_EXIT_POOL: records
    start/stop calls and returns canned results, so no docker or
    Mullvad account is ever touched."""

    def __init__(self, exits=None, start_result=None, stop_result=None):
        self._exits = dict(exits or {})
        self.started = []
        self.stopped = []
        self._start_result = start_result
        self._stop_result = stop_result

    def start(self, relay, name=None):
        self.started.append((relay, name))
        if self._start_result is not None:
            return self._start_result
        exit_name = name or f'exit-{len(self._exits)}'
        record = FakeExit(exit_name, relay)
        self._exits[exit_name] = record
        return record, None

    def stop(self, name):
        self.stopped.append(name)
        if self._stop_result is not None:
            return self._stop_result
        return self._exits.pop(name, None) is not None

    def names(self):
        return list(self._exits)

    def exits(self):
        return {name: {'proxy': record.proxy,
                       'relay': record.relay,
                       'proxyPort': record.proxy_port,
                       'useFor': (f'on-demand exit through Mullvad '
                                  f'relay {record.relay}')}
                for name, record in self._exits.items()}


def _client():
    from starlette.testclient import TestClient
    # /api/vpn-exits/* and /api/browse are in AUTH_PROTECTED_PREFIXES
    # -- needs a real session (mirrors every other protected-endpoint
    # test's _client pattern).
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


class VpnExitStartEndpoint(unittest.TestCase):
    def _start(self, body, operator=(True, 'player'), pool=None):
        pool = pool if pool is not None else FakePool()
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator), \
             unittest.mock.patch.object(serve, 'VPN_EXIT_POOL', pool):
            response = _client().post('/api/vpn-exits/start', json=body)
        return response, pool

    def test_start_requires_the_operator_gate(self):
        response, _ = self._start({'relay': 'se-got-001'},
                                  operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_start_requires_a_json_object_body(self):
        response, _ = self._start('not-an-object')
        self.assertEqual(response.status_code, 400)

    def test_start_requires_a_relay(self):
        for body in ({}, {'relay': '   '}):
            response, _ = self._start(body)
            self.assertEqual(response.status_code, 400)

    def test_start_surfaces_pool_refusals_as_409(self):
        # Pool refusals (cap reached, duplicate/bad name) are
        # caller-fixable, so they are a 409, not the host's 502.
        pool = FakePool(start_result=(None,
                                      'device cap reached (5 Mullvad '
                                      'devices per account)'))
        response, _ = self._start({'relay': 'se-got-001'}, pool=pool)
        self.assertEqual(response.status_code, 409)
        self.assertIn('device cap', response.json()['error'])

    def test_start_surfaces_docker_failures_as_502(self):
        pool = FakePool(start_result=(None,
                                      'docker run failed for exit "x"'))
        response, _ = self._start({'relay': 'se-got-001'}, pool=pool)
        self.assertEqual(response.status_code, 502)
        self.assertIn('docker run failed', response.json()['error'])

    def test_start_starts_the_pool_and_describes_the_exit(self):
        response, pool = self._start({'relay': 'se-got-001',
                                      'name': 'se-got-001'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['actor'], 'player')
        self.assertEqual(pool.started, [('se-got-001', 'se-got-001')])
        described = body['exit']
        self.assertEqual(described['name'], 'se-got-001')
        self.assertEqual(described['relay'], 'se-got-001')
        self.assertEqual(described['proxy'], 'http://127.0.0.1:18803')


class VpnExitStopEndpoint(unittest.TestCase):
    def _stop(self, body, operator=(True, 'player'), pool=None):
        pool = pool if pool is not None else FakePool()
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator), \
             unittest.mock.patch.object(serve, 'VPN_EXIT_POOL', pool):
            response = _client().post('/api/vpn-exits/stop', json=body)
        return response, pool

    def test_stop_requires_the_operator_gate(self):
        response, _ = self._stop({'name': 'exit-x'},
                                 operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_stop_requires_a_name(self):
        for body in ({}, {'name': '   '}):
            response, _ = self._stop(body)
            self.assertEqual(response.status_code, 400)

    def test_stop_reports_an_unknown_exit_as_404(self):
        response, _ = self._stop({'name': 'ghost'})
        self.assertEqual(response.status_code, 404)

    def test_stop_stops_the_pool(self):
        pool = FakePool(exits={'exit-x': FakeExit('exit-x', 'se-got-001')})
        response, pool = self._stop({'name': 'exit-x'}, pool=pool)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'ok': True, 'stopped': 'exit-x',
                          'actor': 'player'})
        self.assertEqual(pool.stopped, ['exit-x'])

    def test_stop_surfaces_removal_failures_as_502(self):
        pool = FakePool(exits={'exit-x': FakeExit('exit-x', 'se-got-001')},
                        stop_result=False)
        response, _ = self._stop({'name': 'exit-x'}, pool=pool)
        self.assertEqual(response.status_code, 502)


class BrowseThroughPoolExit(unittest.TestCase):
    def _browse(self, pool, body, fetches):
        def fake_fetch(url, proxy=None):
            fetches.append((url, proxy))
            return ('https://example.com/page', 'text/html',
                    '<html><body>hi</body></html>', False, None)

        with unittest.mock.patch.object(serve, 'VPN_EXIT_POOL', pool), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY',
                                        'test-key'), \
             unittest.mock.patch.object(serve, '_page_budget_exhausted',
                                        return_value=False), \
             unittest.mock.patch.object(serve, 'check_rate_limit',
                                        return_value=True), \
             unittest.mock.patch.object(serve, 'verify_agent_key',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_is_safe_public_host',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_is_allowlisted_host',
                                        return_value=True), \
             unittest.mock.patch.object(serve, 'record_browse_success'), \
             unittest.mock.patch.object(serve, '_fetch_page_sync',
                                        side_effect=fake_fetch), \
             unittest.mock.patch.object(
                 serve, '_jev_re_gate_redirect_final',
                 new=unittest.mock.AsyncMock(return_value=(True, ''))), \
             unittest.mock.patch.object(
                 serve, 'wrap_external_content',
                 return_value=('wrapped', 'nonce', 'tag', 'instruction')), \
             unittest.mock.patch.object(serve, '_find_chrome',
                                        return_value=None), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch.object(serve, '_accrue_page_request'):
            return _client().post('/api/browse', json=body)

    def test_a_browse_can_be_routed_through_a_pool_exit(self):
        pool = FakePool(exits={'exit-x': FakeExit('exit-x', 'se-got-001')})
        fetches = []
        response = self._browse(pool, {
            'url': 'https://example.com/page', 'agentId': 'ada',
            'purpose': 'weather research', 'viaVpnExit': 'exit-x'},
            fetches)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['allowed'])
        # Only THIS fetch was rerouted, through the exit's own
        # tunnel -- no host-wide route change.
        self.assertEqual(fetches,
                         [('https://example.com/page',
                           'http://127.0.0.1:18803')])

    def test_an_unknown_exit_is_rejected_before_any_fetch(self):
        pool = FakePool()
        fetches = []
        response = self._browse(pool, {
            'url': 'https://example.com/page', 'agentId': 'ada',
            'purpose': 'weather research', 'viaVpnExit': 'ghost'},
            fetches)
        self.assertFalse(response.json()['allowed'])
        self.assertIn('POST /api/vpn-exits/start',
                      response.json()['reason'])
        self.assertEqual(fetches, [])


if __name__ == '__main__':
    unittest.main()
