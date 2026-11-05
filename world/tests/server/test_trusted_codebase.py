"""Tests for the trusted-codebase registry (187), the governed fetch with
the owner's size-approval gate (188), the X adapter's trust gate (185),
the director card scan (189), and product ownership transfer (190).

Hermetic: the registry and cache live in a temp dir, HTTP is mocked at
the HEAD/stream boundary, the escalation store is a temp file, and auth
uses a real agent-keys row so the login-middleware path is exercised.
Network is never touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None
_KEY = 'tcb-test-key'


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-tcb-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        ESCALATIONS_PATH=os.path.join(_TMP_DIR, 'escalations.json'),
        _TRUSTED_CODEBASE_PATH=os.path.join(_TMP_DIR, 'trusted_codebase.json'),
        _X_POOL_DIR=os.path.join(_TMP_DIR, 'x_scrape'),
        _X_POOL_DB=os.path.join(_TMP_DIR, 'x_scrape', 'accounts.db'),
        _CODEBASE_CACHE_DIR=os.path.join(_TMP_DIR, 'codebase_cache'),
        _x_tool_path=lambda: '/fake/twscrape',
        verify_session=lambda sess: False,
        check_rate_limit=lambda aid: True,
        _load_env=lambda: {'X_SESSION_AUTH_TOKEN': 'a' * 40,
                           'X_SESSION_CT0': 'c' * 160},
    )
    _PATCHER.start()
    serve.init_db()
    serve.seed_trusted_codebase()
    with serve._db() as conn:
        for agent, director in (('marcus', 'ben'), ('nora', 'theo'),
                                ('theo', None), ('clara', 'ben'),
                                ('ada', None)):
            conn.execute('INSERT INTO agent_keys (agent_id, secret_key, '
                         'created_at) VALUES (?, ?, ?)',
                         (agent, f'key-{agent}', 0.0))
            conn.execute('INSERT INTO roster_agent (id, seq, blob, '
                         'updated_at) VALUES (?, ?, ?, ?)',
                         (agent, 0, json.dumps(
                             {'id': agent, 'director': director,
                              'role': 'worker' if director else 'Chief of '
                                      'Staff'}), 0.0))


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


class TrustedCodebaseRegistry(unittest.TestCase):
    def test_new_endpoint_lists_seed_and_threshold(self):
        r = client().get('/api/trusted-codebase',
                         headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(any(s['source'] == 'vladkens/twscrape'
                            for s in body['sources']))
        self.assertEqual(body['autoApproveMb'],
                         serve.trusted_codebase_auto_mb())

    def test_deprecated_alias_still_works(self):
        r = client().get('/api/trusted-repos',
                         headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)

    def test_agent_adds_url_source(self):
        r = client().post('/api/trusted-codebase',
                          json={'source': 'https://example.com/tool/v1.tar.gz',
                                'kind': 'archive',
                                'purpose': 'reference fixture for desk work'},
                          headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['ok'])

    def test_non_github_source_shapes_accepted(self):
        r = client().post('/api/trusted-codebase',
                          json={'source': 'pypi/requests-tool',
                                'kind': 'pip-cli',
                                'purpose': 'http helper for scrapers'},
                          headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)

    def test_bad_source_rejected(self):
        r = client().post('/api/trusted-codebase',
                          json={'source': 'ftp://evil/x', 'purpose': 'x'},
                          headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 400)


class CodebaseFetchSizeGate(unittest.TestCase):
    def test_unknown_source_refused(self):
        r = client().post('/api/codebase/fetch',
                          json={'source': 'nobody/nothing', 'reason': 'x'},
                          headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('not on the trusted', r.json()['error'])

    def test_under_threshold_downloads_and_surfaces_size(self):
        # A 4-byte artifact under the default 100MB threshold.
        with unittest.mock.patch.object(serve, '_head_size', lambda u: 4), \
                unittest.mock.patch.object(serve, '_codebase_stream',
                    lambda url, path, cap: {'path': path, 'bytes': 4,
                                            'sha256': 'ab' * 32}):
            r = client().post('/api/codebase/fetch',
                              json={'source': 'vladkens/twscrape',
                                    'reason': 'tooling'},
                              headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['requiresOwnerApproval'])
        self.assertEqual(body['sizeBytes'], 4)
        self.assertEqual(body['sha256'], 'ab' * 32)

    def test_over_threshold_files_owner_escalation_with_size(self):
        big = 200 * 1024 * 1024
        with unittest.mock.patch.object(serve, '_head_size',
                                        lambda u: big), \
                unittest.mock.patch.object(serve,
                                           '_mail_oversize_approval',
                                           lambda *a, **k: None):
            r = client().post('/api/codebase/fetch',
                              json={'source': 'vladkens/twscrape',
                                    'reason': 'model weights included'},
                              headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertTrue(body['requiresOwnerApproval'])
        self.assertEqual(body['sizeMb'], 200.0)
        self.assertIn('escalationId', body)
        with open(serve.ESCALATIONS_PATH) as fh:
            store = json.load(fh)
        esc = store[body['escalationId']]
        self.assertEqual(esc['status'], 'open')
        self.assertIn('200', esc['question'])   # the size is in the ask
        self.assertIn('weights', esc['question'])  # the reason too

    def test_owner_approval_unblocks_retry(self):
        big = 150 * 1024 * 1024
        source = 'vladkens/twscrape'
        with open(serve.ESCALATIONS_PATH, 'w') as fh:
            json.dump({'esc-x': {
                'kind': 'codebase download approval', 'status': 'approved',
                'token': 't', 'ts': time.time(), 'resolvedAt': time.time(),
                'question': f'oversize codebase download: {source}',
                'requestedBy': 'marcus'}}, fh)
        with unittest.mock.patch.object(serve, '_head_size', lambda u: big), \
                unittest.mock.patch.object(serve, '_codebase_stream',
                    lambda url, path, cap: {'path': path, 'bytes': 1,
                                            'sha256': 'cd' * 32}):
            r = client().post('/api/codebase/fetch',
                              json={'source': source, 'reason': 'approved '
                                    'earlier'},
                              headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['ownerApproval'], 'approved-in-window')

    def test_x_adapter_refuses_untrusted_source(self):
        with unittest.mock.patch.object(serve, 'source_is_trusted',
                                        lambda s: False):
            r = client().get('/api/x/search', params={'query': 'q'},
                             headers={'X-Agent-Key': 'key-marcus'})
        self.assertEqual(r.status_code, 503)
        self.assertIn('not trusted', r.json()['error'])


class _RosterStub:
    def __init__(self, aid, director):
        self.id = aid
        self.director = director


class DirectorCardScan(unittest.TestCase):
    def _seed_state(self, tasks):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value={'tasks': tasks}):
            return serve._ops_cards(24, ['marcus', 'clara'])

    def test_director_sees_own_workers_only(self):
        r = client().get('/api/ops/cards', params={'hours': '24'},
                         headers={'X-Agent-Key': 'key-marcus'})
        # marcus is a worker, not a director: his span is empty (he has no
        # reports), and he must NOT see other teams.
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['workers'], [])

    def test_failure_rollup_groups_by_worker(self):
        rows = self._seed_state({
            't1': {'assignedTo': 'marcus', 'title': 'Fix login',
                   'status': 'failed', 'updatedAt': time.time() - 3600},
            't2': {'assignedTo': 'marcus', 'title': 'Write docs',
                   'status': 'queued', 'updatedAt': time.time() - 60},
            't3': {'assignedTo': 'ruth', 'title': 'Not my span',
                   'status': 'failed', 'updatedAt': time.time() - 60},
        })
        by_id = {w['workerId']: w for w in rows}
        self.assertEqual(by_id['marcus']['failedCards'][0]['id'], 't1')
        self.assertEqual(by_id['marcus']['openCards'], 1)
        self.assertNotIn('ruth', by_id)

    def test_blocked_commands_counted(self):
        now = time.time()
        with unittest.mock.patch.object(
                serve, 'get_state_from_db', return_value={'tasks': {}}), \
                unittest.mock.patch.object(serve, '_db') as db_ctx:
            conn = db_ctx.return_value.__enter__.return_value
            conn.execute.return_value.fetchall.return_value = [
                ('marcus', 'execute_classify', json.dumps(
                    {'decision': 'blocked', 'reason': 'jev: block'})),
                ('marcus', 'browse_failed', '{}'),
            ]
            rows = serve._ops_cards(24, ['marcus'])
        self.assertEqual(rows[0]['blockedCommands'], 1)
        self.assertEqual(rows[0]['failedActions'], 1)


class ProductOwnershipTransfer(unittest.TestCase):
    def _state(self, products):
        return {'products': products,
                'teams': [{'id': 'ben'}, {'id': 'ada'}],
                'pendingTransfers': {}}

    def test_request_by_non_owner_refused(self):
        st = self._state({'p1': {'name': 'Widget', 'teamId': 'ben'}})
        with unittest.mock.patch.object(serve, '_state_begin',
                                        return_value=st), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           lambda s: None):
            r = client().post('/api/products/transfer-request',
                              json={'productId': 'p1', 'toTeamId': 'ada'},
                              headers={'X-Agent-Key': 'key-nora'})
        self.assertEqual(r.status_code, 403)

    def test_full_handshake_flips_ownership(self):
        st = self._state({'p1': {'name': 'Widget', 'teamId': 'ben'}})
        saved = {}
        with unittest.mock.patch.object(serve, '_state_begin',
                                        return_value=st), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           lambda s: saved.update(s)):
            r1 = client().post('/api/products/transfer-request',
                               json={'productId': 'p1', 'toTeamId': 'ada',
                                     'note': 'ada has the datastore skill'},
                               headers={'X-Agent-Key': 'key-marcus'})
            self.assertEqual(r1.status_code, 200)
            r2 = client().post('/api/products/transfer-accept',
                               json={'productId': 'p1'},
                               headers={'X-Agent-Key': 'key-clara'})
        # clara reports to ben, NOT ada: she cannot accept for ada.
        self.assertEqual(r2.status_code, 403)

    def test_receiver_accepts_and_owner_flips(self):
        st = self._state({'p1': {'name': 'Widget', 'teamId': 'ben'}})
        with unittest.mock.patch.object(serve, '_state_begin',
                                        return_value=st), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           lambda s: None):
            # a ben-side agent proposes
            client().post('/api/products/transfer-request',
                          json={'productId': 'p1', 'toTeamId': 'ada'},
                          headers={'X-Agent-Key': 'key-marcus'})
            # an ada-side agent accepts (ada herself)
            r = client().post('/api/products/transfer-accept',
                              json={'productId': 'p1'},
                              headers={'X-Agent-Key': 'key-ada'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(st['products']['p1']['teamId'], 'ada')
        self.assertNotIn('p1', st.get('pendingTransfers') or {})

    def test_cancel_by_either_side(self):
        st = self._state({'p1': {'name': 'Widget', 'teamId': 'ben'}})
        with unittest.mock.patch.object(serve, '_state_begin',
                                        return_value=st), \
                unittest.mock.patch.object(serve, 'save_state_to_db',
                                           lambda s: None):
            client().post('/api/products/transfer-request',
                          json={'productId': 'p1', 'toTeamId': 'ada'},
                          headers={'X-Agent-Key': 'key-marcus'})
            r = client().post('/api/products/transfer-cancel',
                              json={'productId': 'p1'},
                              headers={'X-Agent-Key': 'key-ada'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('p1', st.get('pendingTransfers') or {})


if __name__ == '__main__':
    unittest.main()
