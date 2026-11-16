"""DB-backed Jev decisions-model setting: the think tank's decisions
model slug resolves through the `settings` table first (switchable live via the
player-only /api/jev/model endpoint, survives restarts, never auto-refreshed)
and only falls back to the JEV_MODEL env/default constant. This closes the Jev
SPOF operability gap -- a new decisions model on OpenRouter is a deliberate
operator switch, not a daily re-pick like model_tiers.

Hermetic: temp DB + patched env/default constant. No live Jev, no real network.
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)


class JevModelSetting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-model-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            # Hermetic resolver: an ambient OPENROUTER_API_KEY (local dev)
            # lets an earlier test's lifespan watchdog latch the real-account
            # credits pause into module state, which the spend-gate filter
            # then honors. Pin all three gate inputs so these tests verify
            # resolver precedence, never the environment's wallet.
            _credits_remaining_cached=lambda: None,
            credits_pause_active=lambda: False,
            _lane_cap_exceeded=lambda band: False,
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_to_env_or_builtin_slug_when_unset(self):
        # No settings row yet -- the resolver falls back to JEV_MODEL.
        self.assertEqual(serve._jev_model(), serve.JEV_MODEL)

    def test_set_setting_persists_across_connections(self):
        # Writes via a fresh connection and reads back via another -- proves
        # it's really in the DB, not in-memory.
        serve._set_setting('jev_model', 'typesafe/jev-2.0')
        self.assertEqual(serve._get_setting('jev_model'), 'typesafe/jev-2.0')

    def test_resolver_uses_db_row_over_env_default(self):
        serve._set_setting('jev_model', 'typesafe/jev-2.0')
        self.assertEqual(serve._jev_model(), 'typesafe/jev-2.0')

    def test_resolver_falls_back_after_clear(self):
        # Deleting the row returns to the fallback -- the switch is reversible.
        serve._set_setting('jev_model', 'typesafe/jev-2.0')
        with serve._db() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'jev_model'")
        self.assertEqual(serve._jev_model(), serve.JEV_MODEL)

    def test_setting_round_trips_update_not_duplicate(self):
        # ON CONFLICT upsert: setting twice keeps one row with the latest value.
        serve._set_setting('jev_model', 'typesafe/jev-1.9')
        serve._set_setting('jev_model', 'typesafe/jev-2.0')
        with serve._db() as conn:
            rows = conn.execute("SELECT value FROM settings WHERE key = 'jev_model'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'typesafe/jev-2.0')


class JevModelEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-model-endpoint-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
            # Hermetic lifespan: with TELEGRAM_BOT_TOKEN unset, the app's
            # _telegram_poll_loop never starts, so TestClient's __enter__/__exit__
            # (which runs the real lifespan) doesn't block on a 25s long-poll
            # during shutdown. No live network, no real credentials.
            TELEGRAM_BOT_TOKEN=None,
            TELEGRAM_ALLOWED_CHAT_IDS=[],
            # Same hermeticity as JevModelSetting: with an ambient
            # OPENROUTER_API_KEY, the lifespan's credits watchdog fetches the
            # REAL balance and latches the paid-floor pause into module state
            # (in-process, surviving the temp-DB teardown), and the spend gate
            # then filters the chain to []. Pin the gate inputs so endpoint
            # tests test the endpoint, not the environment's wallet.
            _credits_remaining_cached=lambda: None,
            credits_pause_active=lambda: False,
            _lane_cap_exceeded=lambda band: False,
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def test_get_returns_current_and_fallback(self):
        with self._client() as c:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['model'], serve.JEV_MODEL)
        self.assertEqual(body['fallback'], serve.JEV_MODEL)

    def test_set_requires_player_session(self):
        # No session cookie at all: the auth middleware rejects it (401)
        # before the handler's player-only check can even run.
        with self._client() as c:
            r = c.post('/api/jev/model', json={'model': 'typesafe/jev-2.0'})
        self.assertEqual(r.status_code, 401)

    def test_set_with_valid_session_switches_live(self):
        with self._client() as c:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.post('/api/jev/model', json={'model': 'typesafe/jev-2.0'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['model'], 'typesafe/jev-2.0')
        # The switch took effect immediately, no restart, no auto-refresh.
        self.assertEqual(serve._jev_model(), 'typesafe/jev-2.0')

    def test_set_rejects_empty_model(self):
        with self._client() as c:
            c.cookies.set(serve.SESSION_COOKIE_NAME, serve.create_session())
            r = c.post('/api/jev/model', json={'model': '   '})
        self.assertEqual(r.status_code, 400)


class JevModelSpendGateEmptyChain(unittest.TestCase):
    """The 2026-11-16 VM outage: below the free-credit floor (or with spend
    paused) the chain filter keeps only the Colab standby / freepool routes.
    The VM runs neither, so the chain filtered to [] and both _jev_model()
    ([0] on an empty list) and the /api/jev/model endpoint (chain[0]) raised
    IndexError -- 14 crashes in the tier-refresh/failover loops and 43 ASGI
    500s on the status poll. The gate is a 'nothing may serve' state, not a
    crash: _jev_model returns '', the decision call fails closed (taped,
    typed exception), the endpoint reports the empty chain."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-model-gate-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            THINK_TANK_DIR=self.tmp,
        )
        self._cm.start()
        serve.init_db()

    def tearDown(self):
        self._cm.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate_empty(self):
        # Force the real spend-gate branch: credits far below the floor,
        # no Colab standby forward, no freepool route -- exactly the VM's
        # shape, so the filter at _decision_model_chain leaves [].
        return unittest.mock.patch.multiple(
            serve,
            _credits_remaining_cached=lambda: 0.0,
            _credits_free_floor=lambda: 1e9,
            _colab_standby_enabled=lambda: False,
            _freepool_route_for_band=lambda band, force=False: None,
        )

    def test_resolver_returns_empty_string_not_indexerror(self):
        serve._set_setting('jev_model', 'typesafe/jev-2.0')
        with self._gate_empty():
            self.assertEqual(serve._decision_model_chain(), [])
            self.assertEqual(serve._jev_model(), '')

    def test_decision_call_fails_closed_with_tape_row(self):
        with self._gate_empty(), \
             unittest.mock.patch.object(serve, '_think_tank_spend_cap_exceeded',
                                        return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                serve._call_openrouter_decision_sync('', {'x': 1}, ['q'])
        self.assertIn('spend gate', str(ctx.exception))
        with serve._db() as conn:
            row = conn.execute(
                "SELECT model, ok, raw FROM decision_tape ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row, 'the lost decision must land on the tape')
        self.assertEqual(row[1], 0)
        self.assertIn('no decision model may serve', row[2])

    def test_endpoint_reports_empty_chain_200(self):
        from starlette.testclient import TestClient
        with unittest.mock.patch.multiple(
                serve,
                TELEGRAM_BOT_TOKEN=None,
                TELEGRAM_ALLOWED_CHAT_IDS=[],
                verify_session=lambda *a, **k: True,
                _decision_model_chain=lambda: [],
                _jev_model=lambda: '',
                _jev_judge_model=lambda: None,
                _escalation_judge_drift_summary=lambda: {}):
            with TestClient(serve.app) as c:
                r = c.get('/api/jev/model')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body['primary'])
        self.assertEqual(body['chain'], [])
        self.assertEqual(body['slugs'], {})


if __name__ == '__main__':
    unittest.main()