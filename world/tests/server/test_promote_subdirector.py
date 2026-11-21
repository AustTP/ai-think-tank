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


class VillageCoherentSpanTests(unittest.TestCase):
    """Owner direction 2026-11-21: directors may span villages but must not
    hold direct reports in two villages. The handover takes one village
    cluster, the new subdirector is placed in that village, and a director
    of no more than two teams may self-serve the local-director promotion."""

    def _client_authed_as(self, state, actor):
        from starlette.testclient import TestClient
        c = TestClient(serve.app)
        # The cookie satisfies the deny-by-default auth middleware around
        # /api/intent; the MOCKED _operator_gate below is what decides the
        # acting identity (player or an agent).
        c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        p1 = unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state)
        p2 = unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=(True, actor))
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        return c

    def _mv_state(self):
        roster = [{'id': 'theo', 'isAdmin': True, 'villageId': 'main'},
                  {'id': 'dev', 'isDirector': True, 'villageId': 'main'}]
        for i in range(1, 7):
            roster.append({'id': f'w{i}', 'director': 'dev',
                           'villageId': 'north' if i >= 4 else 'main'})
        villages = [{'id': 'main', 'name': 'Main'},
                    {'id': 'north', 'name': 'North'}]
        agents = {r['id']: {'id': r['id'], 'busy': False,
                            'villageId': r.get('villageId', 'main')}
                  for r in roster}
        return {
            'agentRoster': roster, 'agents': agents,
            'villages': villages,
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
            'tasks': {}, 'workQueue': [], 'playerInbox': [],
        }

    def _plain_state(self):
        roster = [{'id': 'theo', 'isAdmin': True}, {'id': 'dev', 'isDirector': True}]
        for i in range(1, 7):
            roster.append({'id': f'w{i}', 'director': 'dev',
                           'villageId': 'main'})
        return {
            'agentRoster': roster,
            'agents': {r['id']: {'id': r['id'], 'busy': False,
                                 'villageId': 'main'} for r in roster},
            'villages': [{'id': 'main', 'name': 'Main'}],
            'teams': [{'id': 'dev', 'directorId': 'dev'}],
            'tasks': {}, 'workQueue': [], 'playerInbox': [],
        }

    def test_multi_village_handover_is_village_coherent(self):
        s = self._mv_state()
        # Promotee w1 lives in main; the north cluster (w4,w5,w6) moves
        # whole under w1, who is then placed in north with his crew.
        r = self._client_authed_as(s, 'player').post(
            '/api/intent/promote-subdirector', json={'agentId': 'w1',
                                                     'handOver': 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['handOver'], 3)
        self.assertEqual(body['placedVillage'], 'north')
        g = serve.governance
        # The north cluster moved whole: w1 holds exactly the north workers.
        self.assertEqual(sorted(g.direct_reports(s, 'w1')),
                         ['w4', 'w5', 'w6'])
        # dev's remaining direct reports are main-side only.
        self.assertEqual(sorted(g.direct_reports(s, 'dev')),
                         ['w1', 'w2', 'w3'])
        # The placed row is stamped in the roster too.
        row = next(d for d in s['agentRoster'] if d.get('id') == 'w1')
        self.assertEqual(row.get('villageId'), 'north')

    def test_single_village_handover_unchanged(self):
        s = self._mv_state()
        # Pull the north workers home first: dev's span is one village.
        for d in s['agents'].values():
            d['villageId'] = 'main'
        r = self._client_authed_as(s, 'player').post(
            '/api/intent/promote-subdirector', json={'agentId': 'w1',
                                                     'handOver': 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()['placedVillage'])
        g = serve.governance
        self.assertEqual(sorted(g.direct_reports(s, 'w1')), ['w2', 'w3'])

    def test_director_self_service_capped_at_two_teams(self):
        s = self._plain_state()
        c = self._client_authed_as(s, 'dev')
        # First self-service promotion: dev hands two workers to w1.
        r = c.post('/api/intent/promote-subdirector',
                   json={'agentId': 'w1', 'handOver': 2})
        self.assertEqual(r.status_code, 200, r.text)
        g = serve.governance
        self.assertEqual(sorted(g.direct_reports(s, 'w1')), ['w2', 'w3'])
        # Second self-service promotion: a director may stand over up to
        # TWO subdirector teams (one per village in the owner's model),
        # so this is still allowed.
        r2 = c.post('/api/intent/promote-subdirector',
                    json={'agentId': 'w4', 'handOver': 1})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(g.direct_reports(s, 'w4'), ['w5'])
        # A third subdirector team is player territory.
        r3 = c.post('/api/intent/promote-subdirector',
                    json={'agentId': 'w6', 'handOver': 1})
        self.assertEqual(r3.status_code, 403, r3.text)
        self.assertIn('at most two teams', r3.json()['error'])
        # Nothing moved on the refusal.
        self.assertIn('w6', g.direct_reports(s, 'dev'))
        self.assertEqual(g.direct_reports(s, 'w6'), [])
