import csv
import hashlib
import datetime as dt
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve
import historical_data as history
import blocker_control
import sim


class HistoricalData(unittest.TestCase):
    def test_collects_actual_rows_filters_and_deduplicates(self):
        start = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        candle = [start, 9, 12, 10, 11, 3]
        with tempfile.TemporaryDirectory() as root:
            result = history.collect('BTC-USD', '2024-01-01', '2024-01-03',
                resolve=lambda p: os.path.join(root, p),
                getter=lambda url: [candle, candle, [start+86400, 10, 13, 11, 12, 4],
                                    [start-86400, 9, 12, 10, 11, 3]])
            self.assertEqual(result['rowCount'], 2)
            self.assertEqual(result['missingDays'], 0)
            with open(os.path.join(root, result['path']), encoding='utf-8') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]['close'], '11.0')
            with open(os.path.join(root, result['path']), 'rb') as stream:
                self.assertEqual(hashlib.sha256(stream.read()).hexdigest(), result['sha256'])
            self.assertTrue(os.path.isfile(os.path.join(root, result['metadataPath'])))

    def test_empty_or_invalid_data_is_not_fabricated(self):
        for data in ([], {'error': 'unavailable'}, [[1704067200, 20, 10, 15, 15, 3]]):
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaises(ValueError):
                    history.collect('BTC-USD', '2024-01-01', '2024-01-03',
                        resolve=lambda p: os.path.join(root, p), getter=lambda url: data)
                self.assertEqual(os.listdir(root), [])

    def test_submit_existing_dataset_reopens_review_without_approval(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'report.md'), 'w') as stream:
                stream.write('Actual historical dataset: data.csv')
            with open(os.path.join(root, 'data.csv'), 'w') as stream:
                stream.write('timestamp,close\n2024-01-01,10\n2024-01-02,11\n')
            task = {'id': 't', 'room': 'observatory', 'assignedTo': 'ben', 'status': 'needs_review',
                    'manualAttention': True, 'instructions': 'Collect historical data.',
                    'acceptanceCriteria': 'Save CSV in Library.', '_peerGate': {'escalated': True}}
            state = {'tasks': {'t': task}, 'workQueue': []}
            fake = SimpleNamespace(_safe_library_path=lambda p: os.path.join(root, p),
                _STATE_UPDATE_LOCK=threading.RLock(), get_state_from_db=lambda: state,
                save_state_to_db=lambda s: None, log_action=lambda *a, **kw: None)
            with patch.object(sim, '_enter_peer_review', side_effect=lambda s, t, n, preferred: t.update(status='needs_review')):
                result = blocker_control.submit_artifact(fake, 't', 'report.md')
            self.assertFalse(result['approved'])
            self.assertTrue(result['checks']['passed'])
            self.assertFalse(task['manualAttention'])
            self.assertEqual(task['_peerGate']['approvals'], 0)
            self.assertEqual(task['libraryPath'], 'report.md')
