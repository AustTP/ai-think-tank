"""Tank-owned account vault, TOTP code access, grants, rotation, deletion.

Hermetic: temp DB and Fernet key dir. No live network.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import totp  # noqa: E402


class _TankAccountCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()
        self.tmp = tempfile.mkdtemp(prefix='think-tank-accounts-')
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
        from starlette.testclient import TestClient
        self.client = TestClient(serve.app)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_director_cannot_request_code_or_rotate(self):
        rec = self._create()
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=(True, 'nora')):
            code = self.client.post(f"/api/tank-accounts/{rec['id']}/code",
                                    headers=self.headers)
            rotate = self.client.post(f"/api/tank-accounts/{rec['id']}/rotate",
                                      json={}, headers=self.headers)
        self.assertEqual(code.status_code, 403)
        self.assertEqual(rotate.status_code, 403)

    def test_rotation_revokes_outstanding_grants(self):
        rec = self._create()
        with self._owner():
            grant = self.client.post(f"/api/tank-accounts/{rec['id']}/grant",
                                     json={'agentId': 'ben'}, headers=self.headers).json()
            rotated = self.client.post(f"/api/tank-accounts/{rec['id']}/rotate",
                                       json={}, headers=self.headers)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        from tank_accounts import consume_grant
        self.assertFalse(consume_grant(grant['grant'], 'ben', rec['id']))

    def test_runtime_schema_creation_without_init_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, 'fresh.db')
            with unittest.mock.patch.object(serve, 'DB_PATH', db_path):
                serve._ensure_runtime_schemas()
                with serve._db() as conn:
                    names = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('tank_accounts', names)
            self.assertIn('captcha_requests', names)

    def _owner(self):
        return unittest.mock.patch.object(serve, '_operator_gate',
                                          return_value=(True, 'player'))

    def _create(self, **overrides):
        body = {
            'service': 'Example',
            'account': 'alice',
            'username': 'alice@example.com',
            'totpSecret': 'JBSWY3DPEHPK3PXP',
        }
        body.update(overrides)
        with self._owner():
            r = self.client.post('/api/tank-accounts', json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class Lifecycle(_TankAccountCase):
    def test_create_list_and_no_secret_leak(self):
        rec = self._create()
        self.assertEqual(rec['service'], 'Example')
        self.assertIn('provisioningUri', rec)
        self.assertEqual(len(rec['recoveryCodes']), 10)
        with self._owner():
            r = self.client.get('/api/tank-accounts', headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        accounts = r.json()['accounts']
        self.assertEqual(len(accounts), 1)
        blob = str(accounts)
        self.assertNotIn('JBSWY3DPEHPK3PXP', blob)
        self.assertNotIn('password', blob)
        self.assertNotIn('provisioningUri', blob)

    def test_create_from_otpauth_uri(self):
        rec = self._create(
            service='',
            account='',
            totpSecret=('otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP'
                        '&issuer=GitHub&digits=6&period=30'),
        )
        self.assertEqual(rec['service'], 'GitHub')
        self.assertEqual(rec['account'], 'alice')

    def test_secret_is_encrypted_at_rest(self):
        rec = self._create()
        with serve._db() as conn:
            enc = conn.execute(
                'SELECT encrypted_value FROM external_credentials WHERE name = ?',
                (f"tank_account:{rec['id']}",)).fetchone()[0]
        self.assertNotIn('JBSWY3DPEHPK3PXP', enc)
        self.assertNotIn('alice@example.com', enc)

    def test_code_and_agent_grant(self):
        rec = self._create()
        with self._owner():
            r = self.client.post(f"/api/tank-accounts/{rec['id']}/code",
                                 headers=self.headers)
            self.assertEqual(r.status_code, 200, r.text)
            code = r.json()['code']
            self.assertTrue(totp.verify('JBSWY3DPEHPK3PXP', code))
            g = self.client.post(f"/api/tank-accounts/{rec['id']}/grant",
                                 json={'agentId': 'ben', 'ttlSec': 120},
                                 headers=self.headers)
            self.assertEqual(g.status_code, 200, g.text)
            grant = g.json()['grant']
        r = self.client.post(f"/api/tank-accounts/{rec['id']}/agent-code",
                             json={'grant': grant}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(totp.verify('JBSWY3DPEHPK3PXP', r.json()['code']))
        # Single use.
        r = self.client.post(f"/api/tank-accounts/{rec['id']}/agent-code",
                             json={'grant': grant}, headers=self.headers)
        self.assertEqual(r.status_code, 403)

    def test_grant_is_bound_to_agent(self):
        rec = self._create()
        with self._owner():
            g = self.client.post(f"/api/tank-accounts/{rec['id']}/grant",
                                 json={'agentId': 'ben'}, headers=self.headers)
        other = serve.get_or_create_agent_key('ada')
        r = self.client.post(f"/api/tank-accounts/{rec['id']}/agent-code",
                             json={'grant': g.json()['grant']},
                             headers={'X-Agent-Key': other})
        self.assertEqual(r.status_code, 403)

    def test_rotate_changes_secret(self):
        rec = self._create()
        old = totp.parse_otpauth(rec['provisioningUri'])['secret']
        with self._owner():
            r = self.client.post(f"/api/tank-accounts/{rec['id']}/rotate",
                                 headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        new = totp.parse_otpauth(r.json()['provisioningUri'])['secret']
        self.assertNotEqual(old, new)
        with self._owner():
            code = self.client.post(f"/api/tank-accounts/{rec['id']}/code",
                                    headers=self.headers).json()['code']
        self.assertTrue(totp.verify(new, code))

    def test_delete_removes_vault_and_metadata(self):
        rec = self._create()
        with self._owner():
            r = self.client.delete(f"/api/tank-accounts/{rec['id']}",
                                   headers=self.headers)
            self.assertEqual(r.status_code, 200, r.text)
            r = self.client.get('/api/tank-accounts', headers=self.headers)
            self.assertEqual(r.json()['accounts'], [])
        self.assertIsNone(serve._credential_payload(f"tank_account:{rec['id']}"))


if __name__ == '__main__':
    unittest.main()
