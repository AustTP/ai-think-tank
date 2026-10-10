"""Scoped file download + receive-only SMS inbox.

Downloads: agents pull files into their own filespace under a hard size cap,
public-https only (SSRF), sanitized filenames, optional capability handle for
credentialed downloads. Nothing downloaded is ever executed.

SMS: texts enter ONLY via a shared-secret webhook; the inbox is operator-only;
an agent reads exactly one message via a single-use, short-lived grant minted
by a director/admin/player, and OTP codes are masked unless the grant
explicitly allowed them. Nothing here can send a text.

Hermetic: no live network, temp DB + Fernet key dir, fake stream/download.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


class _TestCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()  # full-suite isolation; see test_feed_registries setUpModule
        self.tmp = tempfile.mkdtemp(prefix='think-tank-dl-sms-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        self.key = serve.get_or_create_agent_key('ben')
        self.headers = {'X-Agent-Key': self.key}

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _as_player(self):
        return unittest.mock.patch.object(serve, '_require_player_session', return_value=True)


class SanitizeFilename(_TestCase):
    def test_strips_traversal_and_control(self):
        self.assertEqual(serve._sanitize_filename('../../etc/passwd'), 'passwd')
        self.assertEqual(serve._sanitize_filename('../evil.sh'), 'evil.sh')
        self.assertEqual(serve._sanitize_filename('a b<c>.txt'), 'a_b_c_.txt')
        self.assertEqual(serve._sanitize_filename(''), 'download.bin')
        self.assertEqual(serve._sanitize_filename('..'), 'download.bin')
        self.assertEqual(serve._sanitize_filename('.hidden'), 'file_hidden')


class DownloadStream(_TestCase):
    def _fake_resp(self, payload=b'x' * 4096, final='https://example.com/f.bin', status=200):
        resp = unittest.mock.Mock()
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        resp.geturl.return_value = final
        resp.status = status

        def _read(n):
            if _read.left >= len(payload):
                return b''
            chunk = payload[_read.left:_read.left + n]
            _read.left += len(chunk)
            return chunk
        _read.left = 0
        resp.read.side_effect = _read
        return resp

    def test_streams_to_file_and_returns_meta(self):
        payload = b'hello world' * 100
        with unittest.mock.patch.object(serve, '_safe_urlopen', return_value=self._fake_resp(payload)):
            dest = os.path.join(self.tmp, 'out.bin')
            res = serve._download_stream_sync('https://example.com/f.bin', {}, dest, 1024 * 1024)
        self.assertEqual(res['bytes'], len(payload))
        self.assertEqual(res['status'], 200)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), payload)

    def test_cap_aborts_and_deletes_partial_file(self):
        with unittest.mock.patch.object(serve, '_safe_urlopen',
                                        return_value=self._fake_resp(b'z' * 2048)):
            dest = os.path.join(self.tmp, 'out.bin')
            with self.assertRaises(ValueError):
                serve._download_stream_sync('https://example.com/f.bin', {}, dest, 1024)
        self.assertFalse(os.path.exists(dest))


class DownloadEndpoint(_TestCase):
    def test_rejects_non_https(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/download', json={'agentId': 'ben', 'url': 'http://example.com/f.bin'},
                   headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('public https', r.json()['error'])

    def test_writes_sanitized_file_into_agent_downloads(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)

        def fake_dl(url, headers, dest_path, max_bytes):
            with open(dest_path, 'wb') as f:
                f.write(b'payload')
            return {'status': 200, 'bytes': 7, 'finalUrl': url}

        with unittest.mock.patch.object(serve, '_download_stream_sync', side_effect=fake_dl):
            r = c.post('/api/download', json={'agentId': 'ben', 'url': 'https://example.com/f.bin',
                                              'filename': '../../evil.sh'}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['path'], 'downloads/evil.sh')
        self.assertEqual(body['bytes'], 7)
        dest = os.path.join(self.tmp, 'agents', 'ben', 'downloads', 'evil.sh')
        self.assertTrue(os.path.exists(dest))

    def test_capability_handle_scope_is_enforced(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        serve._store_credential('gh', 'github', 'secret-1', status='active')
        ok_h, _ = serve.mint_capability_handle('ben', 'gh', 'download', ['example.com'], ['GET'], 'player', 600)
        bad_h, _ = serve.mint_capability_handle('ben', 'gh', 'download', ['api.github.com'], ['GET'], 'player', 600)
        calls = []
        with unittest.mock.patch.object(serve, '_download_stream_sync', side_effect=lambda *a, **k: calls.append(a) or {
                'status': 200, 'bytes': 3, 'finalUrl': 'https://example.com/x'}):
            r = c.post('/api/download', json={'agentId': 'ben', 'url': 'https://example.com/data.csv',
                                              'capabilityHandle': bad_h}, headers=self.headers)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertEqual(calls, [])
            r = c.post('/api/download', json={'agentId': 'ben', 'url': 'https://example.com/data.csv',
                                              'capabilityHandle': ok_h}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(calls), 1)
        _, headers, _, _ = calls[0]
        self.assertEqual(headers.get('Authorization'), 'Bearer secret-1')

    def test_requires_agent_key(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/download', json={'agentId': 'ben', 'url': 'https://example.com/f.bin'})
        self.assertEqual(r.status_code, 401, r.text)


class SmsDetection(_TestCase):
    def test_otp_detection(self):
        self.assertEqual(serve._sms_detect_otp('Your code is 123456.'), '123456')
        self.assertEqual(serve._sms_detect_otp('Voucher 482913 expires soon'), '482913')
        self.assertEqual(serve._sms_detect_otp('no code here'), None)
        self.assertEqual(serve._sms_detect_otp('room 1234 has 7 seats'), '1234')


class SmsInbound(_TestCase):
    def setUp(self):
        super().setUp()
        self.env = unittest.mock.patch.object(serve, '_load_env',
                                              return_value={'SMS_FORWARD_SECRET': 'test-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_unconfigured_returns_501(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with unittest.mock.patch.object(serve, '_load_env', return_value={}):
            r = c.post('/api/sms/inbound', json={'from': '555', 'body': 'hi'},
                       headers={'X-SMS-Forward-Secret': 'x'})
        self.assertEqual(r.status_code, 501, r.text)

    def test_wrong_secret_returns_401(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/sms/inbound', json={'from': '555', 'body': 'hi'},
                   headers={'X-SMS-Forward-Secret': 'wrong'})
        self.assertEqual(r.status_code, 401, r.text)

    def test_valid_inbound_stores_otp(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/sms/inbound', json={'from': '555-1234', 'body': 'Your code is 987654.'},
                   headers={'X-SMS-Forward-Secret': 'test-secret'})
        self.assertEqual(r.status_code, 200, r.text)
        with serve._db() as conn:
            row = conn.execute('SELECT sender, body, otp_code FROM sms_messages').fetchone()
        self.assertEqual(row[0], '555-1234')
        self.assertEqual(row[2], '987654')


class SmsInboxGate(_TestCase):
    def setUp(self):
        super().setUp()
        serve._sms_ingest('555-0001', 'Your code is 111111.')
        serve._sms_ingest('555-0002', 'Plain text with no code')

    def test_agent_key_cannot_list_inbox(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.get('/api/sms/inbox', headers=self.headers)
        self.assertEqual(r.status_code, 401, r.text)

    def test_player_sees_otp_masked_by_default(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with self._as_player():
            r = c.get('/api/sms/inbox')
        self.assertEqual(r.status_code, 200, r.text)
        msgs = r.json()['messages']
        masked = [m for m in msgs if m['id'] == 1][0]
        self.assertNotIn('111111', masked['body'])
        self.assertIsNone(masked['otpCode'])
        self.assertIn('*' * 6, masked['body'])
        with self._as_player():
            r = c.get('/api/sms/inbox?otp=1')
        shown = [m for m in r.json()['messages'] if m['id'] == 1][0]
        self.assertEqual(shown['otpCode'], '111111')


class SmsReadFlow(_TestCase):
    def setUp(self):
        super().setUp()
        self.mid, _ = serve._sms_ingest('555-0001', 'Your code is 333444.')
        serve._sms_ingest('555-0002', 'hello')

    def _grant(self, message_id, agent='ben', otp=False, ttl=300, as_player=True):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        with self._as_player() if as_player else unittest.mock.patch.object(serve, '_require_player_session', return_value=False):
            r = c.post('/api/sms/grant', json={'agentId': agent, 'messageId': message_id,
                                               'otpAllowed': otp, 'ttlSec': ttl}, headers=self.headers)
        return c, r

    def test_non_operator_cannot_grant(self):
        c, r = self._grant(self.mid, as_player=False)
        self.assertEqual(r.status_code, 403, r.text)

    def test_grant_and_read_masks_otp_by_default(self):
        from starlette.testclient import TestClient
        c, r = self._grant(self.mid)
        self.assertEqual(r.status_code, 200, r.text)
        gid = r.json()['grant']
        r = c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        msg = r.json()['message']
        self.assertTrue(msg['otpMasked'])
        self.assertNotIn('333444', msg['body'])
        self.assertNotIn('333444', json.dumps(r.json()))

    def test_grant_with_otp_allowed_reveals_code(self):
        from starlette.testclient import TestClient
        c, r = self._grant(self.mid, otp=True)
        gid = r.json()['grant']
        r = c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['message']['otpCode'], '333444')

    def test_grant_is_single_use(self):
        from starlette.testclient import TestClient
        c, r = self._grant(self.mid)
        gid = r.json()['grant']
        self.assertEqual(c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers).status_code, 200)
        r = c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers)
        self.assertEqual(r.status_code, 403, r.text)

    def test_wrong_agent_cannot_use_grant(self):
        from starlette.testclient import TestClient
        c, r = self._grant(self.mid, agent='zed')
        gid = r.json()['grant']
        r = c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers)
        self.assertEqual(r.status_code, 403, r.text)

    def test_expired_grant_refused(self):
        from starlette.testclient import TestClient
        c, r = self._grant(self.mid, ttl=60)
        gid = r.json()['grant']
        with serve._db() as conn:
            conn.execute('UPDATE sms_read_grants SET expires_at = ?', (serve.time.time() - 10,))
        r = c.post('/api/sms/read', json={'agentId': 'ben', 'grant': gid}, headers=self.headers)
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == '__main__':
    unittest.main()