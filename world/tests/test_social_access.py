"""Need-based read-only social credential access.

Covers the operator config, the agent request endpoint (task-bound, one service,
one short-lived handle, no secret in the response), and the hard read-only
browser policy on Facebook/Instagram hosts at both the server and driver
layers. Hermetic: temp DB, patched auth/rate limit, no Docker, no network.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sandbox'))

import config  # noqa: E402
import serve  # noqa: E402
import browser_driver  # noqa: E402


def _task(text='Search Facebook public groups for Albemarle news'):
    return {'id': 'task-social-1', 'assignedTo': 'cora', 'status': 'working',
            'title': text, 'instructions': 'Read-only. Do not post or message.',
            'playerAuthored': True}


class _SocialCase(unittest.TestCase):
    def setUp(self):
        serve._rate_limit_calls.clear()
        self.tmp = tempfile.mkdtemp(prefix='think-tank-social-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            AGENTS_DIR=os.path.join(self.tmp, 'agents'),
            LIBRARY_DIR=os.path.join(self.tmp, 'library'),
            PASSPORT_PATH=os.path.join(self.tmp, 'library', '.passport.json'),
            _FERNET_EDEK_DIR=os.path.join(self.tmp, '.secret_keys'),
            _FERNET_EDEK_PATH=os.path.join(self.tmp, '.secret_keys', 'edek.key'),
        )
        self._cm.start()
        serve.init_db()
        serve._store_credential(
            'facebook', 'facebook',
            {'kind': 'login', 'username': 'austtp25', 'password': 'not-in-any-file'},
            status='active', proposed_by='player')
        self.client = TestClient(serve.app)
        self.client.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
        self.client.headers.update({'X-Agent-Key': 'agent-key'})

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, task=None):
        serve.save_state_to_db({'tasks': {task['id']: task} if task else {}})

    def _request(self, body):
        with unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True):
            return self.client.post('/api/credentials/request-access', json=body)

    def _handles(self):
        with serve._db() as conn:
            return conn.execute(
                'SELECT handle, credential_name, allowed_hosts, allowed_methods '
                'FROM capability_handles ORDER BY created_at').fetchall()


class ConfigTests(unittest.TestCase):
    def test_facebook_and_instagram_are_read_only_services(self):
        cfg = config.get_credential_access_config()
        self.assertIn('facebook', cfg['autoGrant'])
        self.assertIn('instagram', cfg['autoGrant'])
        self.assertEqual(cfg['autoGrant']['facebook']['methods'], ['POST'])
        self.assertIn('facebook.com', cfg['readOnlyHosts'])


    def test_missing_or_empty_config_revokes_auto_grant(self):
        self.assertEqual(config._load_credential_access('/no/such/file')['autoGrant'], {})
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
            handle.write('{"autoGrant": {}, "readOnlyHosts": []}')
            path = handle.name
        try:
            self.assertEqual(config._load_credential_access(path)['autoGrant'], {})
        finally:
            os.unlink(path)


class RequestEndpointTests(_SocialCase):
    def test_active_task_gets_one_scoped_handle_and_no_secret(self):
        task = _task()
        self._save(task)
        r = self._request({'agentId': 'cora', 'service': 'facebook',
                           'taskId': task['id'],
                           'purpose': 'read public group posts about Albemarle'})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data['readOnly'])
        self.assertEqual(data['credential'], 'facebook')
        self.assertEqual(data['methods'], ['POST'])
        self.assertIn('facebook.com', data['hosts'])
        self.assertNotIn('not-in-any-file', r.text)
        rows = self._handles()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 'facebook')
        self.assertIn('facebook.com', rows[0][2])

    def test_same_task_reuses_the_handle(self):
        task = _task()
        self._save(task)
        first = self._request({'agentId': 'cora', 'service': 'facebook',
                               'taskId': task['id'], 'purpose': 'read group posts'}).json()['handle']
        second = self._request({'agentId': 'cora', 'service': 'facebook',
                                'taskId': task['id'], 'purpose': 'read group posts'}).json()['handle']
        self.assertEqual(first, second)
        self.assertEqual(len(self._handles()), 1)

    def test_a_new_task_replaces_the_old_handle(self):
        first_task = _task()
        self._save(first_task)
        first = self._request({'agentId': 'cora', 'service': 'facebook',
                               'taskId': first_task['id'], 'purpose': 'read group posts'}).json()['handle']
        second_task = dict(first_task, id='task-social-2')
        self._save(second_task)
        second = self._request({'agentId': 'cora', 'service': 'facebook',
                                'taskId': second_task['id'], 'purpose': 'read group posts'}).json()['handle']
        self.assertNotEqual(first, second)
        self.assertEqual(len(self._handles()), 1)

    def test_wrong_agent_or_inactive_task_is_refused(self):
        self._save(_task())
        r = self._request({'agentId': 'maya', 'service': 'facebook',
                           'taskId': 'task-social-1', 'purpose': 'read group posts'})
        self.assertEqual(r.status_code, 403)
        task = _task()
        task['status'] = 'done'
        self._save(task)
        r = self._request({'agentId': 'cora', 'service': 'facebook',
                           'taskId': task['id'], 'purpose': 'read group posts'})
        self.assertEqual(r.status_code, 409)

    def test_task_must_reference_the_service(self):
        task = _task('Write a memo about weather')
        self._save(task)
        r = self._request({'agentId': 'cora', 'service': 'facebook',
                           'taskId': task['id'], 'purpose': 'read group posts'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('does not reference', r.text)

    def test_stale_social_login_handle_is_retryable_not_a_policy_block(self):
        fields = [
            {'selector': '#email', 'credentialHandle': 'stale-handle', 'credentialField': 'username'},
            {'selector': '#pass', 'credentialHandle': 'stale-handle', 'credentialField': 'password'},
        ]
        with unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             unittest.mock.patch.object(serve, '_classify_browser_action') as classify:
            r = self.client.post('/api/browser-act', json={
                'agentId': 'cora', 'sandboxId': 'spike-test', 'op': 'login',
                'url': 'https://www.facebook.com/login',
                'purpose': 'read public group posts',
                'fields': fields,
            })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body.get('allowed'))
        self.assertTrue(body.get('credentialHandleError'))
        classify.assert_not_called()


    def test_login_uses_the_live_task_handle_when_the_copied_one_is_stale(self):
        task = _task()
        self._save(task)
        handle = self._request({'agentId': 'cora', 'service': 'facebook',
                                'taskId': task['id'], 'purpose': 'read group posts'}).json()['handle']
        fields = [
            {'selector': '#email', 'credentialHandle': 'old-token', 'credentialField': 'username'},
            {'selector': '#pass', 'credentialHandle': 'old-token', 'credentialField': 'password'},
        ]
        with unittest.mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             unittest.mock.patch.object(serve, 'check_rate_limit', return_value=True), \
             unittest.mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             unittest.mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             unittest.mock.patch.object(serve, '_classify_browser_action',
                                        new=unittest.mock.AsyncMock(return_value=(True, 'allow'))), \
             unittest.mock.patch.object(serve, '_run_browser_act_sync',
                                        return_value=({'ok': True, 'url': 'https://www.facebook.com/login',
                                                       'title': 'Log in', 'fields': [], 'links': []}, None)):
            r = self.client.post('/api/browser-act', json={
                'agentId': 'cora', 'sandboxId': 'spike-test', 'op': 'login',
                'url': 'https://www.facebook.com/login', 'taskId': task['id'],
                'purpose': 'read public group posts', 'fields': fields,
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get('ok'), r.text)

    def test_unknown_service_is_refused(self):
        self._save(_task())
        r = self._request({'agentId': 'cora', 'service': 'notes',
                           'taskId': 'task-social-1', 'purpose': 'read group posts'})
        self.assertEqual(r.status_code, 403)


class SocialReadOnlyPolicyTests(unittest.TestCase):

    def test_social_spike_searches_before_rss_and_keeps_the_handle_path(self):
        import content
        q = ('Search Facebook public groups for news about Albemarle, NC '
             'from the last 14 days')
        self.assertTrue(content._spike_wants_social_login(q, None))
        self.assertFalse(content._spike_wants_rss(q, None))
        with unittest.mock.patch.object(content._serve, 'TAVILY_API_KEY', 'k'):
            self.assertEqual(content._spike_first_tool(q, None), 'search_web')
        self.assertEqual(content._spike_first_tool('any breaking news?', None),
                         'read_rss_feed')

    def test_write_ops_are_blocked(self):
        for op in ('fill', 'fill_many', 'click', 'submit'):
            err = serve._social_readonly_guard(
                op, 'https://www.facebook.com/groups/albemarle', '', [])
            self.assertIsNotNone(err, op)
            self.assertIn('read-only social policy', err)

    def test_reads_are_allowed(self):
        for op in ('goto', 'read', 'screenshot'):
            self.assertIsNone(serve._social_readonly_guard(
                op, 'https://www.facebook.com/groups/albemarle', '', []))

    def test_login_only_on_login_path_with_scoped_fields(self):
        fields = [
            {'selector': '#email', 'credentialHandle': 'h1', 'credentialField': 'username'},
            {'selector': '#pass', 'credentialHandle': 'h1', 'credentialField': 'password'},
        ]
        self.assertIsNone(serve._social_readonly_guard(
            'login', 'https://www.facebook.com/login', '', fields))
        self.assertIsNotNone(serve._social_readonly_guard(
            'login', 'https://www.facebook.com/groups/albemarle', '', fields))
        literal = [dict(fields[0]), dict(fields[1])]
        literal[0].pop('credentialHandle')
        self.assertIsNotNone(serve._social_readonly_guard(
            'login', 'https://www.facebook.com/login', '', literal))

    def test_classifier_skips_jev_for_social_reads(self):
        with unittest.mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             unittest.mock.patch.object(serve, '_jev_quorum_decision') as jev, \
             unittest.mock.patch.object(serve, '_jev_safety_gate') as gate:
            allowed, reason = asyncio.run(serve._classify_browser_action(
                'goto', 'https://www.facebook.com/groups/albemarle', 'read news', 'cora'))
        self.assertTrue(allowed)
        self.assertIn('read-only social', reason)
        jev.assert_not_called()
        gate.assert_not_called()

    def test_browser_act_executor_does_not_strike_on_stale_handle(self):
        import content
        struck = set()
        execu = content._make_browser_act_executor('cora', 'key', 'spike-test', struck_tools=struck)
        with unittest.mock.patch.object(serve, '_http_json', return_value={
                'allowed': False, 'credentialHandleError': True,
                'reason': 'call request_credential_access first'}):
            out = execu('browser_act', {
                'op': 'login', 'url': 'https://www.facebook.com/login',
                'purpose': 'read public group posts',
                'fields': [
                    {'selector': '#email', 'credentialHandle': 'h', 'credentialField': 'username'},
                    {'selector': '#pass', 'credentialHandle': 'h', 'credentialField': 'password'},
                ],
            })
        self.assertIn('request_credential_access', out)
        self.assertNotIn('browser_act', struck)


class DriverBackstopTests(unittest.TestCase):
    def test_driver_blocks_social_writes_and_allows_login_path(self):
        self.assertIn('read-only social',
                      browser_driver._social_write_guard(
                          'click', 'https://m.facebook.com/groups/x'))
        self.assertIsNone(browser_driver._social_write_guard(
            'login', 'https://m.facebook.com/login'))
        self.assertIsNotNone(browser_driver._social_write_guard(
            'login', 'https://m.facebook.com/groups/x'))
