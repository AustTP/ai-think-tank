"""API tests for the epistemic ledger surface (serve part 445).

The ledger functions themselves are tested unit-style in
test_epistemics.py; these drive the HTTP endpoints and the agent ask tool
against a temp DB with real session auth, so no real think_tank.db and no
network is touched. Same harness as test_evidence_api.py.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-epistemics-api-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        THINK_TANK_DIR=_TMP_DIR,
    )
    _PATCHER.start()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _ApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-epistemics-api-db-')
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


class SnapshotEndpoint(_ApiTestCase):
    def test_requires_auth(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.get('/api/epistemics')
        self.assertEqual(r.status_code, 401)
        r = c.get('/api/epistemics/brief')
        self.assertEqual(r.status_code, 401)

    def test_snapshot_and_brief_roundtrip(self):
        out = serve._epistemics_tool('mira', {
            'action': 'unknown',
            'text': 'Does the alert reach the incident-response rota?'})
        self.assertIn('ek-', out)
        c = _client()
        r = c.get('/api/epistemics')
        self.assertEqual(r.status_code, 200)
        snap = r.json()
        self.assertTrue(any(v['text'].startswith('Does the alert')
                            for v in snap['unknowns']))
        r = c.get('/api/epistemics/brief')
        self.assertEqual(r.status_code, 200)
        self.assertIn('EPISTEMIC STATE', r.json()['brief'])


class WriteEndpoint(_ApiTestCase):
    def test_write_via_http_records_and_audits(self):
        c = _client()
        r = c.post('/api/epistemics', json={
            'action': 'unknown',
            'text': 'Whether blocked injections reach the IR rota'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['action'], 'unknown')
        self.assertIn('recorded', body['result'])
        with serve._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE action = "
                "'epistemics_write'").fetchone()
        self.assertGreaterEqual(int(row[0]), 1)

    def test_unauthorized_write_refused(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/epistemics', json={'action': 'unknown',
                                            'text': 'x'})
        self.assertEqual(r.status_code, 401)


class AgentTool(_ApiTestCase):
    def _seed_item(self, text='Is the pipeline audited?'):
        return serve._epistemics_tool('mira', {'action': 'unknown',
                                               'text': text})

    def test_full_lifecycle_through_the_tool(self):
        import json
        item = json.loads(self._seed_item())['id']
        claim = serve._epistemics_tool('mira', {
            'action': 'evidence', 'itemId': item,
            'sourceUrl': 'https://docs.example.com/audit',
            'claimText': 'The pipeline is audited'})
        self.assertIn('independent source(s)', claim)
        self.assertIn('evidenced', claim)
        val = serve._epistemics_tool('mira', {
            'action': 'validate', 'itemId': item,
            'procedure': 'Cross-check dashboards',
            'estimatedCostUsd': 0.2, 'worthIt': True})
        self.assertIn('proposed', val)
        validation_id = val.split()[1]
        result = serve._epistemics_tool('mira', {
            'action': 'result', 'validationId': validation_id,
            'result': 'Dashboards match the seeded prompt',
            'sourceUrl': 'https://siem.example.com/q',
            'claimText': 'Audit index ingests seeded prompts'})
        self.assertIn('awaiting review', result)
        # mira completed it; mira cannot review it
        self.assertIn('cannot review', serve._epistemics_tool('mira', {
            'action': 'review', 'validationId': validation_id,
            'approve': True}))
        approved = serve._epistemics_tool('verifier', {
            'action': 'review', 'validationId': validation_id,
            'approve': True, 'note': 'evidence checks out'})
        self.assertIn('VALIDATED', approved)

    def test_status_reports_conflicts_and_assertions(self):
        self._seed_item('Is filter v2 deployed?')
        serve._epistemics_tool('june', {
            'action': 'hypothesize',
            'text': 'Coverage gaps live in the tool-execution hop'})
        out = serve._epistemics_tool('mira', {'action': 'status'})
        self.assertIn('EPISTEMIC STATE', out)
        self.assertIn('Asserted, NOT established', out)

    def test_bank_exhaustion_blocks_paid_start(self):
        import json
        item = json.loads(self._seed_item('Rota sees blocked attempts?'))['id']
        val = serve._epistemics_tool('mira', {
            'action': 'validate', 'itemId': item,
            'procedure': 'Trace one blocked attempt',
            'estimatedCostUsd': 5.0, 'worthIt': True})
        validation_id = val.split()[1]
        with unittest.mock.patch.object(
                serve, '_think_tank_spend_cap_exceeded', return_value=True):
            out = serve._epistemics_tool('mira', {
                'action': 'result', 'validationId': validation_id,
                'result': 'x', 'sourceUrl': 'https://siem.example.com/q'})
        self.assertIn('budget is exhausted', out)
        v = serve._epistemics_tool('mira', {'action': 'status'})
        self.assertIn('Blocked validation', v)

    def test_contradiction_blocks_silent_overwrite(self):
        import json
        item = json.loads(self._seed_item('Is filter v2 in prod?'))['id']
        serve._epistemics_tool('mira', {
            'action': 'evidence', 'itemId': item,
            'sourceUrl': 'https://docs.example.com/filter',
            'claimText': 'v2 is deployed'})
        # the doc-side attach already advanced the item; attach the
        # telemetry side and open the contradiction through the tool
        serve._epistemics_tool('june', {
            'action': 'evidence', 'itemId': item,
            'sourceUrl': 'https://telemetry.example.com/c',
            'claimText': 'v2 signature absent in prod'})
        import core.epistemics as epistemics
        snap = epistemics.epistemic_snapshot()
        sources = [s['claimId'] for s in
                   (snap['asserted'] + snap['established'] +
                    snap['unknowns'] + snap['disputed'])
                   if s['id'] == item for s in s['sources']]
        self.assertEqual(len(sources), 2)
        cx = serve._epistemics_tool('verifier', {
            'action': 'contradict', 'itemId': item,
            'claimIdA': sources[0], 'claimIdB': sources[1],
            'note': 'docs vs config'})
        self.assertIn('open', cx)
        contradiction_id = cx.split()[1]
        bad = serve._epistemics_tool('theo', {
            'action': 'resolve', 'contradictionId': contradiction_id,
            'resolutionText': 'telemetry wins',
            'winnerClaimId': 'ev-madeup'})
        self.assertIn('winner must be one of', bad)
        good = serve._epistemics_tool('theo', {
            'action': 'resolve', 'contradictionId': contradiction_id,
            'resolutionText': 'config verified on host; doc is stale',
            'winnerClaimId': sources[1]})
        self.assertIn('Resolved', good)


if __name__ == '__main__':
    unittest.main()
