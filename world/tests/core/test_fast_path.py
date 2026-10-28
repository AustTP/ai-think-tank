"""Fast path: generic JSON-parameterized lane engine.

Covers: the audit chain (single writer, tamper detection, concurrent
emitters), stage composition validation (unknown kinds, impossible
orders, duplicate names), idle-by-default silence, hot reload, the scope
gate, VPS dispatch invariants (pinned host key, declared work only, fail
closed, kill switch), paper-fill math/caps/state persistence, and the
LLM cadence/degradation contract.

Hermetic: temp DB/config + patched HTTP. No network, no real model
calls, no contact with the tank's think_tank.db.
"""

import asyncio
import json
import os
import shutil
import sqlite3
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

    def _events(self, lane=None):
        self._drain()
        events = [e for e in self.log.recent(lane=lane, limit=200)
                  if e['lane'] != '__sync__']
        return list(reversed(events))  # chronological

    def _kinds(self, lane=None):
        return [e['kind'] for e in self._events(lane)]

    def _write_config(self, cfg):
        path = os.path.join(self.tmp, 'fast_lanes.json')
        with open(path, 'w') as f:
            json.dump(cfg, f)
        return path


class LaneChainTests(FastPathBase):
    def test_chain_verifies_after_appends(self):
        for i in range(25):
            self.log.emit('l', 'a', 'tick', {'i': i})
        self._drain()
        self.assertEqual(self.log.verify(), [])
        self.assertEqual(self._event_count(), 25)

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

    def test_tamper_is_detected(self):
        self.log.emit('l', 'a', 'tick', {'price': 1})
        self.log.emit('l', 'a', 'tick', {'price': 2})
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
                self.log.emit('l', f'agent-{n}', 'tick', {'n': n, 'i': i})
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self._drain()
        self.assertEqual(self.log.verify(), [])


class EngineBuildTests(FastPathBase):
    def _cfg(self, **over):
        cfg = {'name': 'watch-a', 'enabled': True, 'cadence_s': 60,
               'pipeline': [{'kind': 'http_get', 'targets': ['a.com'],
                             'rate_limit_s': 0}]}
        cfg.update(over)
        return cfg

    def test_builds_pipeline_lanes_from_json(self):
        cfg = {'lanes': [self._cfg(),
                         {'name': 'markets-a', 'enabled': False, 'cadence_s': 30,
                          'pipeline': [{'kind': 'price_spot', 'symbols': ['BTC-USD']},
                                       {'kind': 'sma_band'},
                                       {'kind': 'paper_fill', 'notional': 100}]}]}
        with unittest.mock.patch.object(fast_path, 'CONFIG_PATH',
                                        self._write_config(cfg)):
            lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual([l.name for l in lanes], ['watch-a', 'markets-a'])
        self.assertEqual([s.kind for s in lanes[0].stages], ['http_get'])
        self.assertEqual([s.kind for s in lanes[1].stages],
                         ['price_spot', 'sma_band', 'paper_fill'])
        self.assertEqual(lanes[1].cadence_s, 30)

    def test_unknown_stage_kind_is_rejected_visibly(self):
        cfg = {'lanes': [{'name': 'rogue', 'enabled': True,
                          'pipeline': [{'kind': 'live_orders'}]}]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual(lanes, [])
        rejected = [e for e in self._events('system') if e['kind'] == 'lane_rejected']
        self.assertEqual(len(rejected), 1)
        self.assertIn('unknown kind', rejected[0]['payload']['error'])
        self.assertIn('live_orders', rejected[0]['payload']['error'])

    def test_impossible_compositions_are_rejected(self):
        cases = [
            # paper_fill without a price source
            {'name': 'bad-1', 'enabled': True,
             'pipeline': [{'kind': 'paper_fill', 'notional': 100}]},
            # sma_band without prices to decide over
            {'name': 'bad-2', 'enabled': True,
             'pipeline': [{'kind': 'sma_band'}]},
            # paper_fill before the decide stage
            {'name': 'bad-3', 'enabled': True,
             'pipeline': [{'kind': 'price_spot', 'symbols': ['BTC-USD']},
                          {'kind': 'paper_fill'},
                          {'kind': 'sma_band'}]},
        ]
        lanes = fast_path.build_lanes({'lanes': cases}, self.log, self.model)
        self.assertEqual(lanes, [])
        rejected = [e for e in self._events('system') if e['kind'] == 'lane_rejected']
        self.assertEqual(len(rejected), 3)

    def test_unknown_price_source_is_rejected_at_build(self):
        cfg = {'lanes': [{'name': 'bad-src', 'enabled': True,
                          'pipeline': [{'kind': 'price_spot',
                                        'source': 'binance_real',
                                        'symbols': ['BTC-USD']}]}]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual(lanes, [])

    def test_duplicate_names_and_missing_pieces_are_rejected(self):
        cfg = {'lanes': [
            {'name': 'dup', 'enabled': True,
             'pipeline': [{'kind': 'http_get', 'targets': ['a.com']}]},
            {'name': 'dup', 'enabled': True,
             'pipeline': [{'kind': 'http_get', 'targets': ['b.com']}]},
            {'name': 'no-pipeline', 'enabled': True, 'pipeline': []},
            {'name': '', 'enabled': True, 'pipeline': [{'kind': 'log'}]},
        ]}
        lanes = fast_path.build_lanes(cfg, self.log, self.model)
        self.assertEqual([l.name for l in lanes], ['dup'])
        rejected = [e for e in self._events('system') if e['kind'] == 'lane_rejected']
        self.assertEqual(len(rejected), 3)


class IdleByDefaultTests(FastPathBase):
    def test_disabled_lane_is_fully_silent(self):
        lane = fast_path.Lane({'name': 'quiet', 'enabled': False, 'cadence_s': 5,
                               'pipeline': [{'kind': 'log'}]},
                              self.log, self.model)
        asyncio.run(lane.cycle())
        asyncio.run(lane.cycle())
        asyncio.run(lane.cycle())
        self.assertEqual(self._kinds('quiet'), [])
        self.assertEqual(lane.mode, 'idle')

    def test_enabled_lane_runs_and_stays_quiet_while_running(self):
        lane = fast_path.Lane({'name': 'busy', 'enabled': True, 'cadence_s': 5,
                               'pipeline': [{'kind': 'log'}]},
                              self.log, self.model)
        asyncio.run(lane.cycle())
        asyncio.run(lane.cycle())
        kinds = self._kinds('busy')
        self.assertEqual(kinds.count('running'), 1)
        self.assertIn('facts', kinds)
        self.assertEqual(lane.mode, 'running')

    def test_stage_discovered_inhibition_is_announced_once(self):
        # Enabled but with no scope: one 'running', one 'idle', then
        # silence on every later cycle (no flapping spam).
        lane = fast_path.Lane({'name': 'bounty', 'enabled': True, 'cadence_s': 5,
                               'pipeline': [{'kind': 'http_get', 'targets': []}]},
                              self.log, self.model)
        for _ in range(4):
            asyncio.run(lane.cycle())
        kinds = self._kinds('bounty')
        self.assertEqual(kinds, ['running', 'idle'])

    def test_kill_switch_pauses_once(self):
        lane = fast_path.Lane({'name': 'k', 'enabled': True, 'cadence_s': 5,
                               'pipeline': [{'kind': 'log'}]},
                              self.log, self.model)
        open(fast_path.KILL_SWITCH_PATH, 'w').write('stop')
        asyncio.run(lane.cycle())
        asyncio.run(lane.cycle())
        self.assertEqual(self._kinds('k'), ['paused'])


class HttpGetStageTests(FastPathBase):
    def _lane(self, targets, client):
        lane = fast_path.Lane({'name': 'watch-a', 'enabled': True, 'cadence_s': 60,
                               'pipeline': [{'kind': 'http_get',
                                             'targets': targets,
                                             'rate_limit_s': 0,
                                             'program': 'h1-acme'}]},
                              self.log, self.model)
        lane.stages[0]._client = client
        return lane

    def test_stays_in_scope(self):
        fetched = []

        class FakeClient:
            async def get(self, url):
                fetched.append(url)

                class R:
                    status_code = 200
                    headers = {'server': 'nginx', 'content-security-policy': 'x'}
                return R()
        lane = self._lane(['example.com', 'https://api.example.com'], FakeClient())
        asyncio.run(lane.cycle())
        self.assertEqual(fetched, ['https://example.com', 'https://api.example.com'])
        cycle = [e for e in self._events('watch-a') if e['kind'] == 'cycle'][0]
        self.assertEqual(cycle['payload']['targets'], 2)
        self.assertEqual(cycle['payload']['program'], 'h1-acme')

    def test_fetch_failures_are_recorded_not_raised(self):
        class BoomClient:
            async def get(self, url):
                raise OSError('no route to host')
        lane = self._lane(['down.example.com'], BoomClient())
        asyncio.run(lane.cycle())
        cycle = [e for e in self._events('watch-a') if e['kind'] == 'cycle'][0]
        f = cycle['payload']['findings'][0]
        self.assertEqual(f['type'], 'fetch_failed')
        self.assertEqual(lane.mode, 'running')  # a dead target is not an idle lane


class VpsDispatchStageTests(FastPathBase):
    """The bounty invariant: work runs on the VPS, never locally, never in
    Docker, and never from undeclared commands."""

    def _pipeline(self, work=None, executor=None):
        return [{'kind': 'vps_dispatch',
                 'work_timeout_s': 30,
                 'executor': executor if executor is not None else {
                     'host': 'vps.example.net', 'user': 'hunter', 'port': 22,
                     'key_path': os.path.join(self.tmp, 'id_ed25519'),
                     'known_hosts': os.path.join(self.tmp, 'known_hosts')},
                 'work': work if work is not None else [
                     {'name': 'subfinder', 'command': 'subfinder -d example.com -silent'},
                     {'name': 'nuclei', 'command': 'nuclei -l targets.txt'}]}]

    def _lane(self, work=None, executor=None, pin=True):
        lane = fast_path.Lane({'name': 'bounty-recon', 'enabled': True,
                               'cadence_s': 60,
                               'pipeline': self._pipeline(work, executor)},
                              self.log, self.model)
        if pin:
            stage = lane.stages[0]
            open(stage.executor.key_path, 'w').write('KEY')
            open(stage.executor.known_hosts, 'w').write('vps ssh-ed25519 AAAA')
        return lane

    def test_unpinned_host_fails_closed(self):
        lane = self._lane(pin=False)
        self.assertFalse(lane.stages[0].executor.configured)
        calls = []
        lane.stages[0].executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        asyncio.run(lane.cycle())
        self.assertEqual(calls, [])
        self.assertEqual(self._kinds('bounty-recon')[-1], 'idle')
        idle = [e for e in self._events('bounty-recon') if e['kind'] == 'idle'][0]
        self.assertIn('known_hosts', ' '.join(idle['payload']['missing']))

    def test_dispatch_runs_only_declared_work_and_returns_receipts(self):
        lane = self._lane()
        ran = []

        def fake_run(command, timeout_s=None):
            ran.append((command, timeout_s))
            return {'exit_code': 0, 'stdout': 'sub.example.com',
                    'stderr': '', 'duration_s': 1.5}
        lane.stages[0].executor.run = fake_run
        asyncio.run(lane.cycle())
        self.assertEqual([c for c, _ in ran],
                         ['subfinder -d example.com -silent',
                          'nuclei -l targets.txt'])
        self.assertEqual(ran[0][1], 30)
        events = self._events('bounty-recon')
        self.assertEqual([e['kind'] for e in events].count('dispatch'), 2)
        self.assertEqual([e['kind'] for e in events].count('result'), 2)
        result = [e for e in events if e['kind'] == 'result'][0]
        self.assertEqual(result['payload']['exit_code'], 0)
        self.assertIn('sub.example.com', result['payload']['stdout_tail'])

    def test_ssh_argv_pins_the_host_key_and_passes_the_command_unmodified(self):
        lane = self._lane()
        argv = lane.stages[0].executor.argv('subfinder -d example.com')
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('UserKnownHostsFile=' + lane.stages[0].executor.known_hosts, argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[-1], 'subfinder -d example.com')

    def test_empty_work_list_runs_nothing_on_the_vps(self):
        lane = self._lane(work=[])
        calls = []
        lane.stages[0].executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        asyncio.run(lane.cycle())
        self.assertEqual(calls, [])
        self.assertIn('idle', self._kinds('bounty-recon'))

    def test_kill_switch_blocks_dispatch_through_the_real_cycle(self):
        lane = self._lane()
        calls = []
        lane.stages[0].executor.run = lambda cmd, t=None: calls.append(cmd) or {}
        open(fast_path.KILL_SWITCH_PATH, 'w').write('stop')
        asyncio.run(lane.cycle())
        self.assertEqual(calls, [])
        self.assertEqual(self._kinds('bounty-recon'), ['paused'])


class PaperMarketsStageTests(FastPathBase):
    def _lane(self, **over):
        pipeline = [
            {'kind': 'price_spot', 'source': 'coinbase_spot', 'symbols': ['BTC-USD']},
            {'kind': 'sma_band', 'window': 30, 'band_pct': 2.0},
            {'kind': 'paper_fill', 'notional': 100},
        ]
        if over.pop('with_llm', False):
            pipeline.append({'kind': 'llm', 'every': 5})
        cfg = {'name': 'markets-paper', 'enabled': True, 'cadence_s': 30,
               'pipeline': pipeline}
        cfg.update(over)
        return fast_path.Lane(cfg, self.log, self.model)

    def _feed(self, lane, price):
        stage = lane.stages[0]
        async def fake_run(facts):
            facts['prices'] = {'BTC-USD': price}
        stage.run = fake_run
        for i, st in enumerate(lane.stages):
            if st.kind == 'sma_band':
                st._prices.setdefault('BTC-USD', __import__('collections').deque(maxlen=30))

    def test_rule_needs_warmup_then_triggers_off_band(self):
        lane = self._lane()
        sma = lane.stages[1]
        for _ in range(9):
            sma._prices.setdefault('BTC-USD', __import__('collections').deque(maxlen=30)).append(100.0)
        facts = {'prices': {'BTC-USD': 100.0}}
        asyncio.run(sma.run(facts))
        self.assertEqual(facts['decisions']['BTC-USD']['action'], 'hold')  # 9 ticks
        sma._prices['BTC-USD'].append(100.0)
        asyncio.run(sma.run(facts := {'prices': {'BTC-USD': 110.0}}))
        self.assertEqual(facts['decisions']['BTC-USD']['action'], 'sell')

    def test_paper_fill_math(self):
        lane = self._lane()
        facts = {'prices': {'BTC-USD': 100.0},
                 'decisions': {'BTC-USD': {'action': 'buy'}}}
        asyncio.run(lane.stages[2].run(facts))
        fill = facts['fills'][0]
        self.assertEqual(fill['units'], 1.0)
        self.assertEqual(fill['portfolio']['cash'], 900.0)

    def test_daily_trade_cap_blocks(self):
        original = fast_path.MAX_TRADES_PER_DAY
        fast_path.MAX_TRADES_PER_DAY = 1
        try:
            lane = self._lane()
            asyncio.run(lane.stages[2].run({'prices': {'BTC-USD': 100.0},
                                            'decisions': {'BTC-USD': {'action': 'buy'}}}))
            asyncio.run(lane.stages[2].run({'prices': {'BTC-USD': 100.0},
                                            'decisions': {'BTC-USD': {'action': 'buy'}}}))
            blocked = [e for e in self._events('markets-paper')
                       if e['kind'] == 'fill_blocked']
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]['payload']['reason'], 'daily trade cap reached')
        finally:
            fast_path.MAX_TRADES_PER_DAY = original

    def test_notional_is_capped_by_the_structural_limit(self):
        fast_path.MAX_NOTIONAL_PER_TRADE = 500
        lane = self._lane()
        stage = fast_path.PaperFillStage({'notional': 100000}, lane)
        self.assertEqual(stage.notional, 500)

    def test_portfolio_state_survives_a_rebuild(self):
        # Reloads rebuild Lane objects; paper positions live in the lane
        # DB state table (unchained), so a reload must not reset them.
        lane = self._lane()
        asyncio.run(lane.stages[2].run({'prices': {'BTC-USD': 100.0},
                                        'decisions': {'BTC-USD': {'action': 'buy'}}}))
        rebuilt = self._lane()
        self.assertEqual(rebuilt.log.load_state('markets-paper')['portfolio'],
                         {'BTC-USD': {'cash': 900.0, 'units': 1.0}})

    def test_llm_cadence_and_degradation_contract(self):
        # every=5: cycles 1-4 are silent, cycle 5 calls the model.
        calls = []

        class CountingModel:
            def __init__(self, log):
                self.log = log
            async def ask(self, lane, agent, system, user, max_tokens=400):
                calls.append((lane, agent))
                return 'notable: nothing' if calls else 'nothing notable'

        lane = self._lane(with_llm=True)
        llm = [s for s in lane.stages if s.kind == 'llm'][0]
        lane.model = CountingModel(self.log)
        for i in range(5):
            facts = {'prices': {'BTC-USD': 100.0 + i}}
            asyncio.run(llm.run(facts))
        self.assertEqual(len(calls), 1)

        class DeadModel:
            def __init__(self, log):
                self.log = log
            async def ask(self, *a, **k):
                self.log.emit('markets-paper', 'lane-analyst',
                              'model_degraded', {'error': 'timeout'})
                return None
        lane.model = DeadModel(self.log)
        llm._cycles = 4  # next run is cycle 5: a firing cycle
        asyncio.run(llm.run({'prices': {'BTC-USD': 100.0}}))
        self.assertIn('model_degraded', self._kinds('markets-paper'))


class HotReloadTests(FastPathBase):
    def test_reload_picks_up_config_changes_and_keeps_state(self):
        original_loop = fast_path._lane_loop

        async def stub_loop(lane):
            await asyncio.sleep(3600)

        cfg = {'lanes': [{'name': 'watch-a', 'enabled': False, 'cadence_s': 60,
                          'pipeline': [{'kind': 'http_get', 'targets': ['a.com'],
                                        'rate_limit_s': 0}]}]}
        config_path = self._write_config(cfg)

        async def drive():
            with unittest.mock.patch.object(fast_path, 'CONFIG_PATH', config_path), \
                 unittest.mock.patch.object(fast_path, '_lane_loop', stub_loop):
                sup = fast_path.LaneSupervisor(self.log, self.model)
                await sup.reload(force=True)
                self.assertEqual([l.name for l in sup.lanes], ['watch-a'])
                self.assertFalse(sup.lanes[0].enabled)
                # Flip the lane on via JSON only.
                cfg['lanes'][0]['enabled'] = True
                time.sleep(0.02)
                self._write_config(cfg)
                await sup.reload()
                self.assertTrue(sup.lanes[0].enabled)
                self.assertIn('watch-a', sup.tasks)

        try:
            asyncio.run(drive())
        finally:
            fast_path._lane_loop = original_loop

    def test_broken_config_is_reported_not_fatal(self):
        bad = os.path.join(self.tmp, 'broken.json')
        open(bad, 'w').write('{not json')

        async def drive():
            with unittest.mock.patch.object(fast_path, 'CONFIG_PATH', bad):
                sup = fast_path.LaneSupervisor(self.log, self.model)
                await sup.reload(force=True)
                self.assertEqual(sup.lanes, [])
        asyncio.run(drive())
        self.assertIn('config_error', self._kinds('system'))


if __name__ == '__main__':
    unittest.main()
