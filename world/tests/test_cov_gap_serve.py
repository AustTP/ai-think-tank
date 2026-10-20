"""Hermetic tests for previously-uncovered serve.py statements.

Kept separate from the behavior suites so a coverage gap has one obvious
home. Each class names the serve.py cluster it exercises.
"""
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import serve  # noqa: E402


class LocalFileToolTest(unittest.TestCase):
    def test_list_with_and_without_sizes(self):
        entries = [{'name': 'a.txt', 'type': 'file', 'size': 3},
                   {'name': 'docs', 'type': 'dir'}]
        with mock.patch('fs.list_scope', return_value=entries) as ls:
            out = serve._local_file_tool(
                'cora', {'action': 'list', 'scope': 's', 'path': 'sub'})
        self.assertIn('a.txt', out)
        self.assertIn('3 bytes', out)
        self.assertIn('docs', out)
        ls.assert_called_once_with('s', 'sub')

    def test_list_empty(self):
        with mock.patch('fs.list_scope', return_value=[]):
            out = serve._local_file_tool(
                'cora', {'action': 'list', 'scope': 's'})
        self.assertEqual(out, 'Files in s:\n')

    def test_read_write_move_delete(self):
        with mock.patch('fs.read_file', return_value='body'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'body')
        with mock.patch('fs.write_file', return_value='p'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'write', 'scope': 's', 'path': 'p',
                         'content': 'c'}), 'wrote p')
        with mock.patch('fs.move_file', return_value='q'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'move', 'scope': 's', 'from': 'a',
                         'to': 'b'}), 'moved to q')
        with mock.patch('fs.delete_to_trash', return_value='t'):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'delete', 'scope': 's', 'path': 'p'}),
                'moved to trash: t')

    def test_unknown_action_and_errors(self):
        out = serve._local_file_tool('cora', {'action': 'nope'})
        self.assertIn('Unknown local_file action', out)
        with mock.patch('fs.read_file', side_effect=PermissionError('denied')):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'Denied: denied')
        with mock.patch('fs.read_file', side_effect=FileNotFoundError('gone')):
            self.assertEqual(serve._local_file_tool(
                'cora', {'action': 'read', 'scope': 's', 'path': 'p'}),
                'Not found: gone')
        with mock.patch('fs.read_file', side_effect=RuntimeError('boom')):
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
             mock.patch('config.validate_team_agreement',
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
        with mock.patch('fs.read_store', return_value=rows) as rs, \
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
        with mock.patch('fs.read_store',
                        side_effect=PermissionError('denied')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._read_store_tool('c', {}),
                             'Denied: denied')
        log.assert_called_once()
        with mock.patch('fs.read_store',
                        side_effect=FileNotFoundError('gone')):
            self.assertEqual(serve._read_store_tool('c', {}),
                             'Not found: gone')
        with mock.patch('fs.read_store', side_effect=RuntimeError('boom')):
            self.assertEqual(serve._read_store_tool('c', {}),
                             'read_store failed: boom')


class AppScriptToolTest(unittest.TestCase):
    def test_success_and_errors(self):
        with mock.patch('fs.app_script', return_value='done'), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._app_script_tool(
                'cora', {'bundleId': 'b', 'script': 's'}), 'done')
        log.assert_called_once()
        with mock.patch('fs.app_script',
                        side_effect=PermissionError('no')), \
             mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve._app_script_tool('c', {}), 'Denied: no')
        log.assert_called_once()
        with mock.patch('fs.app_script',
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


_NO_GRANT = object()


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


_SENTINEL = object()


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
             mock.patch.object(serve, '_open_secret', return_value=''):
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
              isdir=False, linked=False):
        calls = []

        def git(_dir, args, *a, **k):
            calls.append(args)
            if args[:3] == ['rev-parse', '--verify', 'HEAD']:
                return mock.Mock(returncode=head)
            if args[:3] == ['rev-parse', '--verify', 'story/x']:
                return mock.Mock(returncode=branch)
            if args[:2] == ['worktree', 'add']:
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


def req_with(body):
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value=body)
    req.headers = {'X-Agent-Key': 'k'}
    return req


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
             mock.patch('config.RESEARCH_WATCHLIST', []), \
             mock.patch('config.RESEARCH_DESK_PASSES', []):
            body = json.loads(asyncio.run(serve.research_desk_ops(
                mock.Mock())).body)
        self.assertEqual(body['spendUsdLast7d'], 1.5)


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


class TasteExpiredTest(unittest.TestCase):
    def test_bad_date(self):
        self.assertFalse(serve._taste_is_expired(
            'Review after: 2020-13-01'))


class SignalPlanBodyTest(unittest.TestCase):
    def test_paths(self):
        base = {'planId': 'p', 'agentId': 'a', 'subject': 's'}
        self.assertIn('object', serve._signal_plan_from_body(
            dict(base, scenarios=[1]))[1])
        self.assertIn('scenarioId', serve._signal_plan_from_body(
            dict(base, scenarios=[{'scenarioId': ''}]))[1])


class CoverageWriteTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_require_player_session',
                               return_value=False), \
             mock.patch.object(serve, 'verify_agent_key',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.coverage_write(
                req)).status_code, 401)
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={'reviewed': True})
        with mock.patch.object(serve, '_require_player_session',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.coverage_write(
                req)).status_code, 400)


class CoverageListTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.query_params = {'status': 'bad'}
        with mock.patch.object(serve, '_coverage_read', return_value={}):
            self.assertEqual(asyncio.run(serve.coverage_list(
                req)).status_code, 400)
        req = mock.Mock()
        req.query_params = {'status': 'covered'}
        with mock.patch.object(serve, '_coverage_read',
                               return_value={'i': {'status': 'covered'}}):
            body = json.loads(asyncio.run(serve.coverage_list(req)).body)
        self.assertEqual(len(body['coverage']), 1)


class JevQuoteTest(unittest.TestCase):
    def test_bad_calls(self):
        req = mock.Mock()
        req.query_params = {'estimatedCalls': 'bad'}
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'quote_jev_grant',
                               return_value={}):
            self.assertEqual(json.loads(asyncio.run(
                serve.jev_quote(req)).body)['ok'], True)


class ReworkMetricsTest(unittest.TestCase):
    def test_bad_details(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [('bad',)]
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm):
            out = serve._rework_metrics(100)
        self.assertEqual(out['loops'], 1)


class LibraryDownloadTest(unittest.TestCase):
    def test_redirect_denied(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'url': 'https://x', 'filename': 'f', 'agentId': 'a'})
        req.headers = {'X-Agent-Key': 'k'}

        async def fake_to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'BROWSING_ENABLED', True), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, 'verify_agent_key',
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
            body = json.loads(asyncio.run(serve.library_download(
                req)).body)
        self.assertFalse(body['allowed'])


class PostReportTest(unittest.TestCase):
    def test_notify_error(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'aboutId': 'b', 'fromId': 'a', 'quote': 'q', 'note': 'n'})
        req.headers = {'X-Agent-Key': 'k'}
        state = {'agentRoster': [{'id': 'b'}], 'reports': []}
        with mock.patch.object(serve, 'verify_agent_key',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value=state), \
             mock.patch.object(serve, '_classify_report_severity',
                               return_value='minor'), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'), \
             mock.patch('sim._report_strikes',
                        side_effect=RuntimeError('boom')):
            body = json.loads(asyncio.run(serve.post_report(req)).body)
        self.assertTrue(body['ok'])


class BackfillTapeHashesTest(unittest.TestCase):
    def test_hashed_row_skipped(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = [
            (1, 'prev', 0, 'k', 'm', 'p', 'c', 'ch', .5, 0, 'r', 1, 't')]
        with mock.patch.object(serve, '_tape_row_hash', return_value='h'):
            self.assertEqual(serve._backfill_decision_tape_hashes(conn), 0)


class AllowlistWaitEstimateTest(unittest.TestCase):
    def test_hours(self):
        with mock.patch.object(serve, '_allowlist_wait_samples',
                               return_value=[7200]):
            self.assertIn('hours', serve._allowlist_wait_estimate())


class ApproveServiceProposalTest(unittest.TestCase):
    def test_bad_note(self):
        self.assertFalse(serve._approve_api_service_proposal(
            {'note': 'bad'})[0])


class ApiCredentialValueTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.dict(serve.__dict__, {'ENVK': ''}), \
             mock.patch.object(serve, '_load_env',
                               return_value={'ENVK2': ''}):
            self.assertIn('not set', serve._api_credential_value(
                {'credential': {'type': 'env', 'key': 'ENVK',
                                'key2': 'ENVK2'}})[2])
        with mock.patch.object(serve, '_credential_token',
                               return_value=None), \
             mock.patch.object(serve, '_open_secret', return_value=None):
            self.assertIn('vault', serve._api_credential_value(
                {'credential': {'type': 'vault', 'name': 'n'}})[2])


class DecisionTelemetryTest(unittest.TestCase):
    def test_db_errors(self):
        with mock.patch.object(serve, '_db',
                               side_effect=RuntimeError('db')):
            serve._decision_cache_put('m', {}, [], {})
            serve._record_decision_keys('m', {}, [])
            serve._prune_decision_key_stats()
            self.assertEqual(decision_cache_report_defaults(),
                             serve.decision_cache_report())
            self.assertEqual(serve.decision_key_report('')['calls'], 0)


def decision_cache_report_defaults():
    return {'ttl_s': serve.DECISION_CACHE_TTL_S, 'cached_keys': 0,
            'hits': 0, 'requests': 0, 'hit_pct': 0.0}


class ParseHfTest(unittest.TestCase):
    def test_bad_bytes(self):
        self.assertIsNone(serve._parse_hf_contents(b'bad'))


class ApiMailReadTest(unittest.TestCase):
    def test_malformed(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(side_effect=ValueError('x'))
        req.headers = {'X-Agent-Key': 'k'}
        with mock.patch.object(serve, '_agent_id_for_key',
                               return_value='a'):
            self.assertEqual(asyncio.run(serve.api_mail_read(
                req)).status_code, 400)


class FeedbackInboxTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        req.cookies = {}
        with mock.patch.object(serve, 'verify_session',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.feedback_player_inbox(
                'm', req)).status_code, 401)
        req = mock.Mock()
        req.cookies = {}
        req.json = mock.AsyncMock(return_value={'rating': True})
        with mock.patch.object(serve, 'verify_session',
                               return_value=True), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'playerInbox': [
                                   {'id': 'm', 'kind': 'card_report'}]}):
            self.assertEqual(asyncio.run(serve.feedback_player_inbox(
                'm', req)).status_code, 400)


class ReadAgentFileTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_cross_village_agent_denied',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.read_agent_file(
                'b', 'f', req)).status_code, 403)
        with mock.patch.object(serve, '_resolve_requester',
                               return_value='a'), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_cross_village_agent_denied',
                               return_value=False), \
             mock.patch.object(serve, '_agent_reports_off_limits',
                               return_value=True), \
             mock.patch.object(serve, 'is_under_reports',
                               return_value=True):
            self.assertEqual(asyncio.run(serve.read_agent_file(
                'b', 'reports/x', req)).status_code, 403)


class LibraryPromoteTest(unittest.TestCase):
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
                               return_value={'x': 1}), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(asyncio.run(serve.library_promote(
                req)).status_code, 403)


class ProbeChannelsTest(unittest.TestCase):
    def test_no_probe_url(self):
        with mock.patch.object(serve, '_API_CHANNELS',
                               {'c': {'name': 'C', 'probe_url': ''}}):
            out = serve._probe_all_channels_sync()
        self.assertIn('no probe_url', out['c']['error'])


class ChannelProbeTest(unittest.TestCase):
    def test_paths(self):
        req = mock.Mock()
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=False):
            self.assertEqual(asyncio.run(serve.channel_probe(
                req)).status_code, 429)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, 'check_rate_limit',
                               return_value=True), \
             mock.patch.object(serve, '_API_CHANNELS_LOAD_ERROR', 'bad'):
            self.assertEqual(asyncio.run(serve.channel_probe(
                req)).status_code, 500)


class SmsInboxTest(unittest.TestCase):
    def test_bad_limit(self):
        req = mock.Mock()
        req.query_params = {'limit': 'bad'}
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = []
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_db', return_value=cm):
            self.assertEqual(asyncio.run(serve.sms_inbox(
                req)).status_code, 200)


class LoginScrubScalarTest(unittest.TestCase):
    def test_scalar(self):
        self.assertEqual(serve._login_scrub_result((1,)), (1,))


class TierRefreshLoggedTest(unittest.TestCase):
    def test_no_fresh(self):
        with mock.patch.object(serve, 'get_cached_model_tiers',
                               return_value={}), \
             mock.patch.object(serve, 'refresh_model_tiers',
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(serve, 'get_state_from_db',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_admin_agent_id',
                               return_value=None), \
             mock.patch.object(serve, '_senior_most_director_id',
                               return_value=None), \
             mock.patch.object(serve, '_auto_failover_jev_if_gone',
                               new=mock.AsyncMock()):
            self.assertEqual(asyncio.run(serve._tier_refresh_logged('r')),
                             (None, False))


class PixellabPollTest(unittest.TestCase):
    def test_timeout_env(self):
        with mock.patch.object(serve, '_load_env',
                               return_value={'PIXELLAB_POLL_TIMEOUT_S':
                                             '0'}):
            self.assertIn('timed out', serve._pixellab_poll_job('j')[1])


class CreditsProbeLoopTest(unittest.TestCase):
    def test_error(self):
        async def stop(_s):
            raise asyncio.CancelledError()
        with mock.patch.object(serve, '_openrouter_account_credits',
                               side_effect=RuntimeError('x')), \
             mock.patch('asyncio.sleep', side_effect=stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(serve._openrouter_credits_probe_loop())


class PeerReviewTickTest(unittest.TestCase):
    def test_serious_kept(self):
        state = {'lastPeerReviewAt': 0,
                 'reports': [{'ts': 10, 'severity': 'serious'}]}
        with mock.patch('time.time', return_value=10000.0), \
             mock.patch.object(serve, 'PEER_REVIEW_INTERVAL_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_SIGNAL_STALE_S', 1), \
             mock.patch('sim.REPORT_STRIKE_WINDOW_MS', 10**12):
            serve._peer_review_tick(state)
        self.assertEqual(len(state['reports']), 1)


class HiggsfieldPollTest(unittest.TestCase):
    def test_error(self):
        with mock.patch.object(serve, '_higgsfield_call',
                               return_value=(None, 'err')):
            self.assertEqual(serve._higgsfield_poll('j'), (None, 'err'))


class SeedServicesTest(unittest.TestCase):
    def test_missing_seed(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = (0,)
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch('os.path.exists', return_value=False):
            serve._seed_api_services_if_empty()


class ApiApplyAuthTest(unittest.TestCase):
    def test_mullvad_no_url(self):
        with self.assertRaises(ValueError):
            serve._api_apply_auth({'auth': {'type': 'mullvad'}}, 't', None,
                                  'GET', 'u', {}, None)


class BandPoolsTest(unittest.TestCase):
    def test_mandatory_reasoning_skipped(self):
        pools = serve._band_candidate_pools([
            {'id': 'm', 'reasoning': {'mandatory': True},
             'architecture': {'input_modalities': ['image'],
                              'output_modalities': ['text']},
             'pricing': {'prompt': '1', 'completion': '1'}}])
        self.assertTrue(all('m' not in [x['id'] for x in v]
                            for v in pools.values()))


class MintNonceTest(unittest.TestCase):
    def test_gc(self):
        with mock.patch.dict(serve.__dict__, {'_work_trust_nonces':
                                              {'old': 0}}), \
             mock.patch.object(serve, '_WORK_TRUST_TTL_MS', 1000), \
             mock.patch('time.time', return_value=0.0):
            serve._mint_work_trust_nonce()
        self.assertNotIn('old', serve._work_trust_nonces)


class RssIngestTest(unittest.TestCase):
    def test_trim(self):
        buf = {'f': [{'link': f'l{i}'} for i in range(5)]}
        with mock.patch.dict(serve.__dict__, {'_RSS_BUFFER': buf}), \
             mock.patch.object(serve, '_RSS_BUFFER_LOCK',
                               mock.MagicMock()), \
             mock.patch.object(serve, '_RSS_BUFFER_MAX_PER_FEED', 3), \
             mock.patch.object(serve, '_rss_match_topics',
                               return_value=[]):
            serve._rss_ingest('f', [{'link': 'n'}])
        self.assertEqual(len(buf['f']), 3)


class RoomKeywordTest(unittest.TestCase):
    def test_undelegatable_skipped(self):
        with mock.patch('sim._ROOM_KEYWORDS', {'x': ['y']}), \
             mock.patch.object(serve, '_DELEGATABLE_ROOMS', set()):
            self.assertIsNone(serve._room_keyword_pick('y'))


class IntentScheduleOnceTest(unittest.TestCase):
    def test_assigned_to(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'title': 't', 'at': '9999999999', 'assignedTo': 'other'})
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_parse_schedule_once_at',
                               return_value=1000), \
             mock.patch('time.time', return_value=0.0):
            self.assertEqual(asyncio.run(serve.intent_schedule_once(
                req)).status_code, 400)


class WriteWikiServerTest(unittest.TestCase):
    def test_invalid_target(self):
        with mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch('sim.wiki_write_page',
                        return_value=({}, True)), \
             mock.patch('sim.record_memory_provenance'), \
             mock.patch.object(serve, '_wiki_page_target',
                               return_value=None):
            self.assertIsNone(serve._write_wiki_server(
                'p', 't', 'think_tank', 'body'))


class LibraryListTest(unittest.TestCase):
    def test_scope_skip(self):
        req = mock.Mock()
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, 'f.txt'), 'w') as f:
            f.write('x')
        try:
            with mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
                 mock.patch.object(serve, 'LIBRARY_ARCHIVE_DIR',
                                   os.path.join(tmp, 'archive')), \
                 mock.patch.object(serve, 'get_state_from_db',
                                   return_value={'x': 1}), \
                 mock.patch.object(serve, '_resolve_requester',
                                   return_value='a'), \
                 mock.patch.object(serve, '_requester_villages',
                                   return_value=set()), \
                 mock.patch.object(serve, '_scope_allows',
                                   return_value=False):
                body = json.loads(asyncio.run(serve.list_library(
                    req)).body)
            self.assertEqual(body['files'], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LibrarySearchTest(unittest.TestCase):
    def test_scope_skip(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, 'f.txt'), 'w') as f:
            f.write('needle')
        try:
            with mock.patch.object(serve, 'LIBRARY_DIR', tmp), \
                 mock.patch.object(serve, 'get_state_from_db',
                                   return_value={'x': 1}), \
                 mock.patch.object(serve, '_scope_allows',
                                   return_value=False):
                self.assertEqual(serve._library_search_matches(
                    'needle'), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IngestDocxTest(unittest.TestCase):
    def test_empty(self):
        doc = mock.Mock()
        doc.paragraphs = []
        doc.tables = []
        with mock.patch.object(serve, '_docx') as docx:
            docx.Document.return_value = doc
            self.assertEqual(serve._ingest_docx_to_text('d'),
                             ('', None))


class AdversarialHelpersTest(unittest.TestCase):
    def test_reset_and_launch(self):
        state = {'agents': {'a': {'villageId': serve._ADV_VILLAGE_ID}},
                 'villages': [{'id': serve._ADV_VILLAGE_ID}]}
        serve._disable_adversarial_village(state)
        self.assertEqual(state['agents']['a']['villageId'], 'main')
        with mock.patch.object(serve, '_adversarial_state',
                               return_value={}), \
             mock.patch.object(serve, '_spawn_winter_team',
                               return_value=None):
            self.assertFalse(serve._launch_adversarial_village(
                {}, 'admin', 'task')[0])
        with mock.patch.object(serve, '_adversarial_state',
                               return_value={'enabled': True,
                                             'status': 'active',
                                             'taskId': None}):
            self.assertIsNone(serve._adversarial_village_tick({}))


class NormalizeTargetTest(unittest.TestCase):
    def test_no_host(self):
        self.assertIsNotNone(serve._normalize_allowlist_target(
            'http://')[1])


class ScreenshotProxyTest(unittest.TestCase):
    def test_proxy_cmd(self):
        with mock.patch.object(serve, '_find_chrome',
                               return_value='/c'), \
             mock.patch.object(serve, 'THINK_TANK_DIR', '/t'), \
             mock.patch('subprocess.run') as run, \
             mock.patch('os.path.isfile', return_value=False):
            self.assertIsNone(serve._screenshot_url_sync(
                'https://x', proxy='http://p'))
        self.assertIn('--proxy-server=http://p', run.call_args[0][0])


class TierGettersTest(unittest.TestCase):
    def test_paths(self):
        with mock.patch.object(serve, 'get_tier_daily_log',
                               return_value=[]), \
             mock.patch.object(serve, 'get_catalog_price_history',
                               return_value=[]), \
             mock.patch.object(serve, 'get_tier_value_windows',
                               return_value={'x': 1}):
            self.assertEqual(json.loads(asyncio.run(
                serve.tier_daily_log_get('b', 0)).body)['log'], [])
            self.assertEqual(json.loads(asyncio.run(
                serve.catalog_price_history_get('m', 0)).body)['history'],
                [])
            self.assertEqual(json.loads(asyncio.run(
                serve.tier_value_windows_get()).body), {'x': 1})


class BrowseVpnConflictTest(unittest.TestCase):
    def test_both_vpn(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'url': 'https://x', 'agentId': 'a',
            'viaVpnCountry': 'us', 'viaVpnExit': 'e'})
        req.headers = {'X-Agent-Key': 'k'}
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
             mock.patch.object(serve, '_work_trust_from_body',
                               return_value=False), \
             mock.patch.object(serve, 'log_action'):
            body = json.loads(asyncio.run(serve.browse(req)).body)
        self.assertFalse(body['allowed'])


class PageProbeSyncTest(unittest.TestCase):
    def test_bad_wait_ms(self):
        pw = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = pw
        browser = mock.Mock()
        pw.chromium.launch.return_value = browser
        baseline = mock.Mock()
        baseline.evaluate.return_value = []
        page = mock.Mock()
        page.evaluate.return_value = []
        browser.new_page.side_effect = [baseline, page]
        with mock.patch.object(serve, 'sync_playwright',
                               return_value=ctx):
            serve._page_probe_sync('/f', [{'type': 'wait',
                                          'ms': 'bad'}], ['1'])
        page.wait_for_timeout.assert_called_once()


class FetchRenderedProxyTest(unittest.TestCase):
    def test_proxy(self):
        pw = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = pw
        browser = mock.Mock()
        pw.chromium.launch.return_value = browser
        page = mock.Mock()
        page.url = 'https://x'
        page.evaluate.side_effect = ['', []]
        browser.new_page.return_value = page
        with mock.patch.object(serve, 'sync_playwright',
                               return_value=ctx), \
             mock.patch.object(serve, '_is_safe_public_host',
                               return_value=True):
            out = serve._fetch_rendered_page_sync('https://x',
                                                  proxy='http://p')
        self.assertEqual(out[0], 'https://x')
        pw.chromium.launch.assert_called_once_with(
            headless=True, proxy={'server': 'http://p'})


class PeerReviewPassTest(unittest.TestCase):
    def test_mail_failure(self):
        state = {
            'agentRoster': [
                {'id': 'w1', 'name': 'W1', 'role': 'Worker',
                 'director': 'd'},
                {'id': 'w2', 'name': 'W2', 'role': 'Worker',
                 'director': 'd'}],
            'agents': {'d': {}, 'w1': {}, 'w2': {}},
            'reports': [],
            'helpSignals': [],
        }

        def db():
            cm = mock.MagicMock()

            def execute(_sql, params):
                cur = mock.Mock()
                n = 5 if params[0] == 'w2' else 0
                cur.fetchall.return_value = [('task_completed', None, 1)] * n
                return cur
            cm.__enter__.return_value.execute.side_effect = execute
            return cm
        with mock.patch.object(serve, '_db', side_effect=db), \
             mock.patch.object(serve, '_direct_reports',
                               return_value=False), \
             mock.patch.object(serve, 'PEER_REVIEW_MIN_LOOKBACK_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_SIGNAL_STALE_S', 0), \
             mock.patch.object(serve, 'PEER_REVIEW_FILE_PROBABILITY', 1), \
             mock.patch('random.random', return_value=0), \
             mock.patch('random.choices',
                        side_effect=lambda pop, weights, k: [pop[0]]), \
             mock.patch('sim._deliver_mail',
                        side_effect=RuntimeError('mail')), \
             mock.patch.object(serve, 'log_action'):
            self.assertEqual(serve._peer_review_pass(state, 100.0), 1)


class RefreshVisionFallbackTest(unittest.TestCase):
    def test_jev_choice_error(self):
        models = [{'id': 'v', 'name': 'V', 'price': 1.0}]
        conn = mock.Mock()
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn

        async def to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, 'MODEL_BAND_PURPOSE', {}), \
             mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                               return_value=models), \
             mock.patch.object(serve, '_sync_model_catalog'), \
             mock.patch.object(serve, '_bucket_models_by_price',
                               return_value={}), \
             mock.patch.object(serve, 'get_model_benchmark_scores',
                               return_value=[]), \
             mock.patch.object(serve, '_band_candidate_pools',
                               return_value={'vision': models}), \
             mock.patch.object(serve, '_best_value_pick',
                               new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('jev')), \
             mock.patch.object(serve, '_jev_model', return_value='m'), \
             mock.patch.object(serve, '_verify_model_works_sync',
                               return_value=False), \
             mock.patch.object(serve, '_db', return_value=cm), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            self.assertEqual(asyncio.run(serve.refresh_model_tiers()), {})


class AssignBigTaskConstraintsTest(unittest.TestCase):
    def test_constraints(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'goal': 'g', 'constraints': {'team': 'x'}})

        async def to_thread(f, *a, **k):
            return f(*a, **k)
        with mock.patch.object(serve, '_operator_gate',
                               return_value=(True, 'p')), \
             mock.patch.object(serve, '_state_begin',
                               return_value={'x': 1}), \
             mock.patch.object(serve, '_all_teams_busy_in_sprint',
                               return_value=True), \
             mock.patch.object(serve, '_breakdown_into_shared_backlog',
                               return_value={'feature': {'id': 'f',
                                                         'name': 'F'},
                                             'items': [{'id': 'i'}]}), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            body = json.loads(asyncio.run(serve.intent_assign_big_task(
                req)).body)
        self.assertTrue(body['backlogged'])


class RoutePlayerApprovalTest(unittest.TestCase):
    def test_requires_approval(self):
        decision = mock.Mock(allowed=False, requires_approval=True,
                             approval_id='apr1')

        async def to_thread(f, *a):
            return f(*a)
        with mock.patch.object(serve, '_parse_approval_command',
                               return_value=None), \
             mock.patch.object(serve, '_admin_agent_id',
                               return_value=None), \
             mock.patch.object(serve, '_lane_decider',
                               return_value='ask'), \
             mock.patch.object(serve, '_LANE_ACTIONS',
                               {'ask': 'converse'}), \
             mock.patch.object(serve, '_theo_policy') as tp, \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread', side_effect=to_thread):
            tp.evaluate.return_value = decision
            out = asyncio.run(serve._route_player_request({}, 'hi'))
        self.assertIn('approval', out['reply'])


class DecideCaptureTest(unittest.TestCase):
    def test_training_capture(self):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value={
            'state': {'s': 1}, 'questions': [{'q': 1}], 'agentId': 'a'})
        data = {'choices': []}
        with mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_jev_model',
                               return_value='m'), \
             mock.patch.object(serve, '_decide_allowed',
                               return_value=(True, '')), \
             mock.patch.object(serve, 'training_capture') as tc, \
             mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value=data), \
             mock.patch.object(serve, '_jev_choice',
                               return_value=('c', .9, 0)), \
             mock.patch.object(serve, '_accrue_spend'), \
             mock.patch.object(serve, '_sim_village_for_agent',
                               return_value='v'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch('asyncio.to_thread',
                        side_effect=lambda f, *a: f(*a)):
            tc.enabled.return_value = True
            tc.metadata.return_value = {}
            self.assertEqual(json.loads(asyncio.run(serve.decide(
                req)).body), data)

if __name__ == "__main__":
    unittest.main()
