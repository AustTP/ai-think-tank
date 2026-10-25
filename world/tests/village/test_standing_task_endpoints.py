"""Operator-gated standing-task declaration endpoints (ask 4).

schedule.py is pure and already tested on its own. These
tests cover the WIRING in serve: the operator-gated
POST/GET/DELETE /api/standing-tasks endpoints that write
the durable state['standingTasks'] declarations the sim
scheduler rebuilds its StandingTask objects from on every
tick (camelCase keys, a schedule contract that rejects a
broken declaration, an optional room the scheduler reads
when queueing work cards), and the scheduler's per-task
runtime state riding along on the read.

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

import schedule  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-standing-')
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


class StandingTaskCreate(unittest.TestCase):
    def _create(self, body, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().post('/api/standing-tasks', json=body)

    def test_create_requires_the_operator_gate(self):
        response = self._create({'taskId': 't1', 'name': 'T'},
                                operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_create_requires_a_task_id_and_name(self):
        for body in ({}, {'taskId': 't1'}, {'name': 'T'}):
            response = self._create(body)
            self.assertEqual(response.status_code, 400)

    def test_create_rejects_an_unknown_mode(self):
        response = self._create({'taskId': 't1', 'name': 'T',
                                 'mode': 'sometimes'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('unknown mode', response.json()['error'])

    def test_create_rejects_a_broken_schedule_contract(self):
        response = self._create({'taskId': 't1', 'name': 'T',
                                 'mode': 'weekly',
                                 'startTime': '09:00',
                                 'endTime': '17:00'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('days_of_week', response.json()['error'])

    def test_create_rejects_a_bad_day_of_week(self):
        response = self._create({'taskId': 't1', 'name': 'T',
                                 'mode': 'weekly',
                                 'startTime': '09:00',
                                 'endTime': '17:00',
                                 'daysOfWeek': [0, 7]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('day of week', response.json()['error'])

    def test_create_persists_the_declaration(self):
        _seed({})
        response = self._create({
            'taskId': 't1', 'name': 'Observatory sweep',
            'mode': 'window', 'startTime': '09:00',
            'endTime': '17:00', 'room': 'observatory',
            'agentCount': 2,
        })
        self.assertEqual(response.status_code, 200)
        declaration = response.json()['task']
        self.assertEqual(declaration['taskId'], 't1')
        self.assertEqual(declaration['mode'], 'window')
        self.assertEqual(declaration['startTime'], '09:00')
        self.assertEqual(declaration['endTime'], '17:00')
        self.assertEqual(declaration['room'], 'observatory')
        self.assertEqual(declaration['agentCount'], 2)
        state = serve.get_state_from_db()
        self.assertEqual(state['standingTasks'], [declaration])

    def test_create_defaults_to_the_always_mode(self):
        _seed({})
        response = self._create({'taskId': 't1', 'name': 'T'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['task']['mode'], 'always')

    def test_create_rejects_a_duplicate_task_id(self):
        _seed({'standingTasks': [
            {'taskId': 't1', 'name': 'T', 'mode': 'always'}]})
        response = self._create({'taskId': 't1', 'name': 'T'})
        self.assertEqual(response.status_code, 409)


class StandingTaskList(unittest.TestCase):
    def test_list_carries_the_scheduler_runtime_state(self):
        _seed({'standingTasks': [
            {'taskId': 't1', 'name': 'T', 'mode': 'always'},
            {'taskId': 't2', 'name': 'T2', 'mode': 'always'},
        ]})
        runtime = schedule.TaskState(occurrences=2, total_hours=1.5)
        with unittest.mock.patch.dict(
                sim._STANDING_SCHEDULER.states, {'t1': runtime}):
            response = _client().get('/api/standing-tasks')
        self.assertEqual(response.status_code, 200)
        tasks = {task['taskId']: task
                 for task in response.json()['tasks']}
        self.assertEqual(tasks['t1']['scheduler']['occurrences'], 2)
        self.assertEqual(tasks['t1']['scheduler']['totalHours'], 1.5)
        # A task the scheduler has never ticked has no
        # runtime state, and says so.
        self.assertIsNone(tasks['t2']['scheduler'])

    def test_list_is_empty_without_declarations(self):
        _seed({})
        response = _client().get('/api/standing-tasks')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tasks'], [])


class StandingTaskDelete(unittest.TestCase):
    def _delete(self, task_id, operator=(True, 'player')):
        with unittest.mock.patch.object(serve, '_operator_gate',
                                        return_value=operator):
            return _client().delete(
                f'/api/standing-tasks/{task_id}')

    def test_delete_requires_the_operator_gate(self):
        response = self._delete('t1', operator=(False, None))
        self.assertEqual(response.status_code, 401)

    def test_delete_reports_an_unknown_task_as_404(self):
        _seed({'standingTasks': [
            {'taskId': 't1', 'name': 'T', 'mode': 'always'}]})
        response = self._delete('ghost')
        self.assertEqual(response.status_code, 404)

    def test_delete_removes_the_declaration_and_runtime(self):
        _seed({'standingTasks': [
            {'taskId': 't1', 'name': 'T', 'mode': 'always'},
            {'taskId': 't2', 'name': 'T2', 'mode': 'always'},
        ]})
        with unittest.mock.patch.dict(
                sim._STANDING_SCHEDULER.states,
                {'t1': schedule.TaskState(),
                 't2': schedule.TaskState()}):
            response = self._delete('t1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(),
                             {'ok': True, 'removed': 't1'})
            self.assertEqual(list(sim._STANDING_SCHEDULER.states),
                             ['t2'])
        state = serve.get_state_from_db()
        self.assertEqual(
            [task['taskId'] for task in state['standingTasks']],
            ['t2'])


if __name__ == '__main__':
    unittest.main()
