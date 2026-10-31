"""Promote-subdirector endpoint (scale slice 4): POST /api/intent/promote-subdirector.

Promotion is structural: hand over workers, the agent becomes emergent middle
management, and the 3+ rule rebalances at the crossing. The layer ceiling
(SUBDIRECTOR_LAYER_MAX = 6) refuses a seventh promotion with guidance to
spawn a new team instead.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-promote-sub-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _state(subs=0, workers=6):
    roster = [{'id': 'theo', 'isAdmin': True}, {'id': 'dev', 'isDirector': True}]
    for i in range(1, subs + 1):
        roster.append({'id': f'sd{i}', 'director': 'dev'})
        roster.append({'id': f's{i}w1', 'director': f'sd{i}'})
    for i in range(1, workers + 1):
        roster.append({'id': f'w{i}', 'director': 'dev'})
    return {
        'agentRoster': roster,
        'agents': {r['id']: {'id': r['id'], 'busy': False} for r in roster},
        'teams': [{'id': 'dev', 'directorId': 'dev'}],
        'tasks': {}, 'workQueue': [], 'playerInbox': [],
    }


class PromoteSubdirectorEndpoint(unittest.TestCase):
    def _client(self, state):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        patcher = unittest.mock.patch.object(serve, 'get_state_from_db',
                                             return_value=state)
        patcher.start()
        self.addCleanup(patcher.stop)
        return c

    def _promote(self, state, agent_id, hand_over=None):
        body = {'agentId': agent_id}
        if hand_over is not None:
            body['handOver'] = hand_over
        return self._client(state).post(
            '/api/intent/promote-subdirector', json=body)

    def test_requires_operator_auth(self):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        r = c.post('/api/intent/promote-subdirector',
                   json={'agentId': 'w1'})
        self.assertEqual(r.status_code, 401)

    def test_first_promotion_hands_over_workers(self):
        s = _state()
        r = self._promote(s, 'w1', hand_over=2)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['handOver'], 2)
        self.assertFalse(body['layerActive'])
        g = serve.governance
        self.assertEqual(g.direct_reports(s, 'w1'),
                         [r['id'] for r in ()] or ['w2', 'w3'])
        self.assertEqual(g.subdirectors_of(s, 'dev'), ['w1'])

    def test_third_promotion_crosses_and_rebalances(self):
        s = _state()
        g = serve.governance
        # handOver=1: three promotions from six workers, each subdirector
        # ending with exactly one report.
        for i in (1, 3, 5):
            r = self._promote(s, f'w{i}', hand_over=1)
            self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(g.has_subdirector_layer(s, 'dev'))
        # No direct workers remain on dev: the director directs only the
        # subdirector layer now.
        self.assertEqual(g.direct_reports(s, 'dev'), ['w1', 'w3', 'w5'])
        self.assertEqual(
            sum(len(g.direct_reports(s, f'w{i}')) for i in (1, 3, 5)), 3)

    def test_seventh_promotion_refused_with_team_spawn_guidance(self):
        s = _state(subs=6, workers=2)
        r = self._promote(s, 'w1')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('spawn a new team', r.json()['error'])
        g = serve.governance
        # Nothing moved: w1 still reports to dev, still no reports of their own.
        self.assertIn('w1', g.direct_reports(s, 'dev'))
        self.assertEqual(g.direct_reports(s, 'w1'), [])

    def test_sixth_promotion_still_allowed(self):
        s = _state(subs=5, workers=3)
        r = self._promote(s, 'w1')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['layerActive'])

    def test_agent_not_found_and_no_director(self):
        s = _state()
        self.assertEqual(self._promote(s, 'ghost').status_code, 404)
        # A top-level director has no one to promote under.
        s2 = _state()
        s2['agentRoster'].append({'id': 'lonely', 'isDirector': True})
        self.assertEqual(self._promote(s2, 'lonely').status_code, 400)
        # Promoting under the chief of staff is refused.
        s3 = _state()
        s3['agentRoster'].append({'id': 'aide', 'director': 'theo'})
        self.assertEqual(self._promote(s3, 'aide').status_code, 400)


if __name__ == '__main__':
    unittest.main()
