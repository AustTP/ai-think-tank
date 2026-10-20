import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import serve  # noqa: F401  (import order avoids the content/serve re-export cycle)
import content


class AcceptanceReview(unittest.TestCase):
    def test_every_criterion_needs_met_status_evidence_and_explanation(self):
        criteria = {'C1': 'Save a CSV', 'C2': 'Explain the source'}
        rows = [{'criterionId': 'C1', 'status': 'met', 'evidenceIds': ['E1'],
                 'explanation': 'The CSV exists.'}]
        assessment = content._assess_acceptance(rows, criteria)
        self.assertFalse(assessment['verified'])
        self.assertTrue(any('C2' in error for error in assessment['errors']))

    def test_complete_assessment_is_verified(self):
        criteria = {'C1': 'Save a CSV'}
        rows = [{'criterionId': 'C1', 'status': 'met', 'evidenceIds': ['E1'],
                 'explanation': 'The CSV exists.'}]
        self.assertTrue(content._assess_acceptance(rows, criteria)['verified'])
