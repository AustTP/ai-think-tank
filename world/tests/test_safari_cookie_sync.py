import json
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sandbox'))
import serve
import browser_driver


def _binarycookies(cookies):
    records = []
    for name, value, domain in cookies:
        strings = b'\x00'.join(x.encode() for x in (domain, name, '/', value)) + b'\x00'
        record = bytearray(56)
        struct.pack_into('<I', record, 0, 56 + len(strings))
        struct.pack_into('<I', record, 8, 5)
        struct.pack_into('<IIII', record, 16, 56, 56 + len(domain) + 1,
                         56 + len(domain) + 1 + len(name) + 1,
                         56 + len(domain) + 1 + len(name) + 1 + 2)
        struct.pack_into('<d', record, 40, time.time() - 978307200)
        records.append(bytes(record) + strings)
    offsets = []
    body = b''
    start = 8 + 4 * len(records)
    for record in records:
        offsets.append(start + len(body))
        body += record
    page = b'\x00\x00\x01\x00' + struct.pack('<I', len(records))
    page += struct.pack('<' + 'I' * len(offsets), *offsets) + body
    return b'cook' + struct.pack('>I', 1) + struct.pack('>I', len(page)) + page


class SafariCookieSync(unittest.TestCase):
    def test_parses_binarycookies_and_writes_playwright_seed(self):
        raw = _binarycookies([('c_user', '100', '.facebook.com'),
                              ('other', 'x', '.example.com')])
        parsed = serve._parse_safari_binarycookies(raw)
        self.assertEqual({c['name'] for c in parsed}, {'c_user', 'other'})
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, 'Cookies.binarycookies')
            with open(source, 'wb') as stream:
                stream.write(raw)
            profile = os.path.join(root, 'profile')
            with mock.patch.object(serve, 'SAFARI_COOKIES_PATH', source), \
                 mock.patch.object(serve, '_load_env', return_value={
                     'SAFARI_COOKIE_CONSENT': '1',
                     'SAFARI_COOKIE_AGENTS': 'ada'}), \
                 mock.patch.object(serve, '_browser_profile_dir', return_value=profile):
                self.assertEqual(serve._sync_safari_cookies('ada', 'www.facebook.com'), 1)
                seed = os.path.join(profile, 'safari-cookies.json')
                cookies = json.load(open(seed))
                self.assertEqual([c['name'] for c in cookies], ['c_user'])
                self.assertEqual(os.stat(seed).st_mode & 0o777, 0o600)
                self.assertEqual(serve._sync_safari_cookies('ada', 'example.com'), 0)
                # Consent is not a blanket grant: an agent outside the allowlist
                # gets no seed even for a social host.
                self.assertEqual(serve._sync_safari_cookies('ben', 'www.facebook.com'), 0)

    def test_no_consent_means_no_seed(self):
        raw = _binarycookies([('c_user', '100', '.facebook.com')])
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, 'Cookies.binarycookies')
            with open(source, 'wb') as stream:
                stream.write(raw)
            with mock.patch.object(serve, 'SAFARI_COOKIES_PATH', source), \
                 mock.patch.object(serve, '_load_env', return_value={
                     'SAFARI_COOKIE_AGENTS': 'ada'}):
                self.assertEqual(serve._sync_safari_cookies('ada', 'www.facebook.com'), 0)

    def test_driver_applies_the_agent_profile_seed(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'safari-cookies.json'), 'w') as stream:
                json.dump([{'name': 'c_user', 'value': '100', 'domain': '.facebook.com',
                            'path': '/', 'expires': -1, 'secure': True,
                            'httpOnly': True, 'sameSite': 'Lax'}], stream)
            ctx = mock.Mock()
            browser_driver._apply_safari_cookies(ctx, root)
            ctx.add_cookies.assert_called_once()
