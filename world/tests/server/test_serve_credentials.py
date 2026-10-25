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


class CredentialLoginActionTest(unittest.TestCase):
    def _call(self, body, rate=True, key=True, grant=_NO_GRANT, safe=None,
              adapters=None, scrub=None):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        patches = [
            mock.patch.object(serve, 'check_rate_limit',
                              return_value=rate),
            mock.patch.object(serve, 'verify_agent_key',
                              return_value=key),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, 'resolve_capability_handle',
                              return_value=(
                                  {'kind': 'login',
                                   'credential_name': 'c',
                                   'service': 's', 'purpose': 'p'}
                                  if grant is _NO_GRANT else grant)),
            mock.patch.object(serve, '_login_safe_url',
                              side_effect=safe or [True, True]),
            mock.patch.object(serve, '_LOGIN_ADAPTERS',
                              adapters if adapters is not None else {}),
            mock.patch.object(serve, '_login_scrub_result',
                              return_value=scrub or {'clean': True}),
            mock.patch.object(serve, '_append_passport_decision'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.credential_login_action(req))

    def _body(self, **kw):
        body = {'agentId': 'cora', 'capabilityHandle': 'h',
                'service': 'svc',
                'action': {'method': 'get', 'url': 'https://a.com/x'}}
        body.update(kw)
        return body

    def test_rate_and_auth(self):
        self.assertEqual(self._call(self._body(), rate=False).status_code, 429)
        self.assertEqual(self._call(self._body(), key=False).status_code, 401)

    def test_required_fields(self):
        self.assertEqual(self._call({'agentId': 'c'}).status_code, 400)
        self.assertEqual(self._call(
            self._body(capabilityHandle='')).status_code, 400)

    def test_handle_and_url_guards(self):
        self.assertEqual(self._call(self._body(),
                                    grant=None).status_code, 403)
        self.assertEqual(self._call(
            self._body(), grant={'kind': 'token'}).status_code, 400)
        self.assertEqual(self._call(
            self._body(), safe=[False]).status_code, 400)

    def test_adapter_guards(self):
        self.assertEqual(self._call(self._body()).status_code, 501)
        self.assertEqual(self._call(
            self._body(), adapters={'svc': {'login_url': 'https://l',
                                            'fn': lambda g, a: {}}},
            safe=[True, False]).status_code, 500)
        self.assertEqual(self._call(
            self._body(), adapters={'svc': {'login_url': 'https://l',
                                            'fn': None}}).status_code, 500)

    def test_adapter_failures(self):
        def boom(grant, action):
            raise RuntimeError('login failed')

        resp = self._call(
            self._body(), adapters={'svc': {'login_url': 'https://l',
                                            'fn': boom}})
        self.assertEqual(resp.status_code, 502)
        resp = self._call(
            self._body(), adapters={'svc': {'login_url': 'https://l',
                                            'fn': lambda g, a: 'x'}})
        self.assertEqual(resp.status_code, 502)

    def test_success(self):
        resp = self._call(
            self._body(), adapters={'svc': {'login_url': 'https://l',
                                            'fn': lambda g, a: {
                                                'ok': True,
                                                'status': 201}}})
        body = json.loads(resp.body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['status'], 201)


class TierFailureRecheckLoopTest(unittest.TestCase):
    def _run(self, queue, refresh_side=None):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise asyncio.CancelledError()

        refresh = mock.AsyncMock(side_effect=refresh_side)
        with mock.patch('asyncio.sleep', side_effect=fake_sleep), \
             mock.patch.object(serve, '_TIER_RECHECK_QUEUE', queue), \
             mock.patch.object(serve, '_TIER_RECHECK_DRAINED', {}), \
             mock.patch.object(serve, '_tier_refresh_logged', refresh), \
             mock.patch('time.time', return_value=100.0):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._tier_failure_recheck_loop())
        return refresh

    def test_drains_queue(self):
        queue = {'mid': 1.0, 'low': 2.0}
        refresh = self._run(queue)
        self.assertEqual(queue, {})
        refresh.assert_awaited_once()
        self.assertEqual(serve._TIER_RECHECK_DRAINED, {})

    def test_empty_queue_and_failure(self):
        self.assertIsNotNone(self._run({}))
        refresh = self._run({'mid': 1.0}, refresh_side=RuntimeError('boom'))
        refresh.assert_awaited_once()


class GrantTaskBudgetTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        return req

    def _call(self, body, requester='d1', key=True, state=None,
              result=None):
        with mock.patch.object(serve, '_resolve_requester',
                               return_value=requester), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True if key else False), \
             mock.patch.object(serve, '_state_begin',
                               return_value=state), \
             mock.patch.object(serve, '_is_director_or_admin',
                               return_value=True), \
             mock.patch('sim._director_grant_budget_reopen',
                        return_value=result or {'ok': True,
                                                'budgetUsd': 5,
                                                'spentUsd': 1,
                                                'attempts': 2}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'):
            return asyncio.run(serve.grant_task_budget(self._req(body)))

    def test_guards(self):
        self.assertEqual(self._call({}, requester=None).status_code, 401)
        self.assertEqual(self._call({'taskId': 't', 'budgetUsd': 1},
                                    key=False).status_code, 403)
        self.assertEqual(self._call({}).status_code, 400)
        self.assertEqual(self._call({'taskId': 't',
                                     'budgetUsd': 0}).status_code, 400)
        self.assertEqual(self._call({'taskId': 't', 'budgetUsd': 1},
                                    state=None).status_code, 503)
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='d1'), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_is_director_or_admin',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.grant_task_budget(
                self._req({'taskId': 't', 'budgetUsd': 1}))).status_code, 403)

    def test_refusal_and_success(self):
        self.assertEqual(self._call({'taskId': 't', 'budgetUsd': 1},
                                    state={'x': 1},
                                    result={'ok': False,
                                            'error': 'no'}).status_code, 400)
        body = json.loads(self._call(
            {'taskId': 't', 'budgetUsd': 1}, state={'x': 1}).body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['budgetUsd'], 5)


class ApiCallEndpointTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'k'}
        return req

    def _call(self, body, enabled=True, load_error=None, rate=True,
              registered=False, allowlisted=False, standing=False,
              trusted=False, work_host=False, decision=('allow', 0.9, 0.0,
                                                         't'),
              gate=True, out=None, redirect=(True, 'ok'), safe_host=True):
        patches = [
            mock.patch.object(serve, 'API_CALL_ENABLED', enabled),
            mock.patch.object(serve, '_API_SERVICES_LOAD_ERROR', load_error),
            mock.patch.object(serve, 'check_rate_limit',
                              return_value=rate),
            mock.patch.object(serve, 'verify_agent_key', return_value=True),
            mock.patch.object(serve, '_work_trust_from_body',
                              return_value=trusted),
            mock.patch.object(serve, '_api_service_for_host',
                              return_value={'id': 's'} if registered else None),
            mock.patch.object(serve, '_is_allowlisted_host',
                              return_value=allowlisted),
            mock.patch.object(serve, '_is_standing_safe_host',
                              return_value=standing),
            mock.patch.object(serve, '_is_work_context_host',
                              return_value=work_host),
            mock.patch.object(serve, '_is_safe_public_host',
                              return_value=safe_host),
            mock.patch.object(serve, '_jev_quorum_decision',
                              new=mock.AsyncMock(return_value=decision)),
            mock.patch.object(serve, '_jev_safety_gate',
                              return_value=gate),
            mock.patch.object(serve, '_jev_re_gate_redirect_final',
                              new=mock.AsyncMock(return_value=redirect)),
            mock.patch.object(serve, '_api_execute',
                              return_value=out or {'ok': True,
                                                   'allowed': True}),
            mock.patch.object(serve, '_api_call_response',
                              side_effect=lambda agent, auth, payload, **kw:
                              serve.JSONResponse(payload)),
            mock.patch.object(serve, 'log_action'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.api_call(self._req(body)))

    def _body(self, **kw):
        body = {'agentId': 'cora', 'url': 'https://a.com/x',
                'method': 'GET', 'purpose': 'p'}
        body.update(kw)
        return body

    def test_disabled_and_load_error(self):
        self.assertEqual(self._call(self._body(),
                                    enabled=False).status_code, 403)
        self.assertEqual(self._call(self._body(),
                                    load_error='bad').status_code, 500)

    def test_rate_and_validation(self):
        self.assertEqual(self._call(self._body(), rate=False).status_code, 429)
        self.assertEqual(self._call({'url': ''}).status_code, 400)
        self.assertEqual(self._call(self._body(method='TRACE')).status_code,
                         400)
        body = json.loads(self._call({'url': 'ftp://x'}).body)
        self.assertFalse(body['allowed'])
        resp = self._call(self._body(), safe_host=False)
        self.assertFalse(json.loads(resp.body)['allowed'])

    def test_jev_denied_and_allowed(self):
        body = json.loads(self._call(
            self._body(workContext='ctx'), gate=True).body)
        self.assertTrue(body['ok'])
        body = json.loads(self._call(self._body(), gate=False).body)
        self.assertFalse(body['allowed'])
        body = json.loads(self._call(self._body(), gate=True).body)
        self.assertTrue(body['ok'])

    def test_jev_skipped_paths(self):
        for kw in ({'registered': True}, {'allowlisted': True},
                   {'standing': True}, {'trusted': True,
                                        'work_host': True}):
            body = json.loads(self._call(self._body(), **kw).body)
            self.assertTrue(body['ok'])

    def test_redirect_regate_denied(self):
        body = json.loads(self._call(
            self._body(), redirect=(False, 'nope')).body)
        self.assertFalse(body['allowed'])


class SmsRevokeTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        return req

    def test_guards_and_success(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(asyncio.run(serve.sms_revoke(
                self._req({}))).status_code, 403)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')):
            self.assertEqual(asyncio.run(serve.sms_revoke(
                self._req({}))).status_code, 400)
        cm = mock.MagicMock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, 'log_action') as log:
            body = json.loads(asyncio.run(serve.sms_revoke(
                self._req({'grant': 'g1'}))).body)
        self.assertTrue(body['ok'])
        log.assert_called_once()


class InferenceProxyTest(unittest.TestCase):
    CONFIGS = {
        'sublow': {'header': 'X-Sublow-Grant',
                   'validate': 'validate_sublow_grant',
                   'tier': '_sublow_tier_slug',
                   'accrue': 'accrue_sublow_grant',
                   'ledger': 'SUBLOW_LEDGER_SERVICE',
                   'fn': 'sublow_inference'},
        'jev': {'header': 'X-Jev-Grant',
                'validate': 'validate_jev_grant',
                'tier': '_jev_openrouter_slug',
                'accrue': 'accrue_jev_grant',
                'ledger': 'JEV_LEDGER_SERVICE',
                'fn': 'jev_inference'},
    }

    def _grant(self, accrued=0.0):
        return {'id': 'g1', 'agent_id': 'cora', 'task_id': 't1',
                'estimate_usd': 1.0, 'accrued_usd': accrued,
                'max_tokens_per_call': 100, 'calls': 1}

    def _call(self, kind='sublow', grant=_SENTINEL, token='tok', auth=None,
              messages=None, model='m', guard=None, agent_cap=False,
              dir_block=None, task_budget=False, tank=False,
              openrouter=None, side_effect=None, max_tokens=5,
              validate_side=None):
        cfg = self.CONFIGS[kind]
        req = mock.Mock()
        headers = {}
        if token:
            headers[cfg['header']] = token
        if auth:
            headers['Authorization'] = auth
        req.headers = headers
        req.json = mock.AsyncMock(return_value={
            'messages': messages if messages is not None
            else [{'role': 'user', 'content': 'hi'}],
            'max_tokens': max_tokens})
        patches = [
            mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'),
            mock.patch.object(serve, cfg['validate'],
                              return_value=(
                                  (self._grant() if (token or auth) else None)
                                  if grant is _SENTINEL else grant),
                              side_effect=validate_side),
            mock.patch.object(serve, cfg['tier'], return_value=model),
            mock.patch.object(serve, '_input_guard_violation',
                              return_value=guard),
            mock.patch.object(serve, '_agent_spend_cap_exceeded',
                              return_value=agent_cap),
            mock.patch.object(serve, '_director_budget_gate',
                              return_value=dir_block),
            mock.patch.object(serve, '_task_budget_exhausted',
                              return_value=task_budget),
            mock.patch.object(serve, '_task_budget_spent',
                              return_value=(0.5, 2)),
            mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                              return_value=tank),
            mock.patch.object(serve, '_call_openrouter_sync',
                              return_value=openrouter,
                              side_effect=side_effect),
            mock.patch.object(serve, '_sim_village_for_agent',
                              return_value='v'),
            mock.patch.object(serve, '_accrue_spend'),
            mock.patch.object(serve, '_accrue_task_spend'),
            mock.patch.object(serve, cfg['accrue']),
            mock.patch.object(serve, 'log_action'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(getattr(serve, cfg['fn'])(req))

    def test_no_api_key(self):
        for cfg in self.CONFIGS.values():
            req = mock.Mock()
            req.headers = {}
            req.json = mock.AsyncMock(return_value={})
            with mock.patch.object(serve, 'OPENROUTER_API_KEY', ''):
                self.assertEqual(
                    asyncio.run(getattr(serve, cfg['fn'])(req)).status_code,
                    500)

    def test_every_kind_branches(self):
        for kind in self.CONFIGS:
            self.assertEqual(self._call(kind, token=None).status_code, 401)
            self.assertEqual(self._call(kind, grant=None).status_code, 401)
            self.assertEqual(self._call(kind, messages=[]).status_code, 400)
            self.assertEqual(self._call(kind, model=None).status_code, 503)
            self.assertEqual(self._call(
                kind, guard={'patterns': ['p'],
                             'snippets': ['s']}).status_code, 400)
            self.assertEqual(self._call(
                kind, grant=self._grant(accrued=2.0)).status_code, 429)
            self.assertEqual(self._call(kind, agent_cap=True).status_code,
                             429)
            self.assertEqual(self._call(kind, task_budget=True).status_code,
                             429)
            self.assertEqual(self._call(kind, tank=True).status_code, 429)
            block = serve.JSONResponse({'error': 'blocked'}, status_code=403)
            self.assertIs(self._call(kind, dir_block=block), block)

    def test_success_and_bearer(self):
        for kind in self.CONFIGS:
            reply = {'choices': [{'message': {'content': 'hello'}}],
                     'usage': {'cost': 0.5}}
            body = json.loads(self._call(
                kind, token=None, openrouter=reply,
                auth='Bearer tok').body)
            self.assertEqual(body['reply'], 'hello')
            self.assertEqual(body['cost'], 0.5)
            self.assertEqual(body['grant']['estimateUsd'], 1.0)
            body = json.loads(self._call(
                kind, openrouter={'choices': [{'message': {}}],
                                  'usage': {'cost': 0}}).body)
            self.assertEqual(body['cost'], 0.0)

    def test_max_tokens_fallback_and_errors(self):
        import io
        import urllib.error
        for kind in self.CONFIGS:
            body = json.loads(self._call(
                kind, max_tokens='abc',
                openrouter={'choices': [{'message': {'content': 'x'}}],
                            'usage': {}}).body)
            self.assertEqual(body['reply'], 'x')
            err = urllib.error.HTTPError('u', 502, 'bad', {},
                                         io.BytesIO(b'proxy down'))
            resp = self._call(kind, side_effect=err)
            self.assertEqual(resp.status_code, 502)
            resp = self._call(kind, side_effect=RuntimeError('boom'))
            self.assertEqual(resp.status_code, 500)
            body = json.loads(self._call(
                kind, openrouter={'choices': [{'message': {'content': 'y'}}],
                                  'usage': {'cost': 0.1}},
                validate_side=[self._grant(accrued=0.2)] * 2).body)
            self.assertEqual(body['grant']['accruedUsd'], 0.2)


class ReloadEnvConfigTest(unittest.TestCase):
    def _call(self, env, fields, google=None, ensure=None):
        g = {'A': 0, 'BROWSE_ALLOWLIST_DOMAINS': [],
             'TTC_BEST_OF': 1, 'TTC_MAX_BEST_OF': 1,
             'PUBLISH_REPO': '', 'PUBLISH_REMOTE_URL': ''}
        patches = [
            mock.patch.object(serve, '_load_env', return_value=env),
            mock.patch.object(serve, '_ENV_RELOAD_FIELDS', fields),
            mock.patch.object(serve, '_reload_google_rate_limits',
                              return_value=google if google is not None
                              else False),
            mock.patch.object(serve, '_api_key_health', {'k': 'v'}),
            mock.patch.dict(serve.__dict__, g),
        ]
        if ensure is not None:
            patches.append(mock.patch.object(serve, 'ensure_sandbox_networking',
                                             side_effect=ensure))
        else:
            patches.append(mock.patch.object(serve, 'ensure_sandbox_networking'))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return serve._reload_env_config()

    def test_all_branches(self):
        def boom(_):
            raise RuntimeError('bad')
        fields = {'BROWSE_ALLOWLIST_DOMAINS': lambda e: ['x'],
                  'A': lambda e: 5, 'B': boom,
                  'PUBLISH_REPO': lambda e: e.get('PUBLISH_REPO')}
        out = self._call({'TTC_MAX_BEST_OF': '7', 'PUBLISH_REPO': 'r'},
                         fields, google=True)
        self.assertIn('A', out['changed'])
        self.assertIn('BROWSE_ALLOWLIST_DOMAINS', out['changed'])
        self.assertIn('TTC_MAX_BEST_OF', out['changed'])
        self.assertIn('PUBLISH_REMOTE_URL', out['changed'])
        self.assertIn('_GOOGLE_RATE_LIMITS', out['changed'])
        self.assertEqual(serve._api_key_health, {})

    def test_derived_blocks_raise(self):
        class BadStr:
            def __radd__(self, other):
                raise RuntimeError('bad')

        with mock.patch.object(serve, '_load_env', return_value={}), \
             mock.patch.object(serve, '_ENV_RELOAD_FIELDS', {
                 'BROWSE_ALLOWLIST_DOMAINS': lambda e: ['x']}), \
             mock.patch.object(serve, '_reload_google_rate_limits',
                               side_effect=RuntimeError('google')), \
             mock.patch.object(serve, 'ensure_sandbox_networking',
                               side_effect=RuntimeError('net')), \
             mock.patch.object(serve, '_api_key_health', {}), \
             mock.patch.dict(serve.__dict__, {
                 'PUBLISH_REPO': BadStr(), 'PUBLISH_REMOTE_URL': '',
                 'TTC_BEST_OF': 1, 'TTC_MAX_BEST_OF': 1,
                 'BROWSE_ALLOWLIST_DOMAINS': []}):
            out = serve._reload_env_config()
        self.assertIn('BROWSE_ALLOWLIST_DOMAINS', out['changed'])

    def test_derived_errors_swallowed(self):
        out = self._call({'TTC_MAX_BEST_OF': 'bad', 'PUBLISH_REPO': None},
                         {}, google=None)
        self.assertEqual(out['changed'], [])
        out = self._call({'TTC_MAX_BEST_OF': '3'}, {},
                         ensure=RuntimeError('net'))
        self.assertTrue(out['reloaded'])


class ApplyOperatorPolicyTest(unittest.TestCase):
    def test_sets_all_globals(self):
        self.addCleanup(lambda: config.apply_policy_bindings('serve'))
        policy = {
            'successGradeFloor': 'B', 'peerReviewIntervalS': 1,
            'peerReviewFileProbability': 2,
            'directorEscalationReaskCooldownS': 3,
            'directorEscalationMaxAsks': 4, 'simWatchdogIntervalS': 5,
            'simWatchdogStaleS': 6, 'chatMaxTokensCap': 7,
            'smsTtlDefaultS': 8, 'smsTtlMinS': 9, 'smsTtlMaxS': 10,
            'decideBurstAllow': 11, 'decideBurstWindowS': 12,
            'decideMinIntervalS': 13, 'healthCheckIntervalS': 14,
            'healthAlertDedupWindowS': 15, 'healthDigestIntervalS': 16,
        }
        # The declarative bindings read config.get_operator_policy at bind
        # time; patch it there and force a serve-only rebind. The watchdog
        # overrides re-read serve's live env capture, so patching those
        # attributes takes effect at rebind time.
        with mock.patch.object(config, 'get_operator_policy',
                               return_value=policy), \
             mock.patch.object(serve, '_SimWatchdogIntervalSEnv', '1.5'), \
             mock.patch.object(serve, '_SimWatchdogStaleSEnv', ''):
            for key, default in config.OPERATOR_POLICY_DEFAULT.items():
                policy.setdefault(key, default)
            config.apply_policy_bindings('serve')
        self.assertEqual(serve.CHAT_MAX_TOKENS_CAP, 7)
        self.assertEqual(serve._SIM_WATCHDOG_INTERVAL_S, 1.5)
        self.assertEqual(serve._SIM_WATCHDOG_STALE_S, 6.0)
        self.assertEqual(serve.HEALTH_DIGEST_INTERVAL_S, 16)


class TeamStatusTextTest(unittest.TestCase):
    def test_full_view(self):
        now = time.time()
        state = {
            'agents': {'a1': {'task': 't1'}, 'a2': 'bad'},
            'tasks': {
                't1': {'title': 'T1', 'status': 'blocked', 'room': 'r1'},
                't2': {'title': 'T2', 'status': 'failed'},
                't3': {'title': 'T3', 'status': 'working',
                       'workUntil': now - 1000},
                't4': 'bad',
            },
            'workQueue': [
                {'title': 'w1', 'assignedTo': 'a1', 'room': 'r1',
                 'attempts': 3},
                {'title': 'w2'},
            ] + [{'title': f'q{i}'} for i in range(12)],
        }
        rows = [
            ('quality_gate_reject', '{"taskId": "t1", "note": "why"}'),
            ('worker_stuck_help', 'notjson'),
            ('quality_gate_reject', '{"taskId": "t1", "reason": "dup"}'),
            ('x', '{"taskId": "t2", "reason": "stuck"}'),
        ]
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = rows
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        esc = {'e1': {'status': 'pending', 'kind': 'k',
                      'question': 'q?', 'ts': now - 120,
                      'askCount': 1, 'humanNotified': True},
               'e2': 'bad'}
        with mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, '_load_escalations',
                               return_value=esc):
            out = serve._team_status_text()
        self.assertIn('WORKING NOW', out)
        self.assertIn('pinned to a1', out)
        self.assertIn('3 attempts', out)
        self.assertIn('and 4 more queued', out)
        self.assertIn('why: why', out)
        self.assertIn('NEEDS ATTENTION', out)
        self.assertIn('WAITING ON A HUMAN', out)
        self.assertIn('player already notified', out)

    def test_empty_and_db_failure(self):
        with mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_load_escalations',
                               return_value={}):
            out = serve._team_status_text()
        self.assertIn('nobody is on an task' if False else 'nobody is on a task',
                      out)
        self.assertIn('QUEUE: empty', out)
        with mock.patch.object(serve, 'get_state_from_db', return_value={}), \
             mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')), \
             mock.patch.object(serve, '_load_escalations',
                               side_effect=RuntimeError('esc')):
            out = serve._team_status_text()
        self.assertIn('QUEUE: empty', out)


class FoldDesignTasteTest(unittest.TestCase):
    def test_no_briefs_and_unreadable(self):
        self.assertEqual(serve._fold_design_taste([], 'p')[1],
                         'no design briefs in this project yet')
        with mock.patch.object(serve, '_safe_library_path',
                               return_value=None):
            self.assertEqual(serve._fold_design_taste(['a'], 'p')[1],
                             'no design briefs could be read')
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/a'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open', side_effect=OSError('no')):
            self.assertEqual(serve._fold_design_taste(['a'], 'p')[1],
                             'no design briefs could be read')

    def test_model_errors(self):
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/a'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='brief')), \
             mock.patch.object(serve, '_mid_tier_slug', return_value=''):
            self.assertIn('no mid-tier', serve._fold_design_taste(
                ['a'], 'p')[1])
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/a'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='brief')), \
             mock.patch.object(serve, '_mid_tier_slug',
                               return_value='mid'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               side_effect=RuntimeError('net')):
            self.assertIn('failed', serve._fold_design_taste(['a'], 'p')[1])
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/a'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='brief')), \
             mock.patch.object(serve, '_mid_tier_slug',
                               return_value='mid'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [
                                   {'message': {'content': ''}}]}):
            self.assertIn('no reply', serve._fold_design_taste(['a'], 'p')[1])

    def test_success(self):
        reply = {'choices': [{'message': {'content': 'TASTE'}}]}
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/a'), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch('builtins.open',
                        mock.mock_open(read_data='brief')), \
             mock.patch.object(serve, '_mid_tier_slug',
                               return_value='mid'), \
             mock.patch.object(serve, '_call_openrouter_sync',
                               return_value=reply):
            text, err = serve._fold_design_taste(
                ['a'], 'proj', owner='o', review_after_days='bad')
        self.assertIsNone(err)
        self.assertIn('TASTE', text)
        self.assertIn('Owner: o', text)


class AskCoreTest(unittest.TestCase):
    def _state(self, role='worker', instructions=None):
        return {'agents': {'a1': {'name': 'A', 'role': role,
                                  'profile': {'instructions': instructions}},
                           'a2': {'name': 'B'}},
                'tasks': {}, 'workQueue': []}

    def _ask(self, state=None, question='q', loop=None, **kw):
        state = state if state is not None else self._state()
        captured = {}

        def fake_loop(model, messages, tools, execute_tool, *args, **kwargs):
            captured['execute_tool'] = execute_tool
            captured['kwargs'] = kwargs
            captured['tools'] = tools
            return 'reply'

        patches = [
            mock.patch('sim._eligible_candidates',
                      return_value=kw.pop('candidates', ['a1'])),
            mock.patch.object(serve, '_resolve_model_tier',
                              return_value=kw.pop('model', 'model')),
            mock.patch.object(serve, 'get_or_create_agent_key',
                              return_value='k'),
            mock.patch.object(serve, '_make_web_tools_executor',
                              return_value=mock.Mock(return_value='web')),
            mock.patch('content._make_treg_tools_executor',
                      return_value=mock.Mock(return_value='treg')),
            mock.patch.object(serve, '_call_agent_tool_loop',
                              side_effect=loop or fake_loop),
            mock.patch.object(serve, '_apply_plain_writing',
                              side_effect=lambda m: m),
            mock.patch.object(serve, '_sim_village_for_agent',
                              return_value='v'),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_append_passport_decision'),
            mock.patch.object(serve, 'memory_context',
                              return_value=kw.pop('mctx', None)),
            mock.patch.object(serve, 'stm_append'),
            mock.patch.object(serve, 'ltm_maybe_extract'),
            mock.patch.object(serve, 'FILE_ACCESS_ENABLED', True),
            mock.patch.object(serve, 'APP_ACCESS_ENABLED', True),
            mock.patch.object(serve, '_effective_file_grants',
                              return_value=['g']),
            mock.patch.object(serve, '_effective_app_grants',
                              return_value=['g']),
            mock.patch.object(serve, 'TAVILY_API_KEY', 'tvly'),
            mock.patch.object(serve, '_team_status_text',
                              return_value='status'),
            mock.patch.object(serve, '_team_digest_text',
                              return_value='digest'),
            mock.patch.object(serve, '_allowlist_status_text',
                              return_value='allow'),
            mock.patch.object(serve, '_budget_usage_snapshot',
                              return_value=kw.pop('usage', {})),
            mock.patch.object(serve, '_own_work_feedback_text',
                              return_value='own'),
            mock.patch.object(serve, 'get_state_from_db',
                              return_value=state),
            mock.patch.object(serve, '_http_json',
                              return_value={'ok': True}),
            mock.patch.object(serve, '_is_admin',
                              return_value=kw.pop('admin', True)),
            mock.patch.object(serve, '_launch_adversarial_village',
                              return_value=(True, 'enabled')),
            mock.patch.object(serve, '_disable_adversarial_village'),
            mock.patch.object(serve, 'save_state_to_db'),
            mock.patch.object(serve, '_local_file_tool',
                              return_value='lf'),
            mock.patch.object(serve, '_read_store_tool',
                              return_value='rs'),
            mock.patch.object(serve, '_app_script_tool',
                              return_value='as'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = asyncio.run(serve._ask_core(state, question,
                                             session_id=kw.pop('session',
                                                              None),
                                             **kw))
        return result, captured

    def test_empty_question(self):
        with mock.patch('sim._eligible_candidates', return_value=[]):
            result = asyncio.run(serve._ask_core({}, ' '))
        self.assertEqual(result['status'], 400)

    def test_success_and_arms(self):
        result, captured = self._ask(session='s1',
                                     mctx={'history': [
                                               {'role': 'user',
                                                'content': 'earlier'}],
                                           'summary': 'sum',
                                           'ltm': 'ltm'})
        self.assertEqual(result['reply'], 'reply')
        ex = captured['execute_tool']
        self.assertEqual(ex('search_web', {}), 'web')
        self.assertEqual(ex('team_digest', {}), 'digest')
        self.assertEqual(ex('team_status', {}), 'status')
        self.assertEqual(ex('allowlist_status', {'host': 'h'}), 'allow')
        self.assertIn('TEAM STATUS', ex('team_overview', {}))
        result, captured = self._ask(usage={
            'A': {'used': 1, 'cap': 0, 'exceeded': False},
            'B': {'used': 2, 'cap': 5, 'exceeded': True}})
        ex = captured['execute_tool']
        self.assertIn('unlimited', ex('treasury_status', {}))
        self.assertIn('OVER CAP', ex('treasury_status', {}))
        self.assertIn('No budget ledgers',
                      self._ask(usage={})[1]['execute_tool'](
                          'treasury_status', {}))
        self.assertEqual(ex('read_peer_reviews', {}), 'own')
        self.assertIn('Standing of',
                      ex('read_peer_reviews', {'targetAgentId': 'a2'}))
        self.assertIn('No agent with id',
                      ex('read_peer_reviews', {'targetAgentId': 'zz'}))
        self.assertEqual(ex('x_trending_topics', {}), 'treg')
        self.assertEqual(ex('search_linkedin_posts', {}), 'treg')
        self.assertEqual(ex('local_file', {}), 'lf')
        self.assertEqual(ex('read_store', {}), 'rs')
        self.assertEqual(ex('app_script', {}), 'as')
        self.assertEqual(ex('api_call', {}), 'web')
        self.assertEqual(ex('adversarial_village',
                            {'action': 'enable', 'task': 't'}), 'enabled')
        self.assertIn('disabled', ex('adversarial_village',
                                     {'action': 'disable'}))
        self.assertIn('Usage', ex('adversarial_village', {'action': 'x'}))
        self.assertIn('Only the chief of staff',
                      self._ask(admin=False)[1]['execute_tool'](
                          'adversarial_village', {'action': 'enable'}))

    def test_security_role_arms(self):
        state = self._state(role='Red Team Auditor',
                            instructions=['step'])
        result, captured = self._ask(state=state)
        ex = captured['execute_tool']
        self.assertIn('ok', ex('attempt_curl', {'url': 'u'}))
        self.assertIn('ok', ex('request_capability_handle', {}))

    def test_force_first_tool_chain(self):
        for question, tool in [
                ('trending on x', 'x_trending_topics'),
                ('linkedin posts', 'search_linkedin_posts'),
                ('ais ships', 'read_ais_feed'),
                ('rss news', 'read_rss_feed'),
                ('bitcoin price', 'read_market_feed'),
                ('show team overview', 'team_overview'),
                ('team status', 'team_status'),
                ('team digest', 'team_digest'),
                ('allowlist status', 'allowlist_status'),
                ('treasury usage', 'treasury_status'),
                ('read my inbox', 'read_mailbox'),
                ('peer reviews about me', 'read_peer_reviews'),
                ('list services', 'list_services')]:
            _, captured = self._ask(question=question)
            self.assertEqual(captured['kwargs']['force_first_tool'], tool)

    def test_park_and_pin(self):
        state = self._state()
        result, _ = self._ask(state=state, candidates=[],
                              allow_park=False)
        self.assertEqual(result['status'], 409)
        state = self._state()
        result, _ = self._ask(state=state, candidates=[], session='s1',
                              mctx={'history': []})
        self.assertTrue(result['queued'])
        self.assertTrue(state['_pendingAsks'])
        result, _ = self._ask(state=self._state(), agent_id_hint='a1',
                              allow_admin_pin=True)
        self.assertEqual(result['agent'], 'a1')

    def test_model_none_and_loop_error(self):
        result, _ = self._ask(model='')
        self.assertIn("couldn't settle", result['reply'])
        result, _ = self._ask(loop=RuntimeError('boom'))
        self.assertEqual(result['status'], 500)


class PixelLabCreditsTest(unittest.TestCase):
    def test_none_token(self):
        with mock.patch.object(serve, '_credential_token',
                               return_value=None), \
             mock.patch.object(serve, '_open_secret', return_value=''), \
             mock.patch.object(serve, '_pixellab_token', return_value=None):
            self.assertIsNone(serve._pixellab_credits_remaining())

    def test_success_and_failure(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = (
            b'{"credits": {"usd": 5}}')
        with mock.patch.object(serve, '_credential_token',
                               return_value='tok'), \
             mock.patch.object(serve, '_open_secret', return_value='tok'), \
             mock.patch('urllib.request.urlopen', return_value=resp):
            self.assertEqual(serve._pixellab_credits_remaining(), 5.0)
        with mock.patch.object(serve, '_credential_token',
                               return_value='tok'), \
             mock.patch.object(serve, '_open_secret', return_value='tok'), \
             mock.patch('urllib.request.urlopen',
                        side_effect=RuntimeError('net')):
            self.assertIsNone(serve._pixellab_credits_remaining())


class CanSetTeamAgreementTest(unittest.TestCase):
    def test_all_paths(self):
        state = {'teams': [{'id': 't1', 'directorId': 'd1'}]}
        with mock.patch.object(serve, '_is_admin', return_value=False):
            self.assertTrue(serve._can_set_team_agreement(
                state, 'player', 't1'))
            self.assertTrue(serve._can_set_team_agreement(
                state, 'd1', 't1'))
            self.assertTrue(serve._can_set_team_agreement(
                state, 't1', 't1'))
            self.assertFalse(serve._can_set_team_agreement(
                state, 'other', 't1'))
            self.assertFalse(serve._can_set_team_agreement(
                state, 'd1', 'nope'))
        with mock.patch.object(serve, '_is_admin', return_value=True):
            self.assertTrue(serve._can_set_team_agreement(
                state, 'x', 't1'))


class HiggsfieldManifestTest(unittest.TestCase):
    def test_writes_and_swallows(self):
        with mock.patch.object(serve, '_http_json') as post:
            serve._higgsfield_log_manifest(
                'image', 'A Prompt!', 'ep', ['u1', 'u2'], 'cora', 'k')
        body = post.call_args[0]
        self.assertEqual(body[2], '/api/library/file')
        self.assertIn('media/generated/', body[3]['path'])
        with mock.patch.object(serve, '_http_json',
                               side_effect=RuntimeError('net')):
            serve._higgsfield_log_manifest(
                'image', 'p', 'ep', ['u'], 'cora', 'k')


class ApiKeyBrokenTest(unittest.TestCase):
    def test_states(self):
        with mock.patch.object(serve, '_api_key_health', {}):
            self.assertFalse(serve._api_key_is_broken('s'))
        with mock.patch.object(serve, '_api_key_health',
                               {'s': {'blocked_until': 0}}):
            self.assertFalse(serve._api_key_is_broken('s'))
        health = {'s': {'blocked_until': 100.0}}
        with mock.patch.object(serve, '_api_key_health', health), \
             mock.patch('time.time', return_value=200.0):
            self.assertFalse(serve._api_key_is_broken('s'))
            self.assertTrue(health['s']['probing'])
            self.assertTrue(serve._api_key_is_broken('s'))
        with mock.patch.object(serve, '_api_key_health',
                               {'s': {'blocked_until': 100.0}}), \
             mock.patch('time.time', return_value=50.0):
            self.assertTrue(serve._api_key_is_broken('s'))
