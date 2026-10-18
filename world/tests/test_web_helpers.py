"""Pure web/text/path/security helpers from web_helpers.py.

Every helper here is pure (stdlib only, no module state), so these tests
drive them directly. `_is_safe_public_host` resolves hostnames through
socket.getaddrinfo, which is patched per-test to control which addresses a
hostname "resolves" to without touching the network.
"""
import email.utils
import http.client
import ipaddress
import os
import sys
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_helpers  # noqa: E402


class StripHtmlToText(unittest.TestCase):
    def test_script_and_style_content_is_removed(self):
        html = '<html><body><script>var x = 1;</script>Hello <b>world</b><style>.a{}</style></body></html>'
        self.assertEqual(web_helpers._strip_html_to_text(html), 'Hello world')

    def test_entities_are_unescaped_and_whitespace_collapsed(self):
        self.assertEqual(web_helpers._strip_html_to_text('A&nbsp;&amp;&nbsp;B'), 'A & B')

    def test_empty_input_stays_empty(self):
        self.assertEqual(web_helpers._strip_html_to_text(''), '')


class ExtractLinks(unittest.TestCase):
    def test_extracts_absolute_and_relative_links(self):
        html = '<a href="/wiki/Foo">Foo page</a> <a href="https://example.com/x">X</a>'
        links = web_helpers._extract_links(html, 'https://en.wikipedia.org')
        self.assertEqual(links[0]['url'], 'https://en.wikipedia.org/wiki/Foo')
        self.assertEqual(links[0]['text'], 'Foo page')
        self.assertEqual(links[1]['url'], 'https://example.com/x')

    def test_skips_hreflang_alternate_links(self):
        html = '<a hreflang="fr" href="https://fr.wikipedia.org/wiki/Foo">Francais</a>'
        self.assertEqual(web_helpers._extract_links(html, 'https://en.wikipedia.org'), [])

    def test_skips_anchors_without_href(self):
        self.assertEqual(web_helpers._extract_links('<a name="x">no href</a>', 'https://e.org'), [])

    def test_skips_fragment_js_mailto_tel_links(self):
        html = ('<a href="#top">frag</a> <a href="javascript:alert(1)">js</a> '
                '<a href="mailto:a@b.c">mail</a> <a href="tel:+1555">tel</a>')
        self.assertEqual(web_helpers._extract_links(html, 'https://e.org'), [])

    def test_skips_non_http_schemes_and_duplicates(self):
        html = ('<a href="ftp://e.org/f">ftp</a> <a href="https://e.org/dup">one</a> '
                '<a href="https://e.org/dup">two</a>')
        links = web_helpers._extract_links(html, 'https://e.org')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['url'], 'https://e.org/dup')

    def test_link_text_is_stripped_of_nested_tags(self):
        html = '<a href="https://e.org/x"><b>Bold</b> text</a>'
        links = web_helpers._extract_links(html, 'https://e.org')
        self.assertEqual(links[0]['text'], 'Bold text')

    def test_empty_text_falls_back_to_the_url(self):
        html = '<a href="https://e.org/x"></a>'
        links = web_helpers._extract_links(html, 'https://e.org')
        self.assertEqual(links[0]['text'], 'https://e.org/x')

    def test_caps_out_after_200_links(self):
        html = ''.join(f'<a href="https://e.org/l{i}">link {i}</a>' for i in range(250))
        links = web_helpers._extract_links(html, 'https://e.org')
        self.assertEqual(len(links), 200)


class ParseHttpDateMs(unittest.TestCase):
    def test_parses_rfc1123_date(self):
        ms = web_helpers._parse_http_date_ms('Wed, 21 Oct 2015 07:28:00 GMT')
        self.assertEqual(ms, 1445412480000)

    def test_naive_date_is_assumed_utc(self):
        ms = web_helpers._parse_http_date_ms('Wed, 21 Oct 2015 07:28:00')
        self.assertEqual(ms, 1445412480000)

    def test_parses_rfc850_and_asctime(self):
        self.assertIsNotNone(web_helpers._parse_http_date_ms('Sunday, 06-Nov-94 08:49:37 GMT'))
        self.assertIsNotNone(web_helpers._parse_http_date_ms('Sun Nov  6 08:49:37 1994'))

    def test_empty_and_garbage_return_none(self):
        self.assertIsNone(web_helpers._parse_http_date_ms(''))
        self.assertIsNone(web_helpers._parse_http_date_ms('not a date'))
        self.assertIsNone(web_helpers._parse_http_date_ms(None))


class IsSafePublicHost(unittest.TestCase):
    def _resolve_to(self, addrs):
        def fake_getaddrinfo(host, *a, **k):
            return [(None, None, None, None, (addr, 0)) for addr in addrs]
        return unittest.mock.patch.object(web_helpers.socket, 'getaddrinfo', fake_getaddrinfo)

    def test_rejects_empty_localhost_and_wildcard(self):
        self.assertFalse(web_helpers._is_safe_public_host(''))
        self.assertFalse(web_helpers._is_safe_public_host('localhost'))
        self.assertFalse(web_helpers._is_safe_public_host('0.0.0.0'))

    def test_rejects_hosts_that_do_not_resolve(self):
        with unittest.mock.patch.object(web_helpers.socket, 'getaddrinfo',
                                        unittest.mock.Mock(side_effect=web_helpers.socket.gaierror)):
            self.assertFalse(web_helpers._is_safe_public_host('nope.invalid'))

    def test_rejects_private_loopback_link_local_reserved_multicast(self):
        for addr in ('10.0.0.1', '127.0.0.1', '169.254.1.1', '240.0.0.1', '224.0.0.1', '::1', 'fe80::1'):
            with self._resolve_to([addr]):
                self.assertFalse(web_helpers._is_safe_public_host('host.test'), addr)

    def test_rejects_non_ip_address_in_result(self):
        with self._resolve_to(['not-an-ip']):
            self.assertFalse(web_helpers._is_safe_public_host('host.test'))

    def test_rejects_if_any_resolved_address_is_unsafe(self):
        with self._resolve_to(['93.184.216.34', '127.0.0.1']):
            self.assertFalse(web_helpers._is_safe_public_host('host.test'))

    def test_accepts_pure_public_address(self):
        with self._resolve_to(['93.184.216.34']):
            self.assertTrue(web_helpers._is_safe_public_host('example.com'))


class DownloadHelpers(unittest.TestCase):
    def test_download_dest_routes_shared_vs_personal(self):
        self.assertEqual(web_helpers._download_dest_rel_path('shared', 'a1', 'f.png'),
                         'pending_review/shared/f.png')
        self.assertEqual(web_helpers._download_dest_rel_path('personal', 'a1', 'f.png'),
                         'pending_review/downloads/a1/f.png')

    def test_sanitize_filename_strips_paths_and_bad_chars(self):
        self.assertEqual(web_helpers._sanitize_download_filename('../../.env'), '.env')
        self.assertEqual(web_helpers._sanitize_download_filename('report v2 (final).pdf'),
                         'report_v2__final_.pdf')
        long_name = web_helpers._sanitize_download_filename('a' * 500)
        self.assertEqual(len(long_name), 200)

    def test_gmail_app_password_validation(self):
        self.assertTrue(web_helpers._looks_like_gmail_app_password('abcd efgh ijkl mnop'))
        self.assertTrue(web_helpers._looks_like_gmail_app_password('abcdefghijklmnop'))
        self.assertFalse(web_helpers._looks_like_gmail_app_password('short'))
        self.assertFalse(web_helpers._looks_like_gmail_app_password('abcd efgh ijkl mnopq'))
        self.assertFalse(web_helpers._looks_like_gmail_app_password(None))


class BlockLinkValue(unittest.TestCase):
    def test_kind_blocks_hash_the_full_serialized_block(self):
        block = {'kind': 'DECISION', 'content': 'x'}
        value = web_helpers._block_link_value(block)
        self.assertEqual(len(value), 64)
        # Same content -> same value; different content -> different value.
        self.assertEqual(value, web_helpers._block_link_value(dict(block)))
        self.assertNotEqual(value, web_helpers._block_link_value({'kind': 'DECISION', 'content': 'y'}))

    def test_kindless_blocks_use_their_own_sha(self):
        self.assertEqual(web_helpers._block_link_value({'sha256': 'abc'}), 'abc')


class Sha256File(unittest.TestCase):
    def test_hashes_file_contents_in_chunks(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'some content' * 10000)
            path = f.name
        try:
            self.assertEqual(web_helpers._sha256_file(path),
                             web_helpers._sha256_file(path))
        finally:
            os.unlink(path)


class _FakeResp:
    """A stand-in for urllib's response object: geturl() + read() + close()."""

    def __init__(self, final_url, content=b''):
        self._final = final_url
        self._content = content

    def geturl(self):
        return self._final

    def read(self, n=-1):
        return self._content[:n] if n and n > 0 else self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class IsShortLink(unittest.TestCase):
    def test_known_shortener_hosts(self):
        self.assertTrue(web_helpers._is_short_link('https://t.co/abc123'))
        self.assertTrue(web_helpers._is_short_link('https://bit.ly/xyz'))
        self.assertTrue(web_helpers._is_short_link('http://tinyurl.com/abc'))
        self.assertTrue(web_helpers._is_short_link('https://lnkd.in/abc'))

    def test_subdomains_of_shorteners_count(self):
        self.assertTrue(web_helpers._is_short_link('https://sub.bit.ly/abc'))

    def test_regular_sites_are_not_shorteners(self):
        self.assertFalse(web_helpers._is_short_link('https://example.com/abc'))
        self.assertFalse(web_helpers._is_short_link('https://docs.python.org/3/'))
        self.assertFalse(web_helpers._is_short_link(''))

    def test_case_insensitive_and_garbage_safe(self):
        self.assertTrue(web_helpers._is_short_link('https://T.CO/abc'))
        self.assertFalse(web_helpers._is_short_link('not a url'))


class ResolveFinalUrl(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_returns_resolved_landing_url(self):
        # t.co/abc123 resolves to a real site; the short string is NOT the
        # landing URL.
        unittest.mock.patch.object(web_helpers, '_safe_urlopen',
                                   return_value=_FakeResp('https://realcompany.com/blog/launch')).start()
        final, hops = web_helpers._resolve_final_url('https://t.co/abc123')
        self.assertEqual(final, 'https://realcompany.com/blog/launch')

    def test_failure_returns_url_unchanged_and_zero_hops(self):
        unittest.mock.patch.object(web_helpers, '_safe_urlopen',
                                   side_effect=urllib.error.URLError('host not public')).start()
        final, hops = web_helpers._resolve_final_url('https://t.co/abc123')
        self.assertEqual(final, 'https://t.co/abc123')
        self.assertEqual(hops, 0)

    def test_non_http_scheme_returns_unchanged(self):
        final, hops = web_helpers._resolve_final_url('ftp://example.com/x')
        self.assertEqual(final, 'ftp://example.com/x')
        self.assertEqual(hops, 0)

    def test_empty_url_returns_unchanged(self):
        final, hops = web_helpers._resolve_final_url('')
        self.assertEqual(final, '')
        self.assertEqual(hops, 0)


class ResolvePublicIp(unittest.TestCase):
    """_resolve_public_ip: the SSRF resolution primitive's own
    branches (distinct from _is_safe_public_host's copy)."""

    def _resolve_to(self, addrs):
        def fake_getaddrinfo(host, *a, **k):
            return [(None, None, None, None, (addr, 0)) for addr in addrs]
        return unittest.mock.patch.object(web_helpers.socket, 'getaddrinfo', fake_getaddrinfo)

    def test_rejects_localhost_wildcard_and_empty(self):
        self.assertIsNone(web_helpers._resolve_public_ip(''))
        self.assertIsNone(web_helpers._resolve_public_ip('localhost'))
        self.assertIsNone(web_helpers._resolve_public_ip('0.0.0.0'))

    def test_unresolvable_host_returns_none(self):
        with unittest.mock.patch.object(
                web_helpers.socket, 'getaddrinfo',
                unittest.mock.Mock(side_effect=web_helpers.socket.gaierror)):
            self.assertIsNone(web_helpers._resolve_public_ip('nope.invalid'))

    def test_non_ip_address_is_skipped(self):
        with self._resolve_to(['not-an-ip']):
            self.assertIsNone(web_helpers._resolve_public_ip('host.test'))

    def test_only_private_addresses_returns_none(self):
        with self._resolve_to(['127.0.0.1', '10.0.0.1']):
            self.assertIsNone(web_helpers._resolve_public_ip('host.test'))

    def test_returns_the_first_public_address(self):
        with self._resolve_to(['127.0.0.1', '93.184.216.34']):
            self.assertEqual(web_helpers._resolve_public_ip('host.test'),
                             '93.184.216.34')


class PinnedHTTPConnect(unittest.TestCase):
    """_PinnedHTTPConnection: the pinned-IP connect and the
    proxy-tunnel branch."""

    def test_connect_uses_the_pinned_ip(self):
        conn = web_helpers._PinnedHTTPConnection('example.com',
                                                 pinned_ip='93.184.216.34')
        sock = unittest.mock.Mock()
        with unittest.mock.patch.object(web_helpers.socket, 'create_connection',
                                        return_value=sock) as create:
            conn.connect()
        self.assertIs(conn.sock, sock)
        create.assert_called_once_with(('93.184.216.34', conn.port),
                                       conn.timeout, conn.source_address)

    def test_connect_runs_the_proxy_tunnel(self):
        conn = web_helpers._PinnedHTTPConnection('example.com',
                                                 pinned_ip='93.184.216.34')
        conn._tunnel_host = 'proxy.example:8080'
        with unittest.mock.patch.object(web_helpers.socket, 'create_connection',
                                        return_value=unittest.mock.Mock()), \
             unittest.mock.patch.object(conn, '_tunnel') as tunnel:
            conn.connect()
        tunnel.assert_called_once_with()


class PinnedHTTPSConnect(unittest.TestCase):
    def test_connect_tunnels_then_wraps_for_the_origin(self):
        conn = web_helpers._PinnedHTTPSConnection('example.com',
                                                  pinned_ip='93.184.216.34')
        sock = unittest.mock.Mock()
        wrapped = unittest.mock.Mock()
        ctx = unittest.mock.Mock()
        ctx.wrap_socket.return_value = wrapped
        conn._context = ctx
        conn._tunnel_host = 'origin.example'
        with unittest.mock.patch.object(web_helpers.socket, 'create_connection',
                                        return_value=sock), \
             unittest.mock.patch.object(conn, '_tunnel') as tunnel:
            conn.connect()
        tunnel.assert_called_once_with()
        self.assertIs(conn.sock, wrapped)
        ctx.wrap_socket.assert_called_once_with(sock, server_hostname='origin.example')


class ResolveProxyIp(unittest.TestCase):
    """_resolve_proxy_ip: the operator-configured proxy resolver
    (accepts loopback/private, unlike the origin resolver)."""

    def _resolve_to(self, addrs):
        def fake_getaddrinfo(host, *a, **k):
            return [(None, None, None, None, (addr, 0)) for addr in addrs]
        return unittest.mock.patch.object(web_helpers.socket, 'getaddrinfo', fake_getaddrinfo)

    def test_empty_hostport_returns_none(self):
        self.assertIsNone(web_helpers._resolve_proxy_ip(''))
        self.assertIsNone(web_helpers._resolve_proxy_ip(None))

    def test_bracketed_ipv6_literal_is_kept(self):
        with self._resolve_to(['::1']):
            self.assertEqual(web_helpers._resolve_proxy_ip('[::1]:8080'), '::1')

    def test_hostport_strips_the_port(self):
        with self._resolve_to(['127.0.0.1']):
            self.assertEqual(web_helpers._resolve_proxy_ip('proxy.example:8080'),
                             '127.0.0.1')

    def test_unresolvable_proxy_returns_none(self):
        with unittest.mock.patch.object(
                web_helpers.socket, 'getaddrinfo',
                unittest.mock.Mock(side_effect=web_helpers.socket.gaierror)):
            self.assertIsNone(web_helpers._resolve_proxy_ip('proxy.invalid:8080'))

    def test_no_addresses_returns_none(self):
        with unittest.mock.patch.object(web_helpers.socket, 'getaddrinfo',
                                        return_value=[]):
            self.assertIsNone(web_helpers._resolve_proxy_ip('proxy.example:8080'))


class PinnedIpFor(unittest.TestCase):
    def test_tunneled_request_pins_the_proxy(self):
        req = urllib.request.Request('https://example.com/x')
        req._tunnel_host = 'proxy.example:8080'
        with unittest.mock.patch.object(
                web_helpers, '_resolve_proxy_ip',
                return_value='127.0.0.1') as resolve:
            self.assertEqual(web_helpers._pinned_ip_for(req), '127.0.0.1')
        resolve.assert_called_once_with('example.com')


class PinnedIPHandlerDoOpen(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_refuses_a_private_host(self):
        handler = web_helpers._PinnedIPHandler()
        req = urllib.request.Request('http://localhost/x')
        with self.assertRaises(urllib.error.URLError):
            handler.do_open(http.client.HTTPConnection, req)

    def test_pins_and_swaps_to_the_pinned_class(self):
        handler = web_helpers._PinnedIPHandler()
        req = urllib.request.Request('http://example.com/x')
        resp = unittest.mock.Mock()
        with unittest.mock.patch.object(web_helpers, '_resolve_public_ip',
                                        return_value='93.184.216.34'), \
             unittest.mock.patch.object(urllib.request.AbstractHTTPHandler,
                                        'do_open', return_value=resp) as do_open:
            result = handler.do_open(http.client.HTTPConnection, req)
        self.assertIs(result, resp)
        self.assertIs(do_open.call_args[0][0], web_helpers._PinnedHTTPConnection)
        self.assertEqual(do_open.call_args[1]['pinned_ip'], '93.184.216.34')


class PinnedHTTPSHandlerDoOpen(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_refuses_a_private_host(self):
        handler = web_helpers._PinnedHTTPSHandler()
        req = urllib.request.Request('https://localhost/x')
        with self.assertRaises(urllib.error.URLError):
            handler.do_open(http.client.HTTPSConnection, req)

    def test_pins_and_swaps_to_the_pinned_class(self):
        handler = web_helpers._PinnedHTTPSHandler()
        req = urllib.request.Request('https://example.com/x')
        resp = unittest.mock.Mock()
        with unittest.mock.patch.object(web_helpers, '_resolve_public_ip',
                                        return_value='93.184.216.34'), \
             unittest.mock.patch.object(urllib.request.AbstractHTTPHandler,
                                        'do_open', return_value=resp) as do_open:
            result = handler.do_open(http.client.HTTPSConnection, req)
        self.assertIs(result, resp)
        self.assertIs(do_open.call_args[0][0],
                      web_helpers._PinnedHTTPSConnection)
        self.assertEqual(do_open.call_args[1]['pinned_ip'], '93.184.216.34')


class SafeUrlOpen(unittest.TestCase):
    """_safe_urlopen: the string-URL wrapper and the proxy-handler
    branch, both fully hermetic (the pinned handlers' do_open is
    mocked, so nothing touches the network)."""

    def tearDown(self):
        unittest.mock.patch.stopall()

    def _resp(self):
        resp = unittest.mock.Mock()
        resp.code = 200
        return resp

    def test_string_url_is_wrapped_and_opened(self):
        resp = self._resp()
        with unittest.mock.patch.object(web_helpers._PinnedIPHandler, 'do_open',
                                        return_value=resp), \
             unittest.mock.patch.object(web_helpers._PinnedHTTPSHandler, 'do_open',
                                        return_value=resp):
            self.assertIs(web_helpers._safe_urlopen('http://example.com/x'), resp)

    def test_proxy_url_adds_the_proxy_handler(self):
        resp = self._resp()
        with unittest.mock.patch.object(web_helpers._PinnedIPHandler, 'do_open',
                                        return_value=resp), \
             unittest.mock.patch.object(web_helpers._PinnedHTTPSHandler, 'do_open',
                                        return_value=resp):
            self.assertIs(
                web_helpers._safe_urlopen('http://example.com/x',
                                          proxy='http://127.0.0.1:18801'),
                resp)


class IsShortLinkEdges(unittest.TestCase):
    def test_unparseable_url_returns_false(self):
        self.assertFalse(web_helpers._is_short_link('http://[::1'))


class CountingRedirect(unittest.TestCase):
    def test_redirect_request_counts_a_hop(self):
        handler = web_helpers._CountingRedirectHandler()
        req = urllib.request.Request('http://example.com/a')
        out = handler.redirect_request(req, None, 301, 'Moved Permanently',
                                       {}, 'http://example.com/b')
        self.assertEqual(handler.hops, 1)
        self.assertIsNotNone(out)


class ResolveFinalUrlEmptyFinal(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_empty_final_url_returns_the_original(self):
        unittest.mock.patch.object(web_helpers, '_safe_urlopen',
                                   return_value=_FakeResp('')).start()
        final, hops = web_helpers._resolve_final_url('https://t.co/abc')
        self.assertEqual(final, 'https://t.co/abc')
        self.assertEqual(hops, 0)


if __name__ == '__main__':
    unittest.main()
