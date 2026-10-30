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


class DesignReferenceTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        return req

    def _call(self, body, actor='player', safe=None, brief=None,
              name='ok', makedirs=None, write=None, decode=None):
        patches = [
            mock.patch.object(serve, '_operator_gate',
                              return_value=(True, actor)),
            mock.patch.object(serve, '_safe_design_reference_name',
                              return_value=name),
            mock.patch.object(serve, '_design_project_rel',
                              return_value='design-references/main'),
            mock.patch.object(serve, '_design_brief_from_image',
                              return_value=brief or {'ok': True,
                                                     'brief': 'b'}),
            mock.patch.object(serve, '_safe_library_path',
                              side_effect=safe or [
                                  '/tmp/d', '/tmp/d/ok.png',
                                  '/tmp/d/ok.txt']),
            mock.patch.object(serve, 'log_action'),
            mock.patch('builtins.open', mock.mock_open()),
        ]
        patches.append(mock.patch('os.makedirs', side_effect=makedirs)
                       if makedirs else mock.patch('os.makedirs'))
        patches.append(mock.patch.object(serve, '_write_file',
                                         side_effect=write)
                       if write else mock.patch.object(serve, '_write_file'))
        if decode:
            patches.append(mock.patch('base64.b64decode',
                                      side_effect=decode))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.design_reference(self._req(body)))

    def test_guards(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.design_reference(
                mock.Mock())).status_code, 401)
        self.assertEqual(self._call({'agentId': 'x'}).status_code, 403)
        self.assertEqual(self._call({}, name='').status_code, 400)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'imageBase64': 'x'},
                                    decode=ValueError('b')).status_code, 400)
        big = b'x' * (12 * 1024 * 1024 + 1)
        self.assertEqual(self._call({}, decode=None, safe=None,
                                    brief=None,
                                    **{'decode': None}).status_code, 400) \
            if False else None
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_safe_design_reference_name',
                               return_value='ok'), \
             mock.patch.object(serve, '_design_project_rel',
                               return_value='d'), \
             mock.patch('base64.b64decode', return_value=big):
            self.assertEqual(asyncio.run(serve.design_reference(
                self._req({'imageBase64': 'x'}))).status_code, 400)

    def test_path_and_brief_failures(self):
        self.assertEqual(self._call({'imageBase64': 'eA=='},
                                    safe=[None]).status_code, 400)
        self.assertEqual(self._call({'imageBase64': 'eA=='},
                                    makedirs=OSError('d')).status_code, 500)
        self.assertEqual(self._call({'imageBase64': 'eA=='},
                                    safe=['/tmp/d', None]).status_code, 400)
        self.assertEqual(self._call(
            {'imageBase64': 'eA=='},
            brief={'ok': False, 'note': 'no'}).status_code, 502)
        self.assertEqual(self._call({'imageBase64': 'eA=='},
                                    write=OSError('d')).status_code, 500)

    def test_success_and_extensions(self):
        png = __import__('base64').b64encode(
            b'\x89PNG\r\n\x1a\nxx').decode()
        jpg = __import__('base64').b64encode(b'\xff\xd8\xffxx').decode()
        webp = __import__('base64').b64encode(b'RIFFxxxx').decode()
        for payload in (png, jpg, webp):
            body = json.loads(self._call(
                {'imageBase64': payload}).body)
            self.assertTrue(body['ok'])
        text = __import__('base64').b64encode(b'plain').decode()
        body = json.loads(self._call({'imageBase64': text}).body)
        self.assertTrue(body['image'].endswith('.png'))


class LoadApiChannelsTest(unittest.TestCase):
    def _call(self, exists=True, data=None, open_side=None):
        patches = [
            mock.patch.object(serve, 'API_SERVICES_PATH', '/tmp/a.json'),
            mock.patch('os.path.exists', return_value=exists),
        ]
        if open_side is not None:
            patches.append(mock.patch('builtins.open', side_effect=open_side))
        else:
            patches.append(mock.patch('builtins.open',
                                      mock.mock_open(read_data=json.dumps(data))))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        serve._load_api_channels()

    def test_missing_file(self):
        with mock.patch.dict(serve.__dict__, {'_API_CHANNELS': {},
                                              '_API_CHANNELS_LOAD_ERROR': 'x'}):
            self._call(exists=False)
            self.assertEqual(serve._API_CHANNELS, {})
            self.assertIsNone(serve._API_CHANNELS_LOAD_ERROR)

    def test_valid_channels(self):
        data = {'channels': {
            'comment': 'x',
            'bad': 'not-dict',
            'c1': {'hosts': ['.Example.COM'], 'backends': ['svc']},
        }}
        with mock.patch.dict(serve.__dict__, {'_API_CHANNELS': {},
                                              '_API_CHANNELS_LOAD_ERROR': None}):
            self._call(data=data)
            self.assertIn('example.com', serve._API_CHANNELS)
            self.assertEqual(serve._API_CHANNELS['example.com']['id'], 'c1')

    def test_invalid_channels(self):
        cases = [
            {'channels': {'c': {'hosts': [], 'backends': ['b']}}},
            {'channels': {'c': {'hosts': ['nodot'], 'backends': ['b']}}},
        ]
        for data in cases:
            with mock.patch.dict(serve.__dict__, {'_API_CHANNELS': {},
                                                  '_API_CHANNELS_LOAD_ERROR': None}):
                self._call(data=data)
                self.assertIn('channel c', serve._API_CHANNELS_LOAD_ERROR)
        with mock.patch.dict(serve.__dict__, {'_API_CHANNELS': {},
                                              '_API_CHANNELS_LOAD_ERROR': None}):
            self._call(open_side=OSError('disk'))
            self.assertEqual(serve._API_CHANNELS_LOAD_ERROR, 'disk')


class ListFileGrantsTest(unittest.TestCase):
    def test_all_paths(self):
        req = mock.Mock()
        req.cookies = {'sid': 'c'}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.list_file_grants(
                req)).status_code, 401)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.list_file_grants(
                req)).status_code, 503)
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'agents': {}}), \
             mock.patch.object(serve, '_effective_file_grants',
                               return_value=[{'id': 'g', 'scope': 's',
                                              'label': 'L'}]):
            body = json.loads(asyncio.run(serve.list_file_grants(
                req)).body)
        self.assertEqual(body['grants'][0]['caps'], {})


class BudgetDirectorSetTest(unittest.TestCase):
    def _req(self, body, cookies=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.cookies = cookies or {}
        return req

    def _call(self, body, state=None, player=False, admin=False,
              supervisor=False, cookies=None, json_side=None):
        req = mock.Mock()
        if json_side is not None:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        req.cookies = cookies or {}
        patches = [
            mock.patch.object(serve, '_operator_gate',
                              return_value=(True, 'actor')),
            mock.patch.object(serve, 'verify_session',
                              return_value=player),
            mock.patch.object(serve, '_state_begin', return_value=state),
            mock.patch.object(serve, '_is_admin', return_value=admin),
            mock.patch.object(serve, '_is_direct_supervisor',
                              return_value=supervisor),
            mock.patch.object(serve, 'save_state_to_db'),
            mock.patch.object(serve, 'log_action'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.budget_director_set(req))

    def test_guards(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.budget_director_set(
                self._req({}))).status_code, 401)
        self.assertEqual(self._call({}, json_side=ValueError('x')).status_code,
                         400)
        self.assertEqual(self._call('notdict').status_code, 400)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd',
                                     'budgetUsd': 'bad'}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd',
                                     'budgetUsd': -1}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd',
                                     'budgetUsd': float('nan')}).status_code,
                         400)
        self.assertEqual(self._call({'directorId': 'd', 'budgetUsd': 1},
                                    state=None).status_code, 503)
        self.assertEqual(self._call({'directorId': 'd', 'budgetUsd': 1},
                                    state={'teams': []}).status_code, 400)

    def test_permission_and_set(self):
        state = {'agentRoster': [{'id': 'd1'}],
                 'teams': [{'id': 't1', 'directorId': 'd1'}],
                 'budgetRequests': [
                     {'directorId': 'd1', 'status': 'pending',
                      'requestedUsd': 5},
                     {'directorId': 'd1', 'status': 'pending',
                      'requestedUsd': 'bad'},
                     {'directorId': 'd1', 'status': 'granted'},
                 ]}
        self.assertEqual(self._call({'directorId': 'd1', 'budgetUsd': 1},
                                    state=state).status_code, 403)
        body = json.loads(self._call(
            {'directorId': 'd1', 'budgetUsd': 6}, state=state,
            player=True).body)
        self.assertEqual(body['capUsd'], 6.0)
        self.assertEqual(body['requestsGranted'], 2)
        body = json.loads(self._call(
            {'directorId': 'd1', 'budgetUsd': None}, state=state,
            admin=True).body)
        self.assertEqual(body['capUsd'], 0.0)
        self.assertNotIn('d1', state['directorBudgets'])

    def test_team_known_and_supervisor(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}]}
        body = json.loads(self._call(
            {'directorId': 't1', 'budgetUsd': 2}, state=state,
            supervisor=True).body)
        self.assertTrue(body['ok'])


class BudgetRequestTest(unittest.TestCase):
    def _req(self, body, cookies=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.cookies = cookies or {}
        req.headers = {'X-Agent-Key': 'k'}
        return req

    def _call(self, body, player=False, requester='d1', key=True,
              state=None, admin_id='admin', mail=None, json_side=None):
        req = mock.Mock()
        if json_side is not None:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        req.cookies = {}
        req.headers = {'X-Agent-Key': 'k'}
        patches = [
            mock.patch.object(serve, 'verify_session', return_value=player),
            mock.patch.object(serve, '_resolve_requester',
                              return_value=requester),
            mock.patch.object(serve, 'verify_agent_key',
                              return_value=True if key else False),
            mock.patch.object(serve, '_state_begin', return_value=state),
            mock.patch.object(serve, '_is_admin', return_value=False),
            mock.patch.object(serve, '_admin_agent_id',
                              return_value=admin_id),
            mock.patch('sim._deliver_mail', side_effect=mail),
            mock.patch.object(serve, 'save_state_to_db'),
            mock.patch.object(serve, 'log_action'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.budget_request(req))

    def test_guards(self):
        self.assertEqual(self._call({}, requester=None).status_code, 401)
        self.assertEqual(self._call({}, key=False).status_code, 403)
        self.assertEqual(self._call({}, json_side=ValueError('x')).status_code,
                         400)
        self.assertEqual(self._call('notdict').status_code, 400)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd',
                                     'requestedUsd': 'bad'}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd',
                                     'requestedUsd': 0}).status_code, 400)
        self.assertEqual(self._call({'directorId': 'd', 'requestedUsd': 1},
                                    state=None).status_code, 503)
        self.assertEqual(self._call({'directorId': 'd', 'requestedUsd': 1},
                                    state={'teams': []}).status_code, 400)
        state = {'teams': [{'directorId': 'd1'}]}
        self.assertEqual(self._call({'directorId': 'd1', 'requestedUsd': 1},
                                    state=state,
                                    requester='other').status_code, 403)

    def test_success_mail_and_failure(self):
        state = {'teams': [{'directorId': 'd1'}],
                 'directorBudgets': {'d1': 2}}
        body = json.loads(self._call(
            {'directorId': 'd1', 'requestedUsd': 5, 'reason': 'r'},
            state=state).body)
        self.assertTrue(body['mailed'])
        self.assertEqual(body['mailedTo'], 'admin')
        self.assertEqual(body['request']['currentUsd'], 2.0)
        state = {'teams': [{'directorId': 'd1'}]}
        body = json.loads(self._call(
            {'directorId': 'd1', 'requestedUsd': 5}, state=state,
            mail=RuntimeError('mail')).body)
        self.assertFalse(body['mailed'])
        state = {'teams': [{'directorId': 'd1'}]}
        body = json.loads(self._call(
            {'directorId': 'd1', 'requestedUsd': 5}, state=state,
            player=True, requester='player', admin_id='player').body)
        self.assertEqual(body['mailedTo'], 'player')


class WsApproveTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        return req

    def _call(self, body, authorized=True, existing='proposed', db=None,
              validate=('sid', {'id': 'sid'}, None)):
        patches = [
            mock.patch.object(serve, '_operator_gate',
                              return_value=(authorized, 'actor')),
            mock.patch.object(serve, '_feed_registry_status',
                              return_value=existing),
            mock.patch.object(serve, '_ws_feed_row_to_spec',
                              return_value={}),
            mock.patch.object(serve, '_validate_ws_feed_spec',
                              return_value=validate),
            mock.patch.object(serve, '_ws_feed_upsert'),
            mock.patch.object(serve, 'log_action'),
        ]
        if db is not None:
            patches.append(mock.patch.object(serve, '_db', side_effect=db))
        else:
            conn = mock.Mock()
            conn.execute.return_value.fetchone.return_value = {'id': 'sid'}
            cm = mock.MagicMock()
            cm.__enter__.return_value = conn
            patches.append(mock.patch.object(serve, '_db', return_value=cm))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.ws_approve(self._req(body)))

    def test_guards(self):
        self.assertEqual(self._call({}, authorized=False).status_code, 403)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'feedId': 'x'},
                                    existing=None).status_code, 404)
        self.assertEqual(self._call({'feedId': 'x'},
                                    existing='active').status_code, 409)
        self.assertEqual(self._call({'feedId': 'x'},
                                    db=RuntimeError('db')).status_code, 503)
        self.assertEqual(self._call(
            {'feedId': 'x'},
            validate=('x', {}, 'bad')).status_code, 400)

    def test_approve_and_deny(self):
        body = json.loads(self._call({'feedId': 'X'}).body)
        self.assertEqual(body['status'], 'active')
        body = json.loads(self._call({'feedId': 'X',
                                      'decision': 'deny'}).body)
        self.assertEqual(body['status'], 'denied')


class TierPriceWatchLoopTest(unittest.TestCase):
    def _run(self, moves=None, side_effect=None):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise asyncio.CancelledError()

        refresh = mock.AsyncMock()
        with mock.patch('asyncio.sleep', side_effect=fake_sleep), \
             mock.patch.object(serve, '_tier_watch_moves_sync',
                               return_value=moves, side_effect=side_effect), \
             mock.patch.object(serve, '_tier_refresh_logged', refresh):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._tier_price_watch_loop())
        return refresh

    def test_moves_and_errors(self):
        self.assertIsNotNone(self._run(moves={'mid'}))
        self.assertIsNotNone(self._run(moves=set()))
        self.assertIsNotNone(self._run(side_effect=RuntimeError('boom')))


class AvatarLoopTest(unittest.TestCase):
    def _run(self, state=None, available=True, complete=None,
             in_flight=None, side_effect=None, sleep_side=None):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if sleep_side:
                raise sleep_side
            raise asyncio.CancelledError()

        threads = []
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=state,
                               side_effect=side_effect), \
             mock.patch.object(serve, '_pixellab_avatar_available',
                               return_value=available), \
             mock.patch.object(serve, '_avatar_set_complete',
                               side_effect=complete or (lambda a: False)), \
             mock.patch.object(serve, '_AVATAR_IN_FLIGHT', in_flight or {}), \
             mock.patch('threading.Thread',
                        side_effect=lambda **kw: threads.append(kw) or
                        mock.Mock()), \
             mock.patch('asyncio.sleep', side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._avatar_loop())
        return threads

    def test_threads_started(self):
        state = {'agentRoster': [{'id': 'admin', 'isAdmin': True},
                                 {'id': 'a1', 'isDirector': True}],
                 'agents': {'admin': {}, 'a1': {'name': 'A'},
                            'a2': {'name': 'B'}}}
        threads = self._run(state=state,
                            complete=lambda a: a == 'a2',
                            in_flight={'a1': True})
        self.assertEqual(threads, [])
        threads = self._run(state=state,
                            complete=lambda a: False)
        self.assertEqual(len(threads), 2)
        self.assertTrue(threads[0]['args'][3])

    def test_no_agents_and_exception(self):
        self.assertEqual(self._run(state={'agents': {}}), [])
        self.assertEqual(self._run(side_effect=RuntimeError('db')), [])


class RssPollLoopTest(unittest.TestCase):
    def test_polls_due_feeds(self):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] >= 3:
                raise asyncio.CancelledError()

        feeds = {'a': {'url': 'u', 'interval_s': 1},
                 'b': {'url': 'u', 'interval_s': 600},
                 'c': {'url': 'u', 'interval_s': 1}}
        status = {'a': {'last_fetch_at': 0}, 'b': {'last_fetch_at': 1000}}
        with mock.patch.object(serve, '_RSS_FEEDS', feeds), \
             mock.patch.object(serve, '_RSS_STATUS', status), \
             mock.patch.object(serve, '_fetch_feed_sync',
                               side_effect=[['item'], RuntimeError('bad')]), \
             mock.patch.object(serve, '_rss_ingest', return_value=1), \
             mock.patch('time.time', return_value=1000.0), \
             mock.patch('asyncio.sleep', side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._rss_poll_loop())
        self.assertTrue(status['a']['ok'])
        self.assertFalse(status['c']['ok'])
        self.assertIn('bad', status['c']['last_error'])


class ApiServiceEndpointsTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        return req

    def test_request_arms(self):
        def call(rate=True, validate=('sid', {'name': 'S',
                                              'base_url': 'http://s'},
                                      None), esc=None):
            with mock.patch.object(serve, 'check_rate_limit',
                                   return_value=rate), \
                 mock.patch.object(serve, 'verify_agent_key',
                                   return_value=True), \
                 mock.patch.object(serve, '_validate_api_service_spec',
                                   return_value=validate), \
                 mock.patch.object(serve, '_api_service_spec_to_note',
                                   return_value='note'), \
                 mock.patch.object(serve, '_load_escalations',
                                   return_value=esc or {}), \
                 mock.patch.object(serve, 'create_escalation',
                                   return_value='e1'), \
                 mock.patch.object(serve, 'log_action'):
                return asyncio.run(serve.api_service_request(
                    self._req({'agentId': 'cora',
                               'service': {}})))
        self.assertEqual(call(rate=False).status_code, 429)
        self.assertEqual(call(validate=('x', {}, 'bad')).status_code, 400)
        body = json.loads(call(esc={
            'e0': {'kind': 'api service proposal', 'status': 'pending',
                   'note': 'notjson'},
            'e1': {'kind': 'api service proposal', 'status': 'pending',
                   'note': '{"id": "sid"}'},
            'e3': {'kind': 'other', 'status': 'pending'}}).body)
        self.assertEqual(body['escalationId'], 'e1')
        body = json.loads(call().body)
        self.assertEqual(body['serviceId'], 'sid')

    def test_list(self):
        specs = {
            'svc': {'name': 'S', 'base_url': 'http://s',
                    'auth': {'type': 'token'}, 'methods': ['GET'],
                    'spend': {'kind': 'metered', 'note': 'n'},
                    'workflow': {'submit': {}}},
            'host.com': {'name': 'skip'},
        }
        with mock.patch.object(serve, '_ensure_api_services_fresh'), \
             mock.patch.object(serve, '_API_SERVICES', specs):
            body = json.loads(asyncio.run(serve.api_service_list()).body)
        self.assertEqual(len(body['services']), 1)
        self.assertTrue(body['services'][0]['hasWorkflow'])


class OpenrouterChatPostTest(unittest.TestCase):
    def _err(self, code=400, body=b'reasoning is mandatory', read_fail=False):
        import io
        import urllib.error
        if read_fail:
            class _FP:
                def read(self, *a):
                    raise RuntimeError('read')

                def close(self):
                    return None
            fp = _FP()
        else:
            fp = io.BytesIO(body)
        return urllib.error.HTTPError('u', code, 'x', {}, fp)

    def test_success(self):
        with mock.patch.object(serve, '_urlopen_with_resilience',
                               return_value='{"ok": 1}'), \
             mock.patch.object(serve.training_capture, 'record'):
            self.assertEqual(serve._openrouter_chat_post({'model': 'm'}),
                             {'ok': 1})

    def test_mandatory_retry_success(self):
        with mock.patch.object(serve, '_urlopen_with_resilience',
                               side_effect=[self._err(),
                                            '{"ok": 2}']), \
             mock.patch.object(serve.training_capture, 'record'):
            self.assertEqual(serve._openrouter_chat_post({'model': 'm'}),
                             {'ok': 2})

    def test_detail_read_failure_and_retry_error(self):
        with mock.patch.object(serve, '_urlopen_with_resilience',
                               side_effect=self._err(read_fail=True,
                                                     code=500)), \
             mock.patch.object(serve.training_capture, 'record'), \
             mock.patch.object(serve, '_queue_tier_recheck_if_availability') as q:
            with self.assertRaises(Exception):
                serve._openrouter_chat_post({'model': 'm'})
        q.assert_called_once()
        with mock.patch.object(serve, '_urlopen_with_resilience',
                               side_effect=[self._err(), self._err(
                                   read_fail=True, code=429)]), \
             mock.patch.object(serve.training_capture, 'record'), \
             mock.patch.object(serve, '_queue_tier_recheck_if_availability') as q:
            with self.assertRaises(Exception):
                serve._openrouter_chat_post({'model': 'm'})
        q.assert_called_once()


class OpenrouterReasoningOffTest(unittest.TestCase):
    def _call(self, tank=False, broken=False, circuit=False, post=None,
              side=None):
        with mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                               return_value=tank), \
             mock.patch.object(serve, '_api_key_is_broken',
                               return_value=broken), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               return_value=circuit), \
             mock.patch.object(serve, '_openrouter_chat_post',
                               return_value=post, side_effect=side), \
             mock.patch.object(serve, 'record_model_result') as rec, \
             mock.patch.object(serve, '_note_api_key_failure') as fail, \
             mock.patch.object(serve, '_note_api_key_success') as ok:
            try:
                out = serve._call_openrouter_sync_reasoning_off('m', [], 10)
            finally:
                self._last = (rec, fail, ok)
        return out

    def test_guards(self):
        for kw in ({'tank': True}, {'broken': True}, {'circuit': True}):
            with self.assertRaises(RuntimeError):
                self._call(**kw)

    def test_http_error(self):
        import urllib.error
        err = urllib.error.HTTPError('u', 401, 'x', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self._call(side=err)
        self._last[1].assert_called_once()

    def test_generic_error_and_success(self):
        with self.assertRaises(RuntimeError):
            self._call(side=RuntimeError('boom'))
        self._last[0].assert_called_with('m', success=False)
        self.assertEqual(self._call(post={'ok': 1}), {'ok': 1})
        self._last[2].assert_called_once()


class EnsureStoryWorktreeTest(unittest.TestCase):
    def _call(self, identity=None, head=1, branch=1, add_rc=0,
              isdir=False, linked=False, add_rcs=None):
        calls = []
        add_codes = list(add_rcs) if add_rcs is not None else None

        def git(_dir, args, *a, **k):
            calls.append(args)
            if args[:3] == ['rev-parse', '--verify', 'HEAD']:
                return mock.Mock(returncode=head)
            if args[:3] == ['rev-parse', '--verify', 'story/x']:
                return mock.Mock(returncode=branch)
            if args[:2] == ['worktree', 'add']:
                if add_codes:
                    return mock.Mock(returncode=add_codes.pop(0))
                return mock.Mock(returncode=add_rc)
            return mock.Mock(returncode=0)

        patches = [
            mock.patch.object(serve, 'SANDBOXES_DIR', '/tmp/sbx'),
            mock.patch.object(serve, '_sandbox_dir_for', return_value='/tmp/b'),
            mock.patch.object(serve, '_base_sandbox_for_story',
                              return_value='base'),
            mock.patch.object(serve, '_ensure_sandbox_git_repo'),
            mock.patch.object(serve, '_git_identity_configured',
                              side_effect=identity or [True, True]),
            mock.patch.object(serve, '_run_git_sync', side_effect=git),
            mock.patch.object(serve, '_story_branch_name',
                              return_value='story/x'),
            mock.patch.object(serve, '_worktree_is_linked',
                              return_value=linked),
            mock.patch('os.path.isdir', return_value=isdir),
            mock.patch('os.listdir', return_value=['f']),
            mock.patch('os.path.exists', return_value=False),
            mock.patch('os.makedirs'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return serve.ensure_story_worktree('s1'), calls

    def test_branch_exists_and_config(self):
        out, calls = self._call(identity=[False, False], head=1, branch=0)
        self.assertEqual(out, '/tmp/sbx/s1')
        self.assertIn(['config', 'user.email',
                       'sandbox-backup@ai-think-tank.local'], calls)
        self.assertIn(['add', '-A'], calls)
        self.assertIn(['worktree', 'add', '/tmp/sbx/s1', 'story/x'], calls)

    def test_new_branch_and_failure(self):
        out, calls = self._call(head=0, branch=1)
        self.assertIn(['worktree', 'add', '-b', 'story/x', '/tmp/sbx/s1',
                       'HEAD'], calls)
        with self.assertRaises(ValueError):
            self._call(head=0, branch=1, add_rc=1)

    def test_stale_registration_prunes_and_retries(self):
        """2026-10-30 Tier-2: a crashed story run left a missing-but-
        registered worktree; every retry of the task then failed at
        creation. The add must prune once and retry before giving up."""
        out, calls = self._call(head=0, branch=1, add_rcs=[1, 0])
        add_calls = [c for c in calls if c[:2] == ['worktree', 'add']]
        self.assertEqual(len(add_calls), 2, 'prune must be followed by one retry')
        self.assertIn(['worktree', 'prune'], calls)
        self.assertEqual(out, '/tmp/sbx/s1')


class ApiExecuteUnitTest(unittest.TestCase):
    def _call(self, url='https://a/x', workflow=False, service=None,
              wf=None, wf_side=None, build=None):
        patches = [
            mock.patch.object(serve, '_ensure_api_services_fresh'),
            mock.patch.object(serve, '_api_service_for_host',
                              return_value=service),
            mock.patch.object(serve, '_is_safe_public_host',
                              return_value=True),
            mock.patch.object(serve, '_api_channel_for_host',
                              return_value=None),
            mock.patch.object(serve, '_api_service_path_allowed',
                              return_value=True),
            mock.patch.object(serve, '_api_credential_value',
                              return_value=(None, None, None)),
            mock.patch.object(serve, '_apify_budget_exceeded',
                              return_value=False),
            mock.patch.object(serve, '_api_daily_calls_exceeded',
                              return_value=False),
            mock.patch.object(serve, '_api_execute_workflow',
                              return_value=wf, side_effect=wf_side),
            mock.patch.object(serve, '_api_build_text',
                              return_value=(None, None, 'wrapped',
                                            'instr')),
            mock.patch.object(serve, '_api_request_sync',
                              return_value={'status': 200,
                                            'finalUrl': url}),
            mock.patch.object(serve, '_api_accrue'),
            mock.patch.object(serve, '_api_accrue_daily_call'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return serve._api_execute('cora', 'k', url, workflow=workflow)

    def test_guards(self):
        self.assertFalse(self._call(url='ftp://a')['allowed'])
        with mock.patch.object(serve, '_is_safe_public_host',
                               return_value=False):
            res = serve._api_execute('cora', 'k', 'https://a')
        self.assertFalse(res['allowed'])

    def test_workflow_success_and_error(self):
        svc = {'name': 'S', 'workflow': {'submit': {}}}
        res = self._call(service=svc, workflow=True,
                         wf={'data': {'x': 1}, 'contentType': 'text',
                             'ids': {'i': 1}, 'usd': 0.5})
        self.assertTrue(res['ok'])
        self.assertEqual(res['data'], {'x': 1})
        res = self._call(service=svc, workflow=True,
                         wf_side=RuntimeError('wf'))
        self.assertFalse(res['ok'])
        self.assertIn('workflow failed', res['error'])


class DownloadFileTest(unittest.TestCase):
    def _call(self, body, rate=True, authorized=True, safe=True,
              grant=_SENTINEL, allowlisted=False, gate=True,
              trusted=True, stream=None, stream_side=None,
              stream_error=False, open_side=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        patches = [
            mock.patch.object(serve, 'check_rate_limit', return_value=rate),
            mock.patch.object(serve, 'verify_agent_key',
                              return_value=authorized),
            mock.patch.object(serve, '_login_safe_url', return_value=safe),
            mock.patch.object(serve, 'resolve_capability_handle',
                              return_value=({'kind': 'token',
                                             'credential_name': 'c',
                                             'secret': 's'}
                                            if grant is _SENTINEL
                                            else grant)),
            mock.patch.object(serve, '_capability_auth_headers',
                              return_value={'X-Token': 't'}),
            mock.patch.object(serve, '_is_allowlisted_host',
                              return_value=allowlisted),
            mock.patch.object(serve, '_work_trust_from_body',
                              return_value=trusted),
            mock.patch.object(serve, '_is_work_context_host',
                              return_value=True),
            mock.patch.object(serve, '_jev_quorum_decision',
                              new=mock.AsyncMock(
                                  return_value=('allow', 0.9, 0.0, 't'))),
            mock.patch.object(serve, '_jev_safety_gate',
                              return_value=gate),
            mock.patch.object(serve, '_download_stream_sync',
                              return_value=stream,
                              side_effect=stream_side),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_append_passport_decision'),
            mock.patch('os.makedirs'),
        ]
        if open_side is not None:
            patches.append(mock.patch('builtins.open',
                                      side_effect=open_side))
        else:
            patches.append(mock.patch('builtins.open',
                                      mock.mock_open(read_data=b'xx')))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.download_file(req))

    def test_guards(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, authorized=False).status_code, 401)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'url': 'u'}, safe=False).status_code,
                         400)

    def test_handle_guards(self):
        body = {'url': 'https://a/f', 'capabilityHandle': 'h'}
        self.assertEqual(self._call(body, grant=None).status_code, 403)
        self.assertEqual(self._call(
            body, grant={'kind': 'login', 'credential_name': 'c',
                         'secret': 's'}).status_code, 400)

    def test_allowlist_and_jev(self):
        body = {'url': 'https://a/f', 'maxBytes': 'bad'}
        resp = self._call(body, allowlisted=True,
                          stream={'bytes': 1, 'finalUrl': 'u',
                                  'status': 200})
        self.assertTrue(json.loads(resp.body)['ok'])
        self.assertEqual(self._call(body, gate=False,
                                    trusted=False).status_code, 403)

    def test_stream_failure_and_success(self):
        body = {'url': 'https://a/f', 'capabilityHandle': 'h'}
        self.assertEqual(self._call(body, stream_side=RuntimeError('x')
                                    ).status_code, 400)
        resp = self._call(body, stream={'bytes': 5, 'finalUrl': 'u',
                                        'status': 200})
        data = json.loads(resp.body)
        self.assertTrue(data['ok'])
        self.assertEqual(data['bytes'], 5)
        resp = self._call(body, stream={'bytes': 5, 'finalUrl': 'u',
                                        'status': 200},
                          open_side=OSError('no'))
        self.assertIsNone(json.loads(resp.body)['sha256'])


class IngestHfFallbackTest(unittest.TestCase):
    def _call(self, models=None, parquet='p', source='s', urlopen=None,
              hf=None, tokens=None):
        conn = mock.Mock()
        conn.execute.return_value.rowcount = 1
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.dict(sys.modules, {'pyarrow': mock.Mock(),
                                           'pyarrow.parquet': mock.Mock()}), \
             mock.patch.object(serve, '_get_data_source',
                               side_effect=lambda k: parquet
                               if 'parquet' in k else source), \
             mock.patch('urllib.request.urlopen', side_effect=urlopen,
                        **({} if urlopen else
                           {'return_value': mock.MagicMock(
                               __enter__=lambda s: s,
                               read=lambda *a: b'{"parquet_files": '
                               b'[{"url": "d"}]}')})), \
             mock.patch.object(serve, '_parse_hf_contents',
                               return_value=hf or {}), \
             mock.patch.object(serve, '_benchmark_name_tokens',
                               side_effect=tokens or
                               (lambda x: set(__import__('re').split(
                                   r'[^a-z0-9]+', str(x).lower())))), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=1.0):
            return serve._ingest_hf_open_llm_fallback(
                models or [{'id': 'm1'}])

    def test_skips(self):
        with mock.patch.dict(sys.modules, {'pyarrow': None,
                                           'pyarrow.parquet': None}):
            self.assertEqual(serve._ingest_hf_open_llm_fallback([]), 0)
        self.assertEqual(self._call(parquet=''), 0)
        self.assertEqual(self._call(urlopen=RuntimeError('net')), 0)
        self.assertEqual(self._call(hf={}), 0)

    def test_matches(self):
        models = [{'id': 'a', 'hugging_face_id': 'X/Y', 'name': 'A'},
                  {'id': 'b', 'name': 'B'},
                  {'id': 'c', 'name': 'C'}]
        out = self._call(models=models, hf={'x/y': 1.0, 'b-extra': 2.0})
        self.assertEqual(out, 2)
