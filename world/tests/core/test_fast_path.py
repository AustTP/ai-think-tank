"""Fast-path lanes: audit chain, scope gate, paper-fill caps, kill switch,
and the JSON lane engine (types from the registry, unknown types rejected).

Hermetic: temp DB + patched HTTP. No network, no real model calls, no
contact with the tank's think_tank.db. The fast path is a separate process
by design; these tests import the module for its classes only.
"""

import asyncio
import json
import sqlite3
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'server'))

import fast_path  # noqa: E402


class FastPathBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fast-path-')
        self._cm = unittest.mock.patch.multiple(
            fast_path,
            LANE_KEY_PATH=os.path.join(self.tmp, 'fast_lane.key'),
            KILL_SWITCH_PATH=os.path.join(self.tmp, 'fast-kill'),
        )
        self._cm.start()
        self.log = fast_path.LaneLog(db_path=os.path.join(self.tmp, 'lane.db'))
        self.model = fast_path.FastModel(self.log)

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _drain(self, timeout=5):
        """Block until every emitted event is committed and visible."""
        self.assertTrue(self.log.flush_sync(timeout),
                        'lane log never flushed')

    def _event_count(self, lane=None):
        conn = sqlite3.connect(os.path.join(self.tmp, 'lane.db'), timeout=10)
        try:
            if lane:
                return conn.execute(
                    'SELECT COUNT(*) FROM lane_events WHERE lane = ?', (lane,)).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM lane_events WHERE lane != '__sync__'").fetchone()[0]
        finally:
            conn.close()

class LaneChainTests(FastPathBase):
    def test_chain_verifies_after_appends(self):
        for i in range(25):
            self.log.emit('bounty-recon', 'lane-observer', 'cycle', {'i': i})
        self._drain()
        self.assertEqual(self.log.verify(), [])
        self.assertEqual(self._event_count(), 25)

    def test_tamper_is_detected(self):
        self.log.emit('markets-paper', 'lane-markets', 'paper_fill', {'price': 1})
        self.log.emit('markets-paper', 'lane-markets', 'paper_fill', {'price': 2})
        self._drain()
        self.assertEqual(self.log.verify(), [])
        conn = sqlite3.connect(os.path.join(self.tmp, 'lane.db'), timeout=10)
        conn.execute("UPDATE lane_events SET payload = '{\"price\": 999}' WHERE id = 1")
        conn.commit()
        conn.close()
        problems = self.log.verify()
        self.assertTrue(any('hash mismatch' in p['issue'] for p in problems))

    def test_threads_never_break_the_chain(self):
        # The single-writer design makes this deterministic; the point of
        # the test is that concurrency cannot reintroduce the race class
        # that produced the tank's 39 tape breaks.
        def worker(n):
            for i in range(20):
                self.log.emit('markets-paper', f'agent-{n}', 'tick', {'n': n, 'i': i})
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self._drain()
        self.assertEqual(self.log.verify(), [])


class HttpWatchLaneTests(FastPathBase):
    def test_unconfigured_lane_runs_no_network(self):
        calls = []

        class NoClient:
            async def get(self, *a, **k):
                calls.append(a)
                raise AssertionError('network attempted without scope')

        lane = fast_path.HttpWatchLane(
            {'name': 'bounty-recon', 'program': '', 'targets': []},
            self.log, self.model)
        lane._client = NoClient()
        asyncio.run(lane.cycle())
        self.assertEqual(calls, [])
        self.assertFalse(lane.configured)
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='bounty-recon')]
        self.assertIn('unconfigured', kinds)

    def test_configured_lane_stays_in_scope(self):
        fetched = []

        class FakeClient:
            async def get(self, url):
                fetched.append(url)

                class R:
                    status_code = 200
                    headers = {'server': 'nginx', 'content-security-policy': 'x'}
                    text = ''
                return R()

        lane = fast_path.HttpWatchLane(
            {'name': 'bounty-recon', 'program': 'h1-acme',
             'targets': ['example.com', 'https://api.example.com'],
             'rate_limit_s': 0},
            self.log, self.model)
        lane._client = FakeClient()
        asyncio.run(lane.cycle())
        self.assertEqual(fetched, ['https://example.com', 'https://api.example.com'])
        self._drain()
        cycle = [e for e in self.log.recent(lane='bounty-recon') if e['kind'] == 'cycle'][0]
        self.assertEqual(cycle['payload']['targets'], 2)
        self.assertEqual(cycle['payload']['program'], 'h1-acme')

    def test_kill_switch_pauses_watch(self):
        lane = fast_path.HttpWatchLane(
            {'name': 'watch-x', 'targets': ['example.com']}, self.log, self.model)
        open(fast_path.KILL_SWITCH_PATH, 'w').write('stop')
        asyncio.run(lane.cycle())
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='watch-x')]
        self.assertEqual(kinds, ['paused'])


class PaperMarketsLaneTests(FastPathBase):
    def _lane(self, **extra):
        cfg = {'name': 'markets-paper', 'symbols': ['BTC-USD'], 'notional': 100}
        cfg.update(extra)
        return fast_path.PaperMarketsLane(cfg, self.log, self.model)

    def test_paper_fill_math(self):
        lane = self._lane()
        fill, blocked = lane._paper_fill('BTC-USD', 'buy', 100.0)
        self.assertIsNone(blocked)
        self.assertEqual(fill['units'], 1.0)
        self.assertEqual(fill['portfolio']['cash'], 900.0)

    def test_daily_trade_cap_blocks(self):
        lane = self._lane()
        original = fast_path.MAX_TRADES_PER_DAY
        fast_path.MAX_TRADES_PER_DAY = 1
        try:
            lane._paper_fill('BTC-USD', 'buy', 100.0)
            fill, blocked = lane._paper_fill('BTC-USD', 'sell', 100.0)
            self.assertIsNone(fill)
            self.assertEqual(blocked, 'daily trade cap reached')
        finally:
            fast_path.MAX_TRADES_PER_DAY = original

    def test_notional_is_capped_by_the_structural_limit(self):
        fast_path.MAX_NOTIONAL_PER_TRADE = 500
        lane = self._lane(notional=100000)
        self.assertEqual(lane.notional, 500)

    def test_sell_without_position_is_refused(self):
        lane = self._lane()
        for _ in range(12):
            lane._prices['BTC-USD'].append(100.0)
        fill, blocked = lane._paper_fill('BTC-USD', 'sell', 100.0)
        self.assertIsNone(fill)
        self.assertEqual(blocked, 'no position to sell')

    def test_kill_switch_pauses_cycles(self):
        lane = self._lane()
        open(fast_path.KILL_SWITCH_PATH, 'w').write('stop')
        asyncio.run(lane.cycle())
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='markets-paper')]
        self.assertEqual(kinds, ['paused'])

    def test_rule_needs_warmup_then_triggers_off_band(self):
        lane = self._lane()
        for _ in range(9):
            lane._prices['BTC-USD'].append(100.0)
        action, why = lane._decide('BTC-USD', 100.0)
        self.assertEqual(action, 'hold')  # 9 ticks: still warming up
        lane._prices['BTC-USD'].append(100.0)
        action, why = lane._decide('BTC-USD', 110.0)  # 10% above SMA
        self.assertEqual(action, 'sell')

    def test_unknown_price_source_is_unconfigured(self):
        lane = self._lane(price_source='binance_real', symbols=['BTC-USD'])
        self.assertFalse(lane.configured)


class LaneEngineTests(FastPathBase):
    def test_builds_declared_lanes(self):
        cfg = {'lanes': [
            {'name': 'watch-a', 'type': 'http_watch', 'targets': ['a.com'], 'cadence_s': 60},
            {'name': 'markets-a', 'type': 'paper_markets', 'symbols': ['BTC-USD']},
        ]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual([l.name for l in lanes], ['watch-a', 'markets-a'])
        self.assertEqual(lanes[0].cadence_s, 60)

    def test_unknown_type_is_rejected_visibly_not_crashed(self):
        cfg = {'lanes': [
            {'name': 'rogue', 'type': 'live_orders'},
            {'name': 'watch-b', 'type': 'http_watch', 'targets': ['b.com']},
        ]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual([l.name for l in lanes], ['watch-b'])
        self._drain()
        rejected = [e for e in self.log.recent(lane='system') if e['kind'] == 'lane_rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['payload']['type'], 'live_orders')

    def test_multiple_instances_of_one_type(self):
        cfg = {'lanes': [
            {'name': 'watch-a', 'type': 'http_watch', 'targets': ['a.com']},
            {'name': 'watch-b', 'type': 'http_watch', 'targets': ['b.com']},
        ]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual(sorted(l.name for l in lanes), ['watch-a', 'watch-b'])


if __name__ == '__main__':
    unittest.main()


class VpsDispatchTests(FastPathBase):
    """The bounty invariant: work runs on the VPS, never locally, never in
    Docker, and never from undeclared commands. LLM output is analysis
    input, never a command."""

    def _lane(self, work=None, executor=None):
        cfg = {'name': 'bounty-recon', 'program': 'h1-acme',
               'targets': ['example.com'],
               'executor': executor if executor is not None else {
                   'kind': 'vps', 'host': 'vps.example.net', 'user': 'hunter',
                   'port': 22, 'key_path': os.path.join(self.tmp, 'id_ed25519'),
                   'known_hosts': os.path.join(self.tmp, 'known_hosts'),
                   'command_timeout_s': 30},
               'work_timeout_s': 30,
               'work': work if work is not None else [
                   {'name': 'subfinder', 'command': 'subfinder -d example.com -silent'},
                   {'name': 'nuclei', 'command': 'nuclei -l targets.txt -severity medium,high'}]}
        lane = fast_path.HttpWatchLane(cfg, self.log, self.model)
        return lane

    def _pin(self, lane):
        open(lane.executor.key_path, 'w').write('KEY')
        open(lane.executor.known_hosts, 'w').write('vps.example.net ssh-ed25519 AAAA')

    def test_unpinned_host_fails_closed(self):
        lane = self._lane()
        self.assertFalse(lane.executor.configured)
        calls = []
        lane.executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        asyncio.run(lane._vps_cycle())
        self.assertEqual(calls, [])
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='bounty-recon')]
        self.assertIn('executor_unconfigured', kinds)

    def test_dispatch_runs_only_declared_work_and_returns_receipts(self):
        lane = self._lane()
        self._pin(lane)
        ran = []

        def fake_run(command, timeout_s=None):
            ran.append((command, timeout_s))
            return {'exit_code': 0, 'stdout': 'sub.example.com\nsub2.example.com',
                    'stderr': '', 'duration_s': 1.5}
        lane.executor.run = fake_run
        asyncio.run(lane._vps_cycle())
        self.assertEqual([c for c, _ in ran],
                         ['subfinder -d example.com -silent',
                          'nuclei -l targets.txt -severity medium,high'])
        self.assertEqual(ran[0][1], 30)
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='bounty-recon')]
        self.assertEqual(kinds.count('dispatch'), 2)
        self.assertEqual(kinds.count('result'), 2)
        result = [e for e in self.log.recent(lane='bounty-recon') if e['kind'] == 'result'][0]
        self.assertEqual(result['payload']['exit_code'], 0)
        self.assertIn('sub.example.com', result['payload']['stdout_tail'])

    def test_ssh_argv_pins_the_host_key_and_never_gives_a_shell(self):
        lane = self._lane()
        self._pin(lane)
        argv = lane.executor.argv('subfinder -d example.com')
        self.assertIn('-o', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('UserKnownHostsFile=' + lane.executor.known_hosts, argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[-1], 'subfinder -d example.com')
        self.assertTrue(argv[-1].endswith('example.com'))  # the command, unmodified

    def test_no_vps_declared_means_local_recon_only(self):
        # A lane without an executor keeps the plain http_watch behavior
        # (local GET surface watch) -- the VPS is an explicit declaration.
        fetched = []

        class FakeClient:
            async def get(self, url):
                fetched.append(url)

                class R:
                    status_code = 200
                    headers = {}
                return R()
        lane = fast_path.HttpWatchLane(
            {'name': 'watch-plain', 'program': '', 'targets': ['example.com'],
             'rate_limit_s': 0}, self.log, self.model)
        lane._client = FakeClient()
        asyncio.run(lane.cycle())
        self.assertEqual(fetched, ['https://example.com'])

    def test_empty_work_list_runs_nothing_on_the_vps(self):
        lane = self._lane(work=[])
        self._pin(lane)
        calls = []
        lane.executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        asyncio.run(lane._vps_cycle())
        self.assertEqual(calls, [])
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='bounty-recon')]
        self.assertIn('unconfigured', kinds)

    def test_kill_switch_blocks_dispatch(self):
        lane = self._lane()
        self._pin(lane)
        calls = []
        lane.executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        open(fast_path.KILL_SWITCH_PATH, 'w').write('stop')
        asyncio.run(lane.cycle())  # the real path, guard included
        self.assertEqual(calls, [])
        self._drain()
        kinds = [e['kind'] for e in self.log.recent(lane='bounty-recon')]
        self.assertEqual(kinds, ['paused'])
