"""The Work Room sandbox's egress allowlist proxy (sandbox_proxy.py).

Runs as its own standalone Docker container/process -- these tests exercise
its pure logic (is_allowed, rate_limited) directly, without any real network
or Docker involved. `ALLOWED_HOSTS` is built once at import time from
SANDBOX_EGRESS_EXTRA_HOSTS, so tests that need to control it reload the
module under a patched environment rather than mutating the set in place.
"""
import importlib
import os
import runpy
import socket
import sys
import threading
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


class _ConnPair:
    """Two connected socketpairs that stand in for the proxy's (conn, remote)
    sockets and their test-side peers, so handle_client's full CONNECT/HTTP
    relay path can be driven over real loopback sockets -- no Docker, no live
    network. Pair 1: (proxy_sock, test_sock). Pair 2: (remote_proxy_end,
    remote_test_end). The fake create_connection hands back remote_proxy_end,
    so relay(proxy_sock, remote_proxy_end) copies test_sock->remote_test_end
    and remote_test_end->test_sock."""

    def __init__(self, rate_limited=False):
        importlib.reload(sandbox_proxy)
        self.proxy_sock, self.test_sock = socket.socketpair()
        self.remote_proxy_end, self.remote_test_end = socket.socketpair()
        if rate_limited:
            for _ in range(sandbox_proxy.RATE_LIMIT_MAX_REQUESTS):
                sandbox_proxy.rate_limited('pypi.org')

    def read_all(self, sock, timeout=2.0):
        sock.settimeout(timeout)
        out = b''
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        return out

    def read_chunk(self, sock, timeout=2.0):
        sock.settimeout(timeout)
        try:
            return sock.recv(4096)
        except socket.timeout:
            return b''

    def run_handle_client(self):
        sandbox_proxy.handle_client(self.proxy_sock)

    def close(self):
        for s in (self.proxy_sock, self.test_sock, self.remote_proxy_end, self.remote_test_end):
            try:
                s.close()
            except OSError:
                pass


class RelayRelays(unittest.TestCase):
    def test_relay_copies_bytes_both_ways(self):
        c_proxy, c_test = socket.socketpair()
        s_proxy, s_test = socket.socketpair()
        c_test.settimeout(2)
        s_test.settimeout(2)
        try:
            t = threading.Thread(target=sandbox_proxy.relay, args=(c_proxy, s_proxy), daemon=True)
            t.start()
            c_test.sendall(b'hello from client')
            self.assertEqual(s_test.recv(4096), b'hello from client')
            s_test.sendall(b'hi from server')
            self.assertEqual(c_test.recv(4096), b'hi from server')
            c_test.close()
            s_test.close()
            t.join(2)
            self.assertFalse(t.is_alive())
        finally:
            for s in (c_proxy, c_test, s_proxy, s_test):
                try:
                    s.close()
                except OSError:
                    pass


class HandleClient(unittest.TestCase):
    def tearDown(self):
        importlib.reload(sandbox_proxy)

    def _run(self, pair, thread_fn):
        t = threading.Thread(target=thread_fn, daemon=True)
        t.start()
        return t

    def test_connect_allowed_host_tunnels_to_remote(self):
        pair = _ConnPair()
        t = None
        try:
            with unittest.mock.patch.object(sandbox_proxy.socket, 'create_connection',
                                            lambda addr, timeout=0: pair.remote_proxy_end):
                t = self._run(pair, pair.run_handle_client)
                pair.test_sock.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\n\r\n')
                resp = pair.read_all(pair.test_sock)
            self.assertIn(b'200 Connection Established', resp)
            pair.test_sock.sendall(b'client payload')
            self.assertEqual(pair.read_chunk(pair.remote_test_end), b'client payload')
            pair.remote_test_end.sendall(b'server payload')
            self.assertEqual(pair.read_chunk(pair.test_sock), b'server payload')
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_connect_blocked_host_is_403(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n')
            resp = pair.read_all(pair.test_sock)
            self.assertIn(b'403 Forbidden', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_connect_rate_limited_host_is_429(self):
        pair = _ConnPair(rate_limited=True)
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\n\r\n')
            resp = pair.read_all(pair.test_sock)
            self.assertIn(b'429 Too Many Requests', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_connect_unreachable_remote_is_502(self):
        pair = _ConnPair()
        t = None
        try:
            def fake_connect(addr, timeout=0):
                raise OSError('no route to host')
            with unittest.mock.patch.object(sandbox_proxy.socket, 'create_connection', fake_connect):
                t = self._run(pair, pair.run_handle_client)
                pair.test_sock.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\n\r\n')
                resp = pair.read_all(pair.test_sock)
            self.assertIn(b'502 Bad Gateway', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_connect_without_port_defaults_to_443(self):
        pair = _ConnPair()
        t = None
        try:
            seen = {}
            def fake_connect(addr, timeout=0):
                seen['addr'] = addr
                return pair.remote_proxy_end
            with unittest.mock.patch.object(sandbox_proxy.socket, 'create_connection', fake_connect):
                t = self._run(pair, pair.run_handle_client)
                pair.test_sock.sendall(b'CONNECT pypi.org HTTP/1.1\r\n\r\n')
                pair.read_all(pair.test_sock)
            self.assertEqual(seen['addr'], ('pypi.org', 443))
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_plain_http_allowed_host_is_relayed(self):
        pair = _ConnPair()
        t = None
        try:
            with unittest.mock.patch.object(sandbox_proxy.socket, 'create_connection',
                                            lambda addr, timeout=0: pair.remote_proxy_end):
                t = self._run(pair, pair.run_handle_client)
                pair.test_sock.sendall(b'GET / HTTP/1.1\r\nHost: pypi.org\r\n\r\n')
                relayed = pair.read_all(pair.remote_test_end)
            self.assertIn(b'Host: pypi.org', relayed)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_plain_http_blocked_host_is_403(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'GET / HTTP/1.1\r\nHost: evil.example.com\r\n\r\n')
            resp = pair.read_all(pair.test_sock)
            self.assertIn(b'403 Forbidden', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_plain_http_missing_host_is_403(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'GET / HTTP/1.1\r\n\r\n')
            resp = pair.read_all(pair.test_sock)
            self.assertIn(b'403 Forbidden', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_short_malformed_request_is_dropped(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'GARBAGE\r\n\r\n')
            t.join(2)
            self.assertFalse(t.is_alive())
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_oversized_request_without_terminator_is_dropped(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'A' * 70000)
            t.join(2)
            self.assertFalse(t.is_alive())
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_client_disconnect_mid_request_returns_silently(self):
        pair = _ConnPair()
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'CONNECT')
            pair.test_sock.close()
            t.join(2)
            self.assertFalse(t.is_alive())
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_plain_http_rate_limited_host_is_429(self):
        pair = _ConnPair(rate_limited=True)
        t = None
        try:
            t = self._run(pair, pair.run_handle_client)
            pair.test_sock.sendall(b'GET / HTTP/1.1\r\nHost: pypi.org\r\n\r\n')
            resp = pair.read_all(pair.test_sock)
            self.assertIn(b'429 Too Many Requests', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_plain_http_remote_send_failure_is_502(self):
        pair = _ConnPair()
        t = None
        try:
            def fake_connect(addr, timeout=0):
                bad = unittest.mock.Mock()
                bad.sendall = unittest.mock.Mock(side_effect=OSError('broken pipe'))
                return bad
            with unittest.mock.patch.object(sandbox_proxy.socket, 'create_connection', fake_connect):
                t = self._run(pair, pair.run_handle_client)
                pair.test_sock.sendall(b'GET / HTTP/1.1\r\nHost: pypi.org\r\n\r\n')
                resp = pair.read_all(pair.test_sock)
            self.assertIn(b'502 Bad Gateway', resp)
        finally:
            pair.close()
            if t:
                t.join(2)

    def test_oserror_while_reading_request_is_swallowed(self):
        conn = unittest.mock.Mock()
        conn.settimeout = unittest.mock.Mock()
        conn.recv = unittest.mock.Mock(side_effect=OSError('connection reset'))
        conn.close = unittest.mock.Mock(side_effect=OSError('already closed'))
        # handle_client must not raise; the OSError from recv is swallowed and
        # the connection close error is swallowed too.
        sandbox_proxy.handle_client(conn)
        conn.close.assert_called_once()


class MainAcceptsAndServes(unittest.TestCase):
    def tearDown(self):
        importlib.reload(sandbox_proxy)

    def test_main_listens_and_dispatches_an_accepted_connection(self):
        seen = []
        calls = [0]

        class FakeSocket:
            def setsockopt(self, *a):
                pass
            def bind(self, *a):
                seen.append('bind')
            def listen(self, *a):
                seen.append('listen')
            def accept(self):
                calls[0] += 1
                if calls[0] == 1:
                    seen.append('accept')
                    conn = unittest.mock.Mock()
                    conn.recv = lambda *a: b''
                    return conn, ('1.2.3.4', 5555)
                seen.append('accept2')
                raise OSError('closed')
            def __getattr__(self, name):
                raise OSError('no')

        fake = FakeSocket()
        with unittest.mock.patch.object(sandbox_proxy.socket, 'socket',
                                        lambda *a, **k: fake), \
             unittest.mock.patch.object(sandbox_proxy.threading.Thread, 'start', lambda self: None), \
             unittest.mock.patch('builtins.print'):
            with self.assertRaises(OSError):
                sandbox_proxy.main()
        self.assertIn('bind', seen)
        self.assertIn('listen', seen)
        self.assertIn('accept', seen)

    def test_module_runs_as_script_starts_the_proxy(self):
        proxy_path = os.path.join(os.path.dirname(sandbox_proxy.__file__), 'sandbox_proxy.py')
        ran = []

        class FakeServer:
            def setsockopt(self, *a):
                pass
            def bind(self, *a):
                ran.append('bind')
            def listen(self, *a):
                ran.append('listen')
            def accept(self):
                raise OSError('interrupted')  # exit the accept loop immediately

        with unittest.mock.patch.object(socket, 'socket', lambda *a, **k: FakeServer()), \
             unittest.mock.patch('builtins.print'):
            with self.assertRaises(OSError):
                runpy.run_path(proxy_path, run_name='__main__')
        self.assertIn('bind', ran)
        self.assertIn('listen', ran)


if __name__ == '__main__':
    unittest.main()
