"""Split part of test_cov_gap_serve.py (hermetic serve.py coverage tests)."""
import asyncio
import collections
import datetime
import shutil
import tempfile
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
import serve  # noqa: E402
from test_cov_gap_serve_helpers import (  # noqa: E402
    _NO_GRANT, _SENTINEL, req_with, decision_cache_report_defaults)


class PipelineTest(unittest.TestCase):
    def _call(self, body, has_py=False, classify=(True, ''), override=None,
              run=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}

        async def fake_to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda a, s: s), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key', return_value=True), \
             mock.patch.object(serve, '_sandbox_dir_for',
                               return_value='/s'), \
             mock.patch.object(serve, '_sandbox_has_python_files',
                               return_value=has_py), \
             mock.patch.object(serve, '_classify_command',
                               new=mock.AsyncMock(return_value=classify)), \
             mock.patch.object(serve, '_supervisor_override_decision',
                               new=mock.AsyncMock(
                                   return_value=override or (False, 'no'))), \
             mock.patch.object(serve, '_snapshot_sandbox'), \
             mock.patch.object(serve, '_run_in_sandbox_sync',
                               return_value=run or {'exitCode': 0,
                                                     'timedOut': False}), \
             mock.patch.object(serve, 'sync_prototypes'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=fake_to_thread):
            return asyncio.run(serve.pipeline(req))

    def test_paths(self):
        self.assertEqual(self._call({'steps': [{}],
                                     'sandboxId': '..'}).status_code, 400)
        res = self._call({'steps': [{'name': 'mypy',
                                     'command': 'mypy'}]},
                         has_py=False)
        self.assertIn('skipped', json.loads(res.body)['results'][0]['stderr'])
        res = self._call({'steps': [{'name': 'x', 'command': 'rm'}]},
                         classify=(False, 'bad'),
                         override=(True, 'ok'))
        self.assertTrue(json.loads(res.body)['results'][0]['allowed'])


class SimWatchdogNoteTest(unittest.TestCase):
    def test_stamp_and_alert_fail(self):
        class BadModule:
            def __setattr__(self, k, v):
                raise RuntimeError('no')

        with mock.patch.dict(sys.modules, {'sim': BadModule()}), \
             mock.patch.dict(serve.__dict__, {
                 '_SIM_WATCHDOG_RESTARTS': 0,
                 '_SIM_WATCHDOG_PLAYER_ALERTED': False,
                 '_SIM_WATCHDOG_LAST_EVENT_AT': 0,
                 '_SIM_WATCHDOG_PLAYER_ALERT_AFTER': 1}), \
             mock.patch('notify.send_player_telegram_sync',
                        side_effect=RuntimeError('tg')):
            self.assertEqual(serve._sim_watchdog_note_failure('loop', 'd',
                                                              False), 1)


class ValidateApiServiceSpecTest(unittest.TestCase):
    def _valid(self):
        return {'id': 's', 'base_url': 'https://x',
                'credential': {'type': 'none'}, 'auth': {'type': 'none'},
                'methods': ['GET'], 'path_rules': [{'prefix': '/'}]}

    def test_errors(self):
        with mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            self.assertIn('alphanumeric', serve._validate_api_service_spec(
                {'id': '!!'})[2])
            spec = self._valid()
            spec['credential'] = {'type': 'bad'}
            self.assertIn('credential.type', serve._validate_api_service_spec(
                spec)[2])
            spec = self._valid()
            spec['path_rules'] = []
            self.assertIn('path_rules', serve._validate_api_service_spec(
                spec)[2])
            spec = self._valid()
            spec['spend'] = {'kind': 'bad'}
            self.assertIn('spend.kind', serve._validate_api_service_spec(
                spec)[2])
            self.assertIsNone(serve._validate_api_service_spec(
                self._valid())[2])


class ApiKeySnapshotTest(unittest.TestCase):
    def test_paths(self):
        health = {'a': {'consecutive_failures': 3,
                        'blocked_until': 200.0,
                        'last_failure_ts': 50.0,
                        'last_failure_code': 401},
                  'b': {'consecutive_failures': 1}}
        with mock.patch.object(serve, '_api_key_health', health), \
             mock.patch.object(serve, '_api_key_health_lock',
                               mock.MagicMock()), \
             mock.patch.object(serve, 'API_KEY_AUTH_FAILURE_THRESHOLD', 2), \
             mock.patch('time.time', return_value=100.0):
            out = serve._api_key_auth_failures_snapshot()
        self.assertEqual(out['a']['consecutive_failures'], 3)
        self.assertNotIn('b', out)


class ReadMailboxToolTest(unittest.TestCase):
    def test_paths(self):
        state = {'agents': {
            'me': {'role': 'Post Office'},
            't': {'mailbox': [{'kind': 'note', 'title': 'T',
                               'text': 'hello', 'about': 'x'}]}}}
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=state):
            out = serve._read_mailbox_tool_text(
                'me', {'agentId': 't', 'limit': 'bad'})
        self.assertIn('T', out)
        self.assertIn('about: x', out)
        self.assertIn('- [note] T', out)


class ClassifyBrowserActionTest(unittest.TestCase):
    def _call(self, op='read', url='https://x', handle=False, gate=True,
              decision='allow', confidence=0.9, urlparse=None):
        patches = [
            mock.patch.object(serve, '_is_allowlisted_host',
                              return_value=True),
            mock.patch.object(serve, '_is_work_context_host',
                              return_value=False),
            mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'),
            mock.patch.object(serve, '_jev_quorum_decision',
                              new=mock.AsyncMock(
                                  return_value=(decision, confidence, 0,
                                                't'))),
            mock.patch.object(serve, '_jev_safety_gate',
                              return_value=gate),
            mock.patch.object(serve, '_effective_safety_confidence',
                              return_value=0.9),
            mock.patch.object(serve, 'BROWSER_ACT_READ_OPS', {'read'}),
        ]
        if urlparse:
            patches.append(mock.patch('urllib.parse.urlparse',
                                      side_effect=urlparse))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve._classify_browser_action(
            op, url, 'p', handle_scoped_host=handle))

    def test_paths(self):
        self.assertTrue(self._call(urlparse=ValueError('bad'))[0])
        self.assertTrue(self._call(op='fill', handle=True)[0])
        ok, msg = self._call(op='click', gate=False, decision='block')
        self.assertFalse(ok)
        ok, msg = self._call(op='click', gate=False, confidence=0.1)
        self.assertFalse(ok)
        self.assertIn('not confident', msg)


class EnsureSandboxGitRepoTest(unittest.TestCase):
    def test_gitignore_paths(self):
        with mock.patch('os.path.exists', return_value=False), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch.object(serve, '_run_git_sync'), \
             mock.patch('builtins.open', mock.mock_open(read_data='x')):
            serve._ensure_sandbox_git_repo('/s')
        with mock.patch('os.path.exists', return_value=False), \
             mock.patch('os.path.isfile', return_value=False), \
             mock.patch.object(serve, '_run_git_sync'), \
             mock.patch('builtins.open', side_effect=OSError('ro')):
            serve._ensure_sandbox_git_repo('/s')


class BackupDbTest(unittest.TestCase):
    def test_errors(self):
        with mock.patch.object(serve, 'DB_BACKUP_DIR', '/b'), \
             mock.patch.object(serve, 'DB_PATH', '/db'), \
             mock.patch('os.path.isdir', return_value=False), \
             mock.patch('os.makedirs'), \
             mock.patch('os.chmod', side_effect=OSError('ro')), \
             mock.patch('sqlite3.connect', return_value=mock.MagicMock()), \
             mock.patch.object(serve, '_harden_db_perms'), \
             mock.patch('os.listdir', side_effect=OSError('no')), \
             mock.patch('time.strftime', return_value='t'), \
             mock.patch('time.time', return_value=1.0):
            serve._backup_think_tank_db()


class TierWatchMovesTest(unittest.TestCase):
    def test_paths(self):
        models = [{'id': 'a:batch'}, {'id': 'b', 'pricing': {}},
                  {'id': 'c', 'pricing': {'prompt': '1',
                                          'completion': '1'}}]
        with mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               return_value=models), \
             mock.patch.object(serve, 'get_cached_model_tiers',
                               return_value={'band': {'slug': 'c',
                                                      'price': 2000000.0}}), \
             mock.patch.object(serve, 'get_model_benchmark_scores',
                               return_value=[{'model_id': 'c',
                                              'benchmark': 'bm',
                                              'score': 1}]), \
             mock.patch.object(serve, '_band_candidate_pools',
                               return_value={'band': []}), \
             mock.patch.object(serve, '_best_value_qualifiers_sync',
                               return_value=[]), \
             mock.patch.object(serve, 'BAND_BENCHMARK',
                               {'band': ('bm',)}), \
             mock.patch.object(serve, 'BAND_QUALITY_FLOOR_GAP',
                               {'band': 0}):
            self.assertEqual(serve._tier_watch_moves_sync(), set())


class InitDbErrorsTest(unittest.TestCase):
    def test_pragma_errors(self):
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        conn = mock.Mock()

        def execute(sql, *a, **k):
            if isinstance(sql, str) and (
                    'PRAGMA table_info(external_credentials)' in sql or
                    'PRAGMA table_info(api_services)' in sql):
                raise RuntimeError('pragma')
            return cursor
        conn.execute.side_effect = execute
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm):
            serve.init_db()


class PhoneSendTest(unittest.TestCase):
    def _call(self, body, json_side=None, state=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_agent_requester',
                               return_value=('a', None)), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'phone') as phone, \
             mock.patch.object(serve, '_theo_policy', object()), \
             mock.patch.object(serve, '_phone_failure',
                               return_value=None), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            phone.Phone.return_value.send.return_value = mock.Mock(
                agent_id='a', messages=[])
            return asyncio.run(serve.phone_send(req))

    def test_paths(self):
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call([]).status_code, 400)
        self.assertEqual(self._call({'recipientId': 'b', 'text': 't'},
                                    state=None).status_code, 503)


class TheoKeysRevokeTest(unittest.TestCase):
    def _call(self, body, authed=True, json_side=None, revoked=True):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(authed, 'p')), \
             mock.patch.object(serve, '_KEY_REGISTRY') as reg, \
             mock.patch.object(serve, '_save_key_registry'), \
             mock.patch.object(serve, 'log_action'):
            reg.revoke.return_value = revoked
            return asyncio.run(serve.theo_keys_revoke(req))

    def test_paths(self):
        self.assertEqual(self._call({}, authed=False).status_code, 401)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'keyId': 'k'},
                                    revoked=False).status_code, 404)
        self.assertTrue(json.loads(self._call(
            {'keyId': 'k'}).body)['ok'])


class VpnExitsStartTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.vpn_exits_start(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'relay': 'r'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'VPN_EXIT_POOL') as pool, \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, 'vpn_location') as loc, \
             mock.patch.object(serve, '_vpn_exit_registry'):
            pool.start.return_value = (mock.Mock(name='x'), None)
            loc.describe_exit.return_value = {'name': 'x'}
            loc.location_snapshot.return_value = {}
            self.assertTrue(json.loads(asyncio.run(
                serve.vpn_exits_start(req)).body)['ok'])


class AllowlistEndpointsTest(unittest.TestCase):
    def _call(self, endpoint, body, json_side=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            return asyncio.run(endpoint(req))

    def test_grant(self):
        self.assertEqual(self._call(serve.allowlist_grant_endpoint, None,
                                    json_side=ValueError('x')).status_code,
                         400)
        self.assertEqual(self._call(serve.allowlist_grant_endpoint,
                                    {}).status_code, 400)
        with mock.patch.object(serve, '_normalize_allowlist_target',
                               return_value=(None, 'bad')):
            self.assertEqual(self._call(serve.allowlist_grant_endpoint,
                                        {'host': 'h'}).status_code, 400)

    def test_revoke(self):
        self.assertEqual(self._call(serve.allowlist_revoke_endpoint, None,
                                    json_side=ValueError('x')).status_code,
                         400)
        self.assertEqual(self._call(serve.allowlist_revoke_endpoint,
                                    {}).status_code, 400)
        with mock.patch.object(serve, '_normalize_allowlist_target',
                               return_value=(None, 'bad')):
            self.assertEqual(self._call(serve.allowlist_revoke_endpoint,
                                        {'host': 'h'}).status_code, 400)


class ApiBuildTextTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap, \
             mock.patch.object(serve, '_redact_secrets',
                               side_effect=lambda x: x):
            _, ctype, wrapped, instr = serve._api_build_text(
                {'body': 'notjson', 'contentType': 'application/json',
                 'truncated': True, 'status': 200}, {'name': 'S'}, True,
                'h')
            self.assertIn('[truncated', wrap.call_args[0][0])
            _, _, wrapped, _ = serve._api_build_text(
                {'body': '<p>x</p>', 'contentType': 'text/html',
                 'status': 200}, {'name': 'S'}, True, 'h')
            self.assertTrue(wrapped.startswith('w'))


class OptFloatTest(unittest.TestCase):
    def test_paths(self):
        with self.assertRaises(ValueError):
            serve._standing_task_from_body(
                {'taskId': 't', 'name': 'n',
                 'consecutiveResetAfterHours': 'bad'})


class StateBeginErrorTest(unittest.TestCase):
    def test_releases_lock(self):
        lock = mock.MagicMock()
        with mock.patch.object(serve, '_STATE_LOCK', lock), \
             mock.patch.object(serve, '_state_txn',
                               mock.MagicMock(held=False, saved=False)), \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            with self.assertRaises(RuntimeError):
                serve._state_begin()
        lock.release.assert_called_once()


class MatchAaScoreTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_benchmark_name_tokens',
                               return_value=set()):
            self.assertIsNone(serve._match_aa_score('', {'a': 1}))
        with mock.patch.object(serve, '_benchmark_name_tokens',
                               side_effect=lambda x: {'a'} if x == 'm'
                               else {'b'}):
            self.assertIsNone(serve._match_aa_score('m', {'k': 1}))
        with mock.patch.object(serve, '_benchmark_name_tokens',
                               side_effect=lambda x: {'a', 'b'} if x == 'm'
                               else {'b', 'c'}):
            self.assertIsNone(serve._match_aa_score('m', {'k': 1},
                                                    min_overlap=0.9))
        with mock.patch.object(serve, '_benchmark_name_tokens',
                               side_effect=lambda x: {'a'}):
            self.assertEqual(serve._match_aa_score('m', {'k': 1}), 1)


class BudgetReviewTest(unittest.TestCase):
    def test_json_error(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.budget_review(
                req)).status_code, 400)


class ContainersSwitchTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        err = serve.JSONResponse({'error': 'no'}, status_code=401)
        with mock.patch.object(serve, '_agent_requester',
                               return_value=(None, err)):
            self.assertIs(asyncio.run(serve.containers_switch(
                'c', req)), err)
        with mock.patch.object(serve, '_agent_requester',
                               return_value=('a', None)), \
             mock.patch.object(serve, '_CONTAINER_BROKER') as broker, \
             mock.patch.object(serve, 'log_action'):
            broker.switch.return_value = True
            self.assertTrue(json.loads(asyncio.run(serve.containers_switch(
                'c', req)).body)['ok'])


class PutDataSourcesTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.put_data_sources(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'sources': {'k': ''}})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'DATA_SOURCE_KEYS', {'k'}):
            self.assertEqual(asyncio.run(serve.put_data_sources(
                req)).status_code, 400)


class RssRemoveTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.rss_remove(
                req)).status_code, 403)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.rss_remove(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'feedId': 'f'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.rss_remove(
                req)).status_code, 404)


class RssFeedsListTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.rss_feeds_list(
                req)).status_code, 401)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(asyncio.run(serve.rss_feeds_list(
                req)).status_code, 503)


class SmsReadTest(unittest.TestCase):
    def test_paths(self):
        def call(rate=True, key=True, body=None):
            req = mock.Mock()
            req.json = mock.AsyncMock(return_value=body or {})
            req.headers = {'X-Agent-Key': 'k'}
            with mock.patch.object(serve, 'check_rate_limit',
                                   return_value=rate), \
                 mock.patch.object(serve, 'verify_agent_key',
                                   return_value=key), \
                 mock.patch.object(serve, 'log_action'):
                return asyncio.run(serve.sms_read(req))
        self.assertEqual(call(rate=False).status_code, 429)
        self.assertEqual(call(key=False).status_code, 401)
        self.assertEqual(call().status_code, 400)


class LoginScrubResultTest(unittest.TestCase):
    def test_list(self):
        self.assertEqual(serve._login_scrub_result(['a']), ['a'])


class DesignBriefFromImageTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value='v'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {
                                   'content': ''}}]}):
            self.assertIn('no reply', serve._design_brief_from_image(
                'b', 's')['note'])
        bad = mock.Mock()
        bad.get.side_effect = RuntimeError('x')
        with mock.patch.object(serve, '_vision_tier_slug',
                               return_value='v'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value=bad):
            self.assertIn('no reply', serve._design_brief_from_image(
                'b', 's')['note'])


class RssMatchTopicsTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(serve._rss_match_topics('t'), [])
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value={'researchTopics': [
                                   {'topic': ''},
                                   {'topic': 'AI agents'}]}):
            self.assertEqual(serve._rss_match_topics('AI agents rise'), [
                'AI agents'])


class LoadRssFeedsTest(unittest.TestCase):
    def test_db_error(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            with mock.patch.dict(serve.__dict__,
                                 {'_RSS_FEEDS': {},
                                  '_RSS_FEEDS_LOAD_ERROR': None}):
                serve._load_rss_feeds()
                self.assertIn('db unavailable',
                              serve._RSS_FEEDS_LOAD_ERROR)


class EvidenceListTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.query_params = {'status': 'bad'}
        self.assertEqual(asyncio.run(serve.evidence_list(req)).status_code,
                         400)
        req = mock.Mock()
        req.query_params = {'limit': 'bad'}
        with mock.patch.object(serve, 'list_claims', return_value=[]):
            self.assertEqual(json.loads(asyncio.run(
                serve.evidence_list(req)).body)['claims'], [])


class WsFeedsListTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.ws_feeds_list(
                req)).status_code, 401)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(asyncio.run(serve.ws_feeds_list(
                req)).status_code, 503)


class SandboxDownloadTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'url': 'https://x', 'filename': 'f', 'sandboxId': '..',
            'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda a, s: s), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: None)):
            self.assertEqual(asyncio.run(serve.sandbox_download(
                req)).status_code, 400)
        async def fake_to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda a, s: s), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: True)), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_agent_is_in_sandbox_room',
                               return_value=True), \
             mock.patch.object(serve, '_sanitize_download_filename',
                               return_value='f'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               new=mock.AsyncMock(
                                   return_value=('allow', .9, 0, 't'))), \
             mock.patch.object(serve, '_jev_safety_gate',
                               return_value=True), \
             mock.patch.object(serve, '_download_file_sync',
                               return_value=('https://y', 'text/plain',
                                             b'x', False)), \
             mock.patch.object(serve, '_jev_re_gate_redirect_final',
                               new=mock.AsyncMock(
                                   return_value=(False, 'no'))), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=fake_to_thread):
            body = json.loads(asyncio.run(serve.sandbox_download(
                req)).body)
        self.assertFalse(body['allowed'])
        self.assertEqual(body['reason'], 'no')


class HealthAlertsKeysTest(unittest.TestCase):
    def test_api_key_alert(self):
        signals = collections.defaultdict(int, {
            'blocked_or_failed_actions_last_hour': {},
            'tool_volume_by_agent_last_15m': {},
            'api_key_auth_failures': {
                'svc': {'seconds_ago': 5, 'blocked': True,
                        'blocked_for_s': 60, 'consecutive_failures': 3,
                        'status_code': 401}}})
        alerts = serve._health_alerts_for_signals(signals)
        self.assertTrue(any(a['category'] == 'api_keys' for a in alerts))
        signals = collections.defaultdict(int, {
            'blocked_or_failed_actions_last_hour': {},
            'tool_volume_by_agent_last_15m': {},
            'api_key_auth_failures': {
                'svc': {'seconds_ago': 5, 'blocked': False,
                        'blocked_for_s': 0, 'consecutive_failures': 3,
                        'status_code': 401}}})
        alerts = serve._health_alerts_for_signals(signals)
        self.assertTrue(any('half-open' in a['message'] for a in alerts))


class NoteApiKeyFailureTest(unittest.TestCase):
    def test_block_and_log_error(self):
        health = {'svc': {'consecutive_failures': 5}}
        with mock.patch.object(serve, '_api_key_health', health), \
             mock.patch.object(serve, '_api_key_health_lock',
                               mock.MagicMock()), \
             mock.patch.object(serve, 'API_KEY_AUTH_FAILURE_THRESHOLD', 2), \
             mock.patch.object(serve, 'API_KEY_AUTH_COOLDOWN_S', 10), \
             mock.patch('time.time', return_value=100.0), \
             mock.patch.object(serve, 'log_action',
                               side_effect=RuntimeError('log')):
            serve._note_api_key_failure('svc', 401)
        self.assertEqual(health['svc']['blocked_until'], 110.0)


class PhoneEndpointsTest(unittest.TestCase):
    def _call(self, endpoint, requester=('a', None), state=None,
              failure=None):
        req = mock.Mock()
        with mock.patch.object(serve, '_agent_requester',
                               return_value=requester), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'phone') as phone, \
             mock.patch.object(serve, '_theo_policy', object()), \
             mock.patch.object(serve, '_phone_failure',
                               return_value=failure), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            phone.Phone.return_value.inbox.return_value = mock.Mock(
                agent_id='a', messages=[])
            phone.Phone.return_value.mark_all_read.return_value = mock.Mock(
                agent_id='a', messages=[])
            return asyncio.run(endpoint(req))

    def test_inbox(self):
        err = serve.JSONResponse({'e': 1}, status_code=401)
        self.assertIs(self._call(serve.phone_inbox, requester=(None, err)),
                      err)
        self.assertEqual(self._call(serve.phone_inbox,
                                    state=None).status_code, 503)
        bad = serve.JSONResponse({'e': 2}, status_code=400)
        self.assertIs(self._call(serve.phone_inbox, state={'x': 1},
                                 failure=bad), bad)

    def test_read_all(self):
        err = serve.JSONResponse({'e': 1}, status_code=401)
        self.assertIs(self._call(serve.phone_read_all,
                                 requester=(None, err)), err)
        self.assertEqual(self._call(serve.phone_read_all,
                                    state=None).status_code, 503)
        bad = serve.JSONResponse({'e': 2}, status_code=400)
        self.assertIs(self._call(serve.phone_read_all, state={'x': 1},
                                 failure=bad), bad)


class GrantRevokeTest(unittest.TestCase):
    def test_file_grant(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.revoke_file_grant(
                'g', req)).status_code, 401)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(asyncio.run(serve.revoke_file_grant(
                'g', req)).status_code, 503)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'fileGrants': []}):
            self.assertEqual(asyncio.run(serve.revoke_file_grant(
                'g', req)).status_code, 404)

    def test_app_grant(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.revoke_app_grant(
                'g', req)).status_code, 401)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(asyncio.run(serve.revoke_app_grant(
                'g', req)).status_code, 503)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'appGrants': []}):
            self.assertEqual(asyncio.run(serve.revoke_app_grant(
                'g', req)).status_code, 404)

    def test_add_app_grant(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.add_app_grant(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'bundleId': 'com.apple.Notes'})
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(asyncio.run(serve.add_app_grant(
                req)).status_code, 503)


class WsExtractFrameTest(unittest.TestCase):
    def test_paths(self):
        cfg = {'frame_filter': {'data_kind': 'dict',
                                'topic_path': 't',
                                'fields': {'a': 'x'}}}
        self.assertEqual(serve._ws_extract_frame(cfg, []),
                         (None, None, None))
        with mock.patch.object(serve, '_ws_resolve_path',
                               side_effect=['topic', RuntimeError('bad')]):
            self.assertEqual(serve._ws_extract_frame(cfg, {'t': 1}),
                             ('topic', {}, {'t': 1}))


class JgetTest(unittest.TestCase):
    def test_defaults(self):
        spec = serve._api_service_row_to_spec({
            'id': 's', 'default_headers': '',
            'methods': 'notjson', 'path_rules': '[]',
            'workflow': 'null', 'credential_type': 'none'})
        self.assertEqual(spec['methods'], [])
        self.assertEqual(spec['default_headers'], {})


class LifespanTest(unittest.TestCase):
    def test_startup_paths(self):
        task = mock.MagicMock()
        state = {'x': 1}
        with mock.patch.object(serve, '_prune_decision_key_stats'), \
             mock.patch.object(serve, '_backfill_directors_in_db'), \
             mock.patch.object(serve, '_backfill_teams_in_db'), \
             mock.patch.object(serve, '_backfill_agent_identity_in_db'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_init_templates_in_db',
                               return_value=False), \
             mock.patch.object(serve, '_seed_watchlist_topics',
                               return_value=True), \
             mock.patch.object(serve, 'save_state_to_db') as save, \
             mock.patch.object(serve, '_load_api_services'), \
             mock.patch.object(serve, '_load_rss_feeds',
                               side_effect=RuntimeError('rss')), \
             mock.patch.object(serve, '_load_ws_feeds'), \
             mock.patch.object(serve, 'asyncio') as aio, \
             mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False), \
             mock.patch.object(serve, 'TELEGRAM_BOT_TOKEN', ''), \
             mock.patch.object(serve, '_WS_TASKS', {}), \
             mock.patch('sim._sim_loop', mock.MagicMock()), \
             mock.patch('config.ALLOW_WATCHLIST_WRITE', False):
            def fake_task(coro):
                if hasattr(coro, 'close'):
                    coro.close()
                return task
            aio.create_task.side_effect = fake_task

            async def run():
                async with serve._lifespan(None):
                    pass
            asyncio.run(run())
        save.assert_called_once_with(state)


class JevReGateTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch('urllib.parse.urlparse',
                        side_effect=ValueError('bad')):
            self.assertEqual(asyncio.run(serve._jev_re_gate_redirect_final(
                'a', 'act', 'noun', 'u', 'v', 'p', '', False, True,
                None)), (True, None))
        with mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=False), \
             mock.patch.object(serve, '_is_standing_safe_host',
                               return_value=False), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               new=mock.AsyncMock(
                                   return_value=('allow', .9, 0, 't'))), \
             mock.patch.object(serve, '_jev_safety_gate',
                               return_value=True):
            self.assertEqual(asyncio.run(serve._jev_re_gate_redirect_final(
                'a', 'act', 'noun', 'https://x', 'https://y', 'p', 'work',
                False, True, None)), (True, None))


class ApiLogTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.api_log(
                req)).status_code, 401)
        req = mock.Mock()
        req.cookies = {}
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, 'verify_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.api_log(
                req)).status_code, 400)


class LoadWsFeedsTest(unittest.TestCase):
    def test_db_error(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            with mock.patch.dict(serve.__dict__,
                                 {'_WS_FEEDS': {},
                                  '_WS_FEEDS_LOAD_ERROR': None}):
                serve._load_ws_feeds()
                self.assertIn('db unavailable',
                              serve._WS_FEEDS_LOAD_ERROR)


class DesignContextTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/t'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open', side_effect=OSError('no')), \
             mock.patch.object(serve, '_list_design_briefs',
                               return_value=['b1']):
            out = serve._design_context_for_project('p')
        self.assertIn('Raw inspiration briefs', out)


class VpnExitsStopTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.vpn_exits_stop(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=[])
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.vpn_exits_stop(
                req)).status_code, 400)


class StandingTasksCreateTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.standing_tasks_create(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=[])
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.standing_tasks_create(
                req)).status_code, 400)


class CredentialRemoveTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.credential_remove(
                req)).status_code, 403)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.credential_remove(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'name': 'n'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.credential_remove(
                req)).status_code, 404)


class OptIntTest(unittest.TestCase):
    def test_paths(self):
        self.assertIn('integer', serve._standing_task_from_body(
            {'taskId': 't', 'name': 'n', 'agentCount': 'bad'})[1])
        self.assertIn('>=', serve._standing_task_from_body(
            {'taskId': 't', 'name': 'n', 'agentCount': 0})[1])
