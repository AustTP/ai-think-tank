"""DB-backed Jev decisions-model setting (2026-09-28): the village's decisions
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402  (sys.path insert above is the repo test convention)


class JevModelSetting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jev-model-')
        self._cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(self.tmp, 'test.db'),
            VILLAGE_DIR=self.tmp,
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
            VILLAGE_DIR=self.tmp,
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


if __name__ == '__main__':
    unittest.main()