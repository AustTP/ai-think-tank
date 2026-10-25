import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.model_config as model_config


class ModelConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = model_config.load_bands(
            Path(__file__).resolve().parent.parent.parent / 'model_bands.json')

    def test_shipped_config_is_consistent(self):
        problems, _ = model_config.validate(self.config)
        self.assertEqual(problems, [])

    def test_missing_default_gap_is_reported(self):
        config = json.loads(json.dumps(self.config))
        config['quality_floor_gap'].pop('default', None)
        problems, _ = model_config.validate(config)
        self.assertTrue(any('quality_floor_gap.default' in p for p in problems))

    def test_alias_band_is_valid_without_a_tier_row(self):
        config = json.loads(json.dumps(self.config))
        self.assertEqual(config['band_aliases']['3d'], 'coding')
        problems, _ = model_config.validate(config)
        self.assertEqual(problems, [])

    def test_alias_cycle_and_unknown_target_are_reported(self):
        config = json.loads(json.dumps(self.config))
        config['band_aliases'] = {'3d': '3d'}
        problems, _ = model_config.validate(config)
        self.assertTrue(any('alias points at itself' in p for p in problems))
        config['band_aliases'] = {'3d': 'nope'}
        problems, _ = model_config.validate(config)
        self.assertTrue(any('alias target' in p for p in problems))

    def test_overlapping_price_bands_are_reported(self):
        config = json.loads(json.dumps(self.config))
        config['price_bands']['mid'] = [0.4, 5]
        problems, _ = model_config.validate(config)
        self.assertTrue(any('overlap' in p for p in problems))

    def test_database_bands_and_fallback_ranks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(str(Path(tmp) / 'state.db'))
            self.addCleanup(db.close)
            db.execute('CREATE TABLE model_tiers (band TEXT PRIMARY KEY, '
                       'slug TEXT, name TEXT, price_per_m REAL, chosen_at REAL)')
            db.execute('CREATE TABLE model_tier_fallbacks (band TEXT, rank INTEGER, '
                       'slug TEXT, name TEXT, price_per_m REAL, chosen_at REAL)')
            for band in model_config.BANDS:
                db.execute('INSERT INTO model_tiers VALUES (?, ?, ?, ?, 0)',
                           (band, f'provider/{band}', band, 1.0))
            db.execute("INSERT INTO model_tier_fallbacks VALUES "
                       "('low', 2, 'provider/x', 'x', 1, 0)")
            db.commit()
            problems, rows = model_config.validate(self.config, Path(tmp) / 'state.db')
            self.assertEqual(len(rows), len(model_config.BANDS))
            self.assertTrue(any('fallback ranks' in p for p in problems))


if __name__ == '__main__':
    unittest.main()
