"""Tests for the player Ops page and its two read-only endpoints."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pulse
import serve
import pulse_ui
from fastapi.testclient import TestClient


class PulseUi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pulse-ui-test-')
        self.patcher = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            verify_session=lambda *_: True,
            _require_player_session=lambda *_: True,
        )
        self.patcher.start()
        pulse._TABLES_READY = False
        pulse._HAS_WATCHES = None
        pulse._LAST_OBSERVE.clear()
        self.client = TestClient(serve.app)

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_is_the_ops_shell(self):
        r = self.client.get('/pulse')
        self.assertEqual(r.status_code, 200)
        self.assertIn('TANK OPS', r.text)
        self.assertIn('/api/pulse/overview', pulse_ui.PULSE_HTML)
        self.assertIn('/api/project-board', pulse_ui.PULSE_HTML)

    def test_overview_returns_watches_and_events(self):
        watch = pulse.create_watch('cora', 'Tankers', 'ais', 'tanker')
        pulse.observe('ais', 'vessel', {'ts': int(__import__('time').time() * 1000),
                                        'name': 'TANKER A'})
        r = self.client.get('/api/pulse/overview')
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(any(w['watchId'] == watch['watchId'] for w in data['watches']))
        self.assertTrue(data['events'])
        self.assertIn('PULSE BRIEF', data['brief'])

    def test_board_endpoint_is_read_only(self):
        state = {'tasks': {'t1': {'title': 'Open card', 'status': 'working',
                                  'assignedTo': 'cora', 'teamId': 'nora',
                                  'updatedAt': 1}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state):
            r = self.client.get('/api/project-board?status=open')
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data['tasks'][0]['taskId'], 't1')


if __name__ == '__main__':
    unittest.main()
