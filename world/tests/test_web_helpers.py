"""Pure web/text/path/security helpers from web_helpers.py.

Every helper here is pure (stdlib only, no module state), so these tests
drive them directly. `_is_safe_public_host` resolves hostnames through
socket.getaddrinfo, which is patched per-test to control which addresses a
hostname "resolves" to without touching the network.
"""
import email.utils
import ipaddress
import os
import sys
import unittest
import unittest.mock

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


if __name__ == '__main__':
    unittest.main()
