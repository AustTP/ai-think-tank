"""Tests for the steal-list modules: edge scrubbing (redact), dimension
queries + structural evaluator (epistemics extensions), agent-run replay,
Seer failure investigations, and the issue-engine sweep.

The epistemics/Seer/Engine tests patch the ledger seams and run against a
temp SQLite DB for action_log (same harness as the API tests), so no real
think_tank.db, no Postgres, and no network is ever touched.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.evidence as evidence  # noqa: E402
import core.epistemics as epistemics  # noqa: E402
import core.redact as redact  # noqa: E402
import core.agent_replay as agent_replay  # noqa: E402
import core.seer as seer  # noqa: E402
import core.issue_engine as issue_engine  # noqa: E402
import serve  # noqa: E402


def _patch_epistemic_stores():
    """In-memory epistemic + evidence ledgers with a recording index stub
    (query falls back to the blob scan when the index lookup misses)."""
    stores = {'epistemic': {}, 'evidence': {}, 'coverage': {},
              'index': []}
    patchers = [
        unittest.mock.patch('core.epistemics._ledger_read',
                            lambda: stores['epistemic']),
        unittest.mock.patch('core.epistemics._ledger_write',
                            lambda blob: (stores.__setitem__('epistemic', blob),
                                          stores['index'].append('w'))),
        unittest.mock.patch('core.evidence._evidence_read',
                            lambda: stores['evidence']),
        unittest.mock.patch('core.evidence._evidence_write',
                            lambda rows: stores.__setitem__('evidence', rows)),
        unittest.mock.patch('core.evidence._coverage_read',
                            lambda: stores['coverage']),
        unittest.mock.patch('core.evidence._coverage_write',
                            lambda rows: stores.__setitem__('coverage', rows)),
        unittest.mock.patch('core.evidence.append_audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return stores


class TempDbTestCase(unittest.TestCase):
    """A real temp SQLite DB for action_log reads (Seer/Engine/replay)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-steal-tests-')
        self._conn = sqlite3.connect(os.path.join(self._tmp, 'test.db'))
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS action_log ('
            ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
            ' ts REAL NOT NULL, agent_id TEXT, action TEXT,'
            ' details TEXT, authorized INTEGER DEFAULT 1)')
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def log(self, agent_id, action, details, age_s=0.0):
        self._conn.execute(
            'INSERT INTO action_log (ts, agent_id, action, details) '
            'VALUES (?, ?, ?, ?)',
            (time.time() - age_s, agent_id, action,
             details if isinstance(details, str) else
             __import__('json').dumps(details)))
        self._conn.commit()


class Redact(unittest.TestCase):
    def test_api_keys_and_bearers_scrubbed(self):
        text = ('use key sk-abc123def456ghi789jklmno and '
                'Bearer eyY9abcde12345.abcdefgh123456.xyz12345678 and AIzaSyD-1234567890abcdefghijklmnopqrstuvw')
        clean, kinds = redact.scrub(text)
        self.assertNotIn('sk-abc123', clean)
        self.assertNotIn('AIzaSyD', clean)
        self.assertIn('[REDACTED:api_key]', clean)
        self.assertIn('api_key', kinds)
        self.assertIn('google_api_key', kinds)

    def test_password_assignments_and_jwt(self):
        clean, kinds = redact.scrub('password=hunter2secret token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c')
        self.assertNotIn('hunter2secret', clean)
        self.assertIn('password', kinds)
        self.assertIn('jwt', kinds)

    def test_url_credentials(self):
        clean, kinds = redact.scrub('db at https://admin:hunter2@db.example.com/x')
        self.assertNotIn('hunter2@', clean)
        self.assertIn('url_credentials', kinds)

    def test_prose_untouched(self):
        text = ('The pipeline is audited per docs.example.com; contact '
                'ops@example.com or call 10.0.1.96 for the runbook.')
        clean, kinds = redact.scrub(text)
        self.assertEqual(clean, text)
        self.assertEqual(kinds, [])

    def test_idempotent(self):
        once, _ = redact.scrub('key sk-abcdefghijklmnopqrst')
        twice, kinds2 = redact.scrub(once)
        self.assertEqual(once, twice)
        self.assertEqual(kinds2, [])

    def test_evidence_ingestion_scrubs_claim_text(self):
        _patch_epistemic_stores()
        out = evidence.record_source_claim(
            {'source_url': 'https://docs.example.com/k',
             'claim': 'config uses sk-verysecretkey12345678 for auth'},
            actor='tester')
        self.assertTrue(out['recorded'])
        claim = evidence.get_claim(out['id'])
        self.assertNotIn('sk-verysecretkey12345678', claim['claim'])
        self.assertIn('[REDACTED:api_key]', claim['claim'])
        self.assertIn('api_key', claim.get('redacted') or [])


class EpistemicQueryAndEval(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def _seed(self):
        a = epistemics.record_unknown('Is the pipeline audited?', owner='mira',
                                      actor='mira')['id']
        b = epistemics.record_unknown('Is filter v2 deployed?', owner='june',
                                      actor='june')['id']
        return a, b

    def test_query_filters_by_kind_and_owner(self):
        a, b = self._seed()
        out = epistemics.query_items(kind='unknown')
        self.assertEqual(out['source'], 'scan')
        self.assertEqual(out['total'], 2)
        out = epistemics.query_items(owner='mira')
        self.assertEqual([v['id'] for v in out['items']], [a])
        out = epistemics.query_items(status='validated')
        self.assertEqual(out['total'], 0)

    def test_query_min_sources_and_contradiction_flag(self):
        a, _b = self._seed()
        c1 = evidence.record_source_claim(
            {'source_url': 'https://docs.example.com/a', 'claim': 'audited'},
            actor='t')['id']
        c2 = evidence.record_source_claim(
            {'source_url': 'https://telemetry.example.com/b', 'claim': 'seen'},
            actor='t')['id']
        epistemics.attach_evidence(a, c1, actor='mira')
        epistemics.attach_evidence(a, c2, actor='june')
        out = epistemics.query_items(min_sources=2)
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(out['items'][0]['id'], a)
        out = epistemics.query_items(has_open_contradiction=True)
        self.assertEqual(out['total'], 0)

    def test_evaluator_scores_sources_and_caps_contradictions(self):
        a, _b = self._seed()
        c1 = evidence.record_source_claim(
            {'source_url': 'https://docs.example.com/a', 'claim': 'audited',
             'claim_type': 'official_announcement'}, actor='t')['id']
        c2 = evidence.record_source_claim(
            {'source_url': 'https://telemetry.example.com/b', 'claim': 'seen',
             'claim_type': 'official_announcement'}, actor='t')['id']
        epistemics.attach_evidence(a, c1, actor='mira')
        epistemics.attach_evidence(a, c2, actor='june')
        # attach already advanced the item to evidenced; a strong evidenced
        # item with no validation gets the validation proposal instead.
        out = epistemics.evaluate_item(a)
        self.assertGreaterEqual(out['score'], 0.7)
        self.assertEqual(out['recommendation'], 'propose_validation')

    def test_evaluator_promotes_unresolved_after_dispute(self):
        a, _b = self._seed()
        c1 = evidence.record_source_claim(
            {'source_url': 'https://docs.example.com/a', 'claim': 'audited'},
            actor='t')['id']
        c2 = evidence.record_source_claim(
            {'source_url': 'https://telemetry.example.com/b', 'claim': 'seen'},
            actor='t')['id']
        epistemics.attach_evidence(a, c1, actor='mira')
        epistemics.attach_evidence(a, c2, actor='june')
        cx = epistemics.record_contradiction(a, c1, c2, actor='v')['id']
        epistemics.resolve_contradiction(
            cx, 'neither side is trustworthy', winner_claim_id=None,
            actor='v')
        self.assertEqual(epistemics.get_item(a)['status'], 'unresolved')
        out = epistemics.evaluate_item(a, apply=True, actor='eval')
        self.assertEqual(out['recommendation'], 'promote')
        self.assertTrue(out['applied'])
        self.assertEqual(epistemics.get_item(a)['status'], 'evidenced')
        self.assertTrue(any('evaluator promotion' in h['note']
                            for h in epistemics.get_item(a)['history']))

    def test_evaluator_never_validates_and_caps_assertions(self):
        item = epistemics.record_assertion('Pager auto-escalates',
                                           actor='june')['id']
        out = epistemics.evaluate_item(item, apply=True)
        self.assertLessEqual(out['score'], 0.2)
        self.assertEqual(out['recommendation'], 'flag_unsupported')
        self.assertEqual(epistemics.get_item(item)['status'], 'unknown',
                         'evaluator must never promote past the evidence gate')

    def test_evaluator_flags_contradiction(self):
        a, _b = self._seed()
        c1 = evidence.record_source_claim(
            {'source_url': 'https://docs.example.com/a', 'claim': 'yes'},
            actor='t')['id']
        c2 = evidence.record_source_claim(
            {'source_url': 'https://telemetry.example.com/b', 'claim': 'no'},
            actor='t')['id']
        epistemics.attach_evidence(a, c1, actor='mira')
        epistemics.attach_evidence(a, c2, actor='june')
        epistemics.record_contradiction(a, c1, c2, actor='v')
        out = epistemics.evaluate_item(a)
        self.assertEqual(out['recommendation'], 'resolve_contradiction')


class AgentReplay(TempDbTestCase):
    def test_frames_types_stats_and_errors(self):
        self.log('ada', 'browse', {'url': 'https://docs.example.com/a',
                                   'decision': 'allowed'})
        self.log('ada', 'browse_url_rejected',
                 {'url': 'https://x.example.com/', 'reason': 'not_discovered'},
                 age_s=60)
        self.log('ada', 'free_pool_fallback',
                 {'from': 'gemini', 'to': 'deepseek', 'reason': 'circuit_broken'},
                 age_s=120)
        self.log('dev', 'browse', {'url': 'https://other.example.com/'},
                 age_s=30)
        out = agent_replay.replay(self._conn, agent_id='ada')
        kinds = [f['kind'] for f in out['frames']]
        self.assertEqual(kinds.count('browse'), 2)
        self.assertEqual(out['stats']['frames'], 3)
        self.assertTrue(any('not_discovered' in n['summary']
                            for n in out['stats']['notable']))
        text = agent_replay.render_text(out)
        self.assertIn('[model]', text)
        self.assertIn('docs.example.com/a', text)

    def test_task_scope_and_malformed_rows(self):
        self.log('ada', 'claim_recorded',
                 '{"taskId": "task-1", "claimId": "ev-1"}')
        self.log('ada', 'weird_action', 'not json at all')
        out = agent_replay.replay(self._conn, task_id='task-1')
        self.assertEqual(len(out['frames']), 1)
        self.assertEqual(out['frames'][0]['kind'], 'evidence')
        self.assertEqual(out['frames'][0]['detail']['claimId'], 'ev-1')
        self.assertEqual(agent_replay.replay(self._conn),
                         {'error': 'agent_id or task_id is required'})


class Seer(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.stores = _patch_epistemic_stores()
        import core.questions as questions
        self._qpatch = unittest.mock.patch.multiple(
            'core.questions',
            _questions_read=lambda: {},
            _questions_write=lambda rows: None,
            _audit=lambda *a, **k: None)
        self._qpatch.start()

    def tearDown(self):
        self._qpatch.stop()
        unittest.mock.patch.stopall()
        super().tearDown()

    def test_investigation_classifies_and_records(self):
        self.log('sam', 'browse_url_rejected',
                 {'url': 'https://x/', 'reason': 'not_discovered'}, age_s=300)
        self.log('sam', 'task_completion_refused', {'reason': 'no evidence'})
        out = seer.investigate_failure('sam', 'quality', conn=self._conn,
                                       failure_count=1)
        self.assertTrue(out['investigated'])
        self.assertEqual(out['symptom'],
                         'complete work that fails review or verification')
        self.assertIn('source discovery', out['hypothesis'])
        self.assertTrue(out['precursors'])
        self.assertIsNone(out['remediationCard'],
                          'one failure must not file a card')

    def test_budget_failures_hypothesis(self):
        self.log('quinn', 'budget_denied', {'reason': 'spend cap exceeded'})
        out = seer.investigate_failure('quinn', 'infra', conn=self._conn,
                                       failure_count=1)
        self.assertIn('budget', out['hypothesis'])

    def test_sever_survives_db_errors(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError('db gone')
        out = seer.investigate_failure('sam', 'quality', conn=Boom())
        self.assertFalse(out['investigated'])
        self.assertEqual(out['reason'], 'seer_error')


class IssueEngine(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()
        super().tearDown()

    def _seed_cluster(self, symptom_detail, n=3, agent='sam'):
        for i in range(n):
            self.log(agent, 'task_requeued_stale', symptom_detail,
                     age_s=900 * (i + 1))

    def test_clusters_and_causes_dry_run(self):
        self._seed_cluster({'reason': 'work went stale after 2 timeouts'})
        self._seed_cluster({'reason': 'budget cap exceeded'}, n=2,
                           agent='quinn')
        out = issue_engine.sweep(self._conn, file_cards=False)
        self.assertTrue(out['swept'])
        self.assertGreaterEqual(len(out['clusters']), 2)
        stale = next(c for c in out['clusters'] if c['symptom'] == 'timeout')
        self.assertEqual(stale['size'], 3)
        self.assertTrue(stale['causes'])
        self.assertIsNone(stale['fixCard'])

    def test_small_clusters_skipped(self):
        self._seed_cluster({'reason': 'one-off oddity'}, n=1)
        out = issue_engine.sweep(self._conn, file_cards=False)
        self.assertEqual(out['clusters'], [])


if __name__ == '__main__':
    unittest.main()


class StealListApi(unittest.TestCase):
    """The 446 endpoints + extended tool actions against a temp DB with
    real session auth (same harness as test_epistemics_api)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-steal-api-')
        self._mod = unittest.mock.patch.multiple(
            serve, THINK_TANK_DIR=self._tmp,
            DB_PATH=os.path.join(self._tmp, 'test.db'))
        self._mod.start()
        serve.init_db()

    def tearDown(self):
        self._mod.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def test_query_replay_seer_engine_endpoints(self):
        epistemics.record_unknown('Is the pipeline audited?', owner='mira',
                                  actor='mira')
        c = self._client()
        r = c.post('/api/epistemics/query',
                   json={'kind': 'unknown', 'owner': 'mira'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['total'], 1)
        r = c.get('/api/replay/agent/mira')
        self.assertEqual(r.status_code, 200)
        self.assertIn('frames', r.json())
        r = c.post('/api/seer/investigate', json={'agentId': 'mira'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['investigated'])
        r = c.post('/api/issues/engine-sweep', json={'fileCards': False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['swept'])
        # unauthenticated
        from starlette.testclient import TestClient as TC
        anon = TC(serve.app)
        self.assertEqual(anon.get('/api/replay/agent/mira').status_code, 401)
        self.assertEqual(
            anon.post('/api/epistemics/query', json={}).status_code, 401)

    def test_tool_query_evaluate_replay_self_scope(self):
        item = epistemics.record_unknown('Is filter v2 deployed?',
                                         owner='mira', actor='mira')['id']
        out = serve._epistemics_tool('mira', {'action': 'query',
                                              'owner': 'mira'})
        self.assertIn('ek-', out)
        out = serve._epistemics_tool('mira', {'action': 'evaluate',
                                              'itemId': item})
        self.assertIn('Score', out)
        self.assertIn('self-scoped', serve._epistemics_tool(
            'june', {'action': 'replay', 'itemId': 'mira'}))
        out = serve._epistemics_tool('mira', {'action': 'replay'})
        self.assertIn('frames over', out)


class HarnessApi(unittest.TestCase):
    """The optimizer endpoints: operator-gated, page written through the
    sim seam, adoption decision recorded."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='think tank-harness-api-')
        self._mod = unittest.mock.patch.multiple(
            serve, THINK_TANK_DIR=self._tmp,
            DB_PATH=os.path.join(self._tmp, 'test.db'))
        self._mod.start()
        serve.init_db()
        self.stores = _patch_epistemic_stores()

    def tearDown(self):
        unittest.mock.patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _client(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        return c

    def test_optimize_requires_auth_and_reports_no_cluster(self):
        from starlette.testclient import TestClient as TC
        anon = TC(serve.app)
        self.assertEqual(
            anon.post('/api/harness/optimize', json={'apply': False}).status_code,
            401)
        c = self._client()
        r = c.post('/api/harness/optimize', json={'apply': False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['proposed'])
        r = c.post('/api/harness/measure', json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['measured'])
