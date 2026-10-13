import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prepare_unsloth_dataset as exporter


class DatasetTests(unittest.TestCase):
    def test_credentials_are_removed(self):
        text = 'ghp_' + 'a' * 36 + ' Bearer ' + 'b' * 24 + ' api_key=abcdefghijk local-private-value'
        result = exporter.redact(text, ['local-private-value'])
        self.assertNotIn('ghp_', result)
        self.assertNotIn('abcdefghijk', result)
        self.assertNotIn('local-private-value', result)
        self.assertEqual(exporter.redact('task-2174-data-collection.md', []), 'task-2174-data-collection.md')

    def test_approval_evidence_dedup_and_group_isolation(self):
        def row(index, group, response=None, status='approved'):
            return {'id': str(index), 'group_id': group, 'status': status,
                    'instruction': 'Task ' + str(index), 'evidence': 'Verified excerpt from source A',
                    'response': response or ('Report ' + str(index))}
        rows = [row(1, 'story-a', 'Shared report'), row(2, 'story-b', 'Shared report'),
                row(3, 'story-b'), row(4, 'story-c'), row(5, 'story-d', status='pending')]
        rows.append(dict(rows[0]))
        rows.append(row(6, 'story-e', 'api_key=abcdefghijk'))
        with tempfile.TemporaryDirectory() as directory, patch.object(exporter, 'secret_values', return_value=[]):
            out = Path(directory)
            reviewed = out / 'reviewed.jsonl'
            exporter.write_jsonl(reviewed, rows)
            summary = exporter.build(reviewed, out, 0.5, 40000)
            manifest = [json.loads(line) for line in (out / 'manifest.jsonl').read_text().splitlines()]
            self.assertEqual(summary['train'] + summary['eval'], 4)
            self.assertEqual(summary['skipped']['not_approved'], 1)
            self.assertEqual(summary['skipped']['redacted_example_requires_repair'], 1)
            self.assertEqual(summary['skipped']['exact_duplicate'], 1)
            locations = {entry['id']: entry['split'] for entry in manifest}
            self.assertEqual(locations['1'], locations['2'])
            self.assertEqual(locations['2'], locations['3'])
            self.assertNotEqual(locations['3'], locations['4'])
            rows[0]['evidence'] = ''
            exporter.write_jsonl(reviewed, rows)
            with self.assertRaisesRegex(ValueError, 'needs instruction, evidence'):
                exporter.build(reviewed, out, 0.1, 40000)


if __name__ == '__main__':
    unittest.main()
