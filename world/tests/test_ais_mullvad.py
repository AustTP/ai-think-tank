"""Mullvad account-auth + AISStream collector.

Mullvad: a new registry auth type ('mullvad') mints a short-lived bearer from
the account number via the JSON token endpoint and injects it as Bearer -- the
stock oauth2_client_credentials flow can't do this (Mullvad 415s the form grant
and wants JSON {account_number}, not a client_id/secret pair).

AISStream: WebSocket-only maritime feed the HTTP api_call path can never reach;
a collector buffers frames and /api/ais/recent + read_ais_feed serve the buffer.

Hermetic: no live network, no real DB writes (temp dir).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-ais-mullvad-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class MullvadAuth(unittest.TestCase):
    SPEC = {
        'id': 'mullvad',
        'name': 'Mullvad VPN (account)',
        'base_url': 'https://api.mullvad.net',
        'credential': {'type': 'env', 'key': 'MULLVAD_ACCOUNT_NUMBER'},
        'auth': {'type': 'mullvad', 'token_url': 'https://api.mullvad.net/auth/v1/token'},
        'methods': ['GET'],
        'path_rules': [{'prefix': '/accounts/v1/', 'methods': ['GET']}],
        'spend': {'kind': 'none'},
    }

    def test_spec_validates(self):
        sid, spec, err = serve._validate_api_service_spec(self.SPEC)
        self.assertIsNone(err)
        self.assertEqual(spec['auth']['token_url'], 'https://api.mullvad.net/auth/v1/token')

    def test_missing_token_url_rejected(self):
        bad = dict(self.SPEC, auth={'type': 'mullvad'})
        _, _, err = serve._validate_api_service_spec(bad)
        self.assertIn('token_url', err)

    def test_two_part_credential_rejected(self):
        bad = dict(self.SPEC, credential={'type': 'env', 'key': 'A', 'key2': 'B'})
        _, _, err = serve._validate_api_service_spec(bad)
        self.assertIn('single-part', err)

    def test_apply_auth_injects_bearer_from_minted_token(self):
        with unittest.mock.patch.object(serve, '_mullvad_access_token', return_value='mva_minted'):
            url, headers, body = serve._api_apply_auth(
                {'auth': {'type': 'mullvad', 'token_url': 'https://api.mullvad.net/auth/v1/token'}},
                '1234567890123456', None, 'GET', 'https://api.mullvad.net/x', {}, None)
        self.assertEqual(headers['Authorization'], 'Bearer mva_minted')

    def test_mint_caches_until_expiry(self):
        calls = {'n': 0}
        import datetime
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()

        def fake_mint(token_url, account_number):  # noqa: ARG001
            calls['n'] += 1
            return '{"access_token": "mva_x", "expiry": "%s"}' % future

        with unittest.mock.patch.object(serve, '_mullvad_token_request_sync', side_effect=fake_mint):
            a = serve._mullvad_access_token('https://api.mullvad.net/auth/v1/token', 'acct')
            b = serve._mullvad_access_token('https://api.mullvad.net/auth/v1/token', 'acct')
        self.assertEqual(a, 'mva_x')
        self.assertEqual(b, 'mva_x')
        self.assertEqual(calls['n'], 1)  # second call served from cache
        serve._MULLVAD_TOKEN_CACHE.clear()


class AISCollector(unittest.TestCase):
    def test_append_caps_buffer(self):
        serve._AIS_BUFFER.clear()
        for i in range(serve._AIS_BUFFER_MAX + 25):
            serve._ais_append_message({'MessageType': 'PositionReport', 'i': i})
        with serve._AIS_BUFFER_LOCK:
            self.assertEqual(len(serve._AIS_BUFFER), serve._AIS_BUFFER_MAX)
            self.assertEqual(serve._AIS_BUFFER[0]['message']['i'], 25)
        serve._AIS_BUFFER.clear()

    def test_recent_filters_window_and_max(self):
        serve._AIS_BUFFER.clear()
        now_ms = int(time.time() * 1000)
        old = {'ts': now_ms - 3600_000, 'message': {'MessageType': 'old'}}
        fresh = [{'ts': now_ms - i * 1000, 'message': {'MessageType': f'fresh{i}'}} for i in range(5)]
        serve._AIS_BUFFER.extend([old] + fresh)
        out = serve._ais_recent(max_msgs=2, window_s=1800)
        self.assertEqual([m['message']['MessageType'] for m in out], ['fresh3', 'fresh4'])
        # callers can't mutate the buffer through the returned copies
        out[0]['message']['MessageType'] = 'mutated'
        with serve._AIS_BUFFER_LOCK:
            self.assertEqual(serve._AIS_BUFFER[1]['message']['MessageType'], 'fresh0')
        serve._AIS_BUFFER.clear()

    def test_recent_endpoint_requires_auth_and_returns_frames(self):
        from starlette.testclient import TestClient
        serve._AIS_BUFFER.clear()
        serve._AIS_BUFFER.append({'ts': int(time.time() * 1000), 'message': {'MessageType': 'PositionReport', 'MetaData': {'ShipName': 'EVER GIVEN'}}})
        c = TestClient(serve.app)
        r = c.get('/api/ais/recent')
        self.assertEqual(r.status_code, 401)
        key = serve.get_or_create_agent_key('ben')
        r = c.get('/api/ais/recent?agentId=ben&max=5', headers={'X-Agent-Key': key})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(len(body['frames']), 1)
        self.assertEqual(body['frames'][0]['message']['MetaData']['ShipName'], 'EVER GIVEN')
        serve._AIS_BUFFER.clear()


if __name__ == '__main__':
    unittest.main()