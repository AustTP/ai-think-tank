"""Hermetic API tests for the Question Registry, Stakeholder Graph, and
Objective coverage-chain endpoints.

The ledger functions are tested unit-style in test_questions.py /
test_stakeholders.py; these drive the HTTP surface (serve part
335_question_registry.py) against a temp DB with the same session/agent-key
auth real callers use. The security property under test: the READ views
(brief, lists) are open, but every WRITE move (record, answer, contradict,
conflict, stakeholder merge, objective merge) requires the player session or
a valid agent key.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-questions-api-')
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
        self._tmp = tempfile.mkdtemp(prefix='think tank-questions-api-db-')
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


def _raw_client():
    from starlette.testclient import TestClient
    return TestClient(serve.app)


def _agent_headers():
    key = serve.get_or_create_agent_key('testagent')
    return {'X-Agent-Key': key, 'agentId': 'testagent'}


class QuestionEndpoints(_ApiTestCase):
    def test_record_answer_and_list_roundtrip(self):
        c = _client()
        r = c.post('/api/questions', json={
            'agentId': 'testagent',
            'text': 'What telemetry exposes prompt-injection abuse?',
            'objective': 'monitoring',
            'peopleWhoMayKnow': ['Brian Barrett'],
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        qid = r.json()['question']['id']
        self.assertEqual(r.json()['question']['status'], 'unknown')
        r = c.post('/api/questions/answer', json={
            'agentId': 'testagent',
            'questionId': qid,
            'answer': 'Raw findings land in the SIEM.',
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['question']['status'], 'answered')
        r = c.get('/api/questions')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['questions']), 1)
        r = c.get('/api/questions?status=unknown')
        self.assertEqual(r.json()['questions'], [])

    def test_record_requires_auth(self):
        r = _raw_client().post('/api/questions', json={'text': 'q?'})
        self.assertIn(r.status_code, (400, 401, 403))

    def test_brief_is_open_and_counts_only(self):
        c = _client()
        c.post('/api/questions', json={
            'agentId': 'testagent', 'text': 'Where does telemetry land?',
        }, headers=_agent_headers())
        r = _raw_client().get('/api/questions/brief')
        self.assertEqual(r.status_code, 401)
        r = c.get('/api/questions/brief')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('open_questions', body)
        self.assertIn('counts', body)
        self.assertIn('open', body['counts'])
        self.assertNotIn('confidence_percent', str(body))

    def test_answer_rejects_bad_status(self):
        c = _client()
        r = c.post('/api/questions', json={'text': 'q?'})
        qid = r.json()['question']['id']
        r = c.post('/api/questions/answer', json={'questionId': qid, 'answer': 'x', 'status': 'bogus'})
        self.assertEqual(r.status_code, 400)

    def test_contradiction_clears_answer_via_api(self):
        c = _client()
        qid = c.post('/api/questions', json={'text': 'Ingestion is the gap?'}).json()['question']['id']
        c.post('/api/questions/answer', json={'questionId': qid, 'answer': 'yes'})
        r = c.post('/api/questions/contradict', json={'questionId': qid, 'reason': 'reexamined'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['question']['status'], 'contradicted')
        self.assertIsNone(r.json()['question']['answer'])

    def test_conflict_links_old_to_new(self):
        c = _client()
        old_id = c.post('/api/questions', json={'text': 'How do we ingest the data?'}).json()['question']['id']
        r = c.post('/api/questions/conflict', json={
            'questionId': old_id,
            'newText': 'What content should we build from it?',
        })
        self.assertEqual(r.status_code, 200)
        new = r.json()['question']
        self.assertIn(old_id, new['related_question_ids'])
        r = c.get(f'/api/questions?status=contradicted')
        self.assertEqual(len(r.json()['questions']), 1)

    def test_conversation_brief_endpoint(self):
        c = _client()
        c.post('/api/questions', json={
            'text': 'Which events become SOC alerts?',
            'peopleWhoMayKnow': ['Brian Barrett'],
        })
        r = _raw_client().get('/api/conversation-brief?person=Brian Barrett')
        self.assertEqual(r.status_code, 401)
        r = c.get('/api/conversation-brief?person=Brian Barrett')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['person'], 'Brian Barrett')
        self.assertLessEqual(len(body['asks']), 5)
        r = c.get('/api/conversation-brief')
        self.assertEqual(r.status_code, 400)


class StakeholderEndpoints(_ApiTestCase):
    def test_record_merge_and_topic_lookup(self):
        c = _client()
        r = c.post('/api/stakeholders', json={
            'agentId': 'testagent', 'name': 'Brian Barrett',
            'team': 'CloudSec', 'capabilities': ['SIEM detections'],
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        r = c.post('/api/stakeholders', json={
            'agentId': 'testagent', 'name': 'Brian Barrett',
            'capabilities': ['Guardrail logging'],
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['person']['capabilities']), 2)
        r = c.get('/api/stakeholders?topic=siem')
        self.assertEqual([p['name'] for p in r.json()['persons']], ['Brian Barrett'])
        r = c.get('/api/stakeholders?topic=kubernetes')
        self.assertEqual(r.json()['persons'], [])

    def test_record_requires_auth(self):
        r = _raw_client().post('/api/stakeholders', json={'name': 'X'})
        self.assertIn(r.status_code, (400, 401, 403))

    def test_name_required(self):
        r = _client().post('/api/stakeholders', json={'name': '   '})
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_read_is_401(self):
        # Deny-by-default posture: no session, no key -> 401 before the
        # handler runs, same as every other /api route.
        for path in ('/api/questions', '/api/questions/brief',
                     '/api/stakeholders', '/api/objectives'):
            self.assertEqual(_raw_client().get(path).status_code, 401)


class ObjectiveEndpoints(_ApiTestCase):
    def test_record_and_computed_coverage(self):
        c = _client()
        r = c.post('/api/objectives', json={
            'agentId': 'testagent',
            'title': 'Monitor AI prompt-injection threats',
            'requirements': [
                {'text': 'Telemetry exposes injection attempts',
                 'evidence_claim_ids': ['ev-1'], 'owner': 'Brian Barrett'},
                {'text': 'SOC detections on guardrail events'},
            ],
        }, headers=_agent_headers())
        self.assertEqual(r.status_code, 200)
        cov = r.json()['coverage']
        self.assertFalse(cov['covered'])
        self.assertEqual(cov['covered_requirements'], 0)
        self.assertEqual(len(cov['gaps']), 2)
        self.assertEqual(cov['gaps'][0]['missing'],
                         ['implementation', 'validation'])

    def test_merge_updates_requirement_in_place(self):
        c = _client()
        c.post('/api/objectives', json={
            'title': 'Monitor AI threats',
            'requirements': [{'text': 'Telemetry available'}],
        })
        r = c.post('/api/objectives', json={
            'title': 'Monitor AI threats',
            'requirements': [{'text': 'Telemetry available',
                              'evidence_claim_ids': ['ev-9'],
                              'implementation': 'AIDR -> SIEM',
                              'validation': 'quarterly hunt',
                              'owner': 'CloudSec'}],
        })
        cov = r.json()['coverage']
        self.assertEqual(cov['total_requirements'], 1)
        self.assertTrue(cov['covered'])
        self.assertEqual(cov['gaps'], [])

    def test_unknown_objective_is_404(self):
        r = _raw_client().get('/api/objectives?id=obj-nonexistent')
        self.assertEqual(r.status_code, 401)
        r = _client().get('/api/objectives?id=obj-nonexistent')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.status_code, 404)

    def test_record_requires_auth(self):
        r = _raw_client().post('/api/objectives', json={'title': 'x'})
        self.assertIn(r.status_code, (400, 401, 403))


if __name__ == '__main__':
    unittest.main()
