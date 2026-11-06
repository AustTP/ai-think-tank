"""Hermetic tests for the Stakeholder Graph (world/core/stakeholders.py).

Seam-patched like the other ledger tests: the graph row and the audit trail
are swapped for in-memory structures. The safety property under test
throughout: a person record must NEVER become a place where a credential
lands -- every free-text field is redacted on write -- and person identity is
case-insensitive (one person, one row).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.stakeholders as stakeholders  # noqa: E402


def _patch_graph(rows=None):
    store = {'graph': rows if rows is not None else {}}
    patchers = [
        unittest.mock.patch('core.stakeholders._graph_read',
                            lambda: store['graph']),
        unittest.mock.patch('core.stakeholders._graph_write',
                            lambda rows: store.__setitem__('graph', rows)),
        unittest.mock.patch('core.stakeholders._audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return store


class RecordPerson(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_creates_person(self):
        _patch_graph()
        rec = stakeholders.record_person(
            'Brian Barrett', team='CloudSec',
            capabilities=['SIEM detections', 'Guardrail logging'])
        self.assertTrue(rec['id'].startswith('st-'))
        self.assertEqual(rec['team'], 'CloudSec')
        self.assertEqual(rec['capabilities'], ['SIEM detections', 'Guardrail logging'])

    def test_name_case_is_one_person(self):
        _patch_graph()
        a = stakeholders.record_person('Brian Barrett', known_topics=['SIEM'])
        b = stakeholders.record_person('brian barrett', known_topics=['IR'])
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(b['known_topics'], ['SIEM', 'IR'])

    def test_merge_is_additive_never_wipes(self):
        _patch_graph()
        stakeholders.record_person('Elias', capabilities=['AICP architecture'])
        rec = stakeholders.record_person('Elias', capabilities=['Cloud platforms'])
        self.assertEqual(rec['capabilities'], ['AICP architecture', 'Cloud platforms'])

    def test_merge_dedupes_items(self):
        _patch_graph()
        stakeholders.record_person('Elias', decisions=['owns AICP roadmap'])
        rec = stakeholders.record_person('Elias', decisions=['owns AICP roadmap'])
        self.assertEqual(rec['decisions'], ['owns AICP roadmap'])

    def test_name_required(self):
        _patch_graph()
        with self.assertRaises(ValueError):
            stakeholders.record_person('   ')

    def test_unknown_person_is_none_not_a_guess(self):
        _patch_graph()
        self.assertIsNone(stakeholders.get_person('Nobody Named'))


class Redaction(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_known_topics_scrub_credentials(self):
        _patch_graph()
        rec = stakeholders.record_person(
            'Careless',
            known_topics=['password is hunter2isasecret', 'SIEM'])
        self.assertNotIn('hunter2isasecret', ' '.join(rec['known_topics']))
        self.assertIn('[REDACTED]', ' '.join(rec['known_topics']))

    def test_notes_scrub_connection_string(self):
        _patch_graph()
        rec = stakeholders.record_person(
            'Leaky',
            notes='uses postgres://user:secret@host/db for the export')
        self.assertNotIn('secret', rec['notes'])
        self.assertIn('[REDACTED]', rec['notes'])

    def test_redact_pure_function(self):
        self.assertEqual(
            stakeholders._redact_secrets('api_key = AKIAIOSFODNN7EXAMPLE'),
            '[REDACTED]')
        self.assertEqual(stakeholders._redact_secrets('plain text'), 'plain text')
        self.assertEqual(stakeholders._redact_secrets(''), '')


class Routing(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_persons_knowing_matches_capabilities_and_topics(self):
        _patch_graph()
        stakeholders.record_person('Brian Barrett', capabilities=['SIEM detections'])
        stakeholders.record_person('Elias', known_topics=['Agent identity'])
        stakeholders.record_person('Unrelated', capabilities=['Weather'])
        hits = [p['name'] for p in stakeholders.persons_knowing('siem')]
        self.assertEqual(hits, ['Brian Barrett'])
        self.assertEqual([p['name'] for p in stakeholders.persons_knowing('identity')],
                         ['Elias'])

    def test_absence_is_explicit_empty(self):
        _patch_graph()
        stakeholders.record_person('Brian Barrett', capabilities=['SIEM'])
        self.assertEqual(stakeholders.persons_knowing('kubernetes'), [])

    def test_blank_topic_returns_empty(self):
        _patch_graph()
        self.assertEqual(stakeholders.persons_knowing('   '), [])

    def test_all_persons_sorted(self):
        _patch_graph()
        stakeholders.record_person('Zack')
        stakeholders.record_person('Adele')
        self.assertEqual(
            [p['name'] for p in stakeholders.all_persons()], ['Adele', 'Zack'])


if __name__ == '__main__':
    unittest.main()
