"""Hermetic API tests for the Research Desk evidence + coverage endpoints.

These cover the HTTP surface the scheduled research executor and the operator/
Verifier use: POST/GET /api/evidence, /api/evidence/review, /api/evidence/
resolve, and POST/GET /api/coverage. The ledger functions themselves are
tested unit-style in test_evidence.py; these tests drive the endpoints against
a temp DB with the same session/agent-key auth real callers use, so no real
think_tank.db and no network is ever touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402


_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-evidence-api-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        THINK_TANK_DIR=_TMP_DIR,
    )
    _PATCHER.start()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _ApiTestCase(unittest.TestCase):
    """Per-test temp DB so tests never see each other's claims/coverage."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-evidence-api-db-')
        self._db_patch = unittest.mock.patch.object(
            serve, 'DB_PATH', os.path.join(self._tmp, 'test.db'))
        self._db_patch.start()
        serve.init_db()

    def tearDown(self):
        self._db_patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


def _agent_headers():
    key = serve.get_or_create_agent_key('testagent')
    return {'X-Agent-Key': key}


def _record(**over):
    rec = {
        'source_url': 'https://x.com/vendor/status/111',
        'resolved_url': 'https://x.com/vendor/status/111',
        'claim': 'The vendor announced X search.',
        'claim_type': 'official_announcement',
    }
    rec.update(over)
    return rec


class EvidenceEndpoints(_ApiTestCase):
    def test_record_and_list(self):
        c = _client()
        r = c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()},
                    headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body['recorded'])
        self.assertTrue(body['independent'])
        r = c.get('/api/evidence')
        self.assertEqual(r.status_code, 200)
        claims = r.json()['claims']
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['status'], 'needs_review')

    def test_repost_is_recorded_as_not_independent(self):
        c = _client()
        for _ in range(2):
            r = c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()},
                        headers=_agent_headers())
            self.assertTrue(r.json()['recorded'])
        r = c.get('/api/evidence')
        claims = r.json()['claims']
        self.assertEqual(len(claims), 2)
        independents = [cl['independent'] for cl in claims]
        self.assertEqual(sorted(independents), [False, True])
        self.assertTrue(any(cl['repost_of'] for cl in claims))

    def test_review_usable_as_written_clears_and_covers(self):
        c = _client()
        c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()},
               headers=_agent_headers())
        r = c.get('/api/evidence')
        claim_id = r.json()['claims'][0]['id']
        r = c.post('/api/evidence/review', json={
            'agentId': 'testagent', 'claimId': claim_id, 'decision': 'usable_as_written',
            'artifact': 'skills/vendor.md',
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        r = c.get('/api/evidence')
        self.assertEqual(r.json()['claims'][0]['status'], 'cleared')
        r = c.get('/api/coverage')
        covered = r.json()['coverage']
        self.assertEqual(len(covered), 1)
        self.assertEqual(covered[0]['status'], 'covered')
        self.assertEqual(covered[0]['artifact'], 'skills/vendor.md')

    def test_covered_identity_skips_re_report(self):
        c = _client()
        c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()},
               headers=_agent_headers())
        r = c.get('/api/evidence')
        claim_id = r.json()['claims'][0]['id']
        c.post('/api/evidence/review', json={
            'agentId': 'testagent', 'claimId': claim_id, 'decision': 'usable_as_written',
        }, headers=_agent_headers())
        r = c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()},
                    headers=_agent_headers())
        self.assertFalse(r.json()['recorded'])
        self.assertEqual(r.json()['reason'], 'covered')

    def test_resolve_endpoint_returns_landing_url_and_identity(self):
        c = _client()
        with unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_resolve_final_url',
                                        return_value=('https://realcompany.com/blog/launch', 2)):
            r = c.post('/api/evidence/resolve', json={'agentId': 'testagent',
                                                       'url': 'https://t.co/abc123'},
                        headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['finalUrl'], 'https://realcompany.com/blog/launch')
        self.assertTrue(body['resolved'])
        self.assertEqual(body['identity'], 'https://realcompany.com/blog/launch')

    def test_unauthorized_record_is_rejected(self):
        c = _client()
        r = c.post('/api/evidence', json={'agentId': 'testagent', 'record': _record()})
        self.assertEqual(r.status_code, 401)

    def test_invalid_record_is_rejected_with_reason(self):
        c = _client()
        r = c.post('/api/evidence', json={'agentId': 'testagent',
                                           'record': {'source_url': 'https://x.com/a'}},
                    headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['recorded'])
        self.assertEqual(r.json()['reason'], 'invalid')


class CoverageEndpoints(_ApiTestCase):
    def test_pending_review_is_not_covered(self):
        c = _client()
        r = c.post('/api/coverage', json={'agentId': 'testagent',
                                           'identity': 'https://x.com/vendor/status/111',
                                           'artifact': 'skills/vendor.md',
                                           'reviewed': False},
                    headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        r = c.get('/api/coverage')
        row = r.json()['coverage'][0]
        self.assertEqual(row['status'], 'pending_review')
        self.assertNotIn('covered_date', row)

    def test_failure_is_recorded_and_never_suppresses(self):
        c = _client()
        r = c.post('/api/coverage', json={'agentId': 'testagent',
                                           'identity': 'https://x.com/vendor/status/111',
                                           'failure': 'synthesis failed (no model tier)'},
                    headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        r = c.get('/api/coverage')
        row = r.json()['coverage'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertTrue(row['failures'])
        # A failed item is NOT covered: the next attempt can still run. A
        # later reviewed true can still cover it.
        r = c.post('/api/coverage', json={'agentId': 'testagent',
                                           'identity': 'https://x.com/vendor/status/111',
                                           'artifact': 'skills/vendor.md',
                                           'reviewed': True},
                    headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        r = c.get('/api/coverage')
        self.assertEqual(r.json()['coverage'][0]['status'], 'covered')


class PolicyEndpoints(_ApiTestCase):
    def test_policy_view_returns_loaded_config(self):
        c = _client()
        r = c.get('/api/policy')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreater(len(body['plain_writing']['banned']), 10)
        self.assertGreater(len(body['browse_policy']['block_categories']), 0)
        self.assertEqual([p['key'] for p in body['research_desk']['passes']],
                         ['announcements', 'practical_examples', 'limitations'])
        self.assertIsInstance(body['watchlist']['topics'], list)
        self.assertFalse(body['writeback_enabled'])  # not booted via lifespan

    def test_policy_reload_is_agent_key_gated(self):
        c = _client()
        r = c.post('/api/policy/reload', json={'agentId': 'testagent'})
        self.assertEqual(r.status_code, 401)
        r = c.post('/api/policy/reload', json={'agentId': 'testagent'},
                   headers=_agent_headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])

    def test_policy_reload_allows_player_session(self):
        c = _client()
        r = c.post('/api/policy/reload', json={'agentId': 'player'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])


if __name__ == '__main__':
    unittest.main()