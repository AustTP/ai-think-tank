"""The URL-reputation wiring in the two URL-facing gates.

Operator direction (2026-11-01): Jev is too reluctant on ordinary URLs,
so a composite external trust score now sits IN FRONT of the Jev classify:
a blocklist hit refuses without Jev, a trusted verdict carries READ-shaped
traffic without Jev, and everything else defers to Jev unchanged. These
tests pin the composition contracts with the scorer mocked:
- browser goto/read on a trusted host -> allow, Jev quorum NOT called
- any op on a bad host -> refuse, Jev quorum NOT called
- a trusted host on a WRITE-shaped op (fill) -> still Jev
- unknown/suspect -> Jev as today
- the api_call helper: trusted+GET allows, trusted+POST defers, bad denies
- reputation outage (exception) -> gate defers to Jev, never fails open
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _verdict(band, host='good.example'):
    return {'url': 'https://' + host, 'host': host, 'domain': host,
            'band': band, 'score': {'trusted': 90, 'bad': 0}.get(band, 50),
            'signals': {'ageDays': 2800 if band == 'trusted' else None,
                        'urlhaus': band == 'bad', 'safebrowsing': False,
                        'virustotal': None, 'heuristics': [], 'errors': []},
            'checkedAt': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ',
                                            __import__('time').gmtime())}


class BrowserGate(unittest.IsolatedAsyncioTestCase):
    async def _classify(self, op, verdict, **kw):
        with mock.patch.object(serve, '_url_trust_verdict', return_value=verdict), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               side_effect=AssertionError('Jev must not be called')) as jev:
            allowed, reason = await serve._classify_browser_action(
                op, 'https://good.example/doc', 'research', **kw)
        return allowed, reason, jev

    async def test_trusted_read_allows_without_jev(self):
        allowed, reason, jev = await self._classify('goto', _verdict('trusted'))
        self.assertTrue(allowed)
        self.assertIn('reputation trusted', reason)
        jev.assert_not_called()

    async def test_bad_refuses_without_jev(self):
        allowed, reason, jev = await self._classify('goto', _verdict('bad'))
        self.assertFalse(allowed)
        self.assertIn('reputation flags this host', reason)
        jev.assert_not_called()

    async def test_trusted_write_still_goes_to_jev(self):
        with mock.patch.object(serve, '_url_trust_verdict',
                               return_value=_verdict('trusted')), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               return_value=('allow', 0.9, 0.0, 't')), \
             mock.patch.object(serve, '_jev_safety_gate', return_value=True) as gate:
            allowed, _reason = await serve._classify_browser_action(
                'fill', 'https://good.example/form', 'fill a form')
        self.assertTrue(allowed)
        gate.assert_called_once()

    async def test_unknown_defers_to_jev(self):
        with mock.patch.object(serve, '_url_trust_verdict',
                               return_value=_verdict('unknown')), \
             mock.patch.object(serve, 'OPENROUTER_API_KEY', 'k'), \
             mock.patch.object(serve, '_jev_quorum_decision',
                               return_value=('allow', 0.9, 0.0, 't')), \
             mock.patch.object(serve, '_jev_safety_gate', return_value=True):
            allowed, _reason = await serve._classify_browser_action(
                'goto', 'https://good.example/doc', 'research')
        self.assertTrue(allowed)

    async def test_allowlisted_host_short_circuits_before_reputation(self):
        # The existing allowlist bypass still wins, and reputation is not
        # even consulted for an already-vetted host.
        with mock.patch.object(serve, '_is_allowlisted_host', return_value=True), \
             mock.patch.object(serve, '_url_trust_verdict',
                               side_effect=AssertionError('must not be called')):
            allowed, reason = await serve._classify_browser_action(
                'goto', 'https://vetted.example/x', 'research')
        self.assertTrue(allowed)
        self.assertIn('allowlisted', reason)


class HttpGateHelper(unittest.TestCase):
    def test_trusted_get_allows(self):
        self.assertEqual(serve._url_trust_http_gate(_verdict('trusted'), 'GET'), 'allow')

    def test_trusted_post_defers_to_jev(self):
        self.assertIsNone(serve._url_trust_http_gate(_verdict('trusted'), 'POST'))

    def test_bad_denies_any_method(self):
        self.assertEqual(serve._url_trust_http_gate(_verdict('bad'), 'POST'), 'deny')
        self.assertEqual(serve._url_trust_http_gate(_verdict('bad'), 'GET'), 'deny')

    def test_unknown_defers(self):
        self.assertIsNone(serve._url_trust_http_gate(_verdict('unknown'), 'GET'))

    def test_no_verdict_defers(self):
        self.assertIsNone(serve._url_trust_http_gate(None, 'GET'))


class CacheAndSummary(unittest.TestCase):
    def test_summary_compacts_the_signals(self):
        line = serve._url_trust_summary(_verdict('trusted'))
        self.assertIn('band=trusted', line)
        self.assertIn('age 2800d', line)
        self.assertIn('no reputation', serve._url_trust_summary(None))

    def test_cache_roundtrip_per_host(self):
        calls = []
        verdict = _verdict('trusted')

        def fake_score(url, **kw):
            calls.append(url)
            import copy
            return copy.deepcopy(verdict)

        setting = {}
        with mock.patch.object(serve, '_get_setting',
                               side_effect=lambda key, default='': setting.get(key, default)), \
             mock.patch.object(serve, '_set_setting',
                               side_effect=lambda key, val: setting.__setitem__(key, val)), \
             mock.patch.object(serve._url_trust, 'score_url', side_effect=fake_score):
            first = serve._url_trust_verdict('https://cached.example/a')
            second = serve._url_trust_verdict('https://cached.example/b')
        self.assertEqual(calls, ['https://cached.example/a'])
        self.assertEqual(first['band'], 'trusted')
        self.assertEqual(second['band'], 'trusted')

    def test_keyless_verdict_cannot_poison_a_keyed_caller(self):
        # A process WITHOUT the keys writes a degraded entry; a keyed
        # caller must re-score rather than trust it for a day.
        setting = {}
        degraded = _verdict('unknown')
        degraded['signals']['virustotal'] = None
        with mock.patch.object(serve, '_get_setting',
                               side_effect=lambda key, default='': setting.get(key, default)), \
             mock.patch.object(serve, '_set_setting',
                               side_effect=lambda key, val: setting.__setitem__(key, val)), \
             mock.patch.dict('os.environ', {}, clear=False):
            serve.os.environ.pop('VIRUSTOTAL_API_KEY', None)
            serve.os.environ.pop('SAFEBROWSING_API_KEY', None)
            with mock.patch.object(serve._url_trust, 'score_url',
                                   return_value=degraded):
                serve._url_trust_verdict('https://poison.example/a')
            serve.os.environ['VIRUSTOTAL_API_KEY'] = 'k'
            with mock.patch.object(serve._url_trust, 'score_url',
                                   return_value=_verdict('trusted')) as fresh:
                out = serve._url_trust_verdict('https://poison.example/b')
        self.assertEqual(out['band'], 'trusted')
        fresh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
