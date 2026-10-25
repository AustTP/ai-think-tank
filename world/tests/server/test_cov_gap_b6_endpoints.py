"""Coverage-gap tests for serve_parts 220, 270, 320 and 400.

Route tests drive the real FastAPI app through TestClient with auth/state/db
seams patched; the browser probe uses a fake playwright so no real browser is
launched. Nothing touches the live think_tank.db or the network.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock as mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='tt-cov-b6-endpoints-')
    _MODULE_PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        ESCALATIONS_PATH=os.path.join(_MODULE_TMP_DIR, 'escalations.json'),
        COLAB_STANDBY_ENABLED=False,
        verify_session=lambda *a, **k: True,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


def _client():
    return TestClient(serve.app)


class GrantActorTest(unittest.TestCase):
    def _request(self, key='agent-key'):
        return types.SimpleNamespace(cookies={}, headers={'X-Agent-Key': key})

    def test_verified_director_is_actor(self):
        with mock.patch.object(serve, 'verify_session', return_value=False), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='nora'), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve.governance, 'may_grant_entitlements',
                               return_value=True):
            self.assertEqual(serve._grant_actor(self._request()), 'nora')

    def test_non_director_is_rejected(self):
        with mock.patch.object(serve, 'verify_session', return_value=False), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='nora'), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve.governance, 'may_grant_entitlements',
                               return_value=False):
            self.assertIsNone(serve._grant_actor(self._request()))


class DeviceKeyTest(unittest.TestCase):
    def test_get_is_player_only(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'nora')):
            r = _client().get('/api/device/key')
        self.assertEqual(r.status_code, 403)

    def test_rotate_reports_env_write_failure(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_set_env_keys',
                               side_effect=RuntimeError('read-only')):
            r = _client().post('/api/device/key')
        self.assertEqual(r.status_code, 500)
        self.assertIn('could not write', r.json()['error'])


class SchedulesListTest(unittest.TestCase):
    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            r = _client().get('/api/schedules')
        self.assertEqual(r.status_code, 401)

    def test_terminal_tasks_are_not_listed_as_one_offs(self):
        state = {
            'researchTopics': [],
            'workQueue': [{'title': 'queued', 'notBefore': 5}],
            'tasks': {
                'done-1': {'title': 'done', 'notBefore': 9, 'status': 'done'},
                'live-1': {'title': 'live', 'notBefore': 7, 'status': 'queued'},
            },
        }
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, 'get_state_from_db', return_value=state):
            r = _client().get('/api/schedules')
        self.assertEqual(r.status_code, 200)
        keys = [o['key'] for o in r.json()['oneOffs']]
        self.assertIn('q:0', keys)
        self.assertIn('t:live-1', keys)
        self.assertNotIn('t:done-1', keys)


class UpdateScheduleTopicTest(unittest.TestCase):
    PATH = '/api/schedules/topic/t1'

    def _state(self):
        return {'researchTopics': [
            {'id': 't1', 'topic': 'old', 'startUrl': 'https://example.com/a',
             'cadenceMs': 3600000, 'enabled': True},
        ]}

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            r = _client().patch(self.PATH, json={})
        self.assertEqual(r.status_code, 401)

    def test_state_unavailable(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            r = _client().patch(self.PATH, json={})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch(self.PATH, content=b'{bad',
                                headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 400)

    def test_unknown_topic(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'researchTopics': []}):
            r = _client().patch(self.PATH, json={})
        self.assertEqual(r.status_code, 404)

    def test_bad_cadence(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch(self.PATH, json={'cadenceMs': 'abc'})
        self.assertEqual(r.status_code, 400)

    def test_depends_on_task_is_applied(self):
        state = self._state()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve.config, 'update_watchlist_topic'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = _client().patch(self.PATH,
                                json={'dependsOnTask': 'task-9'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['researchTopics'][0]['dependsOnTask'],
                         'task-9')


class DeleteScheduleTopicTest(unittest.TestCase):
    PATH = '/api/schedules/topic/t1'

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            r = _client().delete(self.PATH)
        self.assertEqual(r.status_code, 401)

    def test_state_unavailable(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            r = _client().delete(self.PATH)
        self.assertEqual(r.status_code, 503)

    def test_unknown_topic(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'researchTopics': []}):
            r = _client().delete(self.PATH)
        self.assertEqual(r.status_code, 404)

    def test_delete_success(self):
        state = {'researchTopics': [
            {'id': 't1', 'topic': 'x', 'startUrl': 'https://example.com/a'}]}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve.config, 'remove_watchlist_topic'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = _client().delete(self.PATH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['researchTopics'], [])


class UpdateScheduleOnceTest(unittest.TestCase):
    def _state(self):
        return {
            'workQueue': [{'title': 'old', 'notBefore': 1000}],
            'tasks': {'task-1': {'title': 'task old', 'notBefore': 2000,
                                 'status': 'queued'}},
        }

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            r = _client().patch('/api/schedules/once/q:0', json={})
        self.assertEqual(r.status_code, 401)

    def test_state_unavailable(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            r = _client().patch('/api/schedules/once/q:0', json={})
        self.assertEqual(r.status_code, 503)

    def test_malformed_body(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch('/api/schedules/once/q:0', content=b'{bad',
                                headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 400)

    def test_bad_at(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()), \
             mock.patch.object(serve, '_parse_schedule_once_at',
                               return_value=None):
            r = _client().patch('/api/schedules/once/q:0',
                                json={'at': 'nonsense'})
        self.assertEqual(r.status_code, 400)

    def test_queue_key_unknown(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch('/api/schedules/once/q:9', json={})
        self.assertEqual(r.status_code, 404)

    def test_queue_key_updates(self):
        state = self._state()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'save_state_to_db'):
            r = _client().patch('/api/schedules/once/q:0',
                                json={'title': 'new title'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['workQueue'][0]['title'], 'new title')

    def test_task_key_unknown(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch('/api/schedules/once/t:missing', json={})
        self.assertEqual(r.status_code, 404)

    def test_task_key_updates(self):
        state = self._state()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'save_state_to_db'):
            r = _client().patch('/api/schedules/once/t:task-1',
                                json={'title': 'retitled',
                                      'at': '2030-01-01T00:00:00Z'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['tasks']['task-1']['title'], 'retitled')
        self.assertGreater(state['tasks']['task-1']['notBefore'], 0)

    def test_malformed_key(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().patch('/api/schedules/once/z:0', json={})
        self.assertEqual(r.status_code, 400)


class DeleteScheduleOnceTest(unittest.TestCase):
    def _state(self):
        return {
            'workQueue': [{'title': 'old', 'notBefore': 1000}],
            'tasks': {'task-1': {'title': 'task old', 'notBefore': 2000,
                                 'status': 'queued'}},
        }

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            r = _client().delete('/api/schedules/once/q:0')
        self.assertEqual(r.status_code, 401)

    def test_state_unavailable(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            r = _client().delete('/api/schedules/once/q:0')
        self.assertEqual(r.status_code, 503)

    def test_queue_key_unknown(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().delete('/api/schedules/once/q:9')
        self.assertEqual(r.status_code, 404)

    def test_queue_key_deletes(self):
        state = self._state()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = _client().delete('/api/schedules/once/q:0')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['workQueue'], [])

    def test_task_key_unknown(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().delete('/api/schedules/once/t:missing')
        self.assertEqual(r.status_code, 404)

    def test_task_key_cancels(self):
        state = self._state()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            r = _client().delete('/api/schedules/once/t:task-1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(state['tasks']['task-1']['status'], 'cancelled')

    def test_malformed_key(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()):
            r = _client().delete('/api/schedules/once/z:0')
        self.assertEqual(r.status_code, 400)


class StandingTaskCreateTest(unittest.TestCase):
    def test_script_backed_requires_player_session(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            r = _client().post('/api/standing-tasks', json={
                'script': 'echo hi', 'taskId': 't1', 'name': 'n'})
        self.assertEqual(r.status_code, 403)

    def test_cron_declaration_error(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve.cron_scripts, 'declaration_fields',
                               return_value=({}, 'bad cron field')):
            r = _client().post('/api/standing-tasks', json={
                'taskId': 't1', 'name': 'n'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('bad cron field', r.json()['error'])


class BrowseCrawlRobotsTest(unittest.TestCase):
    def test_robots_disallow_blocks_crawl(self):
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_page_budget_exhausted',
                               return_value=False), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=True), \
             mock.patch.object(serve, '_robots_allows',
                               return_value=(False, 'disallowed by robots')), \
             mock.patch.object(serve, 'log_action') as log:
            r = _client().post('/api/browse', json={
                'url': 'http://example.com/x', 'agentId': 'ada',
                'purpose': 'research', 'crawl': True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['allowed'])
        self.assertIn('robots.txt', r.json()['reason'])
        self.assertEqual(log.call_args[0][2]['decision'], 'blocked')


class BoundedProbeResultTest(unittest.TestCase):
    def test_unserializable_value_falls_back_to_repr(self):
        class Weird:
            def __str__(self):
                raise RuntimeError('no str')

            def __repr__(self):
                return '<weird>'

        value = Weird()
        self.assertIs(serve._bounded_probe_result(value), value)

    def test_oversized_value_is_truncated(self):
        out = serve._bounded_probe_result('x' * 50, max_chars=10)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['chars'], 52)
        self.assertEqual(out['preview'], '"' + 'x' * 9)


class _FakeRoute:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.calls = []

    def continue_(self):
        self.calls.append('continue')

    def abort(self):
        self.calls.append('abort')


class _FakePage:
    def __init__(self):
        self.handlers = {}
        self.keyboard = mock.MagicMock()
        self.closed = False

    def route(self, pattern, handler):
        self.handlers[pattern] = handler

    def on(self, *args):
        pass

    def evaluate(self, expr, arg=None):
        if 'Object.keys(window)' in expr:
            return ['baseline']
        return 'X'

    def goto(self, *args, **kwargs):
        pass

    def close(self):
        self.closed = True

    def wait_for_timeout(self, ms):
        pass

    def click(self, *args, **kwargs):
        pass


class _FakeBrowser:
    def __init__(self):
        self.pages = []

    def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PageProbeRequestGuardTest(unittest.TestCase):
    def test_route_guard_allows_only_local_schemes(self):
        browser = _FakeBrowser()
        with mock.patch.object(serve, 'sync_playwright',
                               return_value=_FakePlaywright(browser)):
            serve._page_probe_sync('/tmp/fake.html', [], [])
        page = browser.pages[1]
        guard = page.handlers['**/*']
        local = _FakeRoute('file:///tmp/fake.html')
        remote = _FakeRoute('https://example.com/x')
        guard(local)
        guard(remote)
        self.assertEqual(local.calls, ['continue'])
        self.assertEqual(remote.calls, ['abort'])


class ResolveEscalationKindsTest(unittest.TestCase):
    def _resolve(self, kind, decision='approve'):
        esc = {'status': 'pending', 'token': 't', 'kind': kind,
               'question': 'why?'}
        with mock.patch.object(serve, '_load_escalations',
                               return_value={'e1': esc}), \
             mock.patch.object(serve, '_save_escalations'):
            return asyncio.run(serve.resolve_escalation('e1', 't', decision))

    def test_dormancy_window_branch(self):
        with mock.patch.object(serve, '_reset_escalation_judge_drift'), \
             mock.patch.object(serve._selfedit, 'resolve_window_escalation',
                               return_value=True):
            resp = self._resolve('dormancy window request')
        self.assertIn('dormancy window', resp.body.decode())

    def test_oversight_self_edit_branch(self):
        with mock.patch.object(serve, '_reset_escalation_judge_drift'), \
             mock.patch.object(serve._selfedit, 'resolve_oversight_signature',
                               return_value=True):
            resp = self._resolve('oversight self-edit')
        self.assertIn('oversight self-edit', resp.body.decode())


if __name__ == '__main__':
    unittest.main()
