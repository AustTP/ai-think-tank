"""The service price catalog: published per-endpoint prices, no guessing."""
import unittest

from core.service_pricing import (describe_service_pricing, estimate_api_cost,
                                  estimate_service_spec_cost, load_services)


class ServicePricing(unittest.TestCase):
    def test_free_service_is_zero(self):
        q = estimate_api_cost('pixellab', '/nope', {})
        self.assertIsNone(q, 'an unknown endpoint must not invent a price')
        q = estimate_api_cost('open-meteo', '/v1/forecast', {})
        self.assertEqual(q['usd'], 0.0)
        self.assertEqual(q['kind'], 'free')

    def test_tavily_is_a_page_request_with_a_reference_price(self):
        q = estimate_api_cost('tavily', '/search', {})
        self.assertAlmostEqual(q['usd'], 0.008)
        self.assertEqual(q['kind'], 'page_request')
        self.assertEqual(q['enforcedBy'], 'page_request_budget')

    def test_apify_is_variable_and_names_its_budget(self):
        q = estimate_api_cost('apify', '/acts/example/run-sync-get-dataset-items', {})
        self.assertIsNone(q['usd'])
        self.assertEqual(q['kind'], 'variable')
        self.assertEqual(q['enforcedBy'], 'apify_monthly_budget')

    def test_pixellab_size_bucket_and_base_path(self):
        svc = load_services()['pixellab']
        q = estimate_service_spec_cost(svc, '/v2/create-image-pixflux',
                                       {'image_size': {'width': 344, 'height': 192}})
        self.assertAlmostEqual(q['usd'], 0.0132)
        self.assertIn('400px', q['basis'])
        # A smaller image lands in the cheaper bucket.
        q = estimate_service_spec_cost(svc, '/create-image-pixflux',
                                       {'image_size': {'width': 64, 'height': 64}})
        self.assertAlmostEqual(q['usd'], 0.00793)

    def test_pixellab_tileset_and_per_object(self):
        svc = load_services()['pixellab']
        q = estimate_service_spec_cost(svc, '/create-tileset',
                                       {'tile_size': {'width': 32, 'height': 32}})
        self.assertAlmostEqual(q['usd'], 0.0099)
        q = estimate_service_spec_cost(svc, '/map-objects', {'objects': [{}, {}, {}]})
        self.assertAlmostEqual(q['usd'], 3 * 0.0099)

    def test_describe_reports_kind_and_enforcement(self):
        svc = load_services()['huggingface']
        d = describe_service_pricing(svc)
        self.assertEqual(d['kind'], 'per_token')
        self.assertTrue(d['note'])


if __name__ == '__main__':
    unittest.main()
