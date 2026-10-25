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

import core.config as config  # noqa: E402
import serve  # noqa: E402
from test_cov_gap_serve_helpers import (  # noqa: E402
    _NO_GRANT, _SENTINEL, req_with, decision_cache_report_defaults)


class AllowlistStatusTest(unittest.TestCase):
    def test_host_tiers(self):
        with mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=True), \
             mock.patch.object(serve, '_runtime_allowlist_grants',
                               return_value=set()), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='wait'):
            out = serve._allowlist_status_text('Example.COM')
        self.assertIn('env baseline', out)
        self.assertIn('PENDING REQUESTS: none', out)
        with mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=True), \
             mock.patch.object(serve, '_runtime_allowlist_grants',
                               return_value={'x.com'}), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='wait'):
            self.assertIn('runtime grant',
                          serve._allowlist_status_text('x.com'))
        with mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=False), \
             mock.patch.object(serve, '_is_standing_safe_host',
                               return_value=True), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='wait'):
            self.assertIn('standing safe domain',
                          serve._allowlist_status_text('x.com'))
        with mock.patch.object(serve, '_is_allowlisted_host',
                               return_value=False), \
             mock.patch.object(serve, '_is_standing_safe_host',
                               return_value=False), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='wait'):
            self.assertIn('NOT allowlisted',
                          serve._allowlist_status_text('x.com'))

    def test_pending_and_recent_activity(self):
        now = time.time()
        esc = {
            'e1': {'status': 'pending', 'kind': 'allowlist request',
                   'note': 'h1', 'ts': now - 120, 'askCount': 2},
            'e2': {'status': 'pending', 'kind': 'allowlist candidate',
                   'ts': now, 'humanNotified': True},
            'e3': {'status': 'done', 'kind': 'allowlist request'},
        }
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [
            ('allowlist_grant', now - 5, '{"host":"h1"}'),
            ('allowlist_request', now - 10, 'notjson'),
        ]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_load_escalations',
                               return_value=esc), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='wait'):
            out = serve._allowlist_status_text()
        self.assertIn('director pass 2', out)
        self.assertIn('player emailed', out)
        self.assertIn('RECENT ACTIVITY', out)
        self.assertIn('h1', out)
        self.assertIn('notjson', out)


class SpawnWinterTeamTest(unittest.TestCase):
    def test_no_team(self):
        state = {}
        with mock.patch('sim.spawn_new_team_for_request',
                        return_value=None), \
             mock.patch.object(serve, '_ADV_VILLAGE_ID', 'winter'), \
             mock.patch.object(serve, '_ADV_VILLAGE_NAME', 'Winter'):
            self.assertIsNone(serve._spawn_winter_team(
                state, 'admin', 'goal', 1000))
        self.assertEqual(state['villages'][0]['id'], 'winter')

    def test_team_reassigned(self):
        state = {'villages': [{'id': 'winter'}]}
        with mock.patch('sim.spawn_new_team_for_request',
                        return_value={'directorId': 'd1',
                                      'members': ['m1', None]}), \
             mock.patch('sim.set_agent_village') as setv, \
             mock.patch.object(serve, '_ADV_VILLAGE_ID', 'winter'), \
             mock.patch.object(serve, '_ADV_TEAM_SIZE', 2):
            self.assertEqual(serve._spawn_winter_team(
                state, 'admin', 'goal', 1000), 'd1')
        self.assertEqual(setv.call_count, 2)


class WsEndpointTest(unittest.TestCase):
    def _req(self, query=None, headers=None):
        req = mock.Mock()
        req.query_params = query or {}
        req.headers = headers or {}
        req.json = mock.AsyncMock(return_value={})
        return req

    def test_recent_unauthorized_and_unknown(self):
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=False), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = asyncio.run(serve.ws_recent(self._req()))
        self.assertEqual(resp.status_code, 401)
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=False), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, '_WS_FEEDS', {'a': {'name': 'A'}}):
            resp = asyncio.run(serve.ws_recent(self._req(
                query={'service': 'zzz'})))
        self.assertEqual(resp.status_code, 400)

    def test_recent_ok(self):
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_WS_FEEDS',
                               {'a': {'name': 'A'}}), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': True}), \
             mock.patch.object(serve, '_ws_recent',
                               return_value=[{'x': 1}]):
            body = json.loads(asyncio.run(serve.ws_recent(self._req(
                query={'agentId': 'cora', 'max': '5'}))).body)
        self.assertEqual(body['services']['a']['name'], 'A')

    def test_remove(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = asyncio.run(serve.ws_remove(self._req()))
        self.assertEqual(resp.status_code, 403)
        req = self._req()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            resp = asyncio.run(serve.ws_remove(req))
        self.assertEqual(resp.status_code, 400)
        req = self._req()
        req.json = mock.AsyncMock(return_value={'feedId': 'missing'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=None):
            resp = asyncio.run(serve.ws_remove(req))
        self.assertEqual(resp.status_code, 404)
        req = self._req()
        req.json = mock.AsyncMock(return_value={'feedId': 'good'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value={}), \
             mock.patch.object(serve, '_ws_feed_upsert'), \
             mock.patch.object(serve, 'log_action'):
            body = json.loads(asyncio.run(serve.ws_remove(req)).body)
        self.assertEqual(body['status'], 'removed')


class WsCollectorLoopTest(unittest.TestCase):
    class _Conn:
        def __init__(self, recv):
            self.recv = recv
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    class _Connect:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *args):
            return False

    def _run(self, conn_factory, patches):
        async def fake_sleep(_):
            raise asyncio.CancelledError()

        for p in list(patches) + [
                mock.patch('asyncio.sleep', side_effect=fake_sleep)]:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(serve._ws_collector_loop('sid'))

    def _base(self):
        feed = {'ws': {'url': 'wss://x', 'subscribe': {}}}
        return [
            mock.patch.object(serve, '_WS_FEEDS', {'sid': feed}),
            mock.patch.object(serve, '_WS_BUFFERS', {}),
            mock.patch.object(serve, '_ws_subscribe_payload',
                              return_value={'s': 1}),
        ]

    def test_credential_error(self):
        calls = {'n': 0}

        async def sleep_once(_):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise asyncio.CancelledError()

        for p in self._base() + [
                mock.patch.object(serve, '_api_credential_value',
                                  return_value=(None, None, 'bad cred')),
                mock.patch('asyncio.sleep', side_effect=sleep_once)]:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(serve._ws_collector_loop('sid'))

    def test_connect_error(self):
        def boom(*a, **k):
            raise RuntimeError('connect failed')

        self._run(None, self._base() + [
            mock.patch.object(serve, '_api_credential_value',
                              return_value=('tok', None, None)),
            mock.patch('websockets.connect', side_effect=boom)])

    def _collect(self, recv_side, extract_side):
        conn = self._Conn(mock.AsyncMock(side_effect=recv_side))
        calls = {'n': 0}

        def connect_side(*a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                return self._Connect(conn)
            raise RuntimeError('second connect failed')

        self._run(None, self._base() + [
            mock.patch.object(serve, '_api_credential_value',
                              return_value=('tok', None, None)),
            mock.patch('websockets.connect', side_effect=connect_side),
            mock.patch.object(serve, '_ws_extract_frame',
                              side_effect=extract_side),
            mock.patch.object(serve, '_ws_ingest')])

    def test_frames_and_generic_error(self):
        self._collect(['notjson', '{"y":2}', '{"z":3}',
                       RuntimeError('closed')],
                      [(None, {}, {}), ('t', {'a': 1}, {'a': 1})])

    def test_timeout_breaks(self):
        self._collect([asyncio.TimeoutError()], [])


class VpnExitsRefreshTest(unittest.TestCase):
    def _req(self, body=None, raises=False):
        req = mock.Mock()
        if raises:
            req.json = mock.AsyncMock(side_effect=ValueError('empty'))
        else:
            req.json = mock.AsyncMock(return_value=body if body is not None else {})
        return req

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = asyncio.run(serve.vpn_exits_refresh(self._req()))
        self.assertEqual(resp.status_code, 401)

    def test_bad_names(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            resp = asyncio.run(serve.vpn_exits_refresh(
                self._req({'exits': 'one'})))
        self.assertEqual(resp.status_code, 400)

    def test_empty_and_recorded(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_vpn_exit_registry',
                               return_value={'e1': {}}), \
             mock.patch.object(serve.vpn_location, 'refresh_exits',
                               return_value={}), \
             mock.patch.object(serve.vpn_location, 'describe_exit',
                               return_value={'name': 'e1'}), \
             mock.patch.object(serve.vpn_location, 'location_snapshot',
                               return_value={}), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}):
            body = json.loads(asyncio.run(serve.vpn_exits_refresh(
                self._req(raises=True))).body)
        self.assertTrue(body['ok'])
        results = {'e1': {'record': {'zip': '1'}},
                   'e2': {'record': 'bad'}}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_vpn_exit_registry',
                               return_value={'e1': {}, 'e2': {}}), \
             mock.patch.object(serve.vpn_location, 'refresh_exits',
                               return_value=results), \
             mock.patch.object(serve.vpn_location, 'record_location') as rec, \
             mock.patch.object(serve.vpn_location, 'describe_exit',
                               return_value={'name': 'e1'}), \
             mock.patch.object(serve.vpn_location, 'location_snapshot',
                               return_value={}), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}), \
             mock.patch.object(serve, 'save_state_to_db') as save:
            asyncio.run(serve.vpn_exits_refresh(self._req({'exits': []})))
        self.assertEqual(rec.call_count, 1)
        save.assert_called_once()


class UpdateTeamTest(unittest.TestCase):
    def _req(self, body=None, raises=False):
        req = mock.Mock()
        req.headers = {'X-Agent-Key': 'k'}
        if raises:
            req.json = mock.AsyncMock(side_effect=ValueError('bad'))
        else:
            req.json = mock.AsyncMock(return_value=body)
        return req

    def _state(self, agreement=None):
        return {'teams': [{'id': 't1', 'name': 'Old', 'purpose': 'p',
                           'agreement': agreement}]}

    def test_unauthenticated(self):
        with mock.patch.object(serve, '_resolve_requester',
                               return_value=None), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=False):
            resp = asyncio.run(serve.update_team('t1', self._req({})))
        self.assertEqual(resp.status_code, 401)

    def test_key_mismatch_and_state(self):
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=False):
            resp = asyncio.run(serve.update_team('t1', self._req({})))
        self.assertEqual(resp.status_code, 403)
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            resp = asyncio.run(serve.update_team('t1', self._req({})))
        self.assertEqual(resp.status_code, 503)

    def test_unknown_team(self):
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'teams': []}):
            resp = asyncio.run(serve.update_team('t1', self._req({})))
        self.assertEqual(resp.status_code, 404)

    def test_permission_denials(self):
        req = self._req({'agreement': {'x': 1}})
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()), \
             mock.patch.object(serve, '_can_set_team_agreement',
                               return_value=False):
            resp = asyncio.run(serve.update_team('t1', req))
        self.assertEqual(resp.status_code, 403)
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()), \
             mock.patch.object(serve, '_can_write_team',
                               return_value=False):
            resp = asyncio.run(serve.update_team('t1', self._req({'name': 'X'})))
        self.assertEqual(resp.status_code, 403)

    def test_player_edits_everything(self):
        state = self._state(agreement={'old': True})
        with mock.patch.object(serve, '_resolve_requester',
                               return_value=None), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch('core.config.validate_team_agreement',
                        return_value={'new': True}), \
             mock.patch.object(serve, 'save_state_to_db') as save, \
             mock.patch.object(serve, 'log_action') as log:
            body = json.loads(asyncio.run(serve.update_team(
                't1', self._req({'name': 'New', 'purpose': 'P2',
                                 'agreement': {'new': True}}))).body)
        self.assertEqual(body['team']['name'], 'New')
        self.assertEqual(body['team']['purpose'], 'P2')
        self.assertEqual(body['team']['agreement'], {'new': True})
        save.assert_called_once()
        log.assert_called_once()

    def test_agreement_clear_and_invalid(self):
        state = self._state(agreement={'old': True})
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='player'), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_can_set_team_agreement',
                               return_value=True), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            body = json.loads(asyncio.run(serve.update_team(
                't1', self._req({'agreement': None}))).body)
        self.assertIsNone(body['team']['agreement'])
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='player'), \
             mock.patch.object(serve, '_state_begin',
                               return_value=self._state()), \
             mock.patch.object(serve, '_can_set_team_agreement',
                               return_value=True):
            resp = asyncio.run(serve.update_team(
                't1', self._req({'agreement': 'bad'})))
        self.assertEqual(resp.status_code, 400)

    def test_empty_body_no_change(self):
        state = self._state()
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='player'), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'save_state_to_db') as save:
            body = json.loads(asyncio.run(serve.update_team(
                't1', self._req(raises=True))).body)
        self.assertEqual(body['team']['name'], 'Old')
        save.assert_not_called()


class ApiExecuteWorkflowTest(unittest.TestCase):
    def _run(self, spec, responses, agent_id='a1', params=None):
        def request(method, url, headers, body):
            item = responses.pop(0)
            if item == '__bad__':
                return {'body': 'not json'}
            return {'body': json.dumps(item)}
        with mock.patch.object(serve, '_api_workflow_url',
                               side_effect=lambda base, path, values: 'u/' + path.lstrip('/')), \
             mock.patch.object(serve, '_api_request_sync',
                               side_effect=request), \
             mock.patch.object(serve, '_accrue_spend') as accrue, \
             mock.patch('time.sleep'):
            out = serve._api_execute_workflow(
                {'workflow': spec}, 'svc', 'http://b', 'POST', {}, {},
                params or {}, agent_id=agent_id)
        return out, accrue

    def test_full_estimate_flow(self):
        spec = {
            'spend': {'kind': 'estimate_first', 'service': 'svc'},
            'estimate': {'path_template': 'est', 'usd_field': 'cost'},
            'submit': {'path_template': 'submit', 'id_field': 'id',
                       'extra_ids': ['job']},
            'poll': {'path_template': 'poll', 'terminal': ['done'],
                     'success': ['done'], 'interval_s': 0,
                     'timeout_s': 5},
            'finalize': {'path_template': 'final'},
        }
        out, accrue = self._run(spec, [
            {'cost': '2.5'}, {'id': 'j1', 'job': 'x'},
            {'status': 'done'}, {'url': 'u'}])
        self.assertEqual(out['data'], {'url': 'u'})
        self.assertEqual(out['ids']['job'], 'x')
        accrue.assert_called_once_with('svc', 2.5, agent_id='a1')

    def test_submit_errors(self):
        spec = {'submit': {'path_template': 'submit', 'id_field': 'id'}}
        with self.assertRaises(ValueError):
            self._run(spec, [[1]])
        with self.assertRaises(ValueError):
            self._run(spec, [{}])

    def test_poll_sleeps_until_terminal(self):
        spec = {
            'submit': {'path_template': 'submit', 'id_field': 'id'},
            'poll': {'path_template': 'poll', 'terminal': ['done'],
                     'success': ['done'], 'interval_s': 0,
                     'timeout_s': 30},
        }
        out, _ = self._run(spec, [{'id': 'j1'}, {'status': 'RUNNING'},
                                  {'status': 'done'}])
        self.assertEqual(out['status'], 'done')

    def test_submit_invalid_json(self):
        spec = {'submit': {'path_template': 'submit', 'id_field': 'id'}}
        with self.assertRaises(ValueError):
            self._run(spec, ['__bad__'])

    def test_poll_timeout(self):
        spec = {
            'submit': {'path_template': 'submit', 'id_field': 'id'},
            'poll': {'path_template': 'poll', 'terminal': ['done'],
                     'interval_s': 0, 'timeout_s': -1},
        }
        with self.assertRaises(TimeoutError):
            self._run(spec, [{'id': 'j1'}, {'status': 'RUNNING'}])

    def test_balance_delta_charges_real_cost(self):
        spec = {
            'spend': {'kind': 'balance_delta', 'service': 'svc',
                      'balance_path': 'bal'},
            'submit': {'path_template': 'submit', 'id_field': 'id'},
            'poll': {'terminal': ['done'], 'success': ['done']},
        }
        out, accrue = self._run(spec, [
            {'balance': 10}, {'id': 'j1', 'status': 'done'},
            {'balance': 7}])
        accrue.assert_called_once_with('svc', 3.0, agent_id='a1')

    def test_bad_estimate_and_non_numeric_balance(self):
        spec = {
            'spend': {'kind': 'estimate_first', 'service': 'svc'},
            'estimate': {'path_template': 'est'},
            'submit': {'path_template': 'submit', 'id_field': 'id'},
            'poll': {'terminal': ['done'], 'success': ['done']},
        }
        out, accrue = self._run(spec, [
            {'usd': 'bad'}, {'id': 'j1', 'status': 'done'}])
        self.assertEqual(out['usd'], 0.0)
        accrue.assert_not_called()
        spec2 = {
            'spend': {'kind': 'balance_delta', 'service': 'svc',
                      'balance_path': 'bal'},
            'submit': {'path_template': 'submit', 'id_field': 'id'},
            'poll': {'terminal': ['done'], 'success': ['done']},
        }
        out, accrue = self._run(spec2, [
            {'balance': 'x'}, {'id': 'j1', 'status': 'done'},
            {'balance': 'y'}])
        accrue.assert_not_called()


class TierRecheckTest(unittest.TestCase):
    def test_guards(self):
        serve._queue_tier_recheck_if_availability('', 'c', 'model not found')
        serve._queue_tier_recheck_if_availability('slug', 'c', 'timeout')
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            serve._queue_tier_recheck_if_availability(
                'slug', 'c', 'model not found')

    def test_queue_and_cooldown(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [
            ('mid',), ('low',), ('high',)]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        queue = {'low': 1.0}
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_TIER_RECHECK_DRAINED',
                               {'mid': 995.0}), \
             mock.patch.object(serve, '_TIER_RECHECK_QUEUE', queue), \
             mock.patch.object(serve, 'TIER_RECHECK_COOLDOWN_S', 10), \
             mock.patch('time.time', return_value=1000.0):
            serve._queue_tier_recheck_if_availability(
                'slug', 'c', 'model_not_found')
        self.assertNotIn('mid', queue)
        self.assertEqual(queue['high'], 1000.0)


class ReadStoreToolTest(unittest.TestCase):
    def test_success(self):
        rows = [{'a': 1}]
        with mock.patch('core.fs.read_store', return_value=rows) as rs, \
             mock.patch.object(serve, 'log_action') as log:
            out = serve._read_store_tool('cora', {
                'scope': 's', 'path': 'p', 'query': 'SELECT 1',
                'gzipColumns': ['g'], 'protoColumns': ['q'],
                'maxRows': 5})
        self.assertIn('"a": 1', out)
        rs.assert_called_once_with('s', 'p', 'SELECT 1', gzip_columns=['g'],
                                   proto_columns=['q'], max_rows=5)
        log.assert_called_once()

    def test_errors(self):
        with mock.patch('core.fs.read_store',
                        side_effect=PermissionError('denied')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._read_store_tool('c', {}),
                             'Denied: denied')
        log.assert_called_once()
        with mock.patch('core.fs.read_store',
                        side_effect=FileNotFoundError('gone')):
            self.assertEqual(serve._read_store_tool('c', {}),
                             'Not found: gone')
        with mock.patch('core.fs.read_store', side_effect=RuntimeError('boom')):
            self.assertEqual(serve._read_store_tool('c', {}),
                             'read_store failed: boom')


class AppScriptToolTest(unittest.TestCase):
    def test_success_and_errors(self):
        with mock.patch('core.fs.app_script', return_value='done'), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._app_script_tool(
                'cora', {'bundleId': 'b', 'script': 's'}), 'done')
        log.assert_called_once()
        with mock.patch('core.fs.app_script',
                        side_effect=PermissionError('no')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._app_script_tool('c', {}), 'Denied: no')
        log.assert_called_once()
        with mock.patch('core.fs.app_script',
                        side_effect=RuntimeError('boom')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._app_script_tool('c', {}),
                             'app_script failed: boom')
        log.assert_called_once()


class BreakdownSharedBacklogTest(unittest.TestCase):
    def _call(self, state, goal='goal', admin='admin', constraints=None,
              reply=None, side_effect=None, add_side_effect=None):
        with mock.patch.object(serve, 'get_or_create_agent_key',
                               return_value='k'), \
             mock.patch.object(serve, '_resolve_model_tier',
                               return_value='high'), \
             mock.patch.object(serve, '_http_json', return_value=reply,
                               side_effect=side_effect), \
             mock.patch('sim.create_or_reuse_feature',
                        return_value={'id': 'f1'}), \
             mock.patch('sim.add_backlog_item',
                        side_effect=add_side_effect):
            return serve._breakdown_into_shared_backlog(
                state, goal, admin, constraints)

    def test_no_admin(self):
        self.assertIsNone(self._call({}, admin=''))

    def test_model_failures(self):
        self.assertIsNone(self._call({}, reply={'error': 'x'}))
        self.assertIsNone(self._call({}, reply={'reply': 'not json'}))
        self.assertIsNone(self._call({}, reply={'reply': '{"items": []}'}))
        self.assertIsNone(self._call(
            {}, reply={'reply': '{"items": [{"type": "x"}]}'}))

    def test_constraints_and_items(self):
        constraints = {'budgetUsd': 10, 'constraints': ['no x'],
                       'roles': ['dev'], 'order': ['a', 'b'],
                       'mustInclude': ['c']}
        reply = {'reply': json.dumps({'feature': 'F', 'items': [
            {'title': 'A', 'type': 'story', 'never': ['n1'],
             'budgetUsd': 3.5, 'acceptanceCriteria': 'ac',
             'sizeEstimate': 'S'},
            {'title': 'B', 'never': 'bad', 'budgetUsd': 0},
        ]})}
        items = [{'id': 'i1'}, None]
        out = self._call({}, constraints=constraints, reply=reply,
                         add_side_effect=items)
        self.assertEqual(out['feature']['id'], 'f1')
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(out['items'][0]['budgetUsd'], 3.5)

    def test_all_items_dropped(self):
        reply = {'reply': '{"items": [{"title": "A"}]}'}
        self.assertIsNone(self._call({}, reply=reply,
                                     add_side_effect=[None]))


class ListMyHandlesTest(unittest.TestCase):
    def _req(self, query, key='k'):
        req = mock.Mock()
        req.query_params = query
        req.headers = {'X-Agent-Key': key}
        return req

    def test_unauthorized(self):
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=False):
            resp = asyncio.run(serve.list_my_handles(self._req({})))
        self.assertEqual(resp.status_code, 401)

    def test_lists_handles(self):
        now = time.time()
        rows = [
            ('h1', 'c1', 'p1', '["a"]', '["GET"]', now + 60),
            ('h2', 'c2', 'p2', ['b'], ['POST'], now - 5),
            ('h3', 'c3', 'p3', 'notjson', 'notjson', now + 10),
        ]
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = rows
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_db', return_value=cm):
            body = json.loads(asyncio.run(serve.list_my_handles(
                self._req({'agentId': 'cora'}))).body)
        self.assertEqual(body['handles'][0]['allowedHosts'], ['a'])
        self.assertEqual(body['handles'][1]['allowedHosts'], ['b'])
        self.assertEqual(body['handles'][2]['allowedHosts'], 'notjson')
        self.assertEqual(body['handles'][1]['expiresInSec'], 0)


class EgressGrantTest(unittest.TestCase):
    def test_no_host(self):
        with mock.patch.object(serve, '_write_egress_grants') as write:
            serve._grant_egress_host('')
            serve._revoke_egress_host('')
        write.assert_not_called()

    def test_grant_merges_existing(self):
        with mock.patch('builtins.open',
                        mock.mock_open(read_data='{"a.com": 5}')), \
             mock.patch.object(serve, '_write_egress_grants') as write, \
             mock.patch('time.time', return_value=100.0):
            serve._grant_egress_host('Example.COM')
        grants = write.call_args[0][0]
        self.assertEqual(grants['a.com'], 5)
        self.assertEqual(grants['example.com'],
                         100.0 + serve._EGRESS_GRANT_TTL_S)

    def test_grant_read_failure(self):
        with mock.patch('builtins.open', side_effect=OSError('no')), \
             mock.patch.object(serve, '_write_egress_grants') as write, \
             mock.patch('time.time', return_value=100.0):
            serve._grant_egress_host('x.com')
        self.assertEqual(list(write.call_args[0][0]), ['x.com'])

    def test_revoke(self):
        with mock.patch('builtins.open',
                        mock.mock_open(read_data='{"a.com": 5}')), \
             mock.patch.object(serve, '_write_egress_grants') as write:
            serve._revoke_egress_host('A.com')
        self.assertEqual(write.call_args[0][0], {})
        with mock.patch('builtins.open', side_effect=OSError('no')), \
             mock.patch.object(serve, '_write_egress_grants') as write:
            serve._revoke_egress_host('a.com')
        self.assertEqual(write.call_args[0][0], {})


class RunBrowserActSyncTest(unittest.TestCase):
    def _call(self, action=None, isfile=None, open_mock=None,
              docker=None, allowlisted=False):
        action = action or {'op': 'goto', 'url': 'https://example.com/x'}
        patches = [
            mock.patch.object(serve, 'tempfile',
                              **{'mkdtemp.return_value': '/tmp/act'}),
            mock.patch.object(serve, 'shutil'),
            mock.patch.object(serve, '_grant_egress_host'),
            mock.patch.object(serve, '_revoke_egress_host'),
            mock.patch.object(serve, '_is_allowlisted_host',
                              return_value=allowlisted),
            mock.patch('os.makedirs'),
        ]
        if isfile is not None:
            patches.append(mock.patch('os.path.isfile', side_effect=isfile))
        if open_mock is not None:
            patches.append(mock.patch('builtins.open', open_mock))
        if docker is not None:
            patches.append(mock.patch.object(serve, '_run_docker_sandbox',
                                             side_effect=docker))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return serve._run_browser_act_sync('/sandbox', action)

    def test_action_write_failure(self):
        with mock.patch('builtins.open', side_effect=OSError('disk')):
            out = self._call()
        self.assertIsNone(out[0])
        self.assertIn('could not write', out[1])

    def test_timeout(self):
        import subprocess
        out = self._call(
            open_mock=mock.mock_open(),
            docker=subprocess.TimeoutExpired('x', 1))
        self.assertEqual(out, (None, 'browser action timed out in sandbox'))

    def test_result_file_with_screenshot(self):
        out = self._call(
            isfile=[True, True],
            open_mock=mock.mock_open(read_data=b'{"ok": true}'),
            docker=lambda *a: mock.Mock())
        self.assertTrue(out[0]['ok'])
        self.assertTrue(out[0]['_shotB64'])

    def test_result_file_bad_json_falls_back(self):
        result = mock.Mock(stdout='{"ok": false}', returncode=0, stderr='')
        out = self._call(
            isfile=[True, False, False],
            open_mock=mock.mock_open(read_data='not json'),
            docker=lambda *a: result)
        self.assertFalse(out[0]['ok'])

    def test_stdout_unparseable(self):
        result = mock.Mock(stdout='nope', returncode=3, stderr='err')
        out = self._call(isfile=[False], open_mock=mock.mock_open(),
                         docker=lambda *a: result)
        self.assertIsNone(out[0])
        self.assertIn('no parseable result', out[1])

    def test_urlparse_failure_and_stdout_shot(self):
        result = mock.Mock(stdout='{"ok": true}', returncode=0, stderr='')
        with mock.patch('urllib.parse.urlparse',
                        side_effect=ValueError('bad')):
            out = self._call(isfile=[False, False], open_mock=mock.mock_open(),
                             docker=lambda *a: result)
        self.assertTrue(out[0]['ok'])
        out = self._call(isfile=[False, True],
                         open_mock=mock.mock_open(read_data=b'img'),
                         docker=lambda *a: result)
        self.assertTrue(out[0]['_shotB64'])

    def test_allowlisted_host_skips_grants(self):
        result = mock.Mock(stdout='{"ok": true}', returncode=0, stderr='')
        out = self._call(isfile=[False, False], open_mock=mock.mock_open(),
                         docker=lambda *a: result, allowlisted=True)
        self.assertTrue(out[0]['ok'])


class DesignReferenceUrlTest(unittest.TestCase):
    def _call(self, body, actor='player', safe_path=None, brief=None,
              makedirs=None, write=None, urlparse=None, safe_host=True):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        patches = [
            mock.patch.object(serve, '_operator_gate',
                              return_value=(True, actor)),
            mock.patch.object(serve, '_is_safe_public_host',
                              return_value=safe_host),
            mock.patch.object(serve, '_screenshot_url_sync',
                              return_value='img'),
            mock.patch.object(serve, '_design_brief_from_image',
                              return_value=brief or {'ok': True,
                                                     'brief': 'body'}),
            mock.patch.object(serve, '_design_project_rel',
                              return_value='design-references/main'),
            mock.patch.object(serve, '_safe_library_path',
                              side_effect=safe_path or
                              ['/tmp/design', '/tmp/design/host.txt']),
            mock.patch.object(serve, 'log_action'),
        ]
        if makedirs is not None:
            patches.append(mock.patch('os.makedirs', side_effect=makedirs))
        else:
            patches.append(mock.patch('os.makedirs'))
        if write is not None:
            patches.append(mock.patch.object(serve, '_write_file',
                                             side_effect=write))
        else:
            patches.append(mock.patch.object(serve, '_write_file'))
        if urlparse is not None:
            patches.append(mock.patch('urllib.parse.urlparse',
                                      side_effect=urlparse))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.design_reference_url(req))

    def test_unauthorized_and_claim_mismatch(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(
                serve.design_reference_url(mock.Mock())).status_code, 401)
        resp = self._call({'agentId': 'other',
                           'url': 'https://a.com'})
        self.assertEqual(resp.status_code, 403)

    def test_input_guards(self):
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(
            self._call({'url': 'https://a.com'},
                       urlparse=ValueError('bad')).status_code, 400)
        self.assertEqual(self._call({'url': 'ftp://a'}).status_code, 400)
        self.assertEqual(
            self._call({'url': 'https://a.com'},
                       safe_host=False).status_code, 400)

    def test_screenshot_and_brief_failures(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_screenshot_url_sync',
                               return_value=None):
            resp = asyncio.run(serve.design_reference_url(
                mock.Mock(json=mock.AsyncMock(
                    return_value={'url': 'https://a.com'}))))
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self._call(
            {'url': 'https://a.com'},
            brief={'ok': False, 'note': 'nope'}).status_code, 502)

    def test_path_and_write_failures(self):
        self.assertEqual(self._call(
            {'url': 'https://a.com'}, safe_path=[None]).status_code, 400)
        self.assertEqual(self._call(
            {'url': 'https://a.com'},
            makedirs=OSError('disk')).status_code, 500)
        self.assertEqual(self._call(
            {'url': 'https://a.com'},
            safe_path=['/tmp/design', None]).status_code, 200)
        self.assertEqual(self._call(
            {'url': 'https://a.com'},
            write=OSError('disk')).status_code, 500)

    def test_success(self):
        body = json.loads(self._call({'url': 'https://a.com'}).body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['briefPath'],
                         'design-references/main/a.com.txt')
