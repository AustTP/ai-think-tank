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


class PruneLogsTest(unittest.TestCase):
    def test_memory_cleanup_error(self):
        conn = mock.Mock()
        conn.execute.return_value.rowcount = 0
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_load_env',
                               return_value={'LOG_RETENTION_DAYS': '1'}), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}), \
             mock.patch.object(serve, '_save_escalations'), \
             mock.patch.object(serve, 'cleanup_expired_sessions',
                               side_effect=RuntimeError('mem')):
            self.assertEqual(serve._prune_logs(), 0)


class DownloadStreamTest(unittest.TestCase):
    def _resp(self, geturl='https://y'):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.geturl.return_value = geturl
        resp.status = 200
        resp.read.side_effect = [b'x', b'']
        return resp

    def test_unsafe_final(self):
        with mock.patch.object(serve, '_safe_urlopen',
                               return_value=self._resp()), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=False):
            with self.assertRaises(ValueError):
                serve._download_stream_sync('https://x', {}, '/d', 10)

    def test_write_error(self):
        with mock.patch.object(serve, '_safe_urlopen',
                               return_value=self._resp()), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch('builtins.open', side_effect=OSError('ro')), \
             mock.patch('os.remove', side_effect=OSError('no')):
            with self.assertRaises(OSError):
                serve._download_stream_sync('https://x', {}, '/d', 10)


class LoadVpnExitsTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_load_env',
                               return_value={'VPN_EXIT_PROXIES':
                                             'a=,=h'}), \
             mock.patch('builtins.open', side_effect=OSError('no')):
            self.assertEqual(serve._load_vpn_exits(), {})


class ToolLoopSpendTest(unittest.TestCase):
    def test_task_spend(self):
        data = {'usage': {'cost': 2}, 'choices': [
            {'message': {'content': 'done'}}]}
        with mock.patch.object(serve, '_post_openrouter_raw',
                               return_value=data), \
             mock.patch.object(serve, '_accrue_spend'), \
             mock.patch.object(serve, '_accrue_task_spend') as accrue:
            out = serve._call_agent_tool_loop(
                'm', [], [], lambda n, a: '', task_id='t',
                force_first_tool=True, escalate_model=['other'])
        self.assertEqual(out, 'done')
        accrue.assert_called_with('t', 2)


class GetDataSourceTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(serve._get_data_source('nope', 'd'), 'd')
        with mock.patch.object(serve, 'DATA_SOURCE_KEYS', {'k'}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(serve._get_data_source('k', 'd'), 'd')


class SignalPlansCreateTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.signal_plans_create(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=[])
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.signal_plans_create(
                req)).status_code, 400)


class SetEnvKeysTest(unittest.TestCase):
    def test_append(self):
        with mock.patch.object(serve, '_env_file_path',
                               return_value='/e'), \
             mock.patch('os.path.exists', return_value=True), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='A=1')):
            self.assertEqual(serve._set_env_keys({'B': '2'}), ['B'])


class ScreenshotTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'width': 'bad'})
        with mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda a, s: s), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: True)):
            self.assertEqual(asyncio.run(serve.screenshot(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_village_sandbox_id',
                               return_value=''), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: True)), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.screenshot(
                req)).status_code, 400)


class Oauth2TokenTest(unittest.TestCase):
    def test_expires_fallback(self):
        with mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_OAUTH2_TOKEN_LOCK',
                               mock.MagicMock()), \
             mock.patch.dict(serve.__dict__, {'_OAUTH2_TOKEN_CACHE': {}}), \
             mock.patch.object(serve, '_oauth2_token_request_sync',
                               return_value='{"access_token": "t", '
                                            '"expires_in": "bad"}'), \
             mock.patch('time.time', return_value=1.0):
            self.assertEqual(serve._oauth2_access_token(
                'https://x', 'id', 'sec'), 't')


class ApproveWikiProposalTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'category': 'c'})
        self.assertEqual(asyncio.run(serve.approve_wiki_proposal(
            'a/b', req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'category': 'c'})
        mfields = {'owner': 'o', 'clock': 'c', 'scope': 's',
                   'source': 's', 'review_after_ms': 0}
        with mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, '_is_director_or_admin',
                               return_value=True), \
             mock.patch.object(serve, '_find_wiki_proposal',
                               return_value=({'title': 't'}, 'r')), \
             mock.patch('sim.resolve_memory_fields',
                        return_value=mfields), \
             mock.patch('sim.village_of_agent', return_value='v'), \
             mock.patch('sim.wiki_write_page',
                        return_value=({}, True)), \
             mock.patch('sim.record_memory_provenance'), \
             mock.patch.object(serve, '_wiki_page_target',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.approve_wiki_proposal(
                'p', req)).status_code, 400)


class LibraryRejectTest(unittest.TestCase):
    def test_cross_village(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'path': 'pending_review/f', 'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/p'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_scope_allows',
                               return_value=False), \
             mock.patch.object(serve, '_requester_villages',
                               return_value=[]), \
             mock.patch.object(serve, '_library_path_village',
                               return_value='v2'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.library_reject(
                req)).status_code, 403)


class AssignVillageTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.assign_agent_to_village(
                req)).status_code, 401)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(asyncio.run(serve.assign_agent_to_village(
                req)).status_code, 503)


class AgentIsVerifierTest(unittest.TestCase):
    def test_db_error(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            self.assertFalse(serve._agent_is_verifier('a'))


class WorktreeLinkedTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch('os.path.isdir', return_value=True), \
             mock.patch('os.path.exists', return_value=True), \
             mock.patch.object(serve, '_run_git_sync',
                               return_value=mock.Mock(returncode=1,
                                                      stdout='')):
            self.assertFalse(serve._worktree_is_linked('/w', '/b'))
        with mock.patch('os.path.isdir', return_value=True), \
             mock.patch('os.path.exists', return_value=True), \
             mock.patch.object(serve, '_run_git_sync',
                               return_value=mock.Mock(returncode=0,
                                                      stdout='git')), \
             mock.patch('os.path.isabs', return_value=False), \
             mock.patch('os.path.realpath',
                        side_effect=lambda p: ('/b' if p == '/b'
                                               else '/b/.git')):
            self.assertTrue(serve._worktree_is_linked('/w', '/b'))


class AddCredentialTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.add_credential(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'name': 'n',
                                                'kind': 'login'})
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.add_credential(
                req)).status_code, 400)


class AddHandleTest(unittest.TestCase):
    def test_ttl_fallback(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'agentId': 'a', 'credentialName': 'c', 'purpose': 'p',
            'ttlSec': 'bad'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'mint_capability_handle',
                               return_value=('h', None)), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'):
            body = json.loads(asyncio.run(serve.add_handle(req)).body)
        self.assertEqual(body['handle'], 'h')


class ExecuteTest(unittest.TestCase):
    def _call(self, body, classify=(True, ''), override=(False, 'no')):
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
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_classify_command',
                               new=mock.AsyncMock(return_value=classify)), \
             mock.patch.object(serve, '_supervisor_override_decision',
                               new=mock.AsyncMock(return_value=override)), \
             mock.patch.object(serve, '_sandbox_dir_for',
                               return_value='/s'), \
             mock.patch.object(serve, '_snapshot_sandbox'), \
             mock.patch.object(serve, '_run_in_sandbox_sync',
                               return_value={'exitCode': 0,
                                             'timedOut': False}), \
             mock.patch.object(serve, 'sync_prototypes'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=fake_to_thread):
            return asyncio.run(serve.execute(req))

    def test_paths(self):
        with mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: None)):
            self.assertEqual(self._call({'command': 'x',
                                         'sandboxId': '..'}).status_code,
                             400)
        with mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: True)):
            res = self._call({'command': 'rm'},
                             classify=(False, 'r'),
                             override=(True, 'ok'))
        self.assertTrue(json.loads(res.body)['allowed'])


class WsIngestTest(unittest.TestCase):
    def test_evict_oldest(self):
        bufs = {'s': {'latest': {'t1': {'ts': 1}}, 'history': [],
                     'status': {}}}
        feeds = {'s': {'ws': {'buffer': {'max_topics': 1,
                                         'history': 10}}}}
        with mock.patch.dict(serve.__dict__, {'_WS_BUFFERS': bufs,
                                              '_WS_FEEDS': feeds}), \
             mock.patch.object(serve, '_WS_LOCK', mock.MagicMock()):
            serve._ws_ingest('s', 't2', {}, {})
        self.assertNotIn('t1', bufs['s']['latest'])
        self.assertIn('t2', bufs['s']['latest'])


class IngestHfTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.dict(sys.modules, {
                'pyarrow': mock.MagicMock(),
                'pyarrow.parquet': mock.MagicMock()}), \
             mock.patch.object(serve, '_get_data_source',
                               side_effect=lambda k, d=None: 'https://p'
                               if 'parquet' in k else 'https://s'), \
             mock.patch('urllib.request.urlopen') as uo:
            uo.return_value.__enter__.return_value.read.return_value = \
                b'{"parquet_files": []}'
            self.assertEqual(serve._ingest_hf_open_llm_fallback(
                [{'id': 'm'}]), 0)
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.rowcount = 1
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.dict(sys.modules, {
                'pyarrow': mock.MagicMock(),
                'pyarrow.parquet': mock.MagicMock()}), \
             mock.patch.object(serve, '_get_data_source',
                               side_effect=lambda k, d=None: 'https://p'
                               if 'parquet' in k else 'https://s'), \
             mock.patch('urllib.request.urlopen') as uo, \
             mock.patch.object(serve, '_parse_hf_contents',
                               return_value={'hub': 1}), \
             mock.patch.object(serve, '_benchmark_name_tokens',
                               return_value=set()), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=1.0):
            uo.return_value.__enter__.return_value.read.side_effect = [
                b'{"parquet_files": [{"url": "https://d"}]}', b'x']
            self.assertEqual(serve._ingest_hf_open_llm_fallback(
                [{'id': 'm'}]), 0)


class RequirePlayerOrAdminTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value=None):
            self.assertFalse(serve._require_player_or_admin(req))
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=False):
            self.assertFalse(serve._require_player_or_admin(req))


class SupervisorOfTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(serve._supervisor_of({'agentRoster': []}, 'player'),
                         'player')
        with mock.patch.object(serve, '_admin_agent_id',
                               return_value=None):
            self.assertEqual(serve._supervisor_of(
                {'agentRoster': []}, 'ghost'), 'player')


class IsDirectSupervisorTest(unittest.TestCase):
    def test_paths(self):
        self.assertFalse(serve._is_direct_supervisor(None, 'a', 's'))
        self.assertFalse(serve._is_direct_supervisor(
            {'agentRoster': []}, 'a', 's'))


class LoadApiServicesTest(unittest.TestCase):
    def test_seed_error(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch('builtins.open', side_effect=OSError('no')), \
             mock.patch.object(serve, '_load_api_channels'), \
             mock.patch.dict(serve.__dict__, {
                 '_API_SERVICES': {},
                 '_API_SERVICES_LOAD_ERROR': None}):
            serve._load_api_services()
            self.assertIn('json seed unavailable',
                          serve._API_SERVICES_LOAD_ERROR)


class WriteEgressGrantsTest(unittest.TestCase):
    def test_writes(self):
        with mock.patch.object(serve, 'EGRESS_GRANTS_PATH', '/g'), \
             mock.patch('builtins.open', mock.mock_open()) as op:
            serve._write_egress_grants({'a': 1})
        op.assert_called_once_with('/g', 'w')


class ListAppGrantsTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.list_app_grants(
                req)).status_code, 401)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.list_app_grants(
                req)).status_code, 503)


class VpnExitsViewTest(unittest.TestCase):
    def test_state_error(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_vpn_exit_registry',
                               return_value={}), \
             mock.patch.object(serve, 'vpn_location') as loc:
            loc.location_snapshot.return_value = {}
            loc.LOCATION_TTL_S = 60
            self.assertFalse(json.loads(asyncio.run(
                serve.vpn_exits_view()).body)['configured'])


class EvidenceAuditTest(unittest.TestCase):
    def test_bad_limit(self):
        req = mock.Mock()
        req.query_params = {'limit': 'bad'}
        with mock.patch.object(serve, 'list_audit', return_value=[]):
            self.assertEqual(json.loads(asyncio.run(
                serve.evidence_audit_view(req)).body)['audit'], [])


class ApiAccrueTest(unittest.TestCase):
    def test_apify_bad_usage(self):
        with mock.patch.object(serve, '_accrue_apify_spend') as accrue:
            serve._api_accrue({'kind': 'apify'}, 'json',
                              {'usageTotalUsd': 'bad'})
        accrue.assert_not_called()


class FeedRegistryStatusTest(unittest.TestCase):
    def test_db_error(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            self.assertIsNone(serve._feed_registry_status('t', 'v'))


class OperatorGateTest(unittest.TestCase):
    def test_state_error(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(serve._operator_gate(req), (False, None))


class SublowQuoteTest(unittest.TestCase):
    def test_bad_calls(self):
        req = mock.Mock()
        req.query_params = {'estimatedCalls': 'bad'}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'quote_sublow_grant',
                               return_value={}):
            body = json.loads(asyncio.run(serve.sublow_quote(req)).body)
        self.assertEqual(body['ok'], True)


class TaskArchiveLoadTest(unittest.TestCase):
    def test_operational_error(self):
        with mock.patch.dict(serve.__dict__, {'_TASK_ARCHIVE_CACHE': {
                'loaded': False, 'db': None, 'tasks': {}}}), \
             mock.patch.object(serve, '_db',
                               side_effect=serve.sqlite3.OperationalError(
                                   'no table')):
            self.assertEqual(serve._task_archive_load_locked(), {})


class ResolveHandleTest(unittest.TestCase):
    def _db(self, row):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = row
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        return cm

    def test_missing_cred(self):
        with mock.patch.object(serve, 'verify_capability_handle_scope',
                               return_value={'credential_name': 'c',
                                             'purpose': 'p'}), \
             mock.patch.object(serve, '_db', return_value=self._db(None)):
            self.assertIsNone(serve.resolve_capability_handle(
                'a', 'h', 'GET', 'https://x'))

    def test_raw_token(self):
        with mock.patch.object(serve, 'verify_capability_handle_scope',
                               return_value={'credential_name': 'c',
                                             'purpose': 'p'}), \
             mock.patch.object(serve, '_db',
                               return_value=self._db(('svc', 'enc'))), \
             mock.patch.object(serve, '_open_secret',
                               return_value='5'):
            out = serve.resolve_capability_handle(
                'a', 'h', 'GET', 'https://x')
        self.assertEqual(out['secret'], '5')


class FlatChartTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(serve._flat_director_chart_violations(None), [])
        self.assertEqual(serve._flat_director_chart_violations(
            {'agentRoster': [1]}), [])


class EnsureApiServicesFreshTest(unittest.TestCase):
    def test_db_error(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            serve._ensure_api_services_fresh()


class ApiAccrueDailyTest(unittest.TestCase):
    def test_ledger_error(self):
        with mock.patch.object(serve, '_api_daily_calls_ledger_read',
                               side_effect=RuntimeError('db')):
            serve._api_accrue_daily_call({'id': 's'})


class OpenrouterOnceTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                               return_value=False), \
             mock.patch.object(serve, '_api_key_is_broken',
                               return_value=True):
            with self.assertRaises(RuntimeError):
                serve._call_openrouter_once_sync('m', [], 10)
        import urllib.error
        with mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                               return_value=False), \
             mock.patch.object(serve, '_api_key_is_broken',
                               return_value=False), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               return_value=False), \
             mock.patch.object(serve, '_openrouter_chat_post',
                               side_effect=urllib.error.HTTPError(
                                   'u', 401, 'x', {}, None)), \
             mock.patch.object(serve, 'record_model_result'), \
             mock.patch.object(serve, '_note_api_key_failure') as fail:
            with self.assertRaises(urllib.error.HTTPError):
                serve._call_openrouter_once_sync('m', [], 10)
        fail.assert_called_once()


class GetCharterTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.get_charter_endpoint(
                req)).status_code, 401)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.get_charter_endpoint(
                req)).status_code, 503)


class WsSupervisorLoopTest(unittest.TestCase):
    def test_cancels_stale(self):
        task = mock.Mock()
        tasks = {'s': ('fp', task)}

        async def fake_sleep(_s):
            raise asyncio.CancelledError()
        with mock.patch.dict(serve.__dict__, {'_WS_FEEDS': {},
                                              '_WS_TASKS': tasks}), \
             mock.patch('asyncio.sleep', side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._ws_supervisor_loop())
        task.cancel.assert_called_once()
        self.assertEqual(tasks, {})


class KeyRegistryTest(unittest.TestCase):
    def test_load_and_save(self):
        with mock.patch.object(serve.theo_policy.KeyRegistry, 'load',
                               side_effect=FileNotFoundError('no')):
            self.assertIsNotNone(serve._load_key_registry())
        with mock.patch.object(serve, 'LIBRARY_DIR', '/l'), \
             mock.patch('os.makedirs') as makedirs, \
             mock.patch.object(serve, '_KEY_REGISTRY') as reg:
            serve._save_key_registry()
        makedirs.assert_called_once_with('/l', exist_ok=True)
        reg.save.assert_called_once()


class ResolveEscalationTest(unittest.TestCase):
    def test_api_service_approve(self):
        esc = {'status': 'pending', 'token': 't',
               'kind': 'api service proposal', 'question': 'q'}
        with mock.patch.object(serve, '_load_escalations',
                               return_value={'1': esc}), \
             mock.patch.object(serve, '_save_escalations'), \
             mock.patch.object(serve, '_approve_api_service_proposal',
                               return_value=(True, 'ok')):
            resp = asyncio.run(serve.resolve_escalation('1', 't',
                                                        'approve'))
        self.assertIn('Recorded', resp.body.decode())


class LibraryProvenanceTest(unittest.TestCase):
    def test_bad_details(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [('badjson',)]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, 'LIBRARY_DIR', '/l'), \
             mock.patch('os.path.isdir', return_value=False):
            out = serve._library_provenance_audit(100, 10)
        self.assertEqual(out['by_source'], {'firsthand': 1})


class SmallHelpersPathsTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(serve._ws_substitute('x', 't', {'x': 'v'}), 'v')
        self.assertIsNone(serve._cohens_kappa_binary(['a'], ['a']))
        self.assertEqual(serve._public_dict(
            datetime.datetime(2020, 1, 1)), '2020-01-01T00:00:00')
        with self.assertRaises(ValueError):
            serve._api_workflow_url('https://b', '/x/{y}', {})
        self.assertEqual(serve._api_daily_calls_used({'id': None}), 0)
        self.assertEqual(serve._api_daily_calls_used(None), 0)
        with self.assertRaises(ValueError):
            serve._sandbox_dir_for('')
        self.assertEqual(serve._village_prefix('nope'), '')
        with self.assertRaises(ValueError):
            serve._story_branch_name('nope')
        self.assertIsNone(serve._wiki_page_target('c', '../../x'))
        self.assertEqual(serve._sms_detect_otp(''), None)
        self.assertEqual(serve._sms_mask_otp('b', ''), 'b')
        self.assertEqual(serve._work_context_hosts(''), set())
        self.assertTrue(serve._work_context_clause('task'))
        self.assertIsNone(serve._api_channel_for_host(''))
        self.assertIsNone(serve._api_service_for_host(''))
        serve._index_api_service({'id': 's'})
        self.assertIn('service', serve._validate_credential_body(
            {'name': 'n', 'service': ' '})[3])
        self.assertTrue(serve._director_parent_ok({'agentRoster': []}, None))
        self.assertIn('- x', serve._render_agents_md(
            'n', 'r', {'notes': ['x']}))


class OwnWorkFeedbackTest(unittest.TestCase):
    def test_cap(self):
        state = {'_feedback': {'a': [
            {'text': f'f{i}'} for i in range(9)]}, 'agents': {}}
        out = serve._own_work_feedback_text(state, 'a')
        self.assertIn('f8', out)
        self.assertNotIn('f0', out)


class LibraryVillageTest(unittest.TestCase):
    def test_downloads_path(self):
        with mock.patch('sim_helpers.village_of_agent',
                        return_value='v2'):
            self.assertEqual(serve._library_path_village(
                'pending_review/downloads/a/x.md', {'x': 1}), 'v2')


class MmmuScoresTest(unittest.TestCase):
    def test_short_row(self):
        html = '<table><tr><td>a</td></tr></table>'
        self.assertEqual(serve._mmmu_leaderboard_scores(html), {})


class ContainersEndpointsTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        err = serve.JSONResponse({'e': 1}, status_code=401)
        with mock.patch.object(serve, '_agent_requester',
                               return_value=(None, err)):
            self.assertIs(asyncio.run(serve.containers_shutdown(
                'c', req)), err)
            self.assertIs(asyncio.run(serve.containers_restart(
                'c', req)), err)


class SmallEndpointsTest(unittest.TestCase):
    def test_unauthorized(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.theo_keys_list(
                req)).status_code, 401)
            self.assertEqual(asyncio.run(serve.allowlist_get(
                req)).status_code, 401)
            self.assertEqual(asyncio.run(serve.ingest_library(
                req)).status_code, 401)
            self.assertEqual(asyncio.run(serve.model_tiers_refresh(
                req)).status_code, 401)

    def test_list_villages(self):
        req = mock.Mock()
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.list_villages(
                req)).status_code, 503)

    def test_backups(self):
        self.assertEqual(asyncio.run(serve.sandbox_backups(
            '..')).status_code, 400)
        req = mock.Mock()
        with mock.patch.object(serve, '_require_player_or_admin',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.sandbox_backups_restore(
                req)).status_code, 401)

    def test_revoke_requires_grant(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')):
            self.assertEqual(asyncio.run(serve.sublow_revoke(
                req)).status_code, 400)
            self.assertEqual(asyncio.run(serve.jev_revoke(
                req)).status_code, 400)

    def test_page_probe_sandbox(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_village_sandbox_id',
                               return_value=''), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: True)), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'sync_playwright', object()):
            self.assertEqual(asyncio.run(serve.page_probe(
                req)).status_code, 400)

    def test_sandbox_save_page(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'sandboxId': '..',
                                                'agentId': 'a'})
        with mock.patch.object(serve, '_village_sandbox_id',
                               side_effect=lambda a, s: s), \
             mock.patch.object(serve, '_SANDBOX_ID_RE',
                               mock.MagicMock(match=lambda s: None)):
            self.assertEqual(asyncio.run(serve.sandbox_save_page(
                req)).status_code, 400)

    def test_sms_inbound(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        req.headers = {'X-SMS-Forward-Secret': 's'}
        with mock.patch.object(serve, '_load_env',
                               return_value={'SMS_FORWARD_SECRET': 's'}):
            self.assertEqual(asyncio.run(serve.sms_inbound(
                req)).status_code, 400)


class LibraryAccessTest(unittest.TestCase):
    def test_read_cross_village(self):
        req = mock.Mock()
        req.query_params = {}
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, '_requester_villages',
                               return_value=[]), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}), \
             mock.patch.object(serve, '_scope_allows',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.read_library_file(
                'x', req)).status_code, 404)

    def test_write_invalid(self):
        req = mock.Mock()
        req.headers = {}
        req.json = mock.AsyncMock(return_value={'path': 'x'})
        with mock.patch.object(serve, '_safe_library_path',
                               side_effect=['/tmp/x', None]), \
             mock.patch.object(serve, '_library_namespace_write',
                               return_value='x'), \
             mock.patch.object(serve, '_owns_library_path',
                               return_value=None), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}):
            self.assertEqual(asyncio.run(serve.write_library_file(
                req)).status_code, 400)

    def test_list_agent_files_denied(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}), \
             mock.patch.object(serve, '_cross_village_agent_denied',
                               return_value=True):
            body = json.loads(asyncio.run(serve.list_agent_files(
                'b', req)).body)
        self.assertEqual(body['files'], [])


class CredentialProposeErrorTest(unittest.TestCase):
    def test_validation_error(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_validate_credential_body',
                               return_value=(None, None, None, 'bad')):
            self.assertEqual(asyncio.run(serve.credential_propose(
                req)).status_code, 400)


class SmsConsumeTest(unittest.TestCase):
    def test_missing_message(self):
        cur = mock.Mock()
        cur.fetchone.side_effect = [(1, 1, 9999999999), None]
        conn = mock.Mock()
        conn.execute.return_value = cur
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=0):
            self.assertEqual(serve._sms_consume_grant('a', 'g'),
                             (None, None))


class HardenPermsTest(unittest.TestCase):
    def test_oserror(self):
        with mock.patch('os.path.exists', return_value=True), \
             mock.patch('os.chmod', side_effect=OSError('ro')):
            serve._harden_db_perms('/db')


class ReconcileArchiveTest(unittest.TestCase):
    def test_operational_error(self):
        with mock.patch.object(serve, '_task_archive_load_locked',
                               return_value={}), \
             mock.patch.object(serve, '_TASK_ARCHIVE_LOCK',
                               mock.MagicMock()), \
             mock.patch.object(serve, '_db',
                               side_effect=serve.sqlite3.OperationalError(
                                   'no table')):
            self.assertFalse(serve._reconcile_task_archive(
                {'t': {'x': 1}}))


class HiggsfieldCallTest(unittest.TestCase):
    def test_generic_error(self):
        with mock.patch.object(serve, 'HIGGSFIELD_API_KEY_ID', 'i'), \
             mock.patch.object(serve, 'HIGGSFIELD_API_KEY_SECRET', 's'), \
             mock.patch('urllib.request.urlopen',
                        side_effect=RuntimeError('boom')):
            data, err = serve._higgsfield_call('GET', '/x')
        self.assertIsNone(data)
        self.assertIn('boom', err)


class ReasoningStarvedTest(unittest.TestCase):
    def test_exception(self):
        self.assertFalse(serve._reasoning_starved(None))


class TierValueRatiosTest(unittest.TestCase):
    def test_thin_history(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [
            ('b', 'today', 1.0, 2.0)]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.strftime', return_value='today'), \
             mock.patch('time.gmtime'):
            self.assertEqual(serve.tier_value_ratios(), {'b': 1.0})


class SimVillageTest(unittest.TestCase):
    def test_state_error(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            import sim as _s
            self.assertEqual(serve._sim_village_for_agent('a'),
                             _s.DEFAULT_VILLAGE)


class WsRowSpecTest(unittest.TestCase):
    def test_bad_json(self):
        out = serve._ws_feed_row_to_spec({
            'id': 'i', 'name': 'n', 'ws_spec': 'bad',
            'credential_type': 'none', 'credential_key': None,
            'credential_key2': None, 'status': 'active',
            'proposed_by': 'p'})
        self.assertEqual(out['id'], 'i')


class WriteWikiPageTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'id': 'a/b',
                                                'category': 'c'})
        self.assertEqual(asyncio.run(serve.write_wiki_page(
            req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'id': 'p', 'category': 'think_tank', 'content': 'x'})
        state = {'wiki': {'categories': {'think_tank': {}}}}
        with mock.patch.object(serve, '_state_begin',
                               return_value=state), \
             mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, '_is_director_or_admin',
                               return_value=True), \
             mock.patch('sim.resolve_memory_fields',
                        return_value={'owner': 'o', 'clock': 'c',
                                      'scope': 's', 'source': 's',
                                      'review_after_ms': 0}), \
             mock.patch('sim.village_of_agent', return_value='v'), \
             mock.patch('sim.wiki_write_page', return_value=({}, True)), \
             mock.patch('sim.record_memory_provenance'), \
             mock.patch.object(serve, '_wiki_page_target',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.write_wiki_page(
                req)).status_code, 400)


class AgentAvatarTest(unittest.TestCase):
    def test_read_error(self):
        name = list(serve._AVATAR_ORIENTATIONS)[0] + '.png'
        with mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open', side_effect=OSError('no')):
            self.assertEqual(asyncio.run(serve.get_agent_avatar(
                'a', name)).status_code, 404)
