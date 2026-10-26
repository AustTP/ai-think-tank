"""The agent-facing price report: quote, budget, and the model-visible line."""
import unittest
import unittest.mock

import serve


class ServicePriceReport(unittest.TestCase):
    def test_list_covers_every_registered_service(self):
        r = serve.service_price_report()
        self.assertTrue(r['ok'])
        ids = {s['id'] for s in r['services']}
        self.assertIn('pixellab', ids)
        self.assertIn('tavily', ids)
        self.assertIn('open-meteo', ids)
        self.assertGreaterEqual(len(ids), 15)
        self.assertIn('sharedCap', r)

    def test_quote_tavily_with_page_budget(self):
        with unittest.mock.patch.object(serve, '_page_budget_used', return_value=530), \
             unittest.mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 1000), \
             unittest.mock.patch.object(serve, '_spend_cap_month_view',
                                        return_value={'cap': 50.0, 'usage': 20.0, 'exceeded': False}):
            r = serve.service_price_report('tavily', '/search', {})
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['quote']['usd'], 0.008)
        self.assertEqual(r['budget']['used'], 530)
        self.assertEqual(r['budget']['cap'], 1000)
        self.assertAlmostEqual(r['sharedCap']['left'], 30.0)

    def test_quote_pixellab_reports_balance(self):
        with unittest.mock.patch.object(serve, '_pixellab_credits_remaining', return_value=6.44), \
             unittest.mock.patch.object(serve, '_spend_cap_month_view',
                                        return_value={'cap': 50.0, 'usage': 20.0, 'exceeded': False}):
            r = serve.service_price_report('pixellab', '/create-image-pixflux',
                                           {'image_size': {'width': 344, 'height': 192}})
        self.assertAlmostEqual(r['quote']['usd'], 0.0132)
        self.assertAlmostEqual(r['budget']['balanceUsd'], 6.44)

    def test_unknown_service_is_explicit(self):
        r = serve.service_price_report('not-a-service')
        self.assertFalse(r['ok'])
        self.assertIn('unknown service', r['error'])

    def test_price_line_is_model_readable(self):
        line = serve._api_price_line({'usd': 0.0132, 'basis': '344px <= 400px',
                                      'kind': 'size_bucket'})
        self.assertIn('$0.0132', line)
        self.assertIn('344px', line)
        line = serve._api_price_line({'usd': None, 'kind': 'variable',
                                      'note': 'per actor run', 'enforcedBy': 'apify_monthly_budget'})
        self.assertIn('variable', line)
        self.assertIn('apify_monthly_budget', line)


if __name__ == '__main__':
    unittest.main()
