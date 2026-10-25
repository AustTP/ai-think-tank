"""The player plan approval gate for large requests."""
import os
import sys
import tempfile
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from test_serve_helpers import setUpModule, tearDownModule  # noqa: E402,F401


def _state():
    roster = [{'id': 'faye', 'name': 'Faye', 'isAdmin': True},
              {'id': 'nora', 'name': 'Nora', 'isDirector': True},
              {'id': 'zara', 'name': 'Zara'}]
    return {
        'agentRoster': roster,
        'agents': {d['id']: {'id': d['id'], 'name': d['name'],
                             'busy': False, 'offDuty': False} for d in roster},
        'teams': [{'id': 'nora', 'name': "Nora's Crew", 'directorId': 'nora',
                   'scrumMasterId': 'zara'}],
        'sprints': {},
        'workQueue': [],
    }


def _patches(state=None, authed=True, actor='player'):
    state = state or _state()
    saved = {}
    patches = [
        unittest.mock.patch.object(serve, '_operator_gate',
                                   return_value=(authed, actor)),
        unittest.mock.patch.object(serve, 'verify_session', return_value=authed),
        unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state),
        unittest.mock.patch.object(serve, 'save_state_to_db',
                                   side_effect=lambda s: saved.update(s)),
        unittest.mock.patch.object(serve, 'log_action'),
        unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint',
                                   return_value=False),
    ]
    return patches, saved, state


class PlanApprovalApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._db = unittest.mock.patch.dict(
            os.environ, {'THINK_TANK_DB': os.path.join(self._tmp.name, 'plans.db')})
        self._db.start()
        self.addCleanup(self._db.stop)
        self.client = TestClient(serve.app)

    def _propose(self, **body):
        body.setdefault('goal', 'build a thing')
        return self.client.post('/api/intent/plan-big-task', json=body)

    def test_propose_and_list_and_detail(self):
        patches, _, _ = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        r = self._propose()
        self.assertEqual(r.status_code, 200, r.text)
        plan = r.json()['plan']
        self.assertEqual(plan['route'], 'staffed')
        self.assertEqual(plan['payload']['team'], 'nora')
        listing = self.client.get('/api/plans')
        self.assertEqual(len(listing.json()['plans']), 1)
        detail = self.client.get(f"/api/plans/{plan['planId']}")
        self.assertEqual(len(detail.json()['revisions']), 1)

    def test_approve_staffed_binds_and_applies(self):
        patches, saved, state = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        plan = self._propose().json()['plan']
        with unittest.mock.patch.object(sim, 'file_large_request',
                                        return_value={'id': 'wrq-1'}) as filed:
            r = self.client.post(f"/api/plans/{plan['planId']}/approve",
                                 json={'version': 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['staffed'])
        self.assertEqual(r.json()['plan']['status'], 'applied')
        filed.assert_called_once()
        self.assertEqual(r.json()['request'], 'wrq-1')

    def test_reject_then_revise_creates_new_version(self):
        patches, _, _ = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        plan = self._propose().json()['plan']
        rejected = self.client.post(f"/api/plans/{plan['planId']}/reject",
                                    json={'version': 1, 'reason': 'too broad'})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        revised = self.client.post(f"/api/plans/{plan['planId']}/revise",
                                   json={'goal': 'narrower thing'})
        self.assertEqual(revised.status_code, 200, revised.text)
        body = revised.json()
        self.assertEqual(body['plan']['version'], 2)
        self.assertTrue(body['diff']['changed'])

    def test_backlog_and_new_team_application(self):
        patches, saved, _ = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        with unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint',
                                        return_value=True):
            plan = self._propose().json()['plan']
        self.assertEqual(plan['route'], 'backlog')
        with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog',
                                        return_value={'feature': {'id': 'f-1', 'name': 'F'},
                                                      'items': [{'id': 'b-1'}]}):
            r = self.client.post(f"/api/plans/{plan['planId']}/approve",
                                 json={'version': 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['backlogged'])

        with unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint',
                                        return_value=True):
            plan2 = self._propose(goal='another').json()['plan']
            with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog',
                                            return_value=None), \
                 unittest.mock.patch.object(sim, 'spawn_new_team_for_request',
                                            return_value={'id': 'team-9', 'name': 'New'}):
                r2 = self.client.post(f"/api/plans/{plan2['planId']}/approve",
                                      json={'version': 1})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()['newTeam'])

    def test_stale_plan_fails_after_team_removed(self):
        patches, _, state = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        plan = self._propose().json()['plan']
        state['teams'] = []
        r = self.client.post(f"/api/plans/{plan['planId']}/approve",
                             json={'version': 1})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['plan']['status'], 'failed')

    def test_budget_and_route_errors(self):
        patches, _, state = _patches()
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        r = self._propose(constraints={'budgetUsd': 9999})
        self.assertEqual(r.status_code, 400)
        state['agents']['faye']['busy'] = True
        state['agents']['nora']['busy'] = True
        self.assertIn('free right now', self._propose().json()['error'])
        state['agents']['faye']['busy'] = False
        state['teams'] = []
        self.assertIn('No free team', self._propose().json()['error'])

    def test_unauthorized_and_missing_plan(self):
        patches, _, _ = _patches(authed=False)
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        self.assertEqual(self._propose().status_code, 401)
        self.assertEqual(self.client.get('/api/plans').status_code, 401)
        self.assertEqual(self.client.get('/api/plans/missing').status_code, 401)


def _auth_patches(authed=True):
    return [
        unittest.mock.patch.object(serve, '_operator_gate', return_value=(authed, 'player')),
        unittest.mock.patch.object(serve, 'verify_session', return_value=True),
        unittest.mock.patch.object(serve, 'get_state_from_db', return_value=_state()),
        unittest.mock.patch.object(serve, 'save_state_to_db'),
        unittest.mock.patch.object(serve, 'log_action'),
    ]


class PlanApprovalErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._db = unittest.mock.patch.dict(
            os.environ, {'THINK_TANK_DB': os.path.join(self._tmp.name, 'plans.db')})
        self._db.start(); self.addCleanup(self._db.stop)
        self.client = TestClient(serve.app)

    def _with(self, authed=True, **overrides):
        patches = _auth_patches(authed)
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        for name, value in overrides.items():
            p = unittest.mock.patch.object(serve, name, value)
            p.start(); self.addCleanup(p.stop)
        return patches

    def test_propose_validation_and_state_unavailable(self):
        self._with()
        self.assertEqual(self.client.post('/api/intent/plan-big-task', content=b'x').status_code, 400)
        self.assertEqual(self.client.post('/api/intent/plan-big-task', json={}).status_code, 400)
        with unittest.mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(self.client.post('/api/intent/plan-big-task',
                                              json={'goal': 'g'}).status_code, 503)

    def test_handler_auth_denials(self):
        self._with(authed=False)
        self.assertEqual(self.client.post('/api/intent/plan-big-task',
                                          json={'goal': 'g'}).status_code, 401)
        self.assertEqual(self.client.get('/api/plans').status_code, 401)
        self.assertEqual(self.client.get('/api/plans/x').status_code, 401)
        self.assertEqual(self.client.post('/api/plans/x/approve', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/plans/x/reject', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/plans/x/revise', json={}).status_code, 401)

    def test_plan_not_found_and_bad_versions(self):
        self._with()
        self.assertEqual(self.client.get('/api/plans/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/plans/missing/approve',
                                          json={'version': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/plans/missing/approve',
                                          json={'version': 1}).status_code, 404)
        self.assertEqual(self.client.post('/api/plans/missing/reject',
                                          json={'version': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/plans/missing/reject',
                                          json={'version': 1}).status_code, 409)
        self.assertEqual(self.client.post('/api/plans/missing/revise',
                                          content=b'x').status_code, 400)
        self.assertEqual(self.client.post('/api/plans/missing/revise',
                                          json={'goal': 'g'}).status_code, 404)

    def test_active_team_invalid_budget_and_offduty_wake(self):
        state = _state()
        state['agents']['faye']['offDuty'] = True
        self._with()
        p = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=state)
        p.start(); self.addCleanup(p.stop)
        plan = self.client.post('/api/intent/plan-big-task',
                                json={'goal': 'g', 'constraints': {'budgetUsd': -1}})
        self.assertEqual(plan.status_code, 400)
        with unittest.mock.patch.object(serve, '_staffing_team_for_authority',
                                         return_value=state['teams'][0]), \
             unittest.mock.patch.object(sim, '_team_in_active_sprint', return_value=True):
            r = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'})
        self.assertIn('entered an active sprint', r.json()['error'])
        with unittest.mock.patch.object(sim, '_team_in_active_sprint', return_value=False):
            plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
            with unittest.mock.patch.object(sim, 'appear_from_outskirts') as wake, \
                 unittest.mock.patch.object(sim, 'file_large_request', return_value={'id': 'w'}):
                r = self.client.post(f"/api/plans/{plan['planId']}/approve",
                                     json={'version': 1})
        self.assertEqual(r.status_code, 200, r.text)
        wake.assert_called_once()

    def test_apply_failures(self):
        self._with()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        with unittest.mock.patch.object(sim, 'file_large_request', return_value=None):
            r = self.client.post(f"/api/plans/{plan['planId']}/approve", json={'version': 1})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(r.json()['plan']['status'], 'failed')

    def test_apply_unknown_route_and_no_admin(self):
        import asyncio
        async def run():
            with self.assertRaises(serve.plan_approvals.PlanError):
                await serve._apply_plan({}, {'route': 'bogus'}, 'player')
            with self.assertRaises(serve.plan_approvals.PlanError):
                await serve._apply_backlog_plan({}, {'goal': 'g', 'payload': {}, 'constraints': {}}, 'player')
        asyncio.run(run())
        self.assertIsNone(serve._plan_json(None))

    def test_revise_errors_and_reject_errors(self):
        self._with()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/revise",
                                          json={}).status_code, 400)
        with unittest.mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/revise",
                                              json={'goal': 'g'}).status_code, 503)
        self.client.post(f"/api/plans/{plan['planId']}/reject",
                         json={'version': 1, 'reason': 'no'})
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/reject",
                                          json={'version': 1, 'reason': 'no'}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/approve",
                                          json={'version': 1}).status_code, 409)


class PlanApprovalMoreEdges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._db = unittest.mock.patch.dict(
            os.environ, {'THINK_TANK_DB': os.path.join(self._tmp.name, 'plans.db')})
        self._db.start(); self.addCleanup(self._db.stop)
        self.client = TestClient(serve.app)

    def _start(self, state=None, authed=True):
        for p in _auth_patches(authed):
            p.start(); self.addCleanup(p.stop)
        self.state = state or _state()
        p = unittest.mock.patch.object(serve, 'get_state_from_db', return_value=self.state)
        p.start(); self.addCleanup(p.stop)
        return self.state

    def test_director_authority_and_no_authority(self):
        self._start({'agentRoster': [{'id': 'nora', 'name': 'N', 'isDirector': True}],
                             'agents': {'nora': {'id': 'nora', 'busy': False, 'offDuty': False}},
                             'teams': [{'id': 'nora', 'name': 'N', 'directorId': 'nora'}],
                             'sprints': {}})
        r = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'})
        self.assertEqual(r.json()['plan']['payload']['admin'], 'nora')
        with unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint', return_value=True):
            r = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'})
        self.assertEqual(r.json()['plan']['payload']['admin'], 'nora')
        self._start({'agentRoster': [], 'agents': {}, 'teams': [], 'sprints': {}})
        with unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint', return_value=True):
            r = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'})
        self.assertIn('No admin or director', r.json()['error'])

    def test_valid_budget_busy_authority_and_active_team_at_apply(self):
        state = self._start()
        plan = self.client.post('/api/intent/plan-big-task',
                                json={'goal': 'g', 'constraints': {'budgetUsd': 1}}).json()['plan']
        self.assertEqual(plan['payload']['estimatedBudgetUsd'], 1)
        state['agents']['faye']['busy'] = True
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/approve",
                                          json={'version': 1}).status_code, 409)
        self._start()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        with unittest.mock.patch.object(sim, '_team_in_active_sprint', return_value=True):
            r = self.client.post(f"/api/plans/{plan['planId']}/approve", json={'version': 1})
        self.assertEqual(r.status_code, 409)

    def test_backlog_total_failure_and_plan_applied_twice(self):
        self._start()
        with unittest.mock.patch.object(serve, '_all_teams_busy_in_sprint', return_value=True):
            plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog', return_value=None), \
             unittest.mock.patch.object(sim, 'spawn_new_team_for_request', return_value=None):
            r = self.client.post(f"/api/plans/{plan['planId']}/approve", json={'version': 1})
        self.assertEqual(r.status_code, 500)
        self._start()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        with unittest.mock.patch.object(sim, 'file_large_request', return_value={'id': 'w'}):
            self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/approve",
                                              json={'version': 1}).status_code, 200)
            again = self.client.post(f"/api/plans/{plan['planId']}/approve", json={'version': 1})
        self.assertTrue(again.json()['alreadyApplied'])

    def test_malformed_bodies_state_unavailable_and_revise_errors(self):
        self._start()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/approve",
                                          content=b'x').status_code, 400)
        with unittest.mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/approve",
                                              json={'version': 1}).status_code, 503)
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/reject",
                                          content=b'x').status_code, 400)
        self.state['agents']['faye']['busy'] = True
        self.state['agents']['nora']['busy'] = True
        self.state['teams'] = []
        self.assertIn('free right now', self.client.post(
            f"/api/plans/{plan['planId']}/revise", json={'goal': 'new'}).json()['error'])
        self._start()
        plan = self.client.post('/api/intent/plan-big-task', json={'goal': 'g'}).json()['plan']
        with unittest.mock.patch.object(sim, 'file_large_request', return_value={'id': 'w'}):
            self.client.post(f"/api/plans/{plan['planId']}/approve", json={'version': 1})
        self.assertEqual(self.client.post(f"/api/plans/{plan['planId']}/revise",
                                          json={'goal': 'new'}).status_code, 409)
