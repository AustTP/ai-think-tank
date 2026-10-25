"""Signal-plan declaration and emission endpoints (ask 6).

signals.py is pure and already tested on its own. These
tests cover the WIRING in serve: the operator-gated
POST/GET/DELETE /api/signal-plans endpoints that write
the durable state['signalPlans'] declarations sim.emit_signals
rebuilds its WakePlan objects from, and the operator-gated
POST /api/signals/emit endpoint that evaluates every plan
against one emission (subject + data, or a task the sim
turns into its completed-task data line), persists the
arming write-back a fired scenario performs, and returns
the wake events.

DB-isolated (setUpModule below, the test_vpn_exit_endpoints
pattern).
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-signals-')
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


def _client():
    from starlette.testclient import TestClient
    c = TestClient(serve.app)
    c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
    return c


def _seed(state):
    serve.save_state_to_db(state)
    return serve.get_state_from_db()


def _plan(plan_id='p1', agent_id='ada', task='task-9'):
    return {
        'planId': plan_id,
        'agentId': agent_id,
        'team': 'dev',
        'subject': 'task.completed',
        'task': task,
        'scenarios': [{
            'scenarioId': 's1',
            'name': 'Observatory done',
            'condition': 'status=done room=observatory',
            'action': 'investigate',
            'exitStrategy': 'report to faye',
            'armed': True,
        }],
    }


def _idle_state(plans=None):
    return {
        'agents': {
            'ada': {'id': 'ada', 'offDuty': False, 'busy': False,
                    'task': None, 'x': 0, 'y': 0},
        },
        'signalPlans': plans if plans is not None else [_plan()],
    }


class SignalPlanCreate(unittest.TestCase):
    def _create(self, body, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().post('/api/signal-plans', json=body)

    def test_create_requires_the_operator_gate(self):
        response = self._create({'planId': 'p1'}, operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_create_requires_the_core_fields(self):
        for body in (
            {},
            {'planId': 'p1'},
            {'planId': 'p1', 'agentId': 'ada'},
            {'planId': 'p1', 'agentId': 'ada', 'subject': 's'},
        ):
            response = self._create(body)
            self.assertEqual(response.status_code, 400)

    def test_create_requires_at_least_one_scenario(self):
        response = self._create({
            'planId': 'p1', 'agentId': 'ada', 'subject': 's',
            'scenarios': [],
        })
        self.assertEqual(response.status_code, 400)

    def test_create_requires_scenario_condition_and_action(self):
        response = self._create({
            'planId': 'p1', 'agentId': 'ada', 'subject': 's',
            'scenarios': [{'scenarioId': 's1', 'condition': 'c'}],
        })
        self.assertEqual(response.status_code, 400)

    def test_create_persists_the_plan(self):
        _seed({})
        response = self._create({
            'planId': 'p1', 'agentId': 'ada', 'team': 'dev',
            'subject': 'task.completed', 'task': 'task-9',
            'scenarios': [{
                'scenarioId': 's1', 'condition': 'status=done',
                'action': 'investigate', 'armed': True,
            }],
        })
        self.assertEqual(response.status_code, 200)
        plan = response.json()['plan']
        self.assertEqual(plan['planId'], 'p1')
        self.assertEqual(plan['scenarios'][0]['armed'], True)
        state = serve.get_state_from_db()
        self.assertEqual(state['signalPlans'], [plan])

    def test_create_rejects_a_duplicate_plan_id(self):
        _seed({'signalPlans': [_plan()]})
        response = self._create({
            'planId': 'p1', 'agentId': 'ada', 'subject': 's',
            'scenarios': [{'scenarioId': 's1',
                           'condition': 'c', 'action': 'a'}],
        })
        self.assertEqual(response.status_code, 409)


class SignalPlanListAndDelete(unittest.TestCase):
    def test_list_returns_the_declarations(self):
        _seed({'signalPlans': [_plan()]})
        response = _client().get('/api/signal-plans')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['plans'], [_plan()])

    def _delete(self, plan_id, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().delete(f'/api/signal-plans/{plan_id}')

    def test_delete_requires_the_operator_gate(self):
        response = self._delete('p1', operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_delete_reports_an_unknown_plan_as_404(self):
        _seed({'signalPlans': [_plan()]})
        response = self._delete('ghost')
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_the_declaration(self):
        _seed({'signalPlans': [_plan()]})
        response = self._delete('p1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'removed': 'p1'})
        self.assertEqual(serve.get_state_from_db()['signalPlans'], [])


class SignalsEmit(unittest.TestCase):
    def _emit(self, body, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().post('/api/signals/emit', json=body)

    def test_emit_requires_the_operator_gate(self):
        response = self._emit({'subject': 'task.completed'},
                              operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_emit_requires_a_subject(self):
        _seed(_idle_state())
        response = self._emit({'data': 'status=done'})
        self.assertEqual(response.status_code, 400)

    def test_emit_rejects_non_string_data(self):
        _seed(_idle_state())
        response = self._emit({'subject': 'task.completed',
                               'data': {'status': 'done'}})
        self.assertEqual(response.status_code, 400)

    def test_emit_fires_matching_plans_and_persists_the_disarm(self):
        _seed(_idle_state())
        response = self._emit({
            'subject': 'task.completed',
            'task': {'title': 'T', 'room': 'observatory'},
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['subject'], 'task.completed')
        events = body['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['agentId'], 'ada')
        self.assertEqual(events[0]['scenarioId'], 's1')
        self.assertEqual(events[0]['action'], 'investigate')
        # The fired scenario's disarm is durable: the
        # persisted declaration now carries armed=false.
        state = serve.get_state_from_db()
        self.assertFalse(state['signalPlans'][0]['scenarios'][0]['armed'])

    def test_emit_accepts_a_raw_data_string(self):
        _seed(_idle_state())
        response = self._emit({
            'subject': 'task.completed',
            'data': 'status=done title=T room=observatory',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['events']), 1)

    def test_emit_with_no_matching_plan_is_still_ok(self):
        _seed({})
        response = self._emit({'subject': 'task.completed',
                               'data': 'status=done'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['events'], [])


if __name__ == '__main__':
    unittest.main()
