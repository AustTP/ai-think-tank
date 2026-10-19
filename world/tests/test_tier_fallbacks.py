"""Tier fallback models: a band's alternates are stored with its pick and a
real availability failure retries the call down that list immediately."""

import sqlite3
import tempfile
import unittest
import urllib.error
from unittest import mock

import serve


def _cand(slug, price):
    return {'id': slug, 'name': slug.upper(), 'price': price}


class FallbackCandidateTest(unittest.TestCase):
    def test_qualifiers_first_then_cheapest_remaining(self):
        pool = [_cand('a', 1.0), _cand('b', 2.0), _cand('c', 3.0),
                _cand('d', 4.0), _cand('e', 5.0)]
        scores = {'a': 90.0, 'b': 89.0, 'c': 88.0, 'd': 50.0}
        with mock.patch.object(serve, 'BAND_QUALITY_FLOOR_GAP', {'low': 5.0}):
            out = serve._tier_fallback_candidates('low', pool, scores,
                                                  _cand('a', 1.0))
        self.assertEqual([m['id'] for m in out], ['b', 'c', 'd'])

    def test_capability_bands_fill_by_score_not_price(self):
        pool = [_cand('pick', 1.0), _cand('cheap', 0.5), _cand('strong', 9.0)]
        scores = {'pick': 90.0, 'cheap': 10.0, 'strong': 80.0}
        with mock.patch.object(serve, 'BAND_QUALITY_FLOOR_GAP',
                               {'coding': 1.0, 'low': 1.0}):
            coding = serve._tier_fallback_candidates(
                'coding', pool, scores, _cand('pick', 1.0))
            low = serve._tier_fallback_candidates(
                'low', pool, scores, _cand('pick', 1.0))
        self.assertEqual([m['id'] for m in coding], ['strong', 'cheap'])
        self.assertEqual([m['id'] for m in low], ['cheap', 'strong'])

    def test_no_pick_no_fallbacks(self):
        self.assertEqual(serve._tier_fallback_candidates(
            'low', [_cand('a', 1.0)], {'a': 1.0}, None), [])


class FallbackLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        with sqlite3.connect(self.tmp.name) as conn:
            conn.execute('''CREATE TABLE model_tiers (
                band TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL,
                price_per_m REAL NOT NULL, chosen_at REAL NOT NULL)''')
            conn.execute('''CREATE TABLE model_tier_fallbacks (
                band TEXT NOT NULL, rank INTEGER NOT NULL, slug TEXT NOT NULL,
                name TEXT NOT NULL, price_per_m REAL NOT NULL,
                chosen_at REAL NOT NULL, PRIMARY KEY (band, rank))''')
            conn.execute("INSERT INTO model_tiers VALUES ('low','primary','P',1,0)")
            conn.execute("INSERT INTO model_tiers VALUES ('coding','primary','P',1,0)")
            conn.executemany(
                'INSERT INTO model_tier_fallbacks VALUES (?,?,?,?,?,?)',
                [('low', 1, 'fb1', 'F1', 1, 0), ('low', 2, 'fb2', 'F2', 1, 0),
                 ('coding', 1, 'fb1', 'F1', 1, 0), ('coding', 2, 'fb3', 'F3', 1, 0)])
        self.patch = mock.patch.object(serve, 'DB_PATH', self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_join_dedupes_and_keeps_rank_order(self):
        self.assertEqual(serve._tier_fallback_slugs('primary'),
                         ['fb1', 'fb2', 'fb3'])

    def test_unknown_slug_has_no_fallbacks(self):
        self.assertEqual(serve._tier_fallback_slugs('not-a-tier-pick'), [])


class AvailabilityFailoverTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(serve, '_think_tank_spend_cap_exceeded', return_value=False),
            mock.patch.object(serve, '_api_key_is_broken', return_value=False),
            mock.patch.object(serve, 'is_model_circuit_broken', return_value=False),
            mock.patch.object(serve, 'record_model_result'),
            mock.patch.object(serve, '_note_api_key_success'),
            mock.patch.object(serve, '_note_api_key_failure'),
            mock.patch.object(serve, 'log_action'),
            mock.patch.object(serve, '_coding_tier_slug', return_value=None),
            mock.patch.object(serve, '_high_tier_slug', return_value=None),
            mock.patch.object(serve, '_mid_tier_slug', return_value=None),
            mock.patch.object(serve, '_reasoning_tier_slug', return_value=None),
            mock.patch.object(serve, '_tier_fallback_slugs',
                              return_value=['fb1', 'fb2']),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _http_error(self, availability):
        err = urllib.error.HTTPError('https://openrouter.ai', 404, 'gone', {}, None)
        err.availability_failure = availability
        return err

    def test_availability_error_serves_from_the_next_slug(self):
        calls = []

        def post(body):
            calls.append(body['model'])
            if body['model'] == 'primary':
                raise self._http_error(True)
            return {'choices': [{'message': {'content': 'ok'}}]}

        with mock.patch.object(serve, '_openrouter_chat_post', side_effect=post):
            result = serve._call_openrouter_once_sync('primary', [], 128)
        self.assertEqual(calls, ['primary', 'fb1'])
        self.assertEqual(result['choices'][0]['message']['content'], 'ok')

    def test_non_availability_error_does_not_fail_over(self):
        calls = []

        def post(body):
            calls.append(body['model'])
            raise self._http_error(False)

        with mock.patch.object(serve, '_openrouter_chat_post', side_effect=post):
            with self.assertRaises(urllib.error.HTTPError):
                serve._call_openrouter_once_sync('primary', [], 128)
        self.assertEqual(calls, ['primary'])

    def test_all_fallbacks_failing_reraises_the_original(self):
        def post(body):
            raise self._http_error(True)

        with mock.patch.object(serve, '_openrouter_chat_post', side_effect=post):
            with self.assertRaises(urllib.error.HTTPError):
                serve._call_openrouter_once_sync('primary', [], 128)


if __name__ == '__main__':
    unittest.main()
