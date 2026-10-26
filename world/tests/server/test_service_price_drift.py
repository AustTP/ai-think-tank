"""Price drift: compare what providers billed with what we quoted."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='tt-price-drift-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP, 'test.db'),
        THINK_TANK_DIR=_TMP,
        AGENTS_DIR=os.path.join(_TMP, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP, 'library'),
        PASSPORT_PATH=os.path.join(_TMP, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


class PriceDrift(unittest.TestCase):
    def setUp(self):
        with serve._db() as conn:
            conn.execute('DELETE FROM service_price_observations')
            conn.execute('DELETE FROM settings')

    def _obs(self, service, endpoint, estimate, actual, n=1):
        for _ in range(n):
            serve.record_service_price_observation(
                service, endpoint, estimate, actual, 'size bucket', 'test')

    def test_sustained_overcharge_is_a_finding(self):
        self._obs('pixellab', '/create-image-pixflux', 0.0132, 0.01716, n=3)
        findings = serve.service_price_drift_report()
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f['service'], 'pixellab')
        self.assertAlmostEqual(f['medianEstimateUsd'], 0.0132)
        self.assertAlmostEqual(f['medianActualUsd'], 0.01716)
        self.assertAlmostEqual(f['deltaPct'], 30.0, places=1)

    def test_small_difference_is_not_a_finding(self):
        self._obs('pixellab', '/create-image-pixflux', 0.0132, 0.0140, n=5)
        self.assertEqual(serve.service_price_drift_report(), [])

    def test_two_samples_are_not_enough(self):
        self._obs('pixellab', '/create-image-pixflux', 0.0132, 0.02, n=2)
        self.assertEqual(serve.service_price_drift_report(), [])

    def test_missing_actual_is_not_recorded(self):
        serve.record_service_price_observation('tavily', '/search', 0.008, None, '', 'test')
        with serve._db() as conn:
            count = conn.execute('SELECT COUNT(*) FROM service_price_observations').fetchone()[0]
        self.assertEqual(count, 0)

    def test_actual_usd_extraction(self):
        self.assertAlmostEqual(serve._api_actual_usd({'usage': {'usd': 0.0121}}), 0.0121)
        self.assertAlmostEqual(serve._api_actual_usd({'usage': {'cost': 0.004}}), 0.004)
        self.assertIsNone(serve._api_actual_usd({'usage': {}}))
        self.assertAlmostEqual(
            serve._api_actual_usd({}, {'actualUsd': 0.02}), 0.02)

    def test_drift_is_logged_at_most_once_per_day(self):
        self._obs('pixellab', '/create-image-pixflux', 0.0132, 0.01716, n=3)
        with unittest.mock.patch.object(serve, 'log_action') as log:
            self.assertEqual(serve.log_service_price_drift(), 1)
            self.assertEqual(serve.log_service_price_drift(), 0)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 'service_price_drift')

    def test_report_includes_drift_and_source(self):
        with unittest.mock.patch.object(serve, 'service_price_drift_report', return_value=[{
                'service': 'pixellab', 'endpoint': '/create-image-pixflux',
                'samples': 3, 'medianEstimateUsd': 0.0132,
                'medianActualUsd': 0.01716, 'deltaPct': 30.0, 'lastAt': 0}]):
            r = serve.service_price_report('pixellab', '/create-image-pixflux',
                                           {'image_size': {'width': 344, 'height': 192}})
        self.assertEqual(len(r['drift']), 1)
        self.assertIn('pixellab.ai', r['sourceUrl'])


if __name__ == '__main__':
    unittest.main()
