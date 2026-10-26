"""Read-only workflow-template API."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='tt-wft-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP, 'test.db'),
        THINK_TANK_DIR=_TMP,
        AGENTS_DIR=os.path.join(_TMP, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP, 'library'),
        PASSPORT_PATH=os.path.join(_TMP, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


class WorkflowTemplatesAPI(unittest.TestCase):
    def setUp(self):
        self.key = serve.get_or_create_agent_key('theo')
        serve.save_state_to_db({
            'agentRoster': [{'id': 'theo', 'isAdmin': True}],
            'agents': {'theo': {'id': 'theo'}},
            'tasks': {}, 'workQueue': [], 'backlogRequests': [],
            'workflowTemplates': {
                'wft-abc': {
                    'id': 'wft-abc', 'name': 'Cited brief',
                    'status': 'active', 'successCount': 2,
                    'crossTeamSuccess': 1, 'ownerTeamId': 'nora',
                    'steps': [{'role': 'deliverable', 'type': 'report'}],
                    'tokens': ['brief', 'research', 'sources'],
                },
            },
        })
        self.client = TestClient(serve.app)

    def test_requires_auth(self):
        self.assertEqual(self.client.get('/api/workflow-templates').status_code, 401)

    def test_lists_and_reads_one(self):
        r = self.client.get('/api/workflow-templates',
                            headers={'X-Agent-Key': self.key})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['active'], 1)
        self.assertEqual(body['templates'][0]['id'], 'wft-abc')

        one = self.client.get('/api/workflow-templates/wft-abc',
                              headers={'X-Agent-Key': self.key})
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()['name'], 'Cited brief')

    def test_unknown_is_404(self):
        r = self.client.get('/api/workflow-templates/nope',
                            headers={'X-Agent-Key': self.key})
        self.assertEqual(r.status_code, 404)
