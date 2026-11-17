"""Price-driven JEV decisions-chain ordering and the Cheaper Inference
typed-decisions transport (operator directions, 2026-11-17).

The chain used to run in whatever order the operator wrote the settings row.
Now the PAID slugs stable-sort by measured per-call cost (trailing 7d
decision_tape AVG per slug, >=3 decided samples, 5-min cache): the cheapest
provider leads and the other becomes the failover, so a price drift flips
the order without an operator edit. Laya (a '://' loopback URL) and the
free pool ('@freepool:') keep their positions AFTER the paid group.

`@cheaperinference:<model>` chain entries dial Cheaper's /v1/decisions (a
different wire shape than OpenRouter's /api/alpha/decisions) and normalize
the array-of-answers response into the OpenRouter dict shape so _jev_choice,
the tape, and __jev__ accrual work unchanged.

Hermetic: temp DB, patched prices, no real network.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402  (repo test convention)


class PriceOrdering(unittest.TestCase):
    def test_cheapest_paid_slug_leads(self):
        chain = serve._price_order_decision_chain([
            'typesafe/jev-1.14', 'typesafe/jev-1.13',
            '@cheaperinference:jev-1.14.0'])
        self.assertEqual(chain, [
            'typesafe/jev-1.14', 'typesafe/jev-1.13',
            '@cheaperinference:jev-1.14.0'])

    def test_unknown_priced_slug_sorts_after_known(self):
        prices = {'typesafe/jev-1.13': 0.00003}
        with unittest.mock.patch.object(serve, '_jev_slug_prices',
                                        return_value=prices):
            chain = serve._price_order_decision_chain([
                'typesafe/jev-1.14', 'typesafe/jev-1.13',
                '@cheaperinference:jev-1.14.0'])
        self.assertEqual(chain[0], 'typesafe/jev-1.13')
        # Operator order preserved among the unknown-priced remainder.
        self.assertEqual(chain[1:], ['typesafe/jev-1.14',
                                     '@cheaperinference:jev-1.14.0'])

    def test_laya_and_freepool_stay_after_paid_slugs(self):
        prices = {'@cheaperinference:jev-1.14.0': 0.00002}
        laya = 'http://127.0.0.1:8941/v1/systemone'
        with unittest.mock.patch.object(serve, '_jev_slug_prices',
                                        return_value=prices):
            chain = serve._price_order_decision_chain([
                laya, 'typesafe/jev-1.14',
                '@cheaperinference:jev-1.14.0', '@freepool:low:gemini-free'])
        self.assertEqual(chain[0], '@cheaperinference:jev-1.14.0')
        self.assertEqual(chain[1], 'typesafe/jev-1.14')
        self.assertEqual(chain[2], laya)
        self.assertEqual(chain[3], '@freepool:low:gemini-free')

    def test_single_slug_chain_is_untouched(self):
        self.assertEqual(serve._price_order_decision_chain(['typesafe/jev-1.14']),
                         ['typesafe/jev-1.14'])

    def test_chain_resolver_orders_paid_slugs_end_to_end(self):
        tmp = tempfile.mkdtemp(prefix='jev-price-')
        cm = unittest.mock.patch.multiple(
            serve,
            DB_PATH=os.path.join(tmp, 'test.db'),
            THINK_TANK_DIR=tmp,
            _credits_remaining_cached=lambda: None,
            credits_pause_active=lambda: False,
            _lane_cap_exceeded=lambda band: False,
            _colab_standby_enabled=lambda: False,
            _freepool_route_for_band=lambda band, force=False: None,
        )
        cm.start()
        try:
            serve.init_db()
            serve._set_setting(
                'jev_model',
                'typesafe/jev-1.14,typesafe/jev-1.13,'
                '@cheaperinference:jev-1.14.0')
            prices = {'typesafe/jev-1.13': 0.00003,
                      '@cheaperinference:jev-1.14.0': 0.00002}
            with unittest.mock.patch.object(serve, '_jev_slug_prices',
                                            return_value=prices):
                chain = serve._decision_model_chain()
            # Cheapest measured price leads; the expensive slug follows;
            # the unknown-priced slug keeps the operator's tail position.
            self.assertEqual(chain, ['@cheaperinference:jev-1.14.0',
                                     'typesafe/jev-1.13',
                                     'typesafe/jev-1.14'])
        finally:
            cm.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class CheaperDecisionSlug(unittest.TestCase):
    def test_slug_extraction(self):
        self.assertEqual(serve._cheaper_decision_slug(
            '@cheaperinference:jev-1.14.0'), 'jev-1.14.0')
        self.assertIsNone(serve._cheaper_decision_slug('typesafe/jev-1.14'))
        self.assertIsNone(serve._cheaper_decision_slug('@freepool:low:x'))
        self.assertIsNone(serve._cheaper_decision_slug(None))

    def test_transport_normalizes_cheaper_response(self):
        # Cheaper's wire shape: answers as an ARRAY, cost in
        # billing.billed_cost_usd. The transport must hand back the
        # OpenRouter {answers: {name: {...}}, usage.cost} shape that
        # _jev_choice parses.
        body = json.dumps({
            'model': 'jev-1.14.0',
            'answers': [{'type': 'choice', 'name': 'choice',
                         'choice': 'a', 'confidence': 0.9}],
            'usage': {'input_tokens': 10, 'output_tokens': 2},
            'billing': {'status': 'settled', 'billed_cost_usd': '0.000009'},
        })
        env = {'CHEAPER_INFERENCE_API_KEY': 'test-key'}
        with unittest.mock.patch.object(serve, '_load_env', return_value=env), \
                unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                           return_value=_FakeResp(body)):
            data = serve._call_cheaper_decision_sync(
                '@cheaperinference:jev-1.14.0',
                {'messages': [], 'signals': {}},
                {'choice': {'type': 'choice', 'instructions': 'pick',
                            'criteria': {'a': 'first', 'b': 'second'}}})
        self.assertEqual(data['answers']['choice']['choice'], 'a')
        self.assertEqual(data['usage']['cost'], 0.000009)
        choice, confidence, cost = serve._jev_choice(data)
        self.assertEqual(choice, 'a')
        self.assertAlmostEqual(confidence, 0.9)

    def test_transport_raises_without_key(self):
        with unittest.mock.patch.object(serve, '_load_env',
                                        return_value={}):
            with self.assertRaises(RuntimeError):
                serve._call_cheaper_decision_sync(
                    '@cheaperinference:jev-1.14.0', {}, {})

    def test_transport_http_error_raises(self):
        env = {'CHEAPER_INFERENCE_API_KEY': 'test-key'}

        def _raise(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 500, 'boom',
                                         hdrs=None, fp=None)

        with unittest.mock.patch.object(serve, '_load_env', return_value=env), \
                unittest.mock.patch.object(serve.urllib.request, 'urlopen',
                                           side_effect=_raise):
            with self.assertRaises(urllib.error.HTTPError):
                serve._call_cheaper_decision_sync(
                    '@cheaperinference:jev-1.14.0', {}, {})


class _FakeResp:
    def __init__(self, body):
        self._body = body.encode() if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


if __name__ == '__main__':
    unittest.main()
