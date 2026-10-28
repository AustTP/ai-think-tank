"""The access-control lattice (025): org-chart level mapping, the policy
table, fail-closed behavior, and _operator_gate parity with its previous
semantics (player session, or any agent with direct reports)."""
import unittest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import serve
from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402

_ROSTER = {'agentRoster': [
    {'id': 'theo', 'role': 'Chief of Staff', 'director': None, 'isAdmin': True},
    {'id': 'nora', 'role': 'Supporting Director', 'director': None},
    {'id': 'ben', 'role': 'Supervisor', 'director': 'theo'},
    {'id': 'ada', 'role': 'Supervisor', 'director': 'theo'},
    {'id': 'clara', 'role': 'Assistant to Ben', 'director': 'ben'},
]}


class LevelMappingTests(unittest.TestCase):
    def test_chart_decides_level(self):
        self.assertEqual(serve._access_level_of('theo', _ROSTER), 'chief')
        self.assertEqual(serve._access_level_of('nora', _ROSTER), 'director')
        self.assertEqual(serve._access_level_of('ben', _ROSTER), 'supervisor')
        self.assertEqual(serve._access_level_of('clara', _ROSTER), 'assistant')
        self.assertEqual(serve._access_level_of('stranger', _ROSTER), 'external')
        self.assertEqual(serve._access_level_of(None, _ROSTER), 'external')
        self.assertEqual(serve._access_level_of('player', _ROSTER), 'player')


class AuthorizeTests(unittest.TestCase):
    def test_dominance(self):
        ok, lvl, need = serve._access_authorize('ben', 'exec', 'run', _ROSTER)
        self.assertTrue(ok)
        self.assertEqual((lvl, need), ('supervisor', 'supervisor'))
        self.assertFalse(serve._access_authorize('clara', 'exec', 'run', _ROSTER)[0])
        # player outranks every agent level
        self.assertTrue(serve._access_authorize('player', 'exec', 'run', _ROSTER)[0])

    def test_policy_table(self):
        self.assertTrue(serve._access_authorize('theo', 'policy', 'write_global', _ROSTER)[0])
        self.assertFalse(serve._access_authorize('nora', 'policy', 'write_global', _ROSTER)[0])
        self.assertTrue(serve._access_authorize('nora', 'policy', 'write_team', _ROSTER)[0])
        self.assertTrue(serve._access_authorize('theo', 'state_write', 'full', _ROSTER)[0] is False)

    def test_fail_closed(self):
        self.assertFalse(serve._access_authorize('theo', 'unknown', 'action', _ROSTER)[0])
        self.assertFalse(serve._access_authorize('stranger', 'exec', 'run', _ROSTER)[0])


class OperatorGateParityTests(unittest.TestCase):
    """_operator_gate must behave exactly as before the lattice rewiring:
    player session passes; agents with direct reports pass; plain assistants
    do not."""

    def _request(self):
        return unittest.mock.MagicMock(cookies={})

    def test_player_session_passes(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=True):
            self.assertEqual(serve._operator_gate(self._request()), (True, 'player'))

    def test_supervisor_with_reports_passes(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='ben'), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=_ROSTER):
            self.assertEqual(serve._operator_gate(self._request()), (True, 'ben'))

    def test_assistant_without_reports_refused(self):
        with unittest.mock.patch.object(serve, '_require_player_session', return_value=False), \
             unittest.mock.patch.object(serve, '_resolve_requester', return_value='clara'), \
             unittest.mock.patch.object(serve, 'get_state_from_db', return_value=_ROSTER):
            self.assertEqual(serve._operator_gate(self._request()), (False, None))


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
