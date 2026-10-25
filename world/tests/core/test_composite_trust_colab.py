"""External-eval ports: composite multi-signal trust gate + HALF_OPEN
circuit-breaker probe.

From the codebase eval (memory: project_external_eval_sep2026): the think tank's
approval/escalation gates were single-signal and its circuit breaker had no
recovery probe. Two sharp, contained ideas were pulled in:

  1. serve._jes_directory_score / _escalation_floor -- the DIRECTOR auto-approval
     path now blends Jev confidence with a per-kind error-history penalty and a
     per-kind risk FLOOR, so a safety-critical escalation kind (a human's own
     "blocked" verdict on a command/pipeline step) is never auto-approved, and a
     kind that Jev keeps failing on needs stronger confidence to win the human's
     delegation. Fail-closed invariant preserved: a classifier failure leaves the
     escalation pending and raises the bar next time.

  2. serve.is_model_circuit_broken / record_model_result -- the model circuit
     breaker now has a HALF_OPEN recovery probe: once the cooldown elapses, a
     single probe is allowed through and only a SUCCESS closes the circuit; a
     probe failure re-opens it with a fresh cooldown.

Hermetic: pure functions + a mocked _call_openrouter_decision_sync; no live Jev,
no network. The "no state mutation beyond the module counters" claim was
wrong: is_model_circuit_broken/record_model_result log model_circuit_broken/
model_circuit_recovered via a real `log_action` call, which -- unless DB_PATH
is redirected below -- writes into whatever real think_tank.db sits at serve.py's
default path. Found: this file alone added 58 real rows (mostly
`model_circuit_broken` for a fake model "m") to a live production think_tank.db
during a routine test run.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)
import sim  # noqa: E402
import content  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-composite-trust-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Composite director trust gate
# ---------------------------------------------------------------------------

def decision_payload(choice, confidence):
    """A fake /api/alpha/decisions response shaped like what _jev_choice expects."""
    return {'answers': {'q': {'choice': choice, 'confidence': confidence}}, 'usage': {'cost': 0.0}}


def _stub_grade(g):
    def _f(*_a, **_k):
        return g
    return _f


class ColabStandbyTests(unittest.TestCase):
    """The CLI-managed Colab/Laya standby: a dedicated CPU Colab
    session ('think-tank-standby') owned via the colab CLI, laya-serve booted over
    `colab exec`, reached through a LOCALHOST-only ssh forward at
    127.0.0.1:8939, driven by the 15-min _colab_failover_loop. No sentinel
    notebook, no /api/colab/register, no pairing. Hermetic: _colab_cli /
    _colab_session_exists / subprocess.Popen are mocked; the only real call is
    the loopback /health check under fake urlopen."""

    def setUp(self):
        serve._model_circuit_state.clear()
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')

    def _seed_jev(self, failures, successes):
        for _ in range(failures):
            serve._append_decision_tape('probe', 'typesafe/jev-1.13', '', '', None, None, None, {}, False)
        for _ in range(successes):
            serve._append_decision_tape('probe', 'typesafe/jev-1.13', '', '', None, None, None, {}, True)

    def test_provider_chain_entry_appends_the_standby_url_last(self):
        serve._set_setting('jev_model', 'primary-x')
        self.assertTrue(serve.COLAB_STANDBY_URL.startswith('http://127.0.0.1:'),
                        'the standby is loopback-only by construction')
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertEqual(serve._decision_model_chain(),
                             ['primary-x', serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH],
                             'the standby trails the chain as the LAST fallback')
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', False):
            self.assertEqual(serve._decision_model_chain(), ['primary-x'],
                             'standby disabled -> no provider in the chain')
        self.assertEqual(serve._jev_model(), 'primary-x',
                         'the primary slug still leads; _jev_model is unchanged')

    def test_chain_dedupes_a_manually_configured_provider(self):
        provider = serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH
        serve._set_setting('jev_model', 'primary-x,' + provider)
        with unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertEqual(serve._decision_model_chain(), ['primary-x', provider],
                             'an already-configured standby URL is not appended twice')

    def test_provider_request_is_direct_not_openrouter(self):
        provider = serve.COLAB_STANDBY_URL + serve.COLAB_STANDBY_DECISION_PATH
        req = serve._decision_request(provider, {'messages': []}, {'choice': {}})
        self.assertEqual(req.full_url, provider)
        self.assertIsNone(req.get_header('Authorization'),
                          'the loopback standby needs no bearer key')
        body = json.loads(req.data.decode())
        self.assertNotIn('model', body, 'laya routes internally; no slug is sent')
        self.assertIn('state', body)
        self.assertIn('questions', body)

    def test_jev_is_degraded_reuses_the_health_predicate(self):
        self._seed_jev(failures=6, successes=6)  # 12 attempts at exactly 50%
        self.assertTrue(serve._jev_is_degraded())
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
        self._seed_jev(failures=6, successes=12)  # 33% -> healthy
        self.assertFalse(serve._jev_is_degraded())
        with serve._db() as conn:
            conn.execute('DELETE FROM decision_tape')
        self._seed_jev(failures=4, successes=0)  # all failed but sparse
        self.assertFalse(serve._jev_is_degraded(),
                         'a handful of attempts never trips the standby')

    def test_ensure_session_provisions_when_missing(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append(a) or (0, '')):
            ok, msg = serve._colab_standby_ensure_session()
        self.assertTrue(ok, msg)
        self.assertEqual(calls, [('new', '-s', serve.COLAB_STANDBY_SESSION)],
                         'a missing standby session is created via the CLI')

    def test_ensure_session_reuses_an_existing_one(self):
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=True), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            ok, _ = serve._colab_standby_ensure_session()
        self.assertTrue(ok)
        cli.assert_not_called(), 'an existing session is not recreated'

    def test_boot_code_installs_and_starts_laya(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            serve._colab_standby_ensure_service()
        args, kwargs = calls[0]
        self.assertEqual(args, ('exec', '-s', serve.COLAB_STANDBY_SESSION, '--timeout', '600'))
        payload = kwargs['input']
        self.assertIn('laya[serve]', payload)
        self.assertIn('laya-serve', payload)
        self.assertIn('__STANDBY_UP__', payload)

    def test_teardown_kills_the_laya_process_only(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_cli',
                                        side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            serve._colab_standby_teardown_service()
        payload = calls[0][1]['input']
        self.assertIn('pkill', payload)
        self.assertIn('laya-serve', payload)

    def test_forward_spawned_once_and_reused_for_the_same_session(self):
        fake = unittest.mock.Mock(pid=111)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', return_value=None):
            serve._COLAB_STANDBY_FORWARD.clear()
            first = serve._colab_standby_ensure_forward('think-tank-standby')
            second = serve._colab_standby_ensure_forward('think-tank-standby')
        self.assertIs(first, second)
        self.assertEqual(popen.call_count, 1, 'a live forward is reused, not respawned')
        cmd = popen.call_args[0][0]
        self.assertIn('ssh', cmd)
        self.assertTrue(any(c == '-L' and n == f'127.0.0.1:{serve.COLAB_STANDBY_PORT}:localhost:8000'
                            for c, n in zip(cmd, cmd[1:])), 'forward targets the loopback port')
        self.assertTrue(any('ProxyCommand' in c and '--proxy-mode' in c for c in cmd),
                        'the forward rides the colab CLI proxy-mode bridge')
        self.assertIn('-N', cmd)
        self.assertTrue(any('User=root' in c for c in cmd))
        serve._colab_standby_stop_forward()

    def test_forward_respawns_when_the_old_one_died(self):
        fake = unittest.mock.Mock(pid=999)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', return_value=None):      # alive: reuse
            serve._COLAB_STANDBY_FORWARD.clear()
            serve._colab_standby_ensure_forward('think-tank-standby')
            serve._colab_standby_ensure_forward('think-tank-standby')
            self.assertEqual(popen.call_count, 1)
        with unittest.mock.patch.object(serve.subprocess, 'Popen', return_value=fake) as popen, \
             unittest.mock.patch('os.kill', side_effect=ProcessLookupError('gone')):  # dead: respawn
            serve._colab_standby_ensure_forward('think-tank-standby')
            self.assertEqual(popen.call_count, 1, 'a dead forward is recognised and respawned')
        serve._colab_standby_stop_forward()

    def test_standby_reachable_checks_the_loopback_health(self):
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen') as u:
            u.return_value.__enter__.return_value.status = 200
            self.assertTrue(serve._colab_standby_reachable())
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                        side_effect=OSError('nothing on the port')):
            self.assertFalse(serve._colab_standby_reachable())
        with unittest.mock.patch.object(serve.urllib.request, 'urlopen') as u:
            u.return_value.__enter__.return_value.status = 503
            self.assertFalse(serve._colab_standby_reachable(),
                             'a non-200 /health is not a standing Laya')

    def test_standby_enabled_gates_on_cli_switch_and_opt_in(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertTrue(serve._colab_standby_enabled())
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', False), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertFalse(serve._colab_standby_enabled(),
                             'COLAB_ENABLED=0 switches the whole standby off')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', False), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', True):
            self.assertFalse(serve._colab_standby_enabled(),
                             'no CLI -> no standby, no chain entry')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_ENABLED', True), \
             unittest.mock.patch.object(serve, 'COLAB_STANDBY_ENABLED', False):
            self.assertFalse(serve._colab_standby_enabled(),
                             'COLAB_STANDBY_ENABLED is a deliberate opt-in')


class ColabComputeTests(unittest.TestCase):
    """run_on_colab agent compute: the dedicated 'think-tank-gpu'
    T4 session, provisioned/ran/stood-down through the colab CLI, metered as
    monthly compute units in the Bank. Hermetic: _colab_cli/_provision are
    mocked, so no CLI, no session, no network ever actually runs."""

    def setUp(self):
        serve._model_circuit_state.clear()

    def test_operator_switch_off_refuses_runs(self):
        with unittest.mock.patch.object(serve, 'COLAB_ENABLED', False), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('disabled by the operator', result['error'])
        cli.assert_not_called()

    def test_drive_mount_code_is_refused(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run(
                'agent-0', 'from google.colab import drive\ndrive.mount("/content/drive")\n',
                'numeric job', [], 60)
        self.assertIn('refusing to run', result['error'])
        self.assertIn('off-limits', result['error'])
        cli.assert_not_called()

    def test_gcloud_and_mining_code_are_refused(self):
        for bad in ('gsutil cp local gs://bucket/x', 'nicehash miner loop', 'gdown 1abc'):
            with unittest.mock.patch.object(serve, '_colab_cli') as cli:
                result = serve._colab_compute_run('agent-0', bad, 'job', [], 60)
            self.assertIn('refusing to run', result['error'], bad)
            cli.assert_not_called()

    def test_exfil_package_is_refused(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run(
                'agent-0', 'print(1)', 'job', ['gdown', 'requests'], 60)
        self.assertIn('package', result['error'])
        self.assertIn('refusing to run', result['error'])
        cli.assert_not_called()

    def test_plain_numeric_code_passes_the_guard(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, '42\n__COLAB_DONE__\n')):
            result = serve._colab_compute_run('agent-0', 'a = 41\nprint(a + 1)', 'sum', [], 60)
        self.assertEqual(result['stdout'], '42')

    def test_budget_gate_refuses_when_gated_out(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 20.0), \
             unittest.mock.patch.object(serve, '_colab_spend_this_month', return_value=25.0), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('COLAB_MONTHLY_UNITS', result['error'])
        cli.assert_not_called(), 'gated out means no provisioning attempt at all'

    def test_exhausted_paid_account_balance_refuses_run(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 1}), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertIn('no prepaid compute units left', result['error'])
        cli.assert_not_called()

    def test_free_tier_zero_balance_proceeds_to_provision(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 2}), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')) as prov, \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, '12\n__COLAB_DONE__\n')):
            result = serve._colab_compute_run('agent-0', 'print(1)', 'probe', [], 60)
        self.assertEqual(result['stdout'], '12')
        prov.assert_called_once()

    def test_empty_and_oversized_code_rejected_without_cli(self):
        with unittest.mock.patch.object(serve, '_colab_cli') as cli:
            self.assertIn('code is required', serve._colab_compute_run('agent-0', '', 'probe', [], 60)['error'])
            big = 'x' * (serve.COLAB_CODE_MAX_CHARS + 1)
            self.assertIn('under', serve._colab_compute_run('agent-0', big, 'probe', [], 60)['error'])
        cli.assert_not_called()

    def test_provisions_gpu_session_with_t4_on_demand(self):
        calls = []
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_cli', side_effect=lambda *a, **k: calls.append((a, k)) or (0, '')):
            ok, msg = serve._colab_compute_provision()
        self.assertTrue(ok)
        new_call = next(a for a, _k in calls if a[0] == 'new')
        self.assertEqual(new_call[1:], ('-s', 'think-tank-gpu', '--gpu', 'T4'),
                         'the dedicated session is a T4 created on demand')

    def test_reuses_existing_session(self):
        with unittest.mock.patch.object(serve, '_colab_session_exists', return_value=True), \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            ok, msg = serve._colab_compute_provision()
        self.assertTrue(ok)
        cli.assert_not_called(), 'an existing session is reused without any CLI call'

    def test_successful_run_returns_stdout_and_accrues_units(self):
        accrued = {}
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, 'answer=42\n__COLAB_DONE__')) as cli, \
             unittest.mock.patch.object(serve, '_accrue_colab_units',
                                        side_effect=lambda u: accrued.setdefault('units', u)), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', 'print("answer=42")', 'probe', [], 60)
        self.assertEqual(result['stdout'], 'answer=42')
        self.assertGreaterEqual(result['units'], serve.COLAB_MIN_UNITS_PER_RUN)
        self.assertEqual(accrued['units'], result['units'])
        self.assertEqual(result['session'], 'think-tank-gpu')
        exec_call = cli.call_args.args
        self.assertIn('exec', exec_call)
        self.assertIn('__COLAB_DONE__', cli.call_args.kwargs.get('input', ''))

    def test_host_side_timeout_exceeds_cli_timeout_on_exec(self):
        # The Colab CLI carries its own --timeout for the remote exec, but the
        # host-side _colab_cli subprocess defaults to a 120s timeout -- a 120s
        # default would kill a long transcription (CLI --timeout up to 630s)
        # before the CLI finished or reported. The host timeout must always
        # exceed the CLI's own --timeout, so the CLI (not the host) decides
        # when a remote exec is done.
        calls = []
        def fake_cli(*a, **k):
            calls.append((a, k))
            return (0, 'ok\n__COLAB_DONE__\n')
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', side_effect=fake_cli), \
             unittest.mock.patch.object(serve, '_accrue_colab_units'), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', 'print(1)', 'transcribe', [], 600)
        self.assertEqual(result['stdout'], 'ok')
        exec_calls = [c for c in calls if c[0][0] == 'exec']
        self.assertTrue(exec_calls, 'a successful run must exec the code')
        for args, kwargs in exec_calls:
            self.assertIn('--timeout', args)
            cli_timeout = int(args[args.index('--timeout') + 1])
            self.assertGreater(kwargs.get('timeout', 0), cli_timeout,
                               'host subprocess timeout must exceed the CLI --timeout or the host kills long execs')

    def test_failed_cli_run_returns_honest_error(self):
        with unittest.mock.patch.object(serve, '_colab_budget_exceeded', return_value=False), \
             unittest.mock.patch.object(serve, '_colab_compute_provision', return_value=(True, 'ok')), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(1, 'Traceback')), \
             unittest.mock.patch.object(serve, '_accrue_colab_units'), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._colab_compute_run('agent-0', '1/0', 'probe', [], 60)
        self.assertIn('exit 1', result['error'])

    def test_executor_formats_error_for_the_model(self):
        with unittest.mock.patch.object(serve, '_colab_compute_run',
                                        return_value={'error': 'boom'}):
            executor = content._make_colab_compute_executor('a1')
            out = executor('run_on_colab', {'code': 'print(1)'})
        self.assertIn('__TOOL_ERROR__', out)
        self.assertIn('boom', out)

    def test_redteam_code_is_refused_on_colab(self):
        # Offensive-security/scan work runs in the local
        # Work Room sandbox, NEVER on the player's real Google Colab account.
        for bad in ('nmap -sV example.com', 'msfconsole -q', 'sqlmap -u http://x/page?id=1',
                    'hydra -l admin ssh://host', 'nuclei -u https://victim.dev',
                    'exfiltrate / proc/self/maps over a reverse shell'):
            with unittest.mock.patch.object(serve, '_colab_cli') as cli:
                result = serve._colab_compute_run('agent-0', bad, 'probe', [], 60)
            self.assertIn('refusing to run', result['error'], bad)
            self.assertIn('off-limits', result['error'], bad)
            cli.assert_not_called()

    def test_colab_target_hosts_extracts_urls(self):
        hosts = serve._colab_target_hosts(
            'r = requests.get("https://api.example.com/v1/data")  # https://cdn.example.com/x.png\n',
            'pull from https://finance.example.org quote page')
        self.assertEqual(hosts, {'api.example.com', 'cdn.example.com', 'finance.example.org'})

    def test_colab_gate_urls_allowlisted_host_skips_jev(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync') as jev:
            reason = serve._colab_gate_urls('agent-0', 'https://dreyx.com/data', 'pull it')
        self.assertIsNone(reason)
        jev.assert_not_called()

    def test_colab_gate_urls_private_host_is_refused(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=False), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync') as jev:
            reason = serve._colab_gate_urls('agent-0', 'https://10.0.0.5/secret', 'call it')
        self.assertIn('private or internal', reason)
        jev.assert_not_called()

    def test_colab_gate_urls_low_confidence_jev_is_refused(self):
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=(None, 1.0, 0.0)):
            reason = serve._colab_gate_urls('agent-0', 'https://example.com/x', 'fetch it')
        self.assertIsNotNone(reason)
        self.assertIn('not approved', reason)

    def test_colab_gate_urls_confident_allow_passes(self):
        calls = {'n': 0}
        def fake_gate(agent_id, action, noun, target, purpose, decision, confidence, cost, authorized, trace_id=None):
            calls['n'] += 1
            return True
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=('allow', 0.95, 0.0)), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', side_effect=fake_gate), \
             unittest.mock.patch.object(serve, 'record_browse_success'):
            reason = serve._colab_gate_urls('agent-0', 'https://example.com/x', 'fetch it')
        self.assertIsNone(reason)
        self.assertEqual(calls['n'], 1)

    def test_colab_gate_urls_denial_blocked_by_jev_categories(self):
        # A host Jev says belongs to a blocked category never runs.
        def fake_gate(agent_id, action, noun, target, purpose, decision, confidence, cost, authorized, trace_id=None):
            return False
        with unittest.mock.patch.object(serve, '_is_allowlisted_host', return_value=False), \
             unittest.mock.patch.object(serve, '_is_safe_public_host', return_value=True), \
             unittest.mock.patch.object(serve, '_jev_quorum_choice_sync',
                                        return_value=('block', 0.9, 0.0)), \
             unittest.mock.patch.object(serve, '_jev_safety_gate', side_effect=fake_gate):
            reason = serve._colab_gate_urls('agent-0', 'https://archive.org/x', 'backup media')
        self.assertIsNotNone(reason)
        self.assertIn('not approved', reason)

    def test_colab_compute_run_refuses_url_gated_job_before_provision(self):
        with unittest.mock.patch.object(serve, '_colab_gate_urls',
                                        return_value='host was blocked'), \
             unittest.mock.patch.object(serve, '_colab_compute_provision') as prov, \
             unittest.mock.patch.object(serve, '_colab_cli') as cli:
            result = serve._colab_compute_run('agent-0', 'https://example.com/x', 'fetch it', [], 60)
        self.assertIn('refusing to run', result['error'])
        self.assertIn('host was blocked', result['error'])
        prov.assert_not_called(), 'a gated job never provisions a session'
        cli.assert_not_called()

    def test_account_usage_parses_colab_usage_output(self):
        raw = ('Current balance: 4.25 compute units\n'
               'Usage rate: 1.15/hr\n'
               'Active assignments: 2')
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, '_colab_cli', return_value=(0, raw)):
            usage = serve._colab_account_usage()
        self.assertEqual(usage['balance'], 4.25)
        self.assertEqual(usage['rate'], 1.15)
        self.assertEqual(usage['assignments'], 2)

    def test_budget_exceeded_when_real_account_balance_is_zero(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 1}):
            self.assertTrue(serve._colab_budget_exceeded(),
                            'a genuinely exhausted paid account gates runs even with no think tank cap')

    def test_free_tier_zero_balance_is_not_a_gate(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', True), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 0.0, 'rate': 1.15, 'assignments': 2}):
            self.assertFalse(serve._colab_budget_exceeded(),
                             'free tier has no prepaid wallet -- 0.00 balance must not block runs')

    def test_budget_not_exceeded_when_balance_exists_and_no_think_tank_cap(self):
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 4.25, 'rate': 1.15, 'assignments': 2}):
            self.assertFalse(serve._colab_budget_exceeded())
        with unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 0.0), \
             unittest.mock.patch.object(serve, 'COLAB_FREE_TIER', False), \
             unittest.mock.patch.object(serve, '_colab_account_usage', return_value=None):
            self.assertFalse(serve._colab_budget_exceeded(),
                             'an unknown balance fails open to the operator cap alone')

    def test_bank_view_seeds_colab_units_row(self):
        with unittest.mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True), \
             unittest.mock.patch.object(serve, 'COLAB_MONTHLY_UNITS', 20.0), \
             unittest.mock.patch.object(serve, '_colab_spend_this_month', return_value=3.0), \
             unittest.mock.patch.object(serve, '_colab_account_usage',
                                        return_value={'balance': 4.25, 'rate': 1.15, 'assignments': 2}):
            services = serve._bank_budget_view({})
        row = services[serve.COLAB_LEDGER_KEY]
        self.assertEqual(row['used'], 3.0)
        self.assertEqual(row['cap'], 20.0)
        self.assertEqual(row['left'], 17.0)
        self.assertEqual(row['balance_units'], 4.25)
        self.assertFalse(row['over'])

    def test_budget_cap_reads_units_not_usd(self):
        self.assertEqual(serve._budget_cap_usd(serve.COLAB_LEDGER_KEY),
                         serve.COLAB_MONTHLY_UNITS)


