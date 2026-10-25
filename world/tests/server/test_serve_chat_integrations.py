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


class ChatEndpointTest(unittest.TestCase):
    def _call(self, body, key='k', session=False, cap=False, gate_block=None,
              openrouter=None, side_effect=None, retry=None,
              retry_side=None, capture=False, settings=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.cookies = {}
        req.headers = {'X-Agent-Key': 'k'}
        patches = [
            mock.patch.object(serve, 'OPENROUTER_API_KEY', key),
            mock.patch.object(serve, 'verify_session', return_value=session),
            mock.patch.object(serve, 'verify_agent_key', return_value=True),
            mock.patch.object(serve, '_input_guard_violation',
                              return_value=None),
            mock.patch.object(serve, '_task_budget_exhausted',
                              return_value=False),
            mock.patch.object(serve, '_agent_spend_cap_exceeded',
                              return_value=cap),
            mock.patch.object(serve, '_director_budget_gate',
                              return_value=gate_block),
            mock.patch.object(serve, '_apply_plain_writing',
                              side_effect=lambda m: m),
            mock.patch.object(serve, '_ttc_should_deliberate',
                              return_value=False),
            mock.patch.object(serve, '_ttc_best_of', return_value=1),
            mock.patch.object(serve, '_call_openrouter_sync',
                              return_value=openrouter,
                              side_effect=side_effect),
            mock.patch.object(serve, '_call_openrouter_sync_reasoning_off',
                              return_value=retry,
                              side_effect=retry_side),
            mock.patch.object(serve, '_sim_village_for_agent',
                              return_value='v'),
            mock.patch.object(serve, '_accrue_spend'),
            mock.patch.object(serve, '_accrue_task_spend'),
            mock.patch.object(serve, '_accrue_high_tier_spend'),
            mock.patch.object(serve, '_high_tier_slug',
                              return_value=body.get('model')),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve.training_capture, 'enabled',
                              return_value=capture),
            mock.patch.object(serve.training_capture, 'metadata',
                              return_value={'m': 1}),
            mock.patch.object(serve.training_capture, 'CURRENT',
                              mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.chat(req))

    def test_no_key_and_guards(self):
        self.assertEqual(self._call({'model': 'm', 'messages': []},
                                    key='').status_code, 500)
        self.assertEqual(self._call({'model': 'm', 'messages': []},
                                    session=False).status_code, 400)
        self.assertEqual(self._call({'messages': []}).status_code, 400)

    def test_budget_and_director(self):
        body = {'model': 'm', 'messages': [{'role': 'user',
                                            'content': 'hi'}],
                'agentId': 'a', 'taskId': 't', 'max_tokens': 'bad'}
        self.assertEqual(self._call(body, cap=True).status_code, 429)
        block = serve.JSONResponse({'error': 'blocked'}, status_code=403)
        self.assertIs(self._call(body, gate_block=block), block)

    def test_capture_and_plain_list_content(self):
        body = {'model': 'm',
                'messages': [{'role': 'user',
                              'content': [{'text': 'hi'}]}],
                'agentId': 'a', 'taskId': 't'}
        resp = self._call(body, capture=True,
                          openrouter={'choices': [{'message': {
                              'content': 'ok'}}],
                                      'usage': {'cost': 0.5}})
        self.assertEqual(resp.status_code, 200)

    def test_empty_reply_retry(self):
        body = {'model': 'm', 'messages': [{'role': 'user',
                                            'content': 'hi'}],
                'agentId': 'a'}
        resp = self._call(body,
                          openrouter={'choices': [{'message': {
                              'content': ''}}], 'usage': {}},
                          retry={'choices': [{'message': {
                              'content': 'x'}}], 'usage': {}})
        self.assertEqual(json.loads(resp.body)['reply'], 'x')
        resp = self._call(body,
                          openrouter={'choices': [{'message': {
                              'content': ''}}], 'usage': {}},
                          retry={'choices': [{'message': {
                              'content': ''}}], 'usage': {}})
        self.assertEqual(json.loads(resp.body)['reply'], '')
        resp = self._call(body,
                          openrouter={'choices': [{'message': {
                              'content': ''}}], 'usage': {}},
                          retry_side=RuntimeError('retry'))
        self.assertEqual(json.loads(resp.body)['reply'], '')


class InitDbTest(unittest.TestCase):
    def test_migration_branches(self):
        conn = mock.Mock()
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        cursor.rowcount = 0
        conn.execute.return_value = cursor
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm):
            serve.init_db()
        sql = ' '.join(str(c.args[0]) for c in conn.execute.call_args_list
                       if c.args)
        self.assertIn('ALTER TABLE model_benchmark_scores', sql)
        self.assertIn('ALTER TABLE external_credentials', sql)
        self.assertIn('ALTER TABLE api_services', sql)


class SmallHelpersTest(unittest.TestCase):
    def test_wiki_page_body(self):
        with mock.patch.object(serve, 'LIBRARY_DIR', '/lib'), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='body')):
            self.assertEqual(serve._wiki_page_body('p', 'c'), 'body')
        with mock.patch.object(serve, 'LIBRARY_DIR', '/lib'), \
             mock.patch('builtins.open', side_effect=OSError('no')):
            self.assertEqual(serve._wiki_page_body('p', ''), '')

    def test_fernet_migration(self):
        fernet = mock.MagicMock()
        fernet.generate_key.return_value = b'key'
        with mock.patch('cryptography.fernet.Fernet', fernet), \
             mock.patch.object(serve, '_FERNET_EDEK_PATH', '/e/key'), \
             mock.patch.object(serve, '_LEGACY_FERNET_EDEK_PATH', '/old'), \
             mock.patch.object(serve, '_FERNET_EDEK_DIR', '/e'), \
             mock.patch('os.path.exists',
                        side_effect=[False, True, False]), \
             mock.patch('os.makedirs'), \
             mock.patch('shutil.copyfile') as copy, \
             mock.patch('os.chmod'), \
             mock.patch('builtins.open', mock.mock_open(
                 read_data='key')):
            serve._fernet()
        copy.assert_called_once_with('/old', '/e/key')
        with mock.patch('cryptography.fernet.Fernet', fernet), \
             mock.patch.object(serve, '_FERNET_EDEK_PATH', '/e/key'), \
             mock.patch.object(serve, '_LEGACY_FERNET_EDEK_PATH', '/old'), \
             mock.patch.object(serve, '_FERNET_EDEK_DIR', '/e'), \
             mock.patch('os.path.exists',
                        side_effect=[False, True, True]), \
             mock.patch('os.makedirs', side_effect=OSError('no')), \
             mock.patch('shutil.copyfile'), \
             mock.patch('builtins.open', mock.mock_open(
                 read_data='key')):
            serve._fernet()

    def test_list_design_briefs(self):
        with mock.patch.object(serve, '_design_project_rel',
                               return_value='d'), \
             mock.patch.object(serve, '_safe_library_path',
                               return_value=None):
            self.assertEqual(serve._list_design_briefs('p'), [])
        with mock.patch.object(serve, '_design_project_rel',
                               return_value='d'), \
             mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/d'), \
             mock.patch('os.path.isdir', return_value=True), \
             mock.patch('os.listdir', return_value=['b.txt', 'taste.md',
                                                    'x.png']):
            self.assertEqual(serve._list_design_briefs('p'),
                             ['d/b.txt'])

    def test_safe_director(self):
        roster = [{'id': 'admin', 'isAdmin': True},
                  {'id': 'd1', 'isDirector': True},
                  {'id': 'd2', 'isDirector': True},
                  {'id': 'w1'}]
        state = {'agentRoster': roster}
        with mock.patch.object(serve, '_direct_reports',
                               return_value=[]):
            self.assertEqual(serve._safe_director(
                state, 'w1', 'd1'), 'd1')
            self.assertEqual(serve._safe_director(
                state, 'd1', 'd2'), 'admin')
            self.assertEqual(serve._safe_director(
                state, 'w1', 'admin'), 'd1')
            self.assertEqual(serve._safe_director({}, 'w', ''), '')
        no_senior = {'agentRoster': [{'id': 'admin', 'isAdmin': True},
                                     {'id': 'w1'}, {'id': 'w2'}]}
        with mock.patch.object(serve, '_direct_reports',
                               side_effect=lambda s, a: ['w2']
                               if a == 'w2' else []):
            self.assertEqual(serve._safe_director(no_senior, 'w1',
                                                  'admin'), 'w2')
        none = {'agentRoster': [{'id': 'admin', 'isAdmin': True},
                                {'id': 'w1'}]}
        with mock.patch.object(serve, '_direct_reports',
                               return_value=[]):
            self.assertEqual(serve._safe_director(none, 'w1', 'admin'),
                             'admin')

    def test_load_container_broker(self):
        with mock.patch.dict('os.environ', {
                'CONTAINER_MAX_CONTAINERS': 'bad',
                'CONTAINER_APPROVED_IMAGES': 'a, b',
                'CONTAINER_EGRESS_PROFILES': 'direct=,vpn=h1|h2'}), \
             mock.patch.object(serve, 'containers') as containers:
            serve._load_container_broker()
        kwargs = containers.ContainerBroker.call_args.kwargs
        self.assertEqual(kwargs['max_containers'], 8)
        self.assertEqual(kwargs['approved_images'], ['a', 'b'])
        self.assertEqual(kwargs['egress_profiles']['vpn'], ['h1', 'h2'])
        with mock.patch.dict('os.environ', {}, clear=True), \
             mock.patch.object(serve, 'containers') as containers:
            serve._load_container_broker()
        kwargs = containers.ContainerBroker.call_args.kwargs
        self.assertIn('python:3.12-slim', kwargs['approved_images'])

    def test_memory_ledger(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.memory_ledger(
                mock.Mock())).status_code, 503)
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value={'memoryLedger': [1, 2]}):
            body = json.loads(asyncio.run(serve.memory_ledger(
                mock.Mock())).body)
        self.assertEqual(body['ledger'], [2, 1])

    def test_signals_emit(self):
        def call(body, json_side=None):
            req = mock.Mock()
            if json_side:
                req.json = mock.AsyncMock(side_effect=json_side)
            else:
                req.json = mock.AsyncMock(return_value=body)
            with mock.patch.object(serve, '_operator_gate',
                                   return_value=(True, 'actor')), \
                 mock.patch.object(serve, 'get_state_from_db',
                                   return_value={}), \
                 mock.patch('sim.emit_signals', return_value=[]), \
                 mock.patch('sim.task_completed_data',
                           return_value='td'), \
                 mock.patch.object(serve, 'save_state_to_db'), \
                 mock.patch.object(serve, 'log_action'), \
                 mock.patch.object(serve, '_public_dict',
                                   side_effect=lambda x: x):
                return asyncio.run(serve.signals_emit(req))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.signals_emit(
                mock.Mock())).status_code, 401)
        self.assertEqual(call(None, json_side=ValueError('x')).status_code,
                         400)
        self.assertEqual(call([]).status_code, 400)
        self.assertEqual(call({}).status_code, 400)
        self.assertEqual(call({'subject': 's', 'data': 1}).status_code, 400)
        self.assertTrue(json.loads(call({'subject': 's',
                                         'task': {}}).body)['ok'])
        self.assertTrue(json.loads(call({'subject': 's',
                                         'task': ''}).body)['ok'])
        self.assertTrue(json.loads(call({'subject': 's',
                                         'task': 5}).body)['ok'])


class AvatarEnsureTest(unittest.TestCase):
    def _call(self, complete=False, in_flight=None, pix=True, out=None,
              desc_side=None, urls=None):
        patches = [
            mock.patch.object(serve, '_avatar_set_complete',
                              side_effect=complete if callable(complete)
                              else (lambda a: complete)),
            mock.patch.object(serve, '_pixellab_avatar_available',
                              return_value=pix),
            mock.patch.object(serve, '_AVATAR_IN_FLIGHT',
                              in_flight if in_flight is not None else set()),
            mock.patch.object(serve, 'get_or_create_agent_key',
                              return_value='k'),
            mock.patch.object(serve, '_avatar_description',
                              return_value='d', side_effect=desc_side),
            mock.patch.object(serve, '_api_base_url', return_value='b'),
            mock.patch.object(serve, '_api_execute',
                              return_value=out or {'ok': True, 'data': {
                                  'rotation_urls': urls if urls is not None
                                  else {'south': 'https://x'}}}),
            mock.patch.object(serve, '_avatar_dir', return_value='/a'),
            mock.patch.object(serve, '_avatar_orientation_name',
                              return_value='south'),
            mock.patch('os.makedirs'),
            mock.patch('urllib.request.urlopen',
                        return_value=mock.MagicMock(
                            __enter__=lambda s: s,
                            read=lambda *a: b'img')),
            mock.patch('builtins.open', mock.mock_open()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return serve._ensure_agent_avatar('a1', 'A', 'worker')

    def test_paths(self):
        self.assertTrue(self._call(complete=lambda a: True))
        self.assertFalse(self._call(in_flight={'a1'}))
        self.assertFalse(self._call(out={'ok': True, 'data': {}}))
        self.assertTrue(self._call(complete=lambda a: True,
                                   urls={'d': '', 's': 'https://x'}))
        self.assertFalse(self._call(desc_side=RuntimeError('x')))


class HiggsfieldGenerateTest(unittest.TestCase):
    def _call(self, kind='image', args=None, out=None):
        with mock.patch.object(serve, '_api_base_url',
                               return_value='b'), \
             mock.patch.object(serve, '_api_execute',
                               return_value=out), \
             mock.patch.object(serve, '_higgsfield_log_manifest') as man:
            res = serve._higgsfield_generate(kind, args or {}, 'a', 'k')
        return res, man

    def test_errors(self):
        res, _ = self._call(args={'prompt': 'p'}, out={'ok': False})
        self.assertIn('Could not generate', res)
        res, _ = self._call(kind='video',
                            args={'prompt': 'p', 'duration': 'bad'},
                            out={'ok': False})
        self.assertIn('Could not generate', res)
        res, _ = self._call(args={'prompt': 'p'},
                            out={'ok': True, 'data': {'status': 'failed'}})
        self.assertIn('did not complete', res)
        res, _ = self._call(args={'prompt': 'p'},
                            out={'ok': True, 'data': {'status': 'completed'}})
        self.assertIn('no output URL', res)

    def test_success_with_videos(self):
        res, man = self._call(args={'prompt': 'p'}, out={
            'ok': True, 'usd': 1.5,
            'data': {'status': 'completed', 'videos': [{'url': 'v'}],
                     'video': {'url': 'v2'}, 'images': [{'url': 'i'}]}})
        self.assertIn('v', res)
        man.assert_called_once()


class MullvadAccessTokenTest(unittest.TestCase):
    def _call(self, url='https://m/x', raw='{"access_token": "t"}',
              safe=True):
        with mock.patch.object(serve, '_is_safe_public_host',
                               return_value=safe), \
             mock.patch.object(serve, '_mullvad_token_request_sync',
                               return_value=raw), \
             mock.patch.object(serve, '_MULLVAD_TOKEN_LOCK',
                               mock.MagicMock()), \
             mock.patch.object(serve, '_MULLVAD_TOKEN_CACHE', {}), \
             mock.patch.object(serve, 'MULLVAD_TOKEN_REFRESH_MARGIN_S',
                               60), \
             mock.patch('time.time', return_value=1000.0):
            return serve._mullvad_access_token(url, 'acc')

    def test_paths(self):
        with self.assertRaises(ValueError):
            self._call(url='http://x')
        with self.assertRaises(ValueError):
            self._call(safe=False)
        with self.assertRaises(ValueError):
            self._call(raw='notjson')
        with self.assertRaises(ValueError):
            self._call(raw='{}')
        self.assertEqual(self._call(
            raw='{"access_token": "t", "expiry": "bad"}'), 't')
        self.assertEqual(self._call(raw='{"access_token": "t"}'), 't')


class ApiMailSendTest(unittest.TestCase):
    def _call(self, body, player=False, owner='a', state=None,
              json_side=None, routed=None, admin='admin'):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        req.cookies = {}
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'verify_session',
                               return_value=player), \
             mock.patch.object(serve, '_agent_id_for_key',
                               return_value=owner), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_admin_agent_id',
                               return_value=admin), \
             mock.patch.object(serve, '_route_player_request',
                               new=mock.AsyncMock(
                                   return_value=routed or {})), \
             mock.patch('sim._append_mailbox') as append, \
             mock.patch('sim._queue_player_email'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            res = asyncio.run(serve.api_mail_send(req))
        return res, append

    def test_guards(self):
        with mock.patch.object(serve, 'verify_session',
                               return_value=False), \
             mock.patch.object(serve, '_agent_id_for_key',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.api_mail_send(
                mock.Mock())).status_code, 401)
        self.assertEqual(self._call({}, json_side=ValueError('x'))[0]
                         .status_code, 400)
        self.assertEqual(self._call({'toId': 'a', 'text': 't'},
                                    state=None)[0].status_code, 503)
        state = {'agentRoster': [{'id': 'a', 'name': 'A'}]}
        self.assertEqual(self._call({'toId': 'a', 'text': 't'},
                                    state=state,
                                    owner='zz')[0].status_code, 400)

    def test_success_and_admin_route(self):
        state = {'agentRoster': [{'id': 'a', 'name': 'A'}],
                 'agents': {}}
        res, append = self._call({'toId': 'a', 'text': ' t '},
                                 state=state)
        self.assertTrue(json.loads(res.body)['ok'])
        self.assertIn('agents', state)
        append.assert_called_once()
        state = {'agentRoster': [{'id': 'admin', 'name': 'Theo'}],
                 'agents': {'admin': {}}}
        res, _ = self._call({'toId': 'admin', 'text': 'hi'}, player=True,
                            owner=None, state=state,
                            routed={'queued': False, 'reply': 'yo',
                                    'agent': 'admin'})
        self.assertEqual(len(state['playerInbox']), 1)
        res, _ = self._call({'toId': 'admin', 'text': 'hi'}, player=True,
                            owner=None, state=state,
                            routed={'queued': True})
        self.assertEqual(len(state['playerInbox']), 1)


class TheoKeysGrantTest(unittest.TestCase):
    def _call(self, body, authed=True, granted=('raw', mock.Mock(
            key_id='k', as_dict=lambda: {'id': 'k'})), json_side=None):
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
            reg.grant_key.return_value = granted
            return asyncio.run(serve.theo_keys_grant(req))

    def test_paths(self):
        self.assertEqual(self._call({}, authed=False).status_code, 401)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'role': 'r',
                                     'budget': 'x'}).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'role': 'r',
                                     'ttlSeconds': 'x'}).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'role': 'r'},
                                    granted=None).status_code, 400)
        self.assertTrue(json.loads(self._call(
            {'agentId': 'a', 'role': 'r'}).body)['ok'])


class DesignReferenceTasteTest(unittest.TestCase):
    def _call(self, body, actor='player', briefs=('b',), fold=('t', None),
              paths=('/p', '/p/taste.md', '/p')):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, actor)), \
             mock.patch.object(serve, '_design_project_rel',
                               return_value='p'), \
             mock.patch.object(serve, '_list_design_briefs',
                               return_value=list(briefs)), \
             mock.patch.object(serve, '_fold_design_taste',
                               return_value=fold), \
             mock.patch.object(serve, '_safe_library_path',
                               side_effect=list(paths)), \
             mock.patch('os.makedirs'), \
             mock.patch.object(serve, '_write_file'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.design_reference_taste(req))

    def test_paths(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.design_reference_taste(
                mock.Mock())).status_code, 401)
        self.assertEqual(self._call({'agentId': 'x'}).status_code, 403)
        self.assertEqual(self._call({}, briefs=()).status_code, 400)
        self.assertEqual(self._call({}, fold=(None, 'err')).status_code, 502)
        self.assertEqual(self._call({}, paths=(None,)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_design_project_rel',
                               return_value='p'), \
             mock.patch.object(serve, '_list_design_briefs',
                               return_value=['b']), \
             mock.patch.object(serve, '_fold_design_taste',
                               return_value=('t', None)), \
             mock.patch.object(serve, '_safe_library_path',
                               return_value='/p/t'), \
             mock.patch('os.makedirs', side_effect=OSError('x')), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.design_reference_taste(
                req)).status_code, 500)
        self.assertTrue(json.loads(self._call({}).body)['ok'])


class SupervisorMeetingTest(unittest.TestCase):
    def _call(self, body, json_side=None, view=True):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        meeting = mock.Mock()
        meeting.breakdown.return_value = ('brief', ['s'])
        meeting.assign.return_value = ['a']
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={}), \
             mock.patch('sim.roster_view_from_state',
                        return_value=view or None), \
             mock.patch.object(serve, 'planner') as planner, \
             mock.patch.object(serve, 'story_budget') as budget, \
             mock.patch('dataclasses.asdict',
                        side_effect=lambda x: {'x': 1}), \
             mock.patch.object(serve, '_public_dict',
                               side_effect=lambda x: x), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            planner.SupervisorMeeting.return_value = meeting
            planner.PlannerSubmission.return_value = 'sub'
            planner.DEFAULT_BATCH_SIZE = 1
            budget.BudgetLedger.from_state.return_value = None
            budget.BudgetLedger.return_value = mock.Mock()
            return asyncio.run(serve.supervisor_meeting(req))

    def test_validation(self):
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        base = {'title': 't', 'goal': 'g', 'items': ['i'],
                'specialties': {'s': 1}}
        self.assertEqual(self._call(dict(base, durations={'s': 'x'})
                                    ).status_code, 400)
        self.assertEqual(self._call(dict(base, storiesPerAgent='x')
                                    ).status_code, 400)
        self.assertEqual(self._call(base, view=False).status_code, 409)

    def test_success(self):
        body = {'title': 't', 'goal': 'g', 'items': ['i'],
                'specialties': {'s': 1}, 'durations': {'s': 2}}
        self.assertTrue(json.loads(self._call(body).body)['ok'])


class ProbeBackendSyncTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_API_SERVICES', {}):
            self.assertFalse(serve._probe_backend_sync(
                {'service': 's'}, 'p')['ok'])
        with mock.patch.object(serve, '_API_SERVICES',
                               {'s': {'name': 'S'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               side_effect=RuntimeError('bad')):
            self.assertIn('bad url', serve._probe_backend_sync(
                {'service': 's'}, 'p')['error'])
        with mock.patch.object(serve, '_API_SERVICES',
                               {'s': {'name': 'S'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='http://x'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=False):
            self.assertIn('safe public', serve._probe_backend_sync(
                {'service': 's'}, 'p')['error'])
        with mock.patch.object(serve, '_API_SERVICES',
                               {'s': {'name': 'S'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='https://x'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_safe_urlopen',
                               side_effect=RuntimeError('net')):
            self.assertFalse(serve._probe_backend_sync(
                {'service': 's'}, 'p')['ok'])
        resp = mock.MagicMock()
        resp.__enter__.return_value.status = 200
        with mock.patch.object(serve, '_API_SERVICES',
                               {'s': {'name': 'S'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='https://x'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_safe_urlopen', return_value=resp):
            self.assertTrue(serve._probe_backend_sync(
                {'service': 's'}, 'p')['ok'])


class RssApproveTest(unittest.TestCase):
    def _call(self, body, existing='proposed', db=None,
              validate=('f', {}, None)):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        patches = [
            mock.patch.object(serve, '_operator_gate',
                              return_value=(True, 'p')),
            mock.patch.object(serve, '_feed_registry_status',
                              return_value=existing),
            mock.patch.object(serve, '_rss_feed_row_to_spec',
                              return_value={}),
            mock.patch.object(serve, '_validate_rss_feed_spec',
                              return_value=validate),
            mock.patch.object(serve, '_rss_feed_upsert'),
            mock.patch.object(serve, 'log_action'),
        ]
        if db:
            patches.append(mock.patch.object(serve, '_db', side_effect=db))
        else:
            conn = mock.Mock()
            conn.execute.return_value.fetchone.return_value = {'id': 'f'}
            cm = mock.MagicMock()
            cm.__enter__.return_value = conn
            patches.append(mock.patch.object(serve, '_db', return_value=cm))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.rss_approve(req))

    def test_paths(self):
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'feedId': 'f'},
                                    existing=None).status_code, 404)
        self.assertEqual(self._call({'feedId': 'f'},
                                    existing='active').status_code, 409)
        self.assertEqual(self._call({'feedId': 'f'},
                                    db=RuntimeError('db')).status_code, 503)
        self.assertEqual(self._call(
            {'feedId': 'f'}, validate=('f', {}, 'bad')).status_code, 400)
        self.assertEqual(json.loads(self._call(
            {'feedId': 'F'}).body)['status'], 'active')
        self.assertEqual(json.loads(self._call(
            {'feedId': 'F', 'decision': 'deny'}).body)['status'], 'denied')


class SmsGrantTest(unittest.TestCase):
    def _call(self, body, exists=True):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = (
            (1,) if exists else None)
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_sms_mint_grant',
                               return_value='g'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'):
            return asyncio.run(serve.sms_grant(req))

    def test_paths(self):
        self.assertEqual(self._call({'agentId': 'a',
                                     'messageId': 'bad'}).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'messageId': 1},
                                    exists=False).status_code, 404)
        body = json.loads(self._call({'agentId': 'a', 'messageId': 1,
                                      'ttlSec': 'bad'}).body)
        self.assertEqual(body['expiresInSec'], 300)


class BatchOneExtraPathsTest(unittest.TestCase):
    def test_avatar_more(self):
        def run(complete, pix=True, out=None, urls=None):
            patches = [
                mock.patch.object(serve, '_avatar_set_complete',
                                  side_effect=complete),
                mock.patch.object(serve, '_pixellab_avatar_available',
                                  return_value=pix),
                mock.patch.object(serve, '_AVATAR_IN_FLIGHT', set()),
                mock.patch.object(serve, 'get_or_create_agent_key',
                                  return_value='k'),
                mock.patch.object(serve, '_avatar_description',
                                  return_value='d'),
                mock.patch.object(serve, '_api_base_url', return_value='b'),
                mock.patch.object(serve, '_api_execute',
                                  return_value=out or {
                                      'ok': True,
                                      'data': {'rotation_urls':
                                               urls or {'s': 'https://x'}}}),
                mock.patch.object(serve, '_avatar_dir', return_value='/a'),
                mock.patch.object(serve, '_avatar_orientation_name',
                                  return_value='s'),
                mock.patch('os.makedirs'),
                mock.patch('urllib.request.urlopen',
                           return_value=mock.MagicMock(
                               __enter__=lambda s: s,
                               read=lambda *a: b'i')),
                mock.patch('builtins.open', mock.mock_open()),
            ]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            return serve._ensure_agent_avatar('a1', 'A', 'worker')
        self.assertFalse(run([False], pix=False))
        self.assertFalse(run([False, False], out={'ok': False}))
        self.assertFalse(run([False, False], out={'ok': True, 'data': {}}))
        self.assertTrue(run([False, True], urls={'d': '', 's': 'https://x'}))

    def test_higgsfield_more(self):
        with mock.patch.object(serve, '_api_base_url',
                               return_value='b'), \
             mock.patch.object(serve, '_api_execute',
                               return_value={'ok': False}), \
             mock.patch.object(serve, '_higgsfield_log_manifest'):
            self.assertEqual(serve._higgsfield_generate('image', {}),
                             'prompt is required')
            serve._higgsfield_generate(
                'video', {'prompt': 'p', 'aspect_ratio': '16:9',
                          'sound': 'on'})

    def test_mullvad_cache_hit(self):
        with mock.patch.object(serve, '_MULLVAD_TOKEN_LOCK',
                               mock.MagicMock()), \
             mock.patch.object(serve, '_MULLVAD_TOKEN_CACHE',
                               {('https://m/x', 'a'): {
                                   'token': 'cached',
                                   'expires_at': 999999}}), \
             mock.patch.object(serve, 'MULLVAD_TOKEN_REFRESH_MARGIN_S',
                               60), \
             mock.patch('time.time', return_value=1000.0), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            self.assertEqual(serve._mullvad_access_token(
                'https://m/x', 'a'), 'cached')

    def test_mail_more(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'toId': 'a'})
        req.cookies = {}
        req.headers = {}
        state = {'agentRoster': [{'id': 'a'}]}
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_agent_id_for_key',
                               return_value=None), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.api_mail_send(
                req)).status_code, 400)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'toId': 'zz', 'text': 't'})
        req.cookies = {}
        req.headers = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_agent_id_for_key',
                               return_value=None), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state):
            self.assertEqual(asyncio.run(serve.api_mail_send(
                req)).status_code, 404)

    def test_theo_more(self):
        def call(body):
            req = mock.Mock()
            req.json = mock.AsyncMock(return_value=body)
            with mock.patch.object(serve, '_operator_gate',
                                   return_value=(True, 'p')):
                return asyncio.run(serve.theo_keys_grant(req))
        self.assertEqual(call({'role': 'r'}).status_code, 400)
        self.assertEqual(call({'agentId': 'a'}).status_code, 400)
        self.assertEqual(call({'agentId': 'a', 'role': 'r',
                               'rateLimit': 'x'}).status_code, 400)

    def test_supervisor_more(self):
        def call(body):
            req = mock.Mock()
            req.json = mock.AsyncMock(return_value=body)
            with mock.patch.object(serve, '_operator_gate',
                                   return_value=(True, 'p')):
                return asyncio.run(serve.supervisor_meeting(req))
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.supervisor_meeting(
                mock.Mock())).status_code, 401)
        self.assertEqual(call({'goal': 'g', 'items': ['i'],
                               'specialties': {'s': 1}}).status_code, 400)
        self.assertEqual(call({'title': 't', 'goal': 'g',
                               'specialties': {'s': 1}}).status_code, 400)
        self.assertEqual(call({'title': 't', 'goal': 'g',
                               'items': ['i']}).status_code, 400)

    def test_rss_sms_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.rss_approve(
                mock.Mock())).status_code, 403)
            self.assertEqual(asyncio.run(serve.sms_grant(
                mock.Mock())).status_code, 403)


class ReloadGoogleLimitsTest(unittest.TestCase):
    def test_branches(self):
        with mock.patch.dict(serve.__dict__, {'_GOOGLE_RATE_LIMITS': None}):
            self.assertFalse(serve._reload_google_rate_limits({}))
        limits = {'calendar': (1.0, 0.15)}
        with mock.patch.dict(serve.__dict__, {'_GOOGLE_RATE_LIMITS': limits}):
            self.assertTrue(serve._reload_google_rate_limits({
                'GOOGLE_CALENDAR_RATE_PER_MIN': '2',
                'GOOGLE_SHEETS_RATE_PER_MIN': 'bad'}))
        self.assertEqual(limits['calendar'], (2.0, 0.15))


class CredentialPayloadTest(unittest.TestCase):
    def test_branches(self):
        with mock.patch.object(serve, '_credential_token',
                               return_value=None):
            self.assertIsNone(serve._credential_payload('c'))
        with mock.patch.object(serve, '_credential_token',
                               return_value='raw'), \
             mock.patch.object(serve, '_open_secret', return_value=None):
            self.assertIsNone(serve._credential_payload('c'))
        with mock.patch.object(serve, '_credential_token',
                               return_value='raw'), \
             mock.patch.object(serve, '_open_secret',
                               return_value='notjson'):
            self.assertEqual(serve._credential_payload('c'),
                             {'kind': 'token', 'token': 'notjson'})
        with mock.patch.object(serve, '_credential_token',
                               return_value='raw'), \
             mock.patch.object(serve, '_open_secret',
                               return_value='{"kind": "login"}'):
            self.assertEqual(serve._credential_payload('c'),
                             {'kind': 'login'})


class AllowlistWaitSamplesTest(unittest.TestCase):
    def test_branches(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            self.assertEqual(serve._allowlist_wait_samples(), [])
        rows = [('allowlist_request', 10, '{"host": "H"}'),
                ('allowlist_grant', 20, '{"host": "h"}'),
                ('allowlist_request', 0, 'bad'),
                ('allowlist_request', 0, '{"host": ""}')]
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = rows
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=1000.0):
            self.assertEqual(serve._allowlist_wait_samples(), [10])


class RunDockerSandboxTest(unittest.TestCase):
    def test_timeout_reaps(self):
        tmp = mock.Mock()
        tmp.name = '/t.cid'
        with mock.patch('tempfile.NamedTemporaryFile',
                        return_value=tmp), \
             mock.patch('os.remove', side_effect=[OSError('gone'), None]), \
             mock.patch('subprocess.run',
                        side_effect=[__import__('subprocess')
                                     .TimeoutExpired('x', 1),
                                     mock.Mock()]) as run, \
             mock.patch('builtins.open', mock.mock_open(
                 read_data='cid123')), \
             mock.patch.object(serve, 'SANDBOX_IMAGE', 'img'):
            with self.assertRaises(__import__('subprocess').TimeoutExpired):
                serve._run_docker_sandbox(['docker', 'run', 'img'], 1)
        self.assertEqual(run.call_args_list[1].args[0],
                         ['docker', 'rm', '-f', 'cid123'])


class TierHistoryTest(unittest.TestCase):
    def _db(self, rows):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = rows
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        return cm

    def test_tier_log(self):
        cm = self._db([('d', 'b', 's', 'n', 1.0, 'bm', 9)])
        with mock.patch.object(serve, '_db', return_value=cm):
            self.assertEqual(serve.get_tier_daily_log('b')[0]['band'], 'b')
            self.assertEqual(serve.get_tier_daily_log()[0]['slug'], 's')

    def test_price_history(self):
        cm = self._db([('d', 'm', 1.0)])
        with mock.patch.object(serve, '_db', return_value=cm):
            self.assertEqual(serve.get_catalog_price_history('m')[0]
                             ['model_id'], 'm')
            self.assertEqual(serve.get_catalog_price_history()[0]
                             ['price_per_m'], 1.0)
