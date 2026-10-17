"""Pre-refinement comprehension (world/comprehension.py).

Covers the deterministic read of a backlogRequests record
(summary, player pins, readiness, gaps), the reader
override and its fallbacks, batch comprehension of a
pending refinement set, and the state-safe dict form.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comprehension  # noqa: E402

READY_REQ = {
    'id': 'r1',
    'title': 'Add CSV export to the research desk',
    'reason': 'Researchers need to take their findings into '
              'spreadsheets for the weekly report.',
    'filedBy': 'grace',
    'room': 'research-desk',
    'userStory': 'As a researcher, I want CSV export.',
    'acceptanceCriteria': 'Export button writes a .csv file.',
}


class TestComprehend(unittest.TestCase):

    def test_ready_request(self):
        comp = comprehension.comprehend(READY_REQ)
        self.assertEqual(comp.request_id, 'r1')
        self.assertEqual(comp.title, READY_REQ['title'])
        self.assertEqual(comp.requester, 'grace')
        self.assertEqual(comp.room, 'research-desk')
        self.assertEqual(comp.readiness, comprehension.READY)
        self.assertEqual(comp.gaps, [])
        self.assertEqual(comp.constraints, [])

    def test_summary_starts_with_title(self):
        comp = comprehension.comprehend(READY_REQ)
        self.assertTrue(comp.summary.startswith(READY_REQ['title']))
        self.assertIn('spreadsheets', comp.summary)

    def test_summary_truncated_on_word_boundary(self):
        req = dict(READY_REQ, reason='word ' * 80 + '.')
        comp = comprehension.comprehend(req)
        self.assertLessEqual(len(comp.summary),
                             comprehension.SUMMARY_MAX_CHARS)
        self.assertTrue(comp.summary.endswith('...'))

    def test_missing_title_needs_clarification(self):
        req = dict(READY_REQ, title='')
        comp = comprehension.comprehend(req)
        self.assertEqual(comp.readiness,
                         comprehension.NEEDS_CLARIFICATION)
        self.assertIn('title', comp.gaps)

    def test_missing_reason_needs_clarification(self):
        req = dict(READY_REQ, reason='')
        comp = comprehension.comprehend(req)
        self.assertEqual(comp.readiness,
                         comprehension.NEEDS_CLARIFICATION)
        self.assertIn('reason', comp.gaps)

    def test_thin_reason_needs_clarification(self):
        req = dict(READY_REQ, reason='too thin')
        comp = comprehension.comprehend(req)
        self.assertEqual(comp.readiness,
                         comprehension.NEEDS_CLARIFICATION)
        self.assertTrue(any('under' in gap for gap in comp.gaps))

    def test_missing_contract_fields_are_gaps(self):
        req = {k: v for k, v in READY_REQ.items()
               if k not in ('userStory', 'acceptanceCriteria')}
        comp = comprehension.comprehend(req)
        # Title and reason are present, so the request is
        # ready; the missing contract fields are still gaps.
        self.assertEqual(comp.readiness, comprehension.READY)
        self.assertIn('userStory', comp.gaps)
        self.assertIn('acceptanceCriteria', comp.gaps)

    def test_player_pins_become_constraints(self):
        req = dict(READY_REQ, playerConstraints={
            'budgetUsd': 100,
            'constraints': ['no paid ads'],
            'roles': ['coder'],
            'mustInclude': ['tests'],
        })
        comp = comprehension.comprehend(req)
        self.assertEqual(len(comp.constraints), 4)
        self.assertTrue(any('$100.00' in pin for pin in comp.constraints))
        self.assertTrue(any('no paid ads' in pin for pin in comp.constraints))
        self.assertTrue(any('coder' in pin for pin in comp.constraints))
        self.assertTrue(any('tests' in pin for pin in comp.constraints))

    def test_empty_player_constraints(self):
        comp = comprehension.comprehend(
            dict(READY_REQ, playerConstraints={}))
        self.assertEqual(comp.constraints, [])
        comp = comprehension.comprehend(
            dict(READY_REQ, playerConstraints='nope'))
        self.assertEqual(comp.constraints, [])

    def test_reader_overrides_summary_and_readiness(self):
        def reader(req):
            return {'summary': 'LLM-written digest of the work.',
                    'readiness': 'blocked'}
        comp = comprehension.comprehend(READY_REQ, reader=reader)
        self.assertEqual(comp.summary, 'LLM-written digest of the work.')
        self.assertEqual(comp.readiness, 'blocked')

    def test_reader_partial_override_falls_back(self):
        def reader(req):
            return {'summary': 'Only the summary.'}
        comp = comprehension.comprehend(READY_REQ, reader=reader)
        self.assertEqual(comp.summary, 'Only the summary.')
        # Readiness was left out, so the deterministic read
        # of the title and reason decides.
        self.assertEqual(comp.readiness, comprehension.READY)

    def test_blank_reader_fields_are_ignored(self):
        def reader(req):
            return {'summary': '   ', 'readiness': ''}
        comp = comprehension.comprehend(READY_REQ, reader=reader)
        self.assertTrue(comp.summary.startswith(READY_REQ['title']))
        self.assertEqual(comp.readiness, comprehension.READY)

    def test_non_dict_record_reads_as_empty(self):
        comp = comprehension.comprehend(None)
        self.assertEqual(comp.request_id, '')
        self.assertEqual(comp.readiness,
                         comprehension.NEEDS_CLARIFICATION)
        self.assertIn('title', comp.gaps)

    def test_to_dict_round_trip(self):
        comp = comprehension.comprehend(READY_REQ)
        as_dict = comp.to_dict()
        self.assertEqual(as_dict['requestId'], 'r1')
        self.assertEqual(as_dict['readiness'], comprehension.READY)
        self.assertEqual(as_dict['gaps'], [])
        self.assertEqual(as_dict['constraints'], [])


class TestComprehendBatch(unittest.TestCase):

    def test_batch_keyed_by_id(self):
        second = dict(READY_REQ, id='r2', title='Second card')
        batch = comprehension.comprehend_batch([READY_REQ, second])
        self.assertEqual(sorted(batch), ['r1', 'r2'])
        self.assertEqual(batch['r2'].title, 'Second card')

    def test_batch_skips_malformed_entries(self):
        batch = comprehension.comprehend_batch(
            [READY_REQ, 'not-a-record', {}, {'title': 'no id'}])
        self.assertEqual(list(batch), ['r1'])

    def test_batch_empty(self):
        self.assertEqual(comprehension.comprehend_batch([]), {})
        self.assertEqual(comprehension.comprehend_batch(None), {})

    def test_batch_passes_reader_through(self):
        def reader(req):
            return {'readiness': 'needs-clarification'}
        batch = comprehension.comprehend_batch([READY_REQ], reader=reader)
        self.assertEqual(batch['r1'].readiness, 'needs-clarification')


class TestNeedsClarification(unittest.TestCase):

    def test_ready_is_not_clarification(self):
        self.assertFalse(
            comprehension.needs_clarification(
                comprehension.comprehend(READY_REQ)))

    def test_unready_is_clarification(self):
        comp = comprehension.comprehend(dict(READY_REQ, title=''))
        self.assertTrue(comprehension.needs_clarification(comp))


if __name__ == '__main__':
    unittest.main()
