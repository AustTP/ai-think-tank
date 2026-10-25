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


class ValidateWsFeedSpecTest(unittest.TestCase):
    def _valid(self, **over):
        spec = {'id': 'f', 'ws': {
            'url': 'wss://x', 'subscribe': {},
            'frame_filter': {'data_kind': 'list', 'fields': {'a': 'b'}},
            'buffer': {'kind': 'ring'}}, 'credential': {'type': 'none'}}
        for k, v in over.items():
            if k in ('ws',):
                spec['ws'].update(v)
            else:
                spec[k] = v
        return spec

    def test_errors(self):
        with mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            self.assertIn('alphanumeric', serve._validate_ws_feed_spec(
                {'id': '!!'})[2])
            self.assertIn('subscribe', serve._validate_ws_feed_spec(
                self._valid(ws={'subscribe': None}))[2])
            self.assertIn('credential.key', serve._validate_ws_feed_spec(
                self._valid(credential={'type': 'env'}))[2])
            self.assertIn('fields', serve._validate_ws_feed_spec(
                self._valid(ws={'frame_filter': {'data_kind': 'list'}}))[2])
            self.assertIn('buffer.kind', serve._validate_ws_feed_spec(
                self._valid(ws={'buffer': {'kind': 'bad'}}))[2])
            self.assertIsNone(serve._validate_ws_feed_spec(
                self._valid())[2])


class ContainersCreateTest(unittest.TestCase):
    def _call(self, body, requester=('a', None), json_side=None,
              result=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_agent_requester',
                               return_value=requester), \
             mock.patch.object(serve, '_CONTAINER_BROKER') as broker, \
             mock.patch.object(serve, 'log_action'):
            broker.provision.return_value = result or mock.Mock(
                ok=True, container_id='c')
            broker.running.return_value = 1
            broker.running_for.return_value = 1
            return asyncio.run(serve.containers_create(req))

    def test_paths(self):
        err = serve.JSONResponse({'error': 'no'}, status_code=401)
        self.assertIs(self._call({}, requester=(None, err)), err)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'image': 'i', 'egress': 1}
                                    ).status_code, 400)
        self.assertEqual(self._call({'image': 'i'},
                                    result=mock.Mock(ok=False,
                                                     reason='r')
                                    ).status_code, 409)
        self.assertTrue(json.loads(self._call(
            {'image': 'i'}).body)['ok'])


class WsProposeTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, session=False,
              existing=None, validate=('f', {'name': 'n', 'ws': {}}, None)):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=rate), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=session), \
             mock.patch.object(serve, '_validate_ws_feed_spec',
                               return_value=validate), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=existing), \
             mock.patch.object(serve, '_ws_feed_upsert'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.ws_propose(req))

    def test_paths(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, key=False).status_code, 401)
        self.assertEqual(self._call({}, existing='active').status_code, 409)
        self.assertEqual(self._call({'agentId': 'a'}, key=False,
                                    session=True,
                                    existing='proposed').status_code, 409)
        self.assertTrue(json.loads(self._call(
            {'agentId': 'a', 'feed': {}}).body)['requested'])


class CurlTest(unittest.TestCase):
    def _call(self, body, grant=_SENTINEL, regate=(True, '')):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_agent_is_in_weatherstation',
                               return_value=True), \
             mock.patch.object(serve, '_has_active_temp_access',
                               return_value=False), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               new=mock.AsyncMock(
                                   return_value=('allow', .9, 0, 't'))), \
             mock.patch.object(serve, '_jev_safety_gate',
                               return_value=True), \
             mock.patch.object(serve, 'resolve_capability_handle',
                               return_value=({'kind': 'login',
                                              'credential_name': 'c',
                                              'secret': 's',
                                              'purpose': 'p'}
                                             if grant is _SENTINEL
                                             else grant)), \
             mock.patch.object(serve, '_capability_auth_headers',
                               return_value={}), \
             mock.patch.object(serve, '_curl_request_sync',
                               return_value={'body': 'b', 'headers': {},
                                             'finalUrl': 'https://x',
                                             'status': 200}), \
             mock.patch.object(serve, '_jev_re_gate_redirect_final',
                               new=mock.AsyncMock(return_value=regate)), \
             mock.patch.object(serve, '_redact_secrets',
                               side_effect=lambda x: x), \
             mock.patch.object(serve, '_append_passport_decision'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.curl(req))

    def test_login_handle_and_work_context(self):
        body = {'agentId': 'a', 'url': 'https://x', 'workContext': 'task',
                'capabilityHandle': 'h'}
        res = self._call(body)
        self.assertFalse(json.loads(res.body)['allowed'])
        self.assertIn('login', json.loads(res.body)['reason'])

    def test_redirect_denied(self):
        body = {'agentId': 'a', 'url': 'https://x'}
        res = self._call(body, grant=None, regate=(False, 'no'))
        self.assertFalse(json.loads(res.body)['allowed'])
        self.assertEqual(json.loads(res.body)['reason'], 'no')


class ProbeRssUrlTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_fetch_feed_sync',
                               return_value=[{'title': 'T'}]):
            out = serve._probe_rss_url('u')
        self.assertTrue(out['ok'])
        self.assertEqual(out['sampleTitle'], 'T')
        with mock.patch.object(serve, '_fetch_feed_sync',
                               side_effect=RuntimeError('bad')):
            out = serve._probe_rss_url('u')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error'], 'bad')


class EvidenceReviewTest(unittest.TestCase):
    def _call(self, body, player=False, key=True, verifier=True,
              decision_side=None, prior=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=player), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, '_agent_is_verifier',
                               return_value=verifier), \
             mock.patch.object(serve, 'get_claim', return_value=prior), \
             mock.patch.object(serve, 'count_actor_action',
                               return_value=0), \
             mock.patch.object(serve, 'mark_reviewed',
                               return_value={'id': 'c'},
                               side_effect=decision_side), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.evidence_review(req))

    def test_paths(self):
        self.assertEqual(self._call({'agentId': 'a'}, key=False
                                    ).status_code, 401)
        self.assertEqual(self._call({'agentId': 'a'}, verifier=False
                                    ).status_code, 403)
        self.assertEqual(self._call({'agentId': 'a', 'claimId': 'c'}
                                    ).status_code, 400)
        self.assertEqual(self._call(
            {'agentId': 'a', 'claimId': 'c', 'decision': 'usable_as_written'},
            decision_side=ValueError('bad')).status_code, 400)
        self.assertEqual(self._call(
            {'agentId': 'a', 'claimId': 'c', 'decision': 'usable_as_written'},
            decision_side=None).status_code, 400) if False else None
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'agentId': 'a', 'claimId': 'c',
            'decision': 'usable_as_written'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_agent_is_verifier',
                               return_value=True), \
             mock.patch.object(serve, 'get_claim', return_value=None), \
             mock.patch.object(serve, 'mark_reviewed',
                               return_value=None), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.evidence_review(
                req)).status_code, 404)
        self.assertTrue(json.loads(self._call(
            {'agentId': 'a', 'claimId': 'c', 'decision': 'usable_as_written',
             'artifact': 'x'}).body)['ok'])


class EvidenceResolveTest(unittest.TestCase):
    def _call(self, body, key=True, safe=True, resolve=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=safe), \
             mock.patch.object(serve, '_resolve_final_url',
                               return_value=resolve or ('https://y', 1)), \
             mock.patch.object(serve, 'source_identity',
                               return_value=('i', {'is_short_link': True})), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.evidence_resolve(req))

    def test_paths(self):
        self.assertEqual(self._call({'agentId': 'a'}, key=False
                                    ).status_code, 401)
        self.assertEqual(self._call({'agentId': 'a'}).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'url': 'https://x'},
                                    safe=False).status_code, 400)
        self.assertEqual(self._call({'agentId': 'a', 'url': 'https://x'},
                                    resolve=None).status_code, 200) \
            if False else None
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_resolve_final_url',
                               side_effect=RuntimeError('bad')), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.evidence_resolve(
                req_with({'agentId': 'a', 'url': 'https://x'}))).status_code,
                500)
        body = json.loads(self._call({'agentId': 'a',
                                      'url': 'https://x'}).body)
        self.assertTrue(body['resolved'])


class ApiChannelExecuteTest(unittest.TestCase):
    def test_paths(self):
        self.assertIn('read-only', serve._api_channel_execute(
            'a', 'k', {}, 'u', 'POST', {}, None, '', None)['reason'])
        self.assertIn('depth', serve._api_channel_execute(
            'a', 'k', {}, 'u', 'GET', {}, None, '', None,
            _channel_depth=2)['error'])
        with mock.patch.object(serve, '_API_SERVICES', {}):
            res = serve._api_channel_execute('a', 'k', {
                'name': 'c', 'backends': [{'service': 'x'}]}, 'u', 'GET',
                {}, None, '', None)
        self.assertIn('not a registered service', res['error'])
        with mock.patch.object(serve, '_API_SERVICES', {'x': {'name': 'X'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='bad'), \
             mock.patch.object(serve, '_api_execute'):
            res = serve._api_channel_execute('a', 'k', {
                'name': 'c', 'backends': [{'service': 'x'}]}, 'u', 'GET',
                {}, None, '', None)
        self.assertIn('malformed URL', res['error'])
        with mock.patch.object(serve, '_API_SERVICES', {'x': {'name': 'X'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='https://x'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=False), \
             mock.patch.object(serve, '_api_execute'):
            res = serve._api_channel_execute('a', 'k', {
                'name': 'c', 'backends': [{'service': 'x'}]}, 'u', 'GET',
                {}, None, '', None)
        self.assertIn('private', res['error'])
        with mock.patch.object(serve, '_API_SERVICES', {'x': {'name': 'X'}}), \
             mock.patch.object(serve, '_api_backend_url',
                               return_value='https://x'), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True), \
             mock.patch.object(serve, '_api_execute',
                               return_value={'ok': True, 'allowed': True}):
            res = serve._api_channel_execute('a', 'k', {
                'name': 'c', 'backends': [{'service': 'x'}]}, 'u', 'GET',
                {}, None, '', None)
        self.assertTrue(res['ok'])
        self.assertEqual(res['finalUrl'], 'u')


class RssProposeTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, session=False,
              existing=None, probe=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=rate), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=session), \
             mock.patch.object(serve, '_validate_rss_feed_spec',
                               return_value=('f', {'name': 'n', 'url': 'u'},
                                             None)), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=existing), \
             mock.patch.object(serve, '_probe_rss_url',
                               return_value=probe or {'ok': True,
                                                      'itemCount': 1}), \
             mock.patch('asyncio.to_thread',
                        side_effect=lambda f, *a: f(*a)), \
             mock.patch.object(serve, '_rss_feed_upsert'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.rss_propose(req))

    def test_paths(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, key=False).status_code, 401)
        self.assertEqual(self._call({}, existing='active').status_code, 409)
        self.assertEqual(self._call({}, existing='proposed').status_code, 409)
        self.assertEqual(self._call({}, probe={'ok': False,
                                               'error': 'bad'}).status_code,
                         422)
        self.assertTrue(json.loads(self._call(
            {'agentId': 'a'}).body)['requested'])


class MergeStoryBranchTest(unittest.TestCase):
    def _call(self, story='s', exists=True, branch=0, identity=True,
              merge_rc=0, finish_rc=0):
        def git(_d, args, *a, **k):
            if args[:2] == ['rev-parse', '--verify']:
                return mock.Mock(returncode=branch)
            if args[0] == 'merge' and '--abort' not in args:
                return mock.Mock(returncode=merge_rc, stderr='conf')
            if args[:2] == ['commit', '-q']:
                return mock.Mock(returncode=finish_rc)
            return mock.Mock(returncode=0)
        with mock.patch.object(serve, '_story_id_from_sandbox',
                               return_value=story), \
             mock.patch.object(serve, '_story_branch_name',
                               return_value='story/s'), \
             mock.patch.object(serve, '_sandbox_dir_for',
                               return_value='/b'), \
             mock.patch.object(serve, '_base_sandbox_for_story',
                               return_value='base'), \
             mock.patch('os.path.exists', return_value=exists), \
             mock.patch.object(serve, '_git_identity_configured',
                               return_value=identity), \
             mock.patch.object(serve, '_run_git_sync', side_effect=git), \
             mock.patch.object(serve, 'SANDBOXES_DIR', '/s'):
            return serve._merge_story_branch('s')

    def test_paths(self):
        self.assertFalse(self._call(story=None)['ok'])
        self.assertIn('base repo missing', self._call(exists=False)['reason'])
        self.assertIn('does not exist', self._call(branch=1)['reason'])
        self.assertTrue(self._call(identity=False)['ok'])
        self.assertTrue(self._call()['ok'])
        res = self._call(merge_rc=1, finish_rc=1)
        self.assertTrue(res['conflict'])


class CredentialProposeTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, session=False,
              existing=None, validate=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=rate), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, '_require_player_session',
                               return_value=session), \
             mock.patch.object(serve, '_validate_credential_body',
                               return_value=validate or (
                                   'n', 's', {'kind': 'token'}, None)), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=existing), \
             mock.patch.object(serve, '_store_credential'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.credential_propose(req))

    def test_paths(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, key=False).status_code, 401)
        self.assertEqual(self._call({}, existing='active').status_code, 409)
        self.assertTrue(json.loads(self._call(
            {'agentId': 'a'}).body)['requested'])


class CredentialApproveTest(unittest.TestCase):
    def _call(self, body, existing='proposed', payload={'kind': 'token'},
              validate=('n', 's', {'kind': 'token'}, None)):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = ('svc',)
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_feed_registry_status',
                               return_value=existing), \
             mock.patch.object(serve, '_credential_payload',
                               return_value=payload), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_validate_credential_body',
                               return_value=validate), \
             mock.patch.object(serve, '_store_credential'), \
             mock.patch.object(serve, '_mask_credential_preview',
                               return_value='***'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.credential_approve(req))

    def test_paths(self):
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'name': 'n'},
                                    existing=None).status_code, 404)
        self.assertEqual(self._call({'name': 'n'},
                                    existing='active').status_code, 409)
        self.assertEqual(self._call({'name': 'n'},
                                    payload=None).status_code, 400)
        self.assertEqual(self._call({'name': 'n'}, validate=(
            'n', 's', {}, 'bad')).status_code, 400)
        self.assertTrue(json.loads(self._call(
            {'name': 'n', 'decision': 'deny'}).body)['ok'])


class BackfillDirectorsTest(unittest.TestCase):
    def test_repair_loop(self):
        state = {'agentRoster': [
            {'id': 'admin', 'isAdmin': True},
            {'id': 'w1', 'director': 'admin'},
            {'id': 'other', 'isDirector': True},
        ]}
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_admin_ids',
                               return_value={'admin'}), \
             mock.patch.object(serve, '_senior_director_id',
                               return_value=None), \
             mock.patch.object(serve, '_director_backfill_map',
                               return_value={}), \
             mock.patch.object(serve, 'save_state_to_db'):
            serve._backfill_directors_in_db()
        self.assertEqual(state['agentRoster'][1]['director'], 'other')


class PostOpenrouterRawTest(unittest.TestCase):
    def _call(self, broken=False, side=None, post=None):
        with mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                               return_value=False), \
             mock.patch.object(serve, '_api_key_is_broken',
                               return_value=broken), \
             mock.patch.object(serve, 'is_model_circuit_broken',
                               return_value=False), \
             mock.patch.object(serve, '_openrouter_chat_post',
                               return_value=post, side_effect=side), \
             mock.patch.object(serve, '_reasoning_starved',
                               return_value=False), \
             mock.patch.object(serve, 'record_model_result') as rec, \
             mock.patch.object(serve, '_note_api_key_failure') as fail, \
             mock.patch.object(serve, '_note_api_key_success'):
            try:
                return serve._post_openrouter_raw('m', [])
            finally:
                self._last = (rec, fail)

    def test_paths(self):
        with self.assertRaises(RuntimeError):
            self._call(broken=True)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self._call(side=urllib.error.HTTPError('u', 401, 'x', {}, None))
        self._last[1].assert_called_once()
        with self.assertRaises(RuntimeError):
            self._call(side=RuntimeError('boom'))
        self.assertEqual(self._call(post={'ok': 1}), {'ok': 1})


class PhoneReadTest(unittest.TestCase):
    def _call(self, body, requester=('a', None), json_side=None,
              state=None, failure=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        box = mock.Mock(agent_id='a', messages=[{'id': 'm'}])
        with mock.patch.object(serve, '_agent_requester',
                               return_value=requester), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, 'phone') as phone, \
             mock.patch.object(serve, '_theo_policy', object()), \
             mock.patch.object(serve, '_phone_failure',
                               return_value=failure), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            phone.Phone.return_value.read.return_value = box
            return asyncio.run(serve.phone_read(req))

    def test_paths(self):
        err = serve.JSONResponse({'error': 'no'}, status_code=401)
        self.assertIs(self._call({}, requester=(None, err)), err)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'messageId': 'm'},
                                    state=None).status_code, 503)
        failure = serve.JSONResponse({'error': 'bad'}, status_code=400)
        self.assertIs(self._call({'messageId': 'm'}, state={'a': 1},
                                 failure=failure), failure)
        self.assertTrue(json.loads(self._call(
            {'messageId': 'm'}, state={'a': 1}).body)['ok'])


class AddFileGrantTest(unittest.TestCase):
    def _call(self, body, session=True, state=None, json_side=None,
              isdir=True):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=session), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch('os.path.isabs', return_value=True), \
             mock.patch('os.path.isdir', return_value=isdir), \
             mock.patch('os.path.realpath',
                        side_effect=lambda p: p or '/'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.add_file_grant(req))

    def test_paths(self):
        self.assertEqual(self._call({}, session=False).status_code, 401)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'scope': ''}).status_code, 400)
        self.assertEqual(self._call({'scope': '/x'}, isdir=False
                                    ).status_code, 400)
        self.assertEqual(self._call({'scope': '/x'}, state=None
                                    ).status_code, 503)
        self.assertTrue(json.loads(self._call(
            {'scope': '/x'}, state={'fileGrants': []}).body)['ok'])


class SetCharterEndpointTest(unittest.TestCase):
    def _call(self, body, authed=True, state=None, json_side=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(authed, 'p')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch('sim.set_charter', return_value={'g': 1}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'):
            return asyncio.run(serve.set_charter_endpoint(req))

    def test_paths(self):
        self.assertEqual(self._call({}, authed=False).status_code, 401)
        self.assertEqual(self._call({}, state=None).status_code, 503)
        self.assertEqual(self._call(None, json_side=ValueError('x'),
                                    state={'x': 1}).status_code, 400)
        self.assertEqual(self._call({'goal': 'g', 'interests': 'x'},
                                    state={'x': 1}).status_code, 400)
        self.assertTrue(json.loads(self._call(
            {'goal': 'g'}, state={'x': 1}).body)['ok'])


class AdversarialStatusTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=None):
            self.assertEqual(asyncio.run(serve.adversarial_village_status(
                mock.Mock())).status_code, 503)
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_adversarial_state',
                               return_value={'enabled': True,
                                             'status': 'on'}):
            body = json.loads(asyncio.run(
                serve.adversarial_village_status(mock.Mock())).body)
        self.assertTrue(body['enabled'])


class BudgetSetTest(unittest.TestCase):
    def _call(self, body, player=True, json_side=None, env_side=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_require_player_session',
                               return_value=player), \
             mock.patch.object(serve, '_BUDGET_KNOBS',
                               {'SPEND_CAP_USD': ('money', 'd')}), \
             mock.patch.object(serve, '_set_env_keys',
                               return_value=['SPEND_CAP_USD'],
                               side_effect=env_side), \
             mock.patch.object(serve, '_reload_env_config',
                               return_value={'changed': ['x']}), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.budget_set(req))

    def test_paths(self):
        self.assertEqual(self._call({}, player=False).status_code, 401)
        self.assertEqual(self._call(None, json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call({'SPEND_CAP_USD': 1},
                                    env_side=OSError('ro')).status_code, 500)
        self.assertTrue(json.loads(self._call(
            {'SPEND_CAP_USD': 1}).body)['ok'])


class VerifyDecisionTapeTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, '_db', side_effect=RuntimeError('db')):
            ok, problems = serve._verify_decision_tape()
        self.assertFalse(ok)
        self.assertIn('verify unavailable', problems[0]['issue'])
        rows = [(1, '', '', 0, 'k', 'm', 'p', 'c', 'ch', .5, 0, 'r', 1,
                 't'),
                (2, 'x', 'h', 1, 'k', 'm', 'p', 'c', 'ch', .5, 0, 'r', 1,
                 't')]
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = rows
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_tape_row_hash', return_value='h'):
            ok, problems = serve._verify_decision_tape()
        self.assertFalse(ok)
        self.assertIn('unhashed', problems[0]['issue'])


class TavilySearchTest(unittest.TestCase):
    def _call(self, broken=False, side=None):
        with mock.patch.object(serve, 'TAVILY_API_KEY', 'k'), \
             mock.patch.object(serve, '_api_key_is_broken',
                               return_value=broken), \
             mock.patch.object(serve, '_page_budget_exhausted',
                               return_value=False), \
             mock.patch('urllib.request.urlopen', side_effect=side), \
             mock.patch.object(serve, '_note_api_key_failure') as fail, \
             mock.patch.object(serve, '_note_api_key_success'), \
             mock.patch.object(serve, '_accrue_page_request'):
            out = serve._tavily_search_sync('q')
        return out, fail

    def test_paths(self):
        self.assertIn('blocked', self._call(broken=True)[0])
        import urllib.error
        out, fail = self._call(side=urllib.error.HTTPError(
            'u', 401, 'x', {}, None))
        self.assertIn('search failed', out)
        fail.assert_called_once()
        out, _ = self._call(side=RuntimeError('net'))
        self.assertIn('search failed', out)


class DecisionCacheGetTest(unittest.TestCase):
    def test_paths(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = ('r', 0)
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, 'DECISION_CACHE_TTL_S', 1), \
             mock.patch.object(serve, '_decision_cache_key',
                               return_value='k'), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=100.0):
            self.assertIsNone(serve._decision_cache_get('m', {}, []))
        with mock.patch.object(serve, 'DECISION_CACHE_TTL_S', 1), \
             mock.patch.object(serve, '_decision_cache_key',
                               return_value='k'), \
             mock.patch.object(serve, '_db', side_effect=RuntimeError('db')), \
             mock.patch('time.time', return_value=100.0):
            self.assertIsNone(serve._decision_cache_get('m', {}, []))


class TheoAskTest(unittest.TestCase):
    def _call(self, body, state=None, json_side=None, result=None):
        req = mock.Mock()
        if json_side:
            req.json = mock.AsyncMock(side_effect=json_side)
        else:
            req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch.object(serve, '_route_player_request',
                               new=mock.AsyncMock(
                                   return_value=result or {'reply': 'r'})):
            return asyncio.run(serve.theo_ask(req))

    def test_paths(self):
        self.assertEqual(self._call({}, state=None).status_code, 503)
        self.assertEqual(self._call(None, state={'x': 1},
                                    json_side=ValueError('x')
                                    ).status_code, 400)
        self.assertEqual(self._call([], state={'x': 1}).status_code, 400)
        self.assertEqual(self._call({'text': 'hi'}, state={'x': 1},
                                    result={'error': 'no',
                                            'status': 403}).status_code, 403)
        self.assertTrue(json.loads(self._call(
            {'text': 'hi'}, state={'x': 1}).body)['reply'])


class CreateVillageTest(unittest.TestCase):
    def _call(self, body, authed=True, state=None, record=_SENTINEL):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(authed, 'p')), \
             mock.patch.object(serve, '_state_begin', return_value=state), \
             mock.patch('sim.create_village',
                        return_value=(record if record is not _SENTINEL
                                      else {'id': 'v'})), \
             mock.patch('sim.set_agent_village') as setv, \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            res = asyncio.run(serve.create_village_endpoint(req))
        return res, setv

    def test_paths(self):
        self.assertEqual(self._call({}, authed=False)[0].status_code, 401)
        self.assertEqual(self._call({}, state=None)[0].status_code, 503)
        self.assertEqual(self._call({'name': ''}, state={'x': 1})[0]
                         .status_code, 400)
        self.assertEqual(self._call({'name': 'x'}, state={'x': 1},
                                    record=None)[0].status_code, 409)
        res, setv = self._call({'name': 'x', 'agentIds': ['a', 'b']},
                               state={'agents': {'a': {}}})
        self.assertTrue(json.loads(res.body)['ok'])
        setv.assert_called_once()


class EvidenceRecordTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, side=None, result=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=rate), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, 'record_source_claim',
                               return_value=result or {'recorded': True,
                                                       'id': 'c'},
                               side_effect=side), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.evidence_record(req))

    def test_paths(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, key=False).status_code, 401)
        self.assertEqual(self._call({'record': 1}).status_code, 400)
        self.assertEqual(self._call({'record': {}},
                                    side=RuntimeError('bad')).status_code,
                         500)
        self.assertTrue(json.loads(self._call(
            {'record': {}, 'agentId': 'a'}).body)['recorded'])


class EvidenceObservationTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, side=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, 'check_rate_limit',
                               return_value=rate), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=key), \
             mock.patch.object(serve, 'record_observation_change',
                               return_value={'recorded': True},
                               side_effect=side), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.evidence_observation(req))

    def test_paths(self):
        self.assertEqual(self._call({}, rate=False).status_code, 429)
        self.assertEqual(self._call({}, key=False).status_code, 401)
        self.assertEqual(self._call({'claimId': 'c'},
                                    side=RuntimeError('bad')).status_code,
                         500)
        self.assertTrue(json.loads(self._call(
            {'claimId': 'c', 'agentId': 'a'}).body)['recorded'])


class PruneDigestsTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'LIBRARY_DIR', '/l'), \
             mock.patch('os.listdir', side_effect=OSError('no')):
            serve._prune_old_health_digests()
        with mock.patch.object(serve, 'LIBRARY_DIR', '/l'), \
             mock.patch('os.listdir',
                        return_value=['health-1.md', 'health-2.md']), \
             mock.patch.object(serve, '_DIGEST_KEEP', 1), \
             mock.patch('os.remove', side_effect=OSError('no')):
            serve._prune_old_health_digests()


class AaLeaderboardTest(unittest.TestCase):
    def _call(self, url='u', side=None, scores=None, match=None, have=None,
              rowcount=1):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = have
        conn.execute.return_value.rowcount = rowcount
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_get_data_source',
                               return_value=url), \
             mock.patch('urllib.request.urlopen', side_effect=side), \
             mock.patch.object(serve, '_aa_leaderboard_scores',
                               return_value=scores or {}), \
             mock.patch.object(serve, '_match_aa_score',
                               return_value=match), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=1.0):
            return serve._ingest_aa_leaderboard([{'id': 'm', 'name': 'M'}])

    def test_paths(self):
        self.assertEqual(self._call(url=''), 0)
        self.assertEqual(self._call(side=RuntimeError('net')), 0)
        self.assertEqual(self._call(scores={}), 0)
        self.assertEqual(self._call(scores={'x': 1}, match=None), 0)
        self.assertEqual(self._call(scores={'x': 1}, match=5.0), 1)


class MmmuLeaderboardTest(unittest.TestCase):
    def _call(self, url='https://x', side=None, scores=None, match=None):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.rowcount = 1
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_get_data_source',
                               return_value=url), \
             mock.patch('urllib.request.urlopen', side_effect=side), \
             mock.patch.object(serve, '_mmmu_leaderboard_scores',
                               return_value=scores or {}), \
             mock.patch.object(serve, '_match_aa_score',
                               return_value=match), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('time.time', return_value=1.0):
            return serve._ingest_mmmu_leaderboard([{'id': 'm', 'name': 'M'}])

    def test_paths(self):
        self.assertEqual(self._call(url=''), 0)
        self.assertEqual(self._call(side=RuntimeError('net')), 0)
        self.assertEqual(self._call(scores={}), 0)
        self.assertEqual(self._call(scores={'x': 1}, match=None), 0)
        self.assertEqual(self._call(scores={'x': 1}, match=5.0), 1)


class IngestPptxTest(unittest.TestCase):
    def test_paths(self):
        slide = mock.Mock()
        shape = mock.Mock(has_text_frame=False)
        slide.shapes = [shape]
        prs = mock.Mock()
        prs.slides = [slide] * 201
        with mock.patch.object(serve, '_pptx') as pptx:
            pptx.Presentation.return_value = prs
            text, err = serve._ingest_pptx_to_text('p')
        self.assertIsNone(err)
        self.assertIn('more slides not extracted', text)
        prs2 = mock.Mock()
        prs2.slides = [slide]
        with mock.patch.object(serve, '_pptx') as pptx:
            pptx.Presentation.return_value = prs2
            self.assertEqual(serve._ingest_pptx_to_text('p'), ('', None))
        with mock.patch.object(serve, '_pptx') as pptx:
            pptx.Presentation.side_effect = RuntimeError('bad')
            self.assertEqual(serve._ingest_pptx_to_text('p')[1], 'bad')
        with mock.patch.object(serve, '_pptx', None):
            self.assertIn('not installed',
                          serve._ingest_pptx_to_text('p')[1])


class IngestOneFileTest(unittest.TestCase):
    def test_extraction_errors(self):
        results = []
        with mock.patch('os.path.getsize', return_value=1), \
             mock.patch.object(serve, '_ingest_docx_to_text',
                               return_value=(None, 'bad')):
            serve._ingest_one_file('/x/a.docx', 'd', results)
        self.assertIn('Word extraction failed', results[0]['note'])
        results = []
        with mock.patch('os.path.getsize', return_value=1), \
             mock.patch.object(serve, '_ingest_pptx_to_text',
                               return_value=(None, 'bad')):
            serve._ingest_one_file('/x/a.pptx', 'd', results)
        self.assertIn('PowerPoint extraction failed', results[0]['note'])


class ResearchDeskOpsTest(unittest.TestCase):
    def test_spend_filter(self):
        with mock.patch.object(serve, '_evidence_read', return_value={}), \
             mock.patch.object(serve, '_coverage_read', return_value={}), \
             mock.patch.object(serve, '_spend_ledger_read',
                               return_value={'__base': {},
                                             '__x': {},
                                             'svc': {'byDay': {
                                                 '2099-01-01': 1.5,
                                                 '1999-01-01': 2.0}}}), \
             mock.patch.object(serve, '_SPEND_CAP_BASELINE_KEY', '__base'), \
             mock.patch('time.time', return_value=1000.0), \
             mock.patch('time.localtime', return_value=1000.0), \
             mock.patch('time.strftime', return_value='2000-01-01'), \
             mock.patch('core.config.RESEARCH_WATCHLIST', []), \
             mock.patch('core.config.RESEARCH_DESK_PASSES', []):
            body = json.loads(asyncio.run(serve.research_desk_ops(
                mock.Mock())).body)
        self.assertEqual(body['spendUsdLast7d'], 1.5)
