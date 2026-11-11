"""Credential health (199): a 401/403 from an external service while
carrying a credential accumulates a failure streak that pings the player on
the standing health-alert channels; any other status resets it. No network,
no real vault, Telegram/email push mocked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    import tempfile
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-cred-health-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        check_rate_limit=lambda aid: True,
        _load_env=lambda: {},
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def client():
    return TestClient(serve.app)


def _persistence(calls):
    """(persist, push) doubles for the health-alert pipeline."""
    def persist(alerts):
        calls.extend(alerts)
        return alerts
    return (mock.patch.object(serve, '_persist_new_health_alerts', side_effect=persist),
            mock.patch.object(serve, '_push_new_health_alerts', side_effect=lambda a: None))


class CredentialHealth(unittest.TestCase):
    def test_streak_accumulates_and_alerts_at_threshold(self):
        with _db_cleanup():
            calls = []
            p, push = _persistence(calls)
            with p, push:
                serve._credential_note_auth_result('vault', 'github', 401, '401 from api.github.com')
                self.assertEqual([], calls)  # one failure below threshold: silent
                serve._credential_note_auth_result('vault', 'github', 403, '403 from api.github.com')
            self.assertEqual(1, len(calls))
            self.assertEqual('credential', calls[0]['category'])
            self.assertEqual('warning', calls[0]['severity'])
            self.assertIn('github', calls[0]['message'])
            rows = serve._credential_health_rows()
            self.assertEqual(1, len(rows))
            self.assertEqual('vault:github', rows[0]['label'])
            self.assertEqual(2, rows[0]['consecutiveFailures'])

    def test_success_resets_the_streak(self):
        with _db_cleanup():
            serve._credential_note_auth_result('vault', 'github', 401)
            serve._credential_note_auth_result('vault', 'github', 200)
            rows = serve._credential_health_rows()
            self.assertEqual([], rows)

    def test_5xx_is_not_an_auth_failure(self):
        with _db_cleanup():
            serve._credential_note_auth_result('vault', 'pixellab', 503)
            self.assertEqual([], serve._credential_health_rows())

    def test_renotify_after_window_only(self):
        with _db_cleanup():
            calls = []
            p, push = _persistence(calls)
            with p, push:
                for _ in range(4):  # streak 4 -> first alert (warning)
                    serve._credential_note_auth_result('env', 'TAVILY_API_KEY', 401)
            self.assertEqual(1, len(calls))  # dedup window blocks an instant re-ping
            with serve._db() as conn:
                conn.execute('UPDATE credential_health SET notified_at = ? '
                             "WHERE label = 'env:TAVILY_API_KEY'",
                             (serve.time.time() - serve.CREDENTIAL_HEALTH_RENOTIFY_S - 10,))
            calls.clear()
            with p, push:
                serve._credential_note_auth_result('env', 'TAVILY_API_KEY', 401)
            self.assertEqual(1, len(calls))
            self.assertEqual('critical', calls[0]['severity'])  # streak 5

    def test_endpoint_gated_and_clear(self):
        r = client().get('/api/credentials/health')
        self.assertEqual(r.status_code, 401)
        r = client().post('/api/credentials/health/clear', json={'label': 'vault:github'})
        self.assertEqual(r.status_code, 401)
        with mock.patch.object(serve, '_operator_gate', return_value=(True, 'player')):
            r = client().get('/api/credentials/health')
            self.assertEqual(r.status_code, 200)
            r = client().post('/api/credentials/health/clear', json={'label': 'vault:github'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual({'ok': True}, r.json())


import contextlib  # noqa: E402


@contextlib.contextmanager
def _db_cleanup():
    # try/finally is REQUIRED: with a bare @contextmanager, a failing body
    # throws into the yield and the code after it never runs -- the cleanup
    # skip is exactly what polluted rows across tests in the first draft.
    try:
        yield
    finally:
        with serve._db() as conn:
            conn.execute('DELETE FROM credential_health')
