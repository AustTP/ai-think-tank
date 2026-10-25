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


class LocalFileToolTest(unittest.TestCase):
    def test_list_with_and_without_sizes(self):
        entries = [{'name': 'a.txt', 'type': 'file', 'size': 3},
                   {'name': 'docs', 'type': 'dir'}]
        with mock.patch('core.fs.list_scope', return_value=entries) as ls:
            out = serve._local_file_tool(
                'cora', {'action': 'list', 'scope': 's', 'path': 'sub'})
        self.assertIn('a.txt', out)
        self.assertIn('3 bytes', out)
        self.assertIn('docs', out)
        ls.assert_called_once_with('s', 'sub')

    def test_list_empty(self):
        with mock.patch('core.fs.list_scope', return_value=[]):
            out = serve._local_file_tool(
                'cora', {'action': 'list', 'scope': 's'})
        self.assertEqual(out, 'Files in s:\n')

    def test_read_write_move_delete(self):
        with mock.patch('core.fs.read_file', return_value='body'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'body')
        with mock.patch('core.fs.write_file', return_value='p'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'write', 'scope': 's', 'path': 'p',
                         'content': 'c'}), 'wrote p')
        with mock.patch('core.fs.move_file', return_value='q'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'move', 'scope': 's', 'from': 'a',
                         'to': 'b'}), 'moved to q')
        with mock.patch('core.fs.delete_to_trash', return_value='t'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'delete', 'scope': 's', 'path': 'p'}),
                'moved to trash: t')

    def test_unknown_action_and_errors(self):
        out = serve._local_file_tool('cora', {'action': 'nope'})
        self.assertIn('Unknown local_file action', out)
        with mock.patch('core.fs.read_file', side_effect=PermissionError('denied')):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'Denied: denied')
        with mock.patch('core.fs.read_file', side_effect=FileNotFoundError('gone')):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'Not found: gone')
        with mock.patch('core.fs.read_file', side_effect=RuntimeError('boom')):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'local_file failed: boom')


class BudgetUsageSnapshotTest(unittest.TestCase):
    def test_all_ledgers_ok(self):
        with mock.patch.object(serve, '_spend_cap_month_view',
                               return_value={'usage': 1, 'cap': 10,
                                             'exceeded': False}), \
             mock.patch.object(serve, '_high_tier_spend_this_month',
                               return_value=2.0), \
             mock.patch.object(serve, 'HIGH_TIER_MONTHLY_BUDGET_USD', 5), \
             mock.patch.object(serve, '_apify_spend_this_month',
                               return_value=3.0), \
             mock.patch.object(serve, 'APIFY_MONTHLY_BUDGET_USD', 6), \
             mock.patch.object(serve, '_page_budget_used',
                               return_value=4), \
             mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 7), \
             mock.patch.object(serve, '_colab_spend_this_month',
                               return_value=5.0), \
             mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 8):
            out = serve._budget_usage_snapshot()
        self.assertEqual(out['SPEND_CAP_USD']['used'], 1)
        self.assertEqual(out['HIGH_TIER_MONTHLY_BUDGET_USD']['used'], 2.0)
        self.assertEqual(out['APIFY_MONTHLY_BUDGET_USD']['used'], 3.0)
        self.assertEqual(out['PAGE_REQUEST_MONTHLY_BUDGET'], {
            'used': 4, 'cap': 7, 'exceeded': False})
        self.assertEqual(out['COLAB_MONTHLY_UNITS']['cap'], 8)

    def test_every_ledger_failure_is_swallowed(self):
        boom = RuntimeError('boom')
        with mock.patch.object(serve, '_spend_cap_month_view',
                               side_effect=boom), \
             mock.patch.object(serve, '_high_tier_spend_this_month',
                               side_effect=boom), \
             mock.patch.object(serve, '_apify_spend_this_month',
                               side_effect=boom), \
             mock.patch.object(serve, '_page_budget_used',
                               side_effect=boom), \
             mock.patch.object(serve, '_colab_spend_this_month',
                               side_effect=boom):
            self.assertEqual(serve._budget_usage_snapshot(), {})


class BudgetViewTest(unittest.TestCase):
    def _call(self):
        return asyncio.run(serve.budget_view(None))

    def test_unauthorized(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            resp = self._call()
        self.assertEqual(resp.status_code, 401)

    def test_full_view(self):
        state = {
            'directorBudgets': {'d1': '5.0', 'd2': 'bad', 'd3': 0},
            'budgetRequests': [{'id': 'r1'}, 'junk'],
        }
        ledger = {
            '__director__/d1': {'byMonth': {'2026-10': 1.5, '2026-09': 9}},
            'other': {'byMonth': {'2026-10': 3}},
        }
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_budget_usage_snapshot',
                               return_value={'SPEND_CAP_USD': {'used': 1}}), \
             mock.patch.object(serve, '_OPENROUTER_CREDITS_CACHE',
                               {'data': {'totalCredits': 10,
                                         'totalUsage': 4},
                                'at': time.time()}), \
             mock.patch.object(serve, 'OPENROUTER_MONTHLY_BUDGET_USD', 50), \
             mock.patch.object(serve, '_spend_ledger_read',
                               return_value=ledger), \
             mock.patch.object(serve, '_spend_cap_period',
                               return_value='2026-10'), \
             mock.patch.object(serve, '_openrouter_month_to_date',
                               return_value=3.0), \
             mock.patch.object(serve, '_ledger_month_to_date',
                               return_value=4.0), \
             mock.patch.object(serve, '_bank_budget_view',
                               return_value={'apify': {'monthUsed': 1,
                                                       'used': 2, 'calls': 3,
                                                       'cap': 4}}), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_subtree_agent_ids',
                               return_value=['d1', 'a1']), \
             mock.patch.object(serve, 'COLAB_LEDGER_KEY', 'colab'):
            resp = self._call()
        body = json.loads(resp.body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['openrouter']['deposited_usd'], 10.0)
        self.assertEqual(body['openrouter']['remaining_usd'], 6.0)
        self.assertEqual(body['openrouter']['used_pct'], 0.4)
        self.assertEqual(body['openrouter']['monthToDateUsd'], 3.0)
        self.assertEqual(body['monthToDate']['allServicesUsd'], 4.0)
        self.assertEqual(body['services'][0]['service'], 'apify')
        self.assertEqual(body['directorBudgets']['d1']['capUsd'], 5.0)
        self.assertEqual(body['directorBudgets']['d1']['subtreeUsedUsd'], 1.5)
        self.assertEqual(body['budgetRequests'], [{'id': 'r1'}])

    def test_ledger_failure_and_empty_credits(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_budget_usage_snapshot',
                               return_value={}), \
             mock.patch.object(serve, '_OPENROUTER_CREDITS_CACHE',
                               {'data': None, 'at': 0}), \
             mock.patch.object(serve, '_spend_ledger_read',
                               side_effect=[RuntimeError('boom'), {}]), \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            body = json.loads(self._call().body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['monthToDate'], {})
        self.assertEqual(body['services'], [])
        self.assertEqual(body['directorBudgets'], {})
        self.assertNotIn('deposited_usd', body['openrouter'])


class SupervisorOverrideTest(unittest.TestCase):
    def _call(self, **kwargs):
        return asyncio.run(serve._supervisor_override_decision(
            kwargs.get('agent_id', 'a1'), 'execute', 'rm -rf /',
            'cleanup', 'blocked', kwargs.get('cats')))

    def test_no_key(self):
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', ''):
            self.assertEqual(self._call(), (False,
                                            'no model key for the '
                                            'supervisor review'))

    def test_state_failure_and_no_director(self):
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'get_state_from_db',
                               side_effect=RuntimeError('db')):
            self.assertIn('no supervisor', self._call()[1])
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'agentRoster': [
                                   {'id': 'a1', 'name': 'A'}]}):
            self.assertIn('no supervisor', self._call()[1])

    def test_no_mid_model(self):
        state = {'agentRoster': [{'id': 'a1', 'name': 'A',
                                  'director': 'd1'},
                                 {'id': 'd1', 'name': 'D'}]}
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value=state), \
             mock.patch.object(serve, '_mid_tier_slug', return_value=''):
            self.assertIn('no mid model', self._call()[1])

    def _patches(self, state, reply=None, side_effect=None, conf=0.5,
                 cats=None):
        p = [
            mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'),
            mock.patch.object(serve, 'get_state_from_db',
                              return_value=state),
            mock.patch.object(serve, '_mid_tier_slug',
                              return_value='mid'),
            mock.patch.object(serve, '_effective_safety_confidence',
                              return_value=conf),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_append_passport_decision'),
            mock.patch.object(serve, '_call_openrouter_once_sync',
                              return_value=reply,
                              side_effect=side_effect),
        ]
        return p

    def _with(self, patches, fn):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return fn()

    def test_error_path(self):
        state = {'agentRoster': [{'id': 'a1', 'name': 'A',
                                  'director': 'd1'},
                                 {'id': 'd1', 'name': 'D'}]}
        self._with(self._patches(state, side_effect=RuntimeError('net')),
                   lambda: self.assertIn(
                       'supervisor review failed',
                       self._call()[1]))

    def test_kept_blocked_and_override(self):
        state = {'agentRoster': [{'id': 'a1', 'name': 'A',
                                  'director': 'd1'},
                                 {'id': 'd1', 'name': 'D'}]}
        keep = {'choices': [{'message': {'content': json.dumps(
            {'decision': 'keep_blocked', 'confidence': 0.9,
             'reason': 'nope'})}}]}
        over = {'choices': [{'message': {'content': json.dumps(
            {'decision': 'override', 'confidence': 0.9,
             'reason': 'fine'})}}]}
        self._with(self._patches(state, reply=keep),
                   lambda: self.assertEqual(
                       self._call(cats=['x']), (False, 'nope')))
        self._with(self._patches(state, reply=over),
                   lambda: self.assertEqual(
                       self._call(), (True, 'fine')))

    def test_bad_confidence_and_missing_decision(self):
        state = {'agentRoster': [{'id': 'a1', 'name': 'A',
                                  'director': 'd1'},
                                 {'id': 'd1', 'name': 'D'}]}
        bad = {'choices': [{'message': {'content': json.dumps(
            {'decision': 'override', 'confidence': 'bad',
             'reason': 'x'})}}]}
        self._with(self._patches(state, reply=bad, conf=0.5),
                   lambda: self.assertFalse(self._call()[0]))
        empty = {'choices': [{'message': {'content': '{}'}}]}
        self._with(self._patches(state, reply=empty),
                   lambda: self.assertEqual(
                       self._call()[1], 'supervisor kept the block'))


class SimWatchdogLoopTest(unittest.TestCase):
    def _run(self, holder, patches=(), sleep_error=True):
        calls = {'n': 0}

        async def fake_sleep(_):
            calls['n'] += 1
            if sleep_error and calls['n'] >= 2:
                raise asyncio.CancelledError()

        patches = list(patches) + [
            mock.patch('asyncio.sleep', side_effect=fake_sleep)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve._sim_watchdog_loop(holder, 0.01, 0.01))

    def test_shutting_down_returns(self):
        self._run({'shutting_down': True, 'task': None}, sleep_error=False)

    def test_dead_missing_task_restarts_and_note_fails(self):
        holder = {'shutting_down': False, 'task': None}
        patches = [
            mock.patch('sim._sim_loop', new=lambda: asyncio.sleep(0)),
            mock.patch.object(serve, '_sim_watchdog_note_failure',
                              side_effect=RuntimeError('note')),
        ]
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, patches)
        self.assertIsNotNone(holder['task'])

    def test_dead_cancelled_task_and_restart_fails(self):
        task = mock.Mock()
        task.done.return_value = True
        task.cancelled.return_value = True
        holder = {'shutting_down': False, 'task': task}
        patches = [
            mock.patch('asyncio.create_task',
                       side_effect=RuntimeError('no task')),
            mock.patch.object(serve, '_sim_watchdog_note_failure'),
            mock.patch('sim._sim_loop', new=lambda: None),
        ]
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, patches)

    def test_dead_task_exception_lookup_raises(self):
        task = mock.Mock()
        task.done.return_value = True
        task.cancelled.return_value = False
        task.exception.side_effect = BaseException('gone')
        holder = {'shutting_down': False, 'task': task}
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._sim_loop', new=lambda: asyncio.sleep(0)),
                mock.patch.object(serve, '_sim_watchdog_note_failure')])

    def test_alive_healthy_note_raises_and_dormant(self):
        task = mock.Mock()
        task.done.return_value = False
        holder = {'shutting_down': False, 'task': task}
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._LAST_TICK_WALL_EPOCH', time.time()),
                mock.patch.object(serve, '_sim_watchdog_note_healthy',
                                  side_effect=RuntimeError('healthy')),
                mock.patch.object(serve, '_dormant', return_value=True)])

    def test_alive_stale_recycles(self):
        task = mock.Mock()
        task.done.return_value = False
        holder = {'shutting_down': False, 'task': task}
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._LAST_TICK_WALL_EPOCH', 1.0),
                mock.patch.object(serve, '_sim_watchdog_note_healthy'),
                mock.patch.object(serve, '_dormant', return_value=False),
                mock.patch('asyncio.wait_for',
                           side_effect=asyncio.TimeoutError()),
                mock.patch('asyncio.create_task',
                           side_effect=RuntimeError('no task')),
                mock.patch.object(serve, '_sim_watchdog_note_failure',
                                  side_effect=RuntimeError('note')),
                mock.patch('sim._sim_loop', new=lambda: None)])
        task.cancel.assert_called_once()

    def test_alive_zero_stamp_skips_stale(self):
        task = mock.Mock()
        task.done.return_value = False
        holder = {'shutting_down': False, 'task': task}
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._LAST_TICK_WALL_EPOCH', 0.0),
                mock.patch.object(serve, '_sim_watchdog_note_healthy')])

    def test_tick_stamp_conversion_fails(self):
        task = mock.Mock()
        task.done.return_value = False
        holder = {'shutting_down': False, 'task': task}
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._LAST_TICK_WALL_EPOCH', 'abc'),
                mock.patch.object(serve, '_sim_watchdog_note_healthy')])

    def test_alive_stale_restart_succeeds(self):
        task = mock.Mock()
        task.done.return_value = False
        holder = {'shutting_down': False, 'task': task}

        async def _noop():
            return None

        with self.assertRaises(asyncio.CancelledError):
            self._run(holder, [
                mock.patch('sim._LAST_TICK_WALL_EPOCH', 1.0),
                mock.patch.object(serve, '_sim_watchdog_note_healthy'),
                mock.patch.object(serve, '_dormant', return_value=False),
                mock.patch('asyncio.wait_for',
                           side_effect=asyncio.TimeoutError()),
                mock.patch.object(serve, '_sim_watchdog_note_failure'),
                mock.patch('sim._sim_loop', new=_noop)])
        self.assertIsNotNone(holder['task'])

    def test_body_cancelled_error_reraises(self):
        holder = mock.Mock()
        holder.get.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder)

    def test_outer_exception_logged(self):
        holder = mock.Mock()
        holder.get.side_effect = RuntimeError('holder')
        with self.assertRaises(asyncio.CancelledError):
            self._run(holder)


class WebToolsExecutorTest(unittest.TestCase):
    def _ex(self, struck=None, trusted=False):
        with mock.patch.object(serve, '_mint_work_trust_nonce',
                               return_value='nonce'):
            return serve._make_web_tools_executor(
                'cora', 'key', default_query='dq', struck_tools=struck,
                work_context='ctx', work_context_trusted=trusted)

    def test_strike_and_status_arms(self):
        struck = {'browse_page'}
        ex = self._ex(struck)
        self.assertIn('already blocked', ex('browse_page', {}))
        with mock.patch.object(serve, '_team_status_text',
                               return_value='team'), \
             mock.patch.object(serve, '_allowlist_status_text',
                               return_value='allow'), \
             mock.patch.object(serve, '_read_mailbox_tool_text',
                               return_value='mail'), \
             mock.patch.object(serve, '_higgsfield_generate',
                               return_value='gen'):
            self.assertEqual(ex('team_status', {}), 'team')
            self.assertEqual(ex('allowlist_status', {'host': 'h'}), 'allow')
            self.assertEqual(ex('read_mailbox', {}), 'mail')
            self.assertEqual(ex('generate_image', {}), 'gen')
            self.assertEqual(ex('generate_video', {}), 'gen')

    def test_mail_arms(self):
        ex = self._ex()
        with mock.patch.object(serve, '_http_json',
                               return_value={'ok': True, 'marked': 3,
                                             'agentId': 'cora'}):
            self.assertIn('Mail sent', ex('send_mail', {
                'toAgentId': 'bob', 'text': ' hi '}))
            self.assertIn('Marked 3', ex('mark_mailbox_read',
                                         {'agentId': 'cora'}))
        self.assertIn('requires a toAgentId', ex('send_mail', {}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'nope'}):
            self.assertIn('Could not send the mail', ex(
                'send_mail', {'toAgentId': 'b', 'text': 'x'}))
            self.assertIn('Could not mark the mailbox read',
                          ex('mark_mailbox_read', {}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'nope'}):
            self.assertIn('Could not file the allowlist request',
                          ex('request_allowlist', {'host': 'h'}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'message': 'm',
                                             'requested': False}):
            self.assertIn('no further request needed',
                          ex('request_allowlist', {'host': 'h'}))

    def test_allowlist_mutations(self):
        ex = self._ex()
        with mock.patch.object(serve, '_http_json',
                               return_value={'message': 'm'}):
            self.assertIn('granted', ex('grant_allowlist', {'host': 'h'}))
            self.assertIn('revoked', ex('revoke_allowlist', {'host': 'h'}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'no'}):
            self.assertIn('Could not grant', ex('grant_allowlist', {}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            self.assertIn('unexpected response',
                          ex('grant_allowlist', {}))

    def test_search_web(self):
        ex = self._ex()
        with mock.patch.object(serve, '_api_base_url',
                               return_value='http://api'), \
             mock.patch.object(serve, '_api_execute',
                               return_value={'ok': True,
                                             'modelInstruction': 'mi',
                                             'textForModel': 'txt'}):
            self.assertEqual(ex('search_web', {}), 'mi\n\ntxt')
        with mock.patch.object(serve, '_api_base_url',
                               return_value='http://api'), \
             mock.patch.object(serve, '_api_execute',
                               return_value={'ok': False, 'reason': 'r'}):
            self.assertIn('Could not search the web: r',
                          ex('search_web', {'query': 'q'}))

    def test_ais_feed(self):
        ex = self._ex()
        with mock.patch.object(serve, '_ws_recent',
                               return_value=[{'topic': 't', 'lat': 1,
                                              'lon': 2},
                                             {'latitude': 3,
                                              'longitude': 4}]), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': False,
                                             'last_error': 'err'}), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_ais_feed', {'max': 5})
        raw = wrap.call_args[0][0]
        self.assertIn('collector error: err', raw)
        self.assertIn('recent frames', raw)

    def test_rss_feed(self):
        ex = self._ex()
        feeds = {'a': {'name': 'A'}}
        with mock.patch.object(serve, '_RSS_FEEDS', feeds):
            self.assertIn('Unknown feed "zzz"',
                          ex('read_rss_feed', {'feed': 'zzz'}))
        with mock.patch.object(serve, '_RSS_FEEDS', feeds), \
             mock.patch.object(serve, '_RSS_STATUS',
                               {'a': {'ok': False,
                                      'last_error': 'down'}}), \
             mock.patch.object(serve, '_rss_recent',
                               return_value=[
                                   {'title': 'T', 'link': 'L',
                                    'matchedTopics': ['m']},
                                   {'title': 'T2', 'link': None}]), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_rss_feed', {})
        raw = wrap.call_args[0][0]
        self.assertIn('last_error=down', raw)
        self.assertIn('watchlist: m', raw)
        with mock.patch.object(serve, '_RSS_FEEDS', feeds), \
             mock.patch.object(serve, '_RSS_STATUS', {'a': {'ok': True}}), \
             mock.patch.object(serve, '_rss_recent', return_value=[]), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_rss_feed', {})
        self.assertIn('no items buffered', wrap.call_args[0][0])

    def test_market_feed(self):
        ex = self._ex()
        with mock.patch.object(serve, '_kraken_normalize_pair',
                               return_value='BTCUSD'), \
             mock.patch.object(serve, '_ws_latest',
                               return_value={'BTCUSD': {
                                   'price': 1, 'bid': 2, 'ask': 3,
                                   'ts': 4}}), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': False,
                                             'last_error': 'err'}), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_market_feed', {'pair': 'btc'})
        raw = wrap.call_args[0][0]
        self.assertIn('collector error: err', raw)
        self.assertIn('BTCUSD: 1', raw)

    def test_market_feed_no_quotes(self):
        ex = self._ex()
        with mock.patch.object(serve, '_kraken_normalize_pair',
                               return_value=None), \
             mock.patch.object(serve, '_ws_latest', return_value={}), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': True}), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_market_feed', {})
        self.assertIn('no quotes buffered', wrap.call_args[0][0])

    def test_generic_feed(self):
        ex = self._ex()
        with mock.patch.object(serve, '_WS_FEEDS', {'a': {}}):
            self.assertIn('Unknown feed service "zzz"',
                          ex('read_live_feed', {'service': 'zzz'}))
        with mock.patch.object(serve, '_WS_FEEDS', {'a': {}}), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': True}), \
             mock.patch.object(serve, '_ws_recent',
                               return_value=[{'topic': 't', 'x': 1,
                                              'ts': 2}]), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_live_feed', {})
        self.assertIn('x=1', wrap.call_args[0][0])
        with mock.patch.object(serve, '_WS_FEEDS', {'a': {}}), \
             mock.patch.object(serve, '_ws_status',
                               return_value={'connected': False,
                                             'last_error': 'e'}), \
             mock.patch.object(serve, '_ws_recent', return_value=[]), \
             mock.patch.object(serve, 'wrap_external_content',
                               return_value=('w', 'n', 't', 'i')) as wrap:
            ex('read_live_feed', {})
        self.assertIn('no records buffered', wrap.call_args[0][0])

    def test_download_arms(self):
        with mock.patch.object(serve, '_http_json',
                               return_value={'ok': True, 'path': 'd/f.zip',
                                             'bytes': 5, 'sha256': 'abcdef'}):
            self.assertIn('Downloaded to your own filespace',
                          self._ex(struck=set())(
                              'download_file', {'url': 'u'}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'not approved'}), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='soon'):
            self.assertIn('ONE-STRIKE', self._ex(struck=set())(
                'download_file', {'url': 'u'}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'timeout'}):
            self.assertIn('transient', self._ex(struck=set())(
                'download_file', {'url': 'u'}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            self.assertIn('unexpected response', self._ex(struck=set())(
                'download_file', {'url': 'u'}))

    def test_api_call_arms(self):
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True,
                                             'modelInstruction': 'mi',
                                             'textForModel': 'txt'}):
            self.assertEqual(self._ex(struck=set())('api_call', {}),
                             'mi\n\ntxt')
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': False,
                                             'reason': 'no'}), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='soon'):
            self.assertIn('ONE-STRIKE', self._ex(struck=set())(
                'api_call', {}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'boom'}):
            self.assertIn('transient', self._ex(struck=set())(
                'api_call', {}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            self.assertIn('unexpected response', self._ex(struck=set())(
                'api_call', {}))

    def test_service_arms(self):
        ex = self._ex()
        with mock.patch.object(serve, '_http_json',
                               return_value={'requested': True,
                                             'serviceId': 's',
                                             'escalationId': 'e'}):
            self.assertIn("service 's'", ex('propose_api_service', {}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'bad'}):
            self.assertIn('Could not file the service proposal',
                          ex('propose_api_service', {}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            self.assertIn('unexpected response',
                          ex('propose_api_service', {}))
        services = [{'id': 's1', 'name': 'S1', 'baseUrl': 'http://s1',
                     'methods': ['GET'], 'spend': 'metered',
                     'description': 'does things'}]
        with mock.patch.object(serve, '_http_json',
                               return_value={'services': services}):
            out = ex('list_services', {})
        self.assertIn('when to use: does things', out)
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'nope'}):
            self.assertIn('Could not list registered services',
                          ex('list_services', {}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            self.assertIn('unexpected response',
                          ex('list_services', {}))

    def test_browse_arms(self):
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': True,
                                             'modelInstruction': 'mi',
                                             'textForModel': 'txt',
                                             'links': [{'text': 'T',
                                                        'url': 'U'}]}):
            ex = self._ex(struck=set())
            ex.observe_urls('https://a')
            out = ex('browse_page', {'url': 'https://a'})
        self.assertIn('Real links found', out)
        with mock.patch.object(serve, '_http_json',
                               return_value={'allowed': False,
                                             'reason': 'no'}), \
             mock.patch.object(serve, '_allowlist_wait_estimate',
                               return_value='soon'):
            ex = self._ex(struck=set())
            ex.observe_urls('https://a')
            self.assertIn('ONE-STRIKE', ex('browse_page', {'url': 'https://a'}))
        with mock.patch.object(serve, '_http_json',
                               return_value={'error': 'boom'}):
            ex = self._ex(struck=set())
            ex.observe_urls('https://a')
            self.assertIn('transient', ex('browse_page', {'url': 'https://a'}))
        with mock.patch.object(serve, '_http_json', return_value='raw'):
            ex = self._ex(struck=set())
            ex.observe_urls('https://a')
            self.assertIn('unexpected response', ex(
                'browse_page', {'url': 'https://a'}))

    def test_unknown_tool(self):
        with self.assertRaises(ValueError):
            self._ex()('nope', {})


class BrowserActEndpointTest(unittest.TestCase):
    def _req(self, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        req.headers = {'X-Agent-Key': 'key'}
        return req

    def _call(self, body, classify=(True, 'ok'), supervisor=(True, 'ok'),
              run=({'ok': True, 'text': 't', '_shotB64': 's',
                    'value': 'v'}, None), extra=()):
        patches = [
            mock.patch.object(serve, 'EXECUTION_ENABLED', True),
            mock.patch.object(serve, 'BROWSING_ENABLED', True),
            mock.patch.object(serve, '_village_sandbox_id',
                              side_effect=lambda a, s: s),
            mock.patch.object(serve, '_work_trust_from_body',
                              return_value=False),
            mock.patch.object(serve, 'check_rate_limit',
                              return_value=True),
            mock.patch.object(serve, 'verify_agent_key', return_value=True),
            mock.patch.object(serve, '_is_safe_public_host',
                              return_value=True),
            mock.patch.object(serve, '_classify_browser_action',
                              new=mock.AsyncMock(return_value=classify)),
            mock.patch.object(serve, '_supervisor_override_decision',
                              new=mock.AsyncMock(return_value=supervisor)),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_sandbox_dir_for',
                              return_value='/tmp'),
            mock.patch.object(serve, '_run_browser_act_sync',
                              return_value=run),
            mock.patch.object(serve, 'resolve_capability_handle',
                              return_value={'kind': 'login',
                                            'username': 'u',
                                            'password': 'p'}),
            mock.patch.object(serve, 'verify_capability_handle_scope',
                              return_value={'kind': 'login'}),
        ] + list(extra)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(serve.browser_act(self._req(body)))

    def test_disabled(self):
        with mock.patch.object(serve, 'EXECUTION_ENABLED', False):
            self.assertEqual(
                asyncio.run(serve.browser_act(self._req({}))).status_code,
                403)
        with mock.patch.object(serve, 'EXECUTION_ENABLED', True), \
             mock.patch.object(serve, 'BROWSING_ENABLED', False):
            self.assertEqual(
                asyncio.run(serve.browser_act(self._req({}))).status_code,
                403)

    def test_validation(self):
        self.assertEqual(self._call({'sandboxId': '..'}).status_code, 400)
        self.assertEqual(self._call({'op': 'fill'}).status_code, 400)
        self.assertEqual(self._call({'op': 'fill_many'}).status_code, 400)
        self.assertEqual(self._call({'op': 'login'}).status_code, 400)
        self.assertEqual(self._call({'op': 'click'}).status_code, 400)

    def test_credential_validation(self):
        base = {'op': 'fill', 'selector': '#a',
                'credentialHandle': 'h', 'url': 'https://a.com'}
        self.assertEqual(self._call({**base,
                                     'credentialField': 'x'}).status_code,
                         400)
        self.assertEqual(self._call({'op': 'fill', 'selector': '#a',
                                     'credentialHandle': 'h',
                                     'credentialField': 'username'}
                                    ).status_code, 400)
        self.assertEqual(self._call(
            {**base, 'credentialField': 'username'},
            extra=[mock.patch.object(serve, 'verify_capability_handle_scope',
                                     return_value=None)]
        ).status_code, 403)

    def test_supervisor_override_logs_allowed(self):
        resp = self._call({'op': 'goto', 'url': 'https://a.com'},
                          classify=(False, 'blocked'),
                          supervisor=(True, 'ok'))
        self.assertTrue(json.loads(resp.body)['allowed'])

    def test_handle_grant_failures(self):
        body = {'op': 'fill', 'selector': '#a', 'credentialHandle': 'h',
                'credentialField': 'username', 'url': 'https://a.com'}
        self.assertEqual(self._call(
            body, extra=[mock.patch.object(
                serve, 'resolve_capability_handle', return_value=None)]
        ).status_code, 403)
        self.assertEqual(self._call(
            body, extra=[mock.patch.object(
                serve, 'resolve_capability_handle',
                return_value={'kind': 'login'})]
        ).status_code, 400)

    def test_fill_many_fields(self):
        body = {'op': 'fill_many', 'url': 'https://a.com', 'fields': [
            {'selector': '#u', 'credentialHandle': 'h',
             'credentialField': 'password'},
            {'selector': '#p', 'value': 'v'}]}
        resp = self._call(body)
        self.assertTrue(json.loads(resp.body)['ok'])

    def test_field_errors(self):
        base = {'op': 'fill_many', 'url': 'https://a.com'}
        self.assertEqual(self._call({**base, 'fields': [
            {'selector': '#a', 'credentialHandle': 'h',
             'credentialField': 'x'}]}).status_code, 400)
        self.assertEqual(self._call(
            {**base, 'fields': [
                {'selector': '#a', 'credentialHandle': 'h',
                 'credentialField': 'password'}]},
            extra=[mock.patch.object(
                serve, 'resolve_capability_handle', return_value=None)]
        ).status_code, 403)
        self.assertEqual(self._call(
            {**base, 'fields': [
                {'selector': '#a', 'credentialHandle': 'h',
                 'credentialField': 'password'}]},
            extra=[mock.patch.object(
                serve, 'resolve_capability_handle',
                return_value={'kind': 'login'})]
        ).status_code, 400)

    def test_rate_limit_and_input_guards(self):
        self.assertEqual(self._call({}, extra=[mock.patch.object(
            serve, 'check_rate_limit', return_value=False)]).status_code, 429)
        self.assertEqual(self._call({'op': 'nope'}).status_code, 400)
        self.assertEqual(self._call({'op': 'goto'}).status_code, 400)
        self.assertEqual(self._call({'op': 'goto',
                                     'url': 'ftp://x'}).status_code, 400)
        self.assertEqual(self._call(
            {'op': 'goto', 'url': 'https://a.com'},
            extra=[mock.patch.object(serve, '_is_safe_public_host',
                                     return_value=False)]).status_code, 400)

    def test_supervisor_keeps_block(self):
        resp = self._call({'op': 'goto', 'url': 'https://a.com'},
                          classify=(False, 'blocked'),
                          supervisor=(False, 'kept'))
        body = json.loads(resp.body)
        self.assertFalse(body['allowed'])
        self.assertEqual(body['reason'], 'blocked')

    def test_handle_fill_injects_value(self):
        resp = self._call({'op': 'fill', 'selector': '#a',
                           'credentialHandle': 'h',
                           'credentialField': 'username',
                           'url': 'https://a.com'})
        self.assertTrue(json.loads(resp.body)['ok'])

    def test_fill_success_and_run_error(self):
        resp = self._call({'op': 'fill', 'selector': '#a', 'value': 'v'})
        self.assertNotIn('value', json.loads(resp.body))
        resp = self._call({'op': 'goto', 'url': 'https://a.com'},
                          run=(None, 'boom'))
        body = json.loads(resp.body)
        self.assertFalse(body['ok'])
        self.assertEqual(body['error'], 'boom')


class MullvadTokenTest(unittest.TestCase):
    def _resp(self, url='https://safe.example/token', status=200,
              body=b'raw'):
        resp = mock.Mock()
        resp.geturl.return_value = url
        resp.read.return_value = body
        resp.status = status
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        return cm

    def test_missing_account(self):
        with self.assertRaises(ValueError):
            serve._mullvad_token_request_sync('https://x', '')

    def test_success(self):
        with mock.patch.object(serve, '_safe_urlopen',
                               return_value=self._resp()), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            self.assertEqual(serve._mullvad_token_request_sync(
                'https://safe.example/token', '123'), 'raw')

    def test_unsafe_redirect_and_http_error(self):
        with mock.patch.object(serve, '_safe_urlopen',
                               return_value=self._resp()), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=False):
            with self.assertRaises(ValueError):
                serve._mullvad_token_request_sync('https://x', '123')
        with mock.patch.object(serve, '_safe_urlopen',
                               return_value=self._resp(status=500)), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            with self.assertRaises(ValueError):
                serve._mullvad_token_request_sync('https://x', '123')


class CompletePlayerTaskTest(unittest.TestCase):
    def _call(self, task_id='t1'):
        return asyncio.run(serve.complete_player_task_endpoint(
            task_id, mock.Mock()))

    def test_unauthorized_and_no_state(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(False, None)):
            self.assertEqual(self._call().status_code, 401)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value=None):
            self.assertEqual(self._call().status_code, 503)

    def test_unknown_and_success(self):
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value={'x': 1}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch('sim.complete_player_task', return_value=None):
            self.assertEqual(self._call().status_code, 404)
        task = {'id': 't1', 'title': 'T', 'status': 'done'}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'player')), \
             mock.patch.object(serve, '_state_begin', return_value={'x': 1}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch('sim.complete_player_task', return_value=task):
            body = json.loads(self._call().body)
        self.assertEqual(body['task']['status'], 'done')
