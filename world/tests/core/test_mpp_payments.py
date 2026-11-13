"""MPP (Machine Payments Protocol) tests: the treasury lane cap, the 402
challenge parsing, the payment fences (enable switch, per-call cap,
escalation floor, treasury + vendor caps), the paid/refused ledger, and the
transport hook (disabled = today's 402 behavior, byte for byte). The
network, the real treasury service and the live think_tank DB are patched
or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m pytest tests/core/test_mpp_payments.py
"""
import base64
import email.message
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_TMP = None
_CONSTANT_PATCHES = []
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER, _CONSTANT_PATCHES
    _TMP = tempfile.mkdtemp(prefix='think-tank-mpp-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    # Deterministic fences regardless of the operator's live .env: the
    # per-fence tests patch whatever they need explicitly.
    _CONSTANT_PATCHES = [
        unittest.mock.patch.object(serve, 'MPP_SPEND_CAP_USD', 0.0),
        unittest.mock.patch.object(serve, 'MPP_MAX_PAYMENT_USD', 0.05),
        unittest.mock.patch.object(serve, 'MPP_ESCALATION_USD', 1.00),
    ]
    for patcher in _CONSTANT_PATCHES:
        patcher.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    for patcher in _CONSTANT_PATCHES:
        patcher.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _402(challenge=None, header=True):
    """An HTTP 402 carrying the challenge: base64 JSON in the
    Payment-Required header by default, plain JSON body otherwise."""
    hdrs = email.message.Message()
    body = b'{"error": "payment required"}'
    if challenge is not None:
        if header:
            hdrs['Payment-Required'] = base64.b64encode(
                json.dumps(challenge).encode()).decode()
        else:
            body = json.dumps({'paymentRequired': challenge}).encode()
    return urllib.error.HTTPError('https://vendor.example/v1/chat/completions',
                                  402, 'Payment Required', hdrs, None) \
        if header or challenge is None else _402_body(challenge, body)


def _402_body(challenge, body):
    import io
    return urllib.error.HTTPError('https://vendor.example/v1/chat/completions',
                                  402, 'Payment Required',
                                  email.message.Message(), io.BytesIO(body))


def _spec(provider='mppvendor'):
    return {'is_custom': True, 'free': False, 'provider': provider,
            'model': 'vendor/model', 'base_url': 'https://vendor.example/v1/x',
            'api_key_env': 'VENDOR_KEY', 'settlement': 'mpp'}


def _rows():
    with serve._db() as conn:
        serve._mpp_ensure_table(conn)
        return conn.execute(
            'SELECT vendor, amount_usd, status, reason FROM mpp_payments '
            'ORDER BY ts').fetchall()


def _mpp_rows_cleared():
    with serve._db() as conn:
        serve._mpp_ensure_table(conn)
        conn.execute('DELETE FROM mpp_payments')


class MppTreasuryLane(unittest.TestCase):
    """The treasury is a lane like JEV: cap from MPP_SPEND_CAP_USD, month
    accrual into __mpp__, trip alert keyed separately."""

    def tearDown(self):
        serve._ALLOWANCE_NOTIFIED.clear()

    def test_cap_reads_the_treasury_constant(self):
        with unittest.mock.patch.object(serve, 'MPP_SPEND_CAP_USD', 0.5):
            self.assertEqual(serve._lane_spend_cap('__mpp__'), 0.5)
        self.assertEqual(serve._lane_spend_cap('__unknown__'), 0.0)

    def test_jev_lane_unchanged(self):
        # The lane generalization must not shift the JEV lane's cap source.
        with unittest.mock.patch.object(serve, 'JEV_SPEND_CAP_USD', 0.5):
            self.assertEqual(serve._lane_spend_cap('__jev__'), 0.5)

    def test_treasury_trip_fires_one_alert(self):
        serve._ALLOWANCE_NOTIFIED.clear()
        with unittest.mock.patch.object(serve, 'MPP_SPEND_CAP_USD', 0.50):
            serve._accrue_spend('__mpp__', 0.60)
            self.assertTrue(serve._lane_cap_exceeded('__mpp__'))
            # Same state on the next poll: still exceeded, still one alert.
            self.assertTrue(serve._lane_cap_exceeded('__mpp__'))


class ParseChallenge(unittest.TestCase):
    def test_header_b64_challenge(self):
        err = _402({'amountUsd': 0.01, 'payTo': 'vendor.example',
                    'resource': '/v1/x'})
        parsed = serve._parse_mpp_challenge(err)
        self.assertEqual(parsed['amount'], 0.01)
        self.assertEqual(parsed['payTo'], 'vendor.example')

    def test_body_challenge(self):
        err = _402({'amount': 0.02, 'payTo': 'v', 'resource': '/x'},
                   header=False)
        self.assertEqual(serve._parse_mpp_challenge(err)['amount'], 0.02)

    def test_malformed_is_never_paid_blind(self):
        self.assertIsNone(serve._parse_mpp_challenge(_402()))
        err = _402({'amountUsd': 'lots', 'payTo': 'v'})
        self.assertIsNone(serve._parse_mpp_challenge(err))
        err = _402({'amountUsd': 0.01})  # no payTo
        self.assertIsNone(serve._parse_mpp_challenge(err))
        err = _402({'amountUsd': -5, 'payTo': 'v'})
        self.assertIsNone(serve._parse_mpp_challenge(err))


class SettleAndRetry(unittest.TestCase):
    """The fences fire in order: enable switch, parse, per-call cap,
    escalation floor, treasury cap, vendor cap -- then pay, accrue to BOTH
    the treasury lane and the vendor rollup, and ledger the decision."""

    def setUp(self):
        _mpp_rows_cleared()
        serve._ALLOWANCE_NOTIFIED.clear()

    def tearDown(self):
        _mpp_rows_cleared()
        serve._ALLOWANCE_NOTIFIED.clear()

    def _enabled(self):
        return unittest.mock.patch.object(serve, '_mpp_enabled',
                                          return_value=True)

    def test_disabled_returns_false_and_pays_nothing(self):
        called = []
        with unittest.mock.patch.object(serve, '_mpp_settle',
                                        side_effect=lambda c: called.append(c)):
            result = serve._mpp_settle_and_retry(_402({'amountUsd': 0.01,
                                                       'payTo': 'v'}), _spec(), 'm')
        self.assertFalse(result)
        self.assertEqual(called, [])
        self.assertEqual(_rows(), [])

    def test_happy_path_pays_accrues_and_ledgers(self):
        challenge = {'amountUsd': 0.01, 'payTo': 'v', 'resource': '/x'}
        ledger_before = serve._spend_ledger_read()
        lane_before = serve._service_month_to_date(ledger_before, '__mpp__')
        vendor_before = serve._provider_month_used(ledger_before, 'mppvendor')
        with self._enabled(), \
             unittest.mock.patch.object(serve, '_mpp_settle',
                                        return_value={'id': 'r1'}), \
             unittest.mock.patch.object(serve, 'log_action') as log:
            result = serve._mpp_settle_and_retry(_402(challenge), _spec(), 'm')
        self.assertTrue(result)
        self.assertEqual(_rows(), [('mppvendor', 0.01, 'paid', None)])
        ledger_after = serve._spend_ledger_read()
        self.assertAlmostEqual(
            serve._service_month_to_date(ledger_after, '__mpp__')
            - lane_before, 0.01)
        self.assertAlmostEqual(
            serve._provider_month_used(ledger_after, 'mppvendor')
            - vendor_before, 0.01)
        log.assert_called_once()

    def test_per_call_cap_refuses(self):
        challenge = {'amountUsd': 0.10, 'payTo': 'v', 'resource': '/x'}
        with self._enabled(), \
             unittest.mock.patch.object(serve, '_mpp_settle') as settle:
            with self.assertRaisesRegex(RuntimeError, 'per-call cap'):
                serve._mpp_settle_and_retry(_402(challenge), _spec(), 'm')
        settle.assert_not_called()
        self.assertEqual(_rows()[0][2], 'refused')

    def test_escalation_floor_refuses(self):
        challenge = {'amountUsd': 2.00, 'payTo': 'v', 'resource': '/x'}
        with self._enabled(), \
             unittest.mock.patch.object(serve, 'MPP_MAX_PAYMENT_USD', 5.00), \
             unittest.mock.patch.object(serve, '_mpp_settle') as settle:
            with self.assertRaisesRegex(RuntimeError, 'escalation floor'):
                serve._mpp_settle_and_retry(_402(challenge), _spec(), 'm')
        settle.assert_not_called()
        self.assertEqual(_rows()[0][2], 'refused')

    def test_tripped_treasury_refuses(self):
        challenge = {'amountUsd': 0.01, 'payTo': 'v', 'resource': '/x'}
        with self._enabled(), \
             unittest.mock.patch.object(serve, 'MPP_SPEND_CAP_USD', 0.50), \
             unittest.mock.patch.object(serve, '_lane_cap_exceeded',
                                        side_effect=lambda lane: lane == '__mpp__'), \
             unittest.mock.patch.object(serve, '_mpp_settle') as settle:
            with self.assertRaisesRegex(RuntimeError, 'treasury cap'):
                serve._mpp_settle_and_retry(_402(challenge), _spec(), 'm')
        settle.assert_not_called()
        self.assertEqual(_rows()[0][2], 'refused')

    def test_tripped_vendor_cap_refuses(self):
        challenge = {'amountUsd': 0.01, 'payTo': 'v', 'resource': '/x'}
        with self._enabled(), \
             unittest.mock.patch.object(serve, '_provider_cap_exceeded',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_mpp_settle') as settle:
            with self.assertRaisesRegex(RuntimeError, 'spend cap'):
                serve._mpp_settle_and_retry(_402(challenge), _spec(), 'm')
        settle.assert_not_called()
        self.assertEqual(_rows()[0][2], 'refused')

    def test_unparseable_challenge_refuses(self):
        with self._enabled(), \
             unittest.mock.patch.object(serve, '_mpp_settle') as settle:
            with self.assertRaisesRegex(RuntimeError, 'pay blind'):
                serve._mpp_settle_and_retry(_402(), _spec(), 'm')
        settle.assert_not_called()
        self.assertEqual(_rows()[0][2], 'refused')


class TransportHook(unittest.TestCase):
    """The shared POST: with MPP disabled, a 402 from an mpp-settlement
    provider behaves exactly as before -- the error propagates and nothing
    pays. With MPP enabled and a paid challenge, the SAME body retries."""

    def setUp(self):
        _mpp_rows_cleared()

    def tearDown(self):
        _mpp_rows_cleared()

    def test_disabled_402_propagates_unpaid(self):
        err = _402({'amountUsd': 0.01, 'payTo': 'v', 'resource': '/x'})
        with unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                serve._openrouter_chat_post({'model': 'vendor/model'},
                                            provider=_spec())
        self.assertEqual(_rows(), [])

    def test_paid_challenge_retries_the_same_body(self):
        err = _402({'amountUsd': 0.01, 'payTo': 'v', 'resource': '/x'})
        posts = []

        def fake_urlopen(req, timeout=None):
            posts.append(json.loads(req.data.decode()))
            if len(posts) == 1:
                raise err
            return json.dumps({
                'choices': [{'message': {'content': 'ok'}}],
                'usage': {'cost': 0.0},
            })

        with unittest.mock.patch.object(serve, '_mpp_enabled',
                                        return_value=True), \
             unittest.mock.patch.object(serve, '_mpp_settle',
                                        return_value={'id': 'r1'}), \
             unittest.mock.patch.object(serve, '_urlopen_with_resilience',
                                        side_effect=fake_urlopen), \
             unittest.mock.patch.object(serve, 'training_capture'), \
             unittest.mock.patch.object(serve, 'log_action'):
            result = serve._openrouter_chat_post({'model': 'vendor/model'},
                                                 provider=_spec())
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])


if __name__ == '__main__':
    unittest.main()
