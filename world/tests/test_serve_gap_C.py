"""Gap-C coverage tests for serve.py: the Colab compute lifecycle, model
catalog / tier refresh, escalation-judge drift helpers, wake/dormancy and the
state save/merge surface. Everything that would touch the network, a real
process, or the real think_tank.db is patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  COVERAGE_FILE=/tmp/cov_gap_C.coverage python3 -m coverage run --source=serve tests/test_serve_gap_C.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve

_MODULE_TMP_DIR = None
_MODULE_PATCHER = None


def setUpModule():
    global _MODULE_TMP_DIR, _MODULE_PATCHER
    _MODULE_TMP_DIR = tempfile.mkdtemp(prefix='think-tank-serve-gap-')
    _MODULE_PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_MODULE_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_MODULE_TMP_DIR,
        AGENTS_DIR=os.path.join(_MODULE_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_MODULE_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_MODULE_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _MODULE_PATCHER.start()
    serve.init_db()


def tearDownModule():
    _MODULE_PATCHER.stop()
    shutil.rmtree(_MODULE_TMP_DIR, ignore_errors=True)


class _StopLoop(Exception):
    pass


async def _stop_loop(_seconds):
    raise _StopLoop()


class ColabCli(unittest.TestCase):
    def test_runs_subprocess_and_combines_streams(self):
        proc = unittest.mock.Mock(returncode=0, stdout='out', stderr='err')
        with unittest.mock.patch.object(serve.subprocess, 'run', return_value=proc) as run:
            rc, out = serve._colab_cli('status', '-s', 's')
        self.assertEqual((rc, out), (0, 'out\nerr'))
        run.assert_called_once_with(
            [serve.COLAB_CLI_PATH, 'status', '-s', 's'],
            capture_output=True, text=True, timeout=120, input=None,
        )

    def test_timeout_returns_negative_rc(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run',
                side_effect=subprocess.TimeoutExpired(cmd=['colab'], timeout=120)):
            rc, out = serve._colab_cli('usage', timeout=120)
        self.assertEqual(rc, -1)
        self.assertIn('timeout after 120s', out)

    def test_generic_exception_is_surfaced(self):
        with unittest.mock.patch.object(
                serve.subprocess, 'run', side_effect=RuntimeError('boom')):
            rc, out = serve._colab_cli('usage')
        self.assertEqual(rc, -1)
        self.assertEqual(out, 'boom')


class ColabStandbyEnsureSession(unittest.TestCase):
    def test_provision_failure_reports_cli_tail(self):
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(1, 'oops' * 100)):
            ok, msg = serve._colab_standby_ensure_session('sess')
        self.assertFalse(ok)
        self.assertTrue(msg.startswith('provision failed:'))
        self.assertIn('oops', msg)


class ColabStandbyForward(unittest.TestCase):
    def test_spawn_failure_returns_none_and_leaves_state_untouched(self):
        state = {'pid': None, 'proc': None, 'session': None}
        with unittest.mock.patch.object(serve, '_COLAB_STANDBY_FORWARD', state), \
             unittest.mock.patch.object(serve, '_colab_standby_stop_forward') as stop, \
             unittest.mock.patch.object(
                 serve.subprocess, 'Popen',
                 side_effect=Exception('ssh spawn failed')):
            proc = serve._colab_standby_ensure_forward('sess')
        self.assertIsNone(proc)
        stop.assert_called_once()
        self.assertEqual(state, {'pid': None, 'proc': None, 'session': None})

    def test_stop_forward_ignores_kill_failure_and_resets_state(self):
        proc = unittest.mock.Mock()
        proc.kill.side_effect = Exception('kill failed')
        state = {'pid': 42, 'proc': proc, 'session': 'sess'}
        with unittest.mock.patch.object(serve, '_COLAB_STANDBY_FORWARD', state):
            serve._colab_standby_stop_forward()
        self.assertEqual(state, {'pid': None, 'proc': None, 'session': None})
        proc.kill.assert_called_once()

    def test_stop_forward_with_no_proc_is_a_noop(self):
        state = {'pid': None, 'proc': None, 'session': None}
        with unittest.mock.patch.object(serve, '_COLAB_STANDBY_FORWARD', state):
            serve._colab_standby_stop_forward()
        self.assertEqual(state, {'pid': None, 'proc': None, 'session': None})


class ColabFailoverLoopTests(unittest.TestCase):
    def test_disabled_standby_sleeps_and_continues(self):
        calls = {'n': 0}

        def fake_sleep(*a, **k):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise _StopLoop()

        with unittest.mock.patch.object(serve.asyncio, 'sleep',
                                        side_effect=fake_sleep), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_jev_is_degraded') as jev:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        jev.assert_not_called()

    def test_degraded_cannot_provision(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_is_degraded', return_value=True), \
             unittest.mock.patch.object(
                 serve, '_colab_standby_ensure_session',
                 return_value=(False, 'no quota')) as ensure_session, \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        ensure_session.assert_called_once()
        self.assertTrue(any('cannot provision' in str(a) for a in pr.call_args_list))

    def test_degraded_laya_up_behind_forward(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_is_degraded', return_value=True), \
             unittest.mock.patch.object(
                 serve, '_colab_standby_ensure_session', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_standby_ensure_forward',
                                        return_value='proc'), \
             unittest.mock.patch.object(serve, '_colab_standby_reachable',
                                        return_value=True), \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        self.assertTrue(any('Laya up behind the loopback' in str(a) for a in pr.call_args_list))

    def test_degraded_boot_laya_not_reachable_yet(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_is_degraded', return_value=True), \
             unittest.mock.patch.object(
                 serve, '_colab_standby_ensure_session', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_standby_ensure_forward',
                                        return_value='proc'), \
             unittest.mock.patch.object(serve, '_colab_standby_reachable',
                                        return_value=False), \
             unittest.mock.patch.object(
                 serve, '_colab_standby_ensure_service',
                 return_value='boot log') as ensure_service, \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        ensure_service.assert_called_once()
        self.assertTrue(any('standing Laya up' in str(a) for a in pr.call_args_list))

    def test_healthy_tears_down_standby_once_cycles_met(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_is_degraded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_standby_reachable',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_colab_standby_teardown_service') as teardown, \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_HEALTHY_CYCLES', 1), \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        teardown.assert_called_once()
        self.assertTrue(any('stood down' in str(a) for a in pr.call_args_list))

    def test_loop_error_is_swallowed(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_colab_standby_enabled', return_value=True), \
             unittest.mock.patch.object(
                 serve, '_jev_is_degraded', side_effect=RuntimeError('boom')), \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_failover_loop())
        self.assertTrue(any('loop error' in str(a) for a in pr.call_args_list))


class ColabComputeIdleLoop(unittest.TestCase):
    def test_idle_sessions_are_torn_down(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_COLAB_COMPUTE_LAST_USED', 1.0), \
             unittest.mock.patch.object(
                 serve, '_colab_session_exists',
                 side_effect=lambda s: s == serve.COLAB_GPU_SESSION), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_compute_idle_loop())
        cli.assert_called_once_with(
            'stop', '-s', serve.COLAB_GPU_SESSION, timeout=120)

    def test_idle_teardown_error_is_swallowed(self):
        with unittest.mock.patch.object(serve.asyncio, 'sleep', new=_stop_loop), \
             unittest.mock.patch.object(serve, '_COLAB_COMPUTE_LAST_USED', 1.0), \
             unittest.mock.patch.object(
                 serve, '_colab_session_exists',
                 side_effect=RuntimeError('boom')), \
             unittest.mock.patch('builtins.print') as pr:
            with self.assertRaises(_StopLoop):
                asyncio.run(serve._colab_compute_idle_loop())
        self.assertTrue(any('idle teardown error' in str(a) for a in pr.call_args_list))


class ColabSpendThisMonth(unittest.TestCase):
    def test_reads_current_month_from_ledger(self):
        month = serve._colab_budget_month()
        ledger = {serve.COLAB_LEDGER_KEY: {'byMonth': {month: 4.5}}}
        with unittest.mock.patch.object(serve, '_spend_ledger_read', return_value=ledger):
            self.assertEqual(serve._colab_spend_this_month(), 4.5)

    def test_ledger_failure_falls_back_to_zero(self):
        with unittest.mock.patch.object(
                serve, '_spend_ledger_read', side_effect=RuntimeError('boom')):
            self.assertEqual(serve._colab_spend_this_month(), 0.0)


class ColabAccountUsage(unittest.TestCase):
    def test_returns_none_when_cli_missing(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            self.assertIsNone(serve._colab_account_usage())

    def test_serves_fresh_cache(self):
        data = {'balance': 3.0, 'rate': 1, 'assignments': 2}
        cache = {'at': time.time(), 'data': data}
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_COLAB_USAGE_CACHE', cache), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            self.assertEqual(serve._colab_account_usage(), data)
        cli.assert_not_called()

    def test_cli_error_returns_none(self):
        cache = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_COLAB_USAGE_CACHE', cache), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(1, 'err')):
            self.assertIsNone(serve._colab_account_usage())

    def test_unparseable_output_returns_none(self):
        cache = {'at': 0.0, 'data': None}
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_COLAB_USAGE_CACHE', cache), \
             unittest.mock.patch.object(
                 serve, '_colab_cli',
                 return_value=(0, 'Usage rate: 1.0\nActive assignments: 2\n')):
            self.assertIsNone(serve._colab_account_usage())


class ColabComputeProvision(unittest.TestCase):
    def test_refuses_when_cli_missing(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False):
            ok, msg = serve._colab_compute_provision()
        self.assertFalse(ok)
        self.assertIn('not installed', msg)

    def test_provision_failure_reports_tail(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_session_exists',
                                        return_value=False), \
             unittest.mock.patch.object(serve.time, 'sleep'), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(1, 'out of quota')) as cli:
            ok, msg = serve._colab_compute_provision('sess', kind='cpu')
        self.assertFalse(ok)
        self.assertTrue(msg.startswith('provision failed:'))
        self.assertEqual(cli.call_count, 1 + len(serve.COLAB_PROVISION_RETRY_DELAYS_S))

    def test_provision_retries_then_succeeds(self):
        # A transient first refusal recovers: the second attempt provisions.
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_session_exists',
                                        return_value=False), \
             unittest.mock.patch.object(serve.time, 'sleep') as sleep, \
             unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=[(1, 'capacity'), (0, '')]):
            ok, msg = serve._colab_compute_provision('sess')
        self.assertTrue(ok)
        sleep.assert_called_once_with(serve.COLAB_PROVISION_RETRY_DELAYS_S[0])

    def test_provision_gives_up_after_bounded_retries(self):
        # A genuinely unavailable slot retries the bounded number of times,
        # backs off between attempts, then gives up with the honest error.
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_session_exists',
                                        return_value=False), \
             unittest.mock.patch.object(serve.time, 'sleep') as sleep, \
             unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(1, 'no gpu quota')):
            ok, msg = serve._colab_compute_provision('sess')
        self.assertFalse(ok)
        self.assertIn('provision failed', msg)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(delays, list(serve.COLAB_PROVISION_RETRY_DELAYS_S))


class ColabDenied(unittest.TestCase):
    def test_empty_text_is_not_denied(self):
        self.assertIsNone(serve._colab_denied(''))
        self.assertIsNone(serve._colab_denied(None))


class ColabTargetHosts(unittest.TestCase):
    def test_skips_empty_texts(self):
        self.assertEqual(serve._colab_target_hosts(None, ''), set())
        self.assertEqual(serve._colab_target_hosts('', None), set())

    def test_extracts_hostnames(self):
        hosts = serve._colab_target_hosts(
            'fetch https://Example.com/path', 'also http://sub.example.org/x')
        self.assertEqual(hosts, {'example.com', 'sub.example.org'})

    def test_ignores_unparseable_urls(self):
        hosts = serve._colab_target_hosts('http://[::1/oops', None)
        self.assertEqual(hosts, set())


class ColabComputeRun(unittest.TestCase):
    def _base_patches(self, **overrides):
        defaults = {
            'COLAB_ENABLED': True,
            '_jev_is_degraded': unittest.mock.Mock(return_value=False),
            '_colab_denied': unittest.mock.Mock(return_value=None),
            '_colab_gate_urls': unittest.mock.Mock(return_value=None),
            '_colab_budget_exceeded': unittest.mock.Mock(return_value=False),
            '_colab_shard_band': unittest.mock.Mock(return_value=('shard', 0.9)),
        }
        defaults.update(overrides)
        return unittest.mock.patch.multiple(serve, **defaults)

    def test_bad_runtimes_and_timeout_coerce_to_defaults(self):
        with self._base_patches(), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'hello\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 'abc', runtimes='abc',
                env={'K': 'V'})
        self.assertEqual(result['stdout'], 'hello')
        self.assertIn('session', result)

    def test_sharding_disabled_while_jev_degraded(self):
        with self._base_patches(
                _jev_is_degraded=unittest.mock.Mock(return_value=True)), \
             unittest.mock.patch.object(serve, '_colab_compute_provision'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=2)
        self.assertIn('sharding is disabled', result['error'])

    def test_primary_provision_failure_errors(self):
        with self._base_patches(), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(False, 'no gpu quota')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent', 'print(1)', 'testing', [], 300)
        self.assertIn('could not provision', result['error'])

    def test_sharding_degrades_when_extra_runtime_not_granted(self):
        def provision(session=None, kind='gpu'):
            return (True, 'ok') if session == serve.COLAB_GPU_SESSION else (False, 'no quota')

        with self._base_patches(), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        side_effect=provision), \
             unittest.mock.patch.object(
                 serve, '_colab_cli',
                 return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=2)
        self.assertEqual(result['runtimes'], 1)
        self.assertEqual(result['shards'][0]['session'], serve.COLAB_GPU_SESSION)

    def test_package_install_failure_errors(self):
        with self._base_patches(), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli',
                                        return_value=(0, 'no marker here')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', ['numpy'], 300)
        self.assertIn('package install failed', result['error'])

    def test_sharded_run_combines_outputs_and_reaps_extras(self):
        def fake_cli(*args, **kwargs):
            if args and args[0] == 'stop':
                raise RuntimeError('stop failed')
            idx = None
            for a in args:
                if a.startswith('COLAB_SHARD_INDEX='):
                    idx = int(a.split('=')[1])
            if idx == 0:
                return 0, 'shard0 stdout\n__COLAB_DONE__\n'
            return 1, 'shard1 boom\n__COLAB_DONE__\n'

        with self._base_patches(), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', side_effect=fake_cli), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=2)
        self.assertEqual(result['runtimes'], 2)
        self.assertEqual(result['shards'][1]['rc'], 1)
        self.assertIn('RUN FAILED (exit 1)', result['stdout'])
        self.assertIn('shard0 stdout', result['stdout'])
        # The shard gate logged its band decision, then the run logged once.
        self.assertEqual(log.call_count, 2)
        log.assert_any_call('agent', 'colab_compute_run', unittest.mock.ANY, authorized=True)

    def test_shard_gate_caps_count_to_approved_band(self):
        # Jev approved only the 'double' band (2 runtimes) for a 5-runtime ask:
        # the gate caps the run to 2, and the band decision is logged.
        with self._base_patches(
                _colab_shard_band=unittest.mock.Mock(return_value=('double', 0.9))), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=5)
        self.assertEqual(result['runtimes'], 2)
        log.assert_any_call('agent', 'colab_shard_gate',
                            {'requested': 5, 'band': 'double',
                             'confidence': 0.9, 'approved': 2},
                            authorized=False)

    def test_shard_gate_band_single_runs_one_runtime(self):
        # Jev says the computation earns only a single runtime: the sharded
        # ask collapses to the single-runtime result shape (no shards/runtimes).
        with self._base_patches(
                _colab_shard_band=unittest.mock.Mock(return_value=('single', 0.95))), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=3)
        self.assertNotIn('shards', result)
        self.assertNotIn('runtimes', result)
        self.assertEqual(result['stdout'], 'ok')
        log.assert_any_call('agent', 'colab_shard_gate',
                            {'requested': 3, 'band': 'single',
                             'confidence': 0.95, 'approved': 1},
                            authorized=False)

    def test_shard_gate_fails_closed_on_low_confidence(self):
        # A shard band at weak confidence must not spend up: degrade to single,
        # the same fail-to-cheap move as the model-tier gate.
        with self._base_patches(
                _colab_shard_band=unittest.mock.Mock(return_value=('shard', 0.4))), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=4)
        self.assertNotIn('shards', result)
        log.assert_any_call('agent', 'colab_shard_gate',
                            {'requested': 4, 'band': 'shard',
                             'confidence': 0.4, 'approved': 1},
                            authorized=False)

    def test_shard_gate_fails_closed_on_classifier_outage(self):
        # An unreachable/non-binary classifier (None) keeps one runtime.
        with self._base_patches(
                _colab_shard_band=unittest.mock.Mock(return_value=(None, 0.0))), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=2)
        self.assertNotIn('shards', result)

    def test_shard_gate_skipped_for_single_runtime(self):
        # runtimes==1 is the cheap default -- no band decision is ever made.
        gate = unittest.mock.Mock(return_value=('shard', 0.9))
        with self._base_patches(_colab_shard_band=gate), \
             unittest.mock.patch.object(serve, '_colab_compute_provision',
                                        return_value=(True, 'ok')), \
             unittest.mock.patch.object(
                 serve, '_colab_cli', return_value=(0, 'ok\n__COLAB_DONE__\n')), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run(
                'agent', 'print(1)', 'testing', [], 300, runtimes=1)
        self.assertNotIn('shards', result)
        gate.assert_not_called()


class ColabShardBand(unittest.TestCase):
    def test_returns_band_and_confidence(self):
        with unittest.mock.patch.object(
                serve, '_jev_quorum_choice_sync',
                return_value=('double', 0.8, 0.01)):
            self.assertEqual(serve._colab_shard_band('x = 1', 'split it', 4),
                             ('double', 0.8))

    def test_fails_closed_on_classifier_exception(self):
        with unittest.mock.patch.object(
                serve, '_jev_quorum_choice_sync',
                side_effect=RuntimeError('boom')):
            self.assertEqual(serve._colab_shard_band('x = 1', 'split it', 4),
                             (None, 0.0))


class EscalationJudgeDrift(unittest.TestCase):
    def test_bump_parses_and_increments(self):
        with unittest.mock.patch.object(
                serve, '_get_setting', return_value='{"review": 3}'), \
             unittest.mock.patch.object(serve, '_set_setting') as setter:
            serve._bump_escalation_judge_drift('review')
        setter.assert_called_once_with('escalation_judge_drift', '{"review": 4}')

    def test_bump_recovers_from_bad_json(self):
        with unittest.mock.patch.object(serve, '_get_setting', return_value='{bad'), \
             unittest.mock.patch.object(serve, '_set_setting') as setter:
            serve._bump_escalation_judge_drift('review')
        setter.assert_called_once_with('escalation_judge_drift', '{"review": 1}')

    def test_bump_survives_setting_write_failure(self):
        with unittest.mock.patch.object(serve, '_get_setting', return_value='{}'), \
             unittest.mock.patch.object(
                 serve, '_set_setting', side_effect=RuntimeError('boom')):
            serve._bump_escalation_judge_drift('review')  # must not raise

    def test_reset_parses_and_removes_kind(self):
        with unittest.mock.patch.object(
                serve, '_get_setting', return_value='{"review": 2, "other": 1}'), \
             unittest.mock.patch.object(serve, '_set_setting') as setter:
            serve._reset_escalation_judge_drift('review')
        setter.assert_called_once_with('escalation_judge_drift', '{"other": 1}')

    def test_reset_recovers_from_bad_json(self):
        with unittest.mock.patch.object(serve, '_get_setting', return_value='{bad'), \
             unittest.mock.patch.object(serve, '_set_setting') as setter:
            serve._reset_escalation_judge_drift('review')
        setter.assert_not_called()

    def test_reset_survives_setting_write_failure(self):
        with unittest.mock.patch.object(
                serve, '_get_setting', return_value='{"review": 1}'), \
             unittest.mock.patch.object(
                 serve, '_set_setting', side_effect=RuntimeError('boom')):
            serve._reset_escalation_judge_drift('review')  # must not raise

    def test_summary_returns_only_int_string_pairs(self):
        with unittest.mock.patch.object(
                serve, '_get_setting',
                return_value='{"review": 2, "bogus": "x", "float": 2.5}'):
            self.assertEqual(serve._escalation_judge_drift_summary(), {'review': 2})

    def test_summary_returns_empty_on_bad_json(self):
        with unittest.mock.patch.object(serve, '_get_setting', return_value='{bad'):
            self.assertEqual(serve._escalation_judge_drift_summary(), {})


class VerifyModelWorks(unittest.TestCase):
    def test_returns_false_when_probe_keeps_failing(self):
        with unittest.mock.patch.object(
                serve, '_call_openrouter_sync',
                side_effect=RuntimeError('model down')), \
             unittest.mock.patch.object(serve.time, 'sleep') as sleep:
            self.assertFalse(serve._verify_model_works_sync('broken/model'))
        self.assertEqual(sleep.call_count, serve.MODEL_VERIFY_ATTEMPTS - 1)


class VerifyDecisionModelWorks(unittest.TestCase):
    def test_loopback_url_is_reachable_by_construction(self):
        self.assertTrue(
            serve._verify_decision_model_works_sync('http://127.0.0.1:8939/v1/systemone'))

    def test_returns_false_when_probe_keeps_failing(self):
        with unittest.mock.patch.object(
                serve, '_urlopen_with_resilience',
                side_effect=RuntimeError('down')), \
             unittest.mock.patch.object(serve.time, 'sleep') as sleep:
            self.assertFalse(serve._verify_decision_model_works_sync('jev/model'))
        self.assertEqual(sleep.call_count, serve.MODEL_VERIFY_ATTEMPTS - 1)


class SyncModelCatalog(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_catalog')
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_tiers')

    def _model(self, id_, prompt='0.00000005', completion='0.00000005', arch=None):
        return {
            'id': id_, 'name': id_,
            'pricing': {'prompt': prompt, 'completion': completion},
            'architecture': arch or {
                'input_modalities': ['text'], 'output_modalities': ['text']},
        }

    def test_inserts_syncs_scores_and_purges_stale(self):
        serve.set_model_benchmark_score('vendor/cheap', 'MMLU', 0.8, 'url')
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, scores, first_seen, last_seen) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('stale/x', 'Stale', 1, 1, 2, 0, None, 1, time.time() - 25 * 3600),
            )
            conn.execute(
                'INSERT INTO model_benchmark_scores (model_id, benchmark, score, source_url, checked_at) '
                'VALUES (?, ?, ?, ?, ?)',
                ('stale/x', 'MMLU', 0.9, None, time.time() - 25 * 3600),
            )
        models = [self._model('vendor/cheap')]
        purged = serve._sync_model_catalog(models)
        # One unique model is genuinely gone (it appears in BOTH tables, so
        # the count is the unique-model set, not a per-table total). The
        # present model must survive untouched -- the regression the swapped
        # purge params once broke (they matched every row, wiping the whole
        # catalog and reporting a bogus count).
        self.assertEqual(purged, 1)
        with serve._db() as conn:
            gone_cat = conn.execute(
                "SELECT 1 FROM model_catalog WHERE model_id = 'stale/x'").fetchone()
            gone_bench = conn.execute(
                "SELECT 1 FROM model_benchmark_scores WHERE model_id = 'stale/x'").fetchone()
            kept_cat = conn.execute(
                "SELECT 1 FROM model_catalog WHERE model_id = 'vendor/cheap'").fetchone()
            kept_bench = conn.execute(
                "SELECT 1 FROM model_benchmark_scores WHERE model_id = 'vendor/cheap'").fetchone()
        self.assertIsNone(gone_cat)
        self.assertIsNone(gone_bench)
        self.assertIsNotNone(kept_cat)
        self.assertIsNotNone(kept_bench)

    def test_skips_malformed_pricing_entries(self):
        models = [self._model('vendor/bad', prompt='not-a-number')]
        purged = serve._sync_model_catalog(models)
        self.assertEqual(purged, 0)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM model_catalog WHERE model_id = 'vendor/bad'").fetchone()
        self.assertIsNone(row)

    def test_empty_catalog_purges_stale_rows(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_catalog (model_id, name, prompt_price, completion_price, price_per_m, image_capable, scores, first_seen, last_seen) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('stale2/x', 'Stale', 1, 1, 2, 0, None, 1, time.time() - 25 * 3600),
            )
        purged = serve._sync_model_catalog([])
        self.assertEqual(purged, 1)
        with serve._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM model_catalog WHERE model_id = 'stale2/x'").fetchone()
        self.assertIsNone(row)


class FetchOpenRouterCatalog(unittest.TestCase):
    def test_fetches_catalog_json(self):
        resp = unittest.mock.MagicMock()
        resp.read.return_value = b'{"data": [{"id": "x"}]}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with unittest.mock.patch.object(
                serve.urllib.request, 'urlopen', return_value=resp):
            self.assertEqual(serve._fetch_openrouter_catalog_sync(), [{'id': 'x'}])


class BucketModelsByPriceExtra(unittest.TestCase):
    def _model(self, id_, prompt, completion):
        return {
            'id': id_, 'name': id_,
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
            'pricing': {'prompt': prompt, 'completion': completion},
        }

    def test_skips_model_with_missing_pricing(self):
        m = {'id': 'vendor/noprice', 'architecture': {
            'input_modalities': ['text'], 'output_modalities': ['text']}}
        buckets = serve._bucket_models_by_price([m])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))

    def test_skips_model_with_non_numeric_pricing(self):
        buckets = serve._bucket_models_by_price(
            [self._model('vendor/badprice', 'x', 'y')])
        self.assertTrue(all(len(v) == 0 for v in buckets.values()))


class BestValuePick(unittest.TestCase):
    def test_picks_cheapest_qualifying_working_model(self):
        candidates = [
            {'id': 'm1', 'price': 1.0}, {'id': 'm2', 'price': 2.0},
        ]
        scores = {'m1': 0.7, 'm2': 0.8}
        with unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True):
            picked = asyncio.run(serve._best_value_pick(candidates, scores))
        self.assertEqual(picked['id'], 'm1')

    def test_returns_none_when_nothing_scored(self):
        candidates = [{'id': 'm1', 'price': 1.0}]
        picked = asyncio.run(serve._best_value_pick(candidates, {}))
        self.assertIsNone(picked)

    def test_returns_none_when_nothing_verifies(self):
        candidates = [{'id': 'm1', 'price': 1.0}]
        scores = {'m1': 0.7}
        with unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=False):
            picked = asyncio.run(serve._best_value_pick(candidates, scores))
        self.assertIsNone(picked)


class RefreshModelTiers(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_catalog')
            conn.execute('DELETE FROM model_benchmark_scores')
            conn.execute('DELETE FROM model_tiers')

    def _model(self, id_, prompt, completion, input_mods=('text',), output_mods=('text',)):
        return {
            'id': id_, 'name': id_,
            'pricing': {'prompt': prompt, 'completion': completion},
            'architecture': {
                'input_modalities': list(input_mods),
                'output_modalities': list(output_mods),
            },
        }

    def test_picks_every_band_and_vision_from_benchmarks(self):
        models = [
            self._model('low/m1', '0.0000001', '0.0000001'),
            self._model('mid/m1', '0.000002', '0.000002'),
            self._model('mid/v1', '0.0000005', '0.0000005',
                        input_mods=('image', 'text')),
            self._model('mid/c1', '0.0000015', '0.0000015'),
            self._model('high/h1', '0.00001', '0.00001'),
            self._model('vendor/batch:batch', '0.000001', '0.000001'),
            dict(self._model('vendor/reason', '0.000001', '0.000001',
                             input_mods=('image', 'text')),
                 reasoning={'default_enabled': True}),
            self._model('vendor/badprice', 'x', 'x',
                        input_mods=('image', 'text')),
            self._model('vendor/free', '0', '0',
                        input_mods=('image', 'text')),
        ]
        serve.set_model_benchmark_score('low/m1', 'intelligence_index', 0.7, 'u')
        serve.set_model_benchmark_score('mid/m1', 'intelligence_index', 0.75, 'u')
        serve.set_model_benchmark_score('mid/c1', 'coding_index', 0.65, 'u')
        serve.set_model_benchmark_score('high/h1', 'agentic_index', 0.55, 'u')
        serve.set_model_benchmark_score('mid/v1', 'MMMU', 0.6, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/m1')
        self.assertEqual(chosen['mid']['id'], 'mid/m1')
        self.assertEqual(chosen['coding']['id'], 'mid/c1')
        self.assertEqual(chosen['high']['id'], 'high/h1')
        self.assertEqual(chosen['vision']['id'], 'mid/v1')
        log.assert_called_once()
        with serve._db() as conn:
            row = conn.execute(
                "SELECT slug FROM model_tiers WHERE band = 'low'").fetchone()
        self.assertEqual(row[0], 'low/m1')

    def test_chain_prefers_primary_metric_pool(self):
        # BAND_BENCHMARK is an ordered metric CHAIN. As long as ANY candidate in
        # the pool is scored on the primary metric, the fallback metric does NOT
        # let a fallback-only model compete on a different scale -- a modest
        # intelligence_index model wins over a fallback-only MMLU-Pro star.
        models = [
            self._model('low/primary', '0.0000001', '0.0000001'),
            self._model('low/fallback', '0.0000001', '0.0000001'),
        ]
        serve.set_model_benchmark_score('low/primary', 'intelligence_index', 0.7, 'u')
        serve.set_model_benchmark_score('low/fallback', 'MMLU-Pro', 0.95, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/primary')

    def test_chain_uses_fallback_when_primary_pool_empty(self):
        # When NO candidate in the pool has the primary metric, the band falls
        # through to the fallback (HF Open LLM MMLU-Pro) instead of giving up
        # and trusting Jev's classifier -- that is the point of the fallback.
        models = [
            self._model('low/a', '0.0000001', '0.0000001'),
            self._model('low/b', '0.00000005', '0.00000005'),
        ]
        serve.set_model_benchmark_score('low/a', 'MMLU-Pro', 0.7, 'u')
        serve.set_model_benchmark_score('low/b', 'MMLU-Pro', 0.95, 'u')
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_sync_model_catalog',
                                        return_value=0), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['low']['id'], 'low/b')

    def test_empty_band_skips_and_classifier_fallback_picks(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        decision = {'answers': {'q': {'choice': 'mid/only'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['mid']['id'], 'mid/only')
        self.assertNotIn('vision', chosen)

    def test_no_working_candidate_is_logged_and_skipped(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        decision = {'answers': {'q': {'choice': 'mid/only'}}, 'usage': {'cost': 0.01}}
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=False), \
             unittest.mock.patch.object(serve, '_call_openrouter_decision_sync',
                                        return_value=decision), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'), \
             unittest.mock.patch('builtins.print') as pr:
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen, {})
        self.assertTrue(any('no working candidate' in str(a) for a in pr.call_args_list))

    def test_classifier_error_falls_back_to_verify(self):
        models = [self._model('mid/only', '0.000002', '0.000002')]
        with unittest.mock.patch.object(serve, '_fetch_openrouter_catalog_sync',
                                        return_value=models), \
             unittest.mock.patch.object(serve, '_verify_model_works_sync',
                                        return_value=True), \
             unittest.mock.patch.object(
                 serve, '_call_openrouter_decision_sync',
                 side_effect=RuntimeError('jev down')), \
             unittest.mock.patch.object(serve, '_jev_model', return_value='test-jev'), \
             unittest.mock.patch.object(serve, 'log_action'):
            chosen = asyncio.run(serve.refresh_model_tiers())
        self.assertEqual(chosen['mid']['id'], 'mid/only')


class GetCachedModelTiers(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM model_tiers')

    def test_reads_rows_from_db(self):
        with serve._db() as conn:
            conn.execute(
                'INSERT INTO model_tiers (band, slug, name, price_per_m, chosen_at) '
                'VALUES (?, ?, ?, ?, ?)',
                ('low', 'low/m1', 'Low1', 0.2, time.time()),
            )
        tiers = serve.get_cached_model_tiers()
        self.assertEqual(tiers['low'], {'slug': 'low/m1', 'name': 'Low1', 'price': 0.2})


class ShouldWake(unittest.TestCase):
    def _req(self, method, path):
        return types.SimpleNamespace(method=method, url=types.SimpleNamespace(path=path))

    def test_non_get_wakes(self):
        self.assertTrue(serve._should_wake(self._req('POST', '/api/anything')))

    def test_non_waking_read_does_not_wake(self):
        self.assertFalse(serve._should_wake(self._req('GET', '/api/state')))

    def test_escalation_get_except_resolve_does_not_wake(self):
        self.assertFalse(serve._should_wake(self._req('GET', '/api/escalation/abc')))

    def test_resolve_wakes(self):
        self.assertTrue(serve._should_wake(self._req('GET', '/api/escalation/resolve')))

    def test_unlisted_get_wakes(self):
        self.assertTrue(serve._should_wake(self._req('GET', '/api/other')))


class NoStoreMiddleware(unittest.TestCase):
    def test_wakes_dormant_server_and_marks_no_store(self):
        with unittest.mock.patch.object(serve, '_dormant', return_value=True), \
             unittest.mock.patch.object(serve, '_should_wake', return_value=True), \
             unittest.mock.patch.object(serve, '_set_dormant') as setd:
            c = TestClient(serve.app)
            r = c.get('/index.html')
        self.assertEqual(r.status_code, 200)
        setd.assert_called_once_with(False)
        self.assertEqual(r.headers.get('cache-control'), 'no-store')


class SaveCollision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-gap-save-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_saves_valid_grid(self):
        payload = {'grid': [['wall', 'room']], 'cols': 2, 'rows': 1}
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save', json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        with open(os.path.join(self.tmp, 'collision_grid.json')) as f:
            self.assertEqual(json.load(f), payload)
        log.assert_called_once_with(None, 'save_collision_grid')

    def test_rejects_invalid_grid(self):
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save', json={'cols': 2})
        self.assertEqual(r.status_code, 400)


class SaveDoors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='think-tank-gap-doors-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_saves_valid_doors(self):
        payload = {'pressoffice': {'x': 1, 'y': 2, 'w': 3, 'h': 4}}
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'log_action') as log, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save-doors', json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        with open(os.path.join(self.tmp, 'door_triggers.json')) as f:
            self.assertEqual(json.load(f), payload)
        log.assert_called_once_with(None, 'save_door_triggers')

    def test_rejects_invalid_doors(self):
        with unittest.mock.patch.object(serve, 'ROOT', self.tmp), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/save-doors', json={'pressoffice': {'x': 1}})
        self.assertEqual(r.status_code, 400)


class GetState(unittest.TestCase):
    def test_empty_db_seeds_default_roster_then_returns_null(self):
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=None), \
             unittest.mock.patch.object(serve, '_seed_default_roster') as seed, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json())
        seed.assert_called_once()

    def test_heals_identity_and_adds_agent_keys(self):
        state = {'agentRoster': [{'id': 'ada'}], 'agents': {'ada': {}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'get_or_create_agent_key',
                                        return_value='key-1'), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['agentKeys'], {'ada': 'key-1'})
        save.assert_called_once()

    def test_agent_key_caller_gets_no_agent_keys(self):
        # Identity fix: an agent-key-authenticated caller (a content-executor
        # loopback) must not be handed every other agent's key -- those keys
        # gate key-authenticated endpoints.
        state = {'agentRoster': [{'id': 'ada'}], 'agents': {'ada': {}}}
        with unittest.mock.patch.object(serve, 'get_state_from_db',
                                        return_value=state), \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                        return_value=True):
            c = TestClient(serve.app)
            r = c.get('/api/state')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('agentKeys', r.json())


class PostState(unittest.TestCase):
    def test_strips_agent_keys_and_merges_server_owned(self):
        with unittest.mock.patch.object(
                serve, 'get_state_from_db',
                return_value={'templates': {'T': {'mission': 'm'}},
                              'sim': {'owner': 'client'}}), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/state', json={
                'agents': {'ada': {'busy': True}},
                'agentKeys': {'ada': 'secret'},
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, 'saved')
        saved = save.call_args[0][0]
        self.assertNotIn('agentKeys', saved)
        self.assertEqual(saved['templates']['T']['mission'], 'm')
        self.assertEqual(saved['agents']['ada']['busy'], True)

    def test_agent_key_alone_cannot_overwrite_state(self):
        # State mutation is the player's domain: an agent key must not be able
        # to overwrite village state and bypass every escalation/oversight gate.
        with unittest.mock.patch.object(
                serve, 'get_state_from_db', return_value={}), \
             unittest.mock.patch.object(serve, 'save_state_to_db') as save, \
             unittest.mock.patch.object(serve, 'verify_session', return_value=False), \
             unittest.mock.patch.object(serve, '_valid_agent_key_presented',
                                        return_value=True):
            c = TestClient(serve.app)
            r = c.post('/api/state', json={'agents': {'ada': {'busy': True}}})
        self.assertEqual(r.status_code, 401)
        save.assert_not_called()


class MergeServerOwnedExtra(unittest.TestCase):
    def test_server_owned_room_teams_sprints_products_wiki_win(self):
        existing = {
            'sim': {'owner': 'server'},
            'agents': {'ada': {'x': 1}},
            'roomDefinitions': {'r': 'rd'},
            'teams': ['team'],
            'sprints': ['sprint'],
            'products': ['product'],
            'wiki': {'page': 1},
        }
        incoming = {'agents': {'ada': {'x': 99, 'busy': True}}}
        merged = serve._merge_server_owned(existing, incoming)
        self.assertEqual(merged['roomDefinitions'], {'r': 'rd'})
        self.assertEqual(merged['teams'], ['team'])
        self.assertEqual(merged['sprints'], ['sprint'])
        self.assertEqual(merged['products'], ['product'])
        self.assertEqual(merged['wiki'], {'page': 1})
        self.assertEqual(merged['agents']['ada']['x'], 1)
        self.assertEqual(merged['agents']['ada']['busy'], True)


class MergeServerOwnedAgent(unittest.TestCase):
    def test_returns_server_agent_when_client_missing(self):
        self.assertEqual(
            serve._merge_server_owned_agent({'x': 1}, None), {'x': 1})

    def test_returns_client_agent_when_server_missing(self):
        self.assertEqual(
            serve._merge_server_owned_agent(None, {'x': 1}), {'x': 1})

    def test_returns_client_agent_on_non_dict_types(self):
        self.assertEqual(serve._merge_server_owned_agent({'x': 1}, 'not-a-dict'),
                         'not-a-dict')
        self.assertEqual(serve._merge_server_owned_agent('not-a-dict', {'x': 1}),
                         {'x': 1})

    def test_server_spatial_fields_win_over_client(self):
        merged = serve._merge_server_owned_agent(
            {'x': 5, 'y': 6, 'busy': False}, {'x': 1, 'y': 2, 'busy': True})
        self.assertEqual(merged['x'], 5)
        self.assertEqual(merged['y'], 6)
        self.assertEqual(merged['busy'], True)


if __name__ == '__main__':
    unittest.main()