"""The chief of staff's tank stop switch: /api/tank/stop, /api/tank/start,
/api/tank/status (2026-11-08, owner request).

Governed surface for "stop this tank / all of them": admin agent key only,
services restricted to the TANK_SERVICES allowlist, and the main tank answers
the stop request BEFORE the process dies so the caller sees the order land."""
import os
import sys
import unittest
from unittest import mock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve  # noqa: E402
import test_serve_helpers as _helpers  # noqa: E402


def setUpModule():
    _helpers.setUpModule()
    _helpers._AUTH_PATCHER = mock.patch.object(
        serve, '_valid_agent_key_presented', return_value=True)
    _helpers._AUTH_PATCHER.start()


def tearDownModule():
    _helpers._AUTH_PATCHER.stop()
    _helpers.tearDownModule()


_ADMIN_STATE = {'agentRoster': [{'id': 'theo', 'isAdmin': True}],
                'agents': {'theo': {'isAdmin': True}},
                'tasks': {}, 'workQueue': []}


class TankStatus(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)

    def test_status_lists_allowlisted_services_only(self):
        with mock.patch.object(serve, 'subprocess') as sp:
            sp.run.return_value = mock.Mock(
                stdout='PID\tStatus\tLabel\n'
                       '123\t-\tcom.austtp.ai-think-tank\n'
                       '456\t-\tcom.austtp.tank-fast\n'
                       '789\t-\tcom.example.unrelated\n',
                returncode=0)
            r = self.client.get('/api/tank/status')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body['services']), set(serve._TANK_SERVICES))
        self.assertTrue(body['services']['main']['running'])
        self.assertFalse(body['services']['read']['running'])
        self.assertNotIn('com.example.unrelated', r.text)


class TankStopAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)
        self._patches = mock.patch.multiple(
            serve,
            verify_agent_key=mock.Mock(return_value=True),
            get_state_from_db=mock.Mock(return_value=_ADMIN_STATE),
            log_action=mock.Mock(),
        )
        self._patches.start()

    def tearDown(self):
        self._patches.stop()

    def test_non_admin_agent_is_refused_403(self):
        state = {'agentRoster': [{'id': 'cora', 'isAdmin': False}],
                 'agents': {'cora': {}}, 'tasks': {}, 'workQueue': []}
        with mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             mock.patch.object(serve, 'subprocess') as sp:
            r = self.client.post('/api/tank/stop',
                                 json={'agentId': 'cora', 'scope': 'all'})
            self.assertEqual(r.status_code, 403)
            sp.run.assert_not_called()

    def test_bad_key_is_refused_401(self):
        with mock.patch.object(serve, 'verify_agent_key',
                               mock.Mock(return_value=False)), \
             mock.patch.object(serve, 'subprocess') as sp:
            r = self.client.post('/api/tank/stop',
                                 json={'agentId': 'imposter', 'scope': 'all'})
            self.assertEqual(r.status_code, 401)
            sp.run.assert_not_called()

    def test_unknown_service_name_is_refused(self):
        with mock.patch.object(serve, 'subprocess') as sp:
            r = self.client.post('/api/tank/stop',
                                 json={'agentId': 'theo',
                                       'services': ['com.example.unrelated']})
            self.assertEqual(r.status_code, 400)
            sp.run.assert_not_called()

    def test_stop_all_acknowledges_exactly_the_allowlist(self):
        with mock.patch.object(serve, 'subprocess') as sp:
            r = self.client.post('/api/tank/stop',
                                 json={'agentId': 'theo', 'scope': 'all'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body['stopping']),
                         set(serve._TANK_SERVICES.values()))
        self.assertEqual(body['requestedBy'], 'theo')

    def test_stop_single_named_service(self):
        with mock.patch.object(serve, 'subprocess') as sp:
            r = self.client.post('/api/tank/stop',
                                 json={'agentId': 'theo', 'services': ['read']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['stopping'],
                         ['com.austtp.ai-think-tank-read'])

    def test_bootout_runs_launchctl_with_the_gui_domain(self):
        # The timer-callback itself, without HTTP: launchctl bootout gets the
        # gui/<uid>/<label> form, allowlisted labels only.
        seen = {}

        def fake_run(argv, **k):
            # bootout's argv ends with gui/<uid>/<label>; take the label.
            label = argv[-1].rsplit('/', 1)[-1]
            seen[label] = argv
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(serve, 'subprocess') as sp:
            sp.run.side_effect = fake_run
            results = serve._tank_run_ctl(
                'bootout', sorted(serve._TANK_SERVICES.values()))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(set(seen), set(serve._TANK_SERVICES.values()))
        for label, argv in seen.items():
            self.assertEqual(argv[1], 'bootout')
            self.assertEqual(argv[2], f'gui/{os.getuid()}/{label}')


class TankStart(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(serve.app)
        self._patches = mock.patch.multiple(
            serve,
            verify_agent_key=mock.Mock(return_value=True),
            get_state_from_db=mock.Mock(return_value=_ADMIN_STATE),
            log_action=mock.Mock(),
        )
        self._patches.start()

    def tearDown(self):
        self._patches.stop()

    def test_start_bootstraps_the_plist_path(self):
        seen = []

        def fake_run(argv, **k):
            seen.append(argv)
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(serve, 'subprocess') as sp:
            sp.run.side_effect = fake_run
            r = self.client.post('/api/tank/start',
                                 json={'agentId': 'theo', 'services': ['read']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        self.assertEqual(argv[1], 'bootstrap')
        self.assertTrue(argv[2].startswith('gui/'))
        self.assertTrue(argv[3].endswith('com.austtp.ai-think-tank-read.plist'))

    def test_start_reports_launchctl_failures_per_label(self):
        with mock.patch.object(serve, 'subprocess') as sp:
            sp.run.return_value = mock.Mock(returncode=5, stdout='',
                                            stderr='bootstrap failed')
            r = self.client.post('/api/tank/start',
                                 json={'agentId': 'theo',
                                       'services': ['main', 'read']})
        self.assertEqual(r.status_code, 200)
        results = r.json()['started']
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]['ok'])
        self.assertIn('bootstrap failed', results[0]['detail'])


if __name__ == '__main__':
    unittest.main()
