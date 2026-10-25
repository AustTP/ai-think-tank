"""Generic api_call: the registry-gated /api/api-call path for external
services (config, not code). Covers the registry loader and host/path lookup,
credential resolution and auth injection, the raw request helper (including
the redirect re-check), and the endpoint's gate order -- read-only for
unregistered hosts, path/method enforcement and credential injection for
registered hosts, Jev gating, and page/spend accrual.

Hermetic: the endpoint's only IO is log_action (DB) and the model gate, so
this suite patches those along with the network fetch. No real outbound call
or DB write happens in these tests.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-api-call-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        API_CALL_ENABLED=True,
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _FakeResponse:
    def __init__(self, body, status=200, content_type='application/json', final_url=None):
        self._body = body
        self.status = status
        self.headers = {'content-type': content_type}
        self._final_url = final_url or 'https://example.com/x'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._final_url

    def read(self, n=-1):
        data = self._body if isinstance(self._body, bytes) else self._body.encode()
        return data[:n] if n != -1 else data
_VALID_SPEC = {
    'id': 'my_service',
    'name': 'My Service',
    'base_url': 'https://api.myservice.example.com',
    'credential': {'type': 'env', 'key': 'MY_SERVICE_TOKEN'},
    'auth': {'type': 'header_bearer'},
    'methods': ['GET', 'POST'],
    'path_rules': [{'prefix': '/', 'methods': ['GET', 'POST']}],
    'spend': {'kind': 'none'},
}



class DailyCallBudget(unittest.TestCase):
    """The per-service daily call cap (spend.kind = 'daily_calls') that guards
    daily-quota APIs like OpenSky, keyed by UTC date."""

    def setUp(self):
        serve._api_daily_calls_ledger_write({})  # creates the kv_apicalls table + clears it

    def tearDown(self):
        serve._api_daily_calls_ledger_write({})

    def _spec(self, **over):
        spend = {'kind': 'daily_calls', 'cap': 5, 'note': 'n'}
        spend.update(over)
        return {'id': 'opensky', 'spend': spend}

    def test_day_format_is_utc_date(self):
        self.assertRegex(serve._api_daily_calls_day(0), r'^\d{4}-\d{2}-\d{2}$')

    def test_used_starts_at_zero(self):
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 0)

    def test_accrue_then_exceeded(self):
        self.assertFalse(serve._api_daily_calls_exceeded(self._spec()))
        for _ in range(5):
            serve._api_accrue_daily_call({'id': 'opensky'})
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 5)
        self.assertTrue(serve._api_daily_calls_exceeded(self._spec()))

    def test_exceeded_false_for_non_daily_kind(self):
        self.assertFalse(serve._api_daily_calls_exceeded({'id': 'x', 'spend': {'kind': 'none'}}))

    def test_exceeded_false_when_cap_missing(self):
        self.assertFalse(serve._api_daily_calls_exceeded({'id': 'x', 'spend': {'kind': 'daily_calls'}}))

    def test_accrue_with_no_sid_is_noop(self):
        serve._api_accrue_daily_call({'spend': {'kind': 'daily_calls'}})
        self.assertEqual(serve._api_daily_calls_used({'id': 'opensky'}), 0)

    def test_ledger_read_isolated_from_db_failure(self):
        with unittest.mock.patch.object(serve, '_db', side_effect=RuntimeError('no db')):
            self.assertEqual(serve._api_daily_calls_ledger_read(), {})
            serve._api_accrue_daily_call({'id': 'opensky'})  # must not raise


class DailyCallBudgetLedger(unittest.TestCase):
    def test_ledger_write_read_round_trip(self):
        serve._api_daily_calls_ledger_write({'2026-10-08': {'opensky': 3}})
        self.assertEqual(serve._api_daily_calls_ledger_read(), {'2026-10-08': {'opensky': 3}})


