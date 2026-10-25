import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import serve  # noqa: F401  (import order avoids the content/serve re-export cycle)
import content
import sim


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

    def test_clean_vote_with_complete_acceptance_counts(self):
        criteria = {'C1': 'Save a CSV'}
        rows = [{'criterionId': 'C1', 'status': 'met', 'evidenceIds': ['E1'],
                 'explanation': 'The CSV exists.'}]
        assessment = content._assess_acceptance(rows, criteria)
        state, story = self._gated()
        review = {'id': 'rev-1', 'reviewOf': story['id'], 'assignedTo': 'ada',
                  'taskType': 'review', 'status': 'working'}
        sim._apply_content_result(state, review, {
            'note': 'approved', 'peerVerdict': 'clean', 'pipelineOk': True,
            'evidence': '[flake8]\nno issues\n[pytest-cov]\nTOTAL 100%',
            'acceptanceAssessment': assessment})
        self.assertEqual(story['_peerGate']['approvals'], 1)
        self.assertEqual(story['_peerGate']['approvers'], ['ada'])

    def test_blocking_finding_blocks_clean_vote_without_criteria(self):
        state, story = self._gated()
        review = {'id': 'rev-1', 'reviewOf': story['id'], 'assignedTo': 'ada',
                  'taskType': 'review', 'status': 'working'}
        sim._apply_content_result(state, review, {
            'note': 'defect', 'peerVerdict': 'clean', 'pipelineOk': True,
            'evidence': '[flake8]\nno issues\n[pytest-cov]\nTOTAL 100%',
            'peerChecks': ['[FUNCTIONAL_DEFECT] core loop drops input']})
        self.assertEqual(story['_peerGate']['approvals'], 0)
        self.assertEqual(story['_peerGate']['approvers'], [])

    def _gated(self):
        story = {'id': 'story-1', 'title': 'Build checkout', 'room': 'pressoffice',
                 'assignedTo': 'ben', 'projectLabel': 'storefront', 'taskType': 'code'}
        state = {'sim': {'owner': 'server'}, 'agentRoster': [
            {'id': 'maya', 'name': 'Maya', 'role': 'director', 'isAdmin': True},
            {'id': 'ada', 'name': 'Ada', 'role': 'engineer', 'director': 'maya'},
            {'id': 'ben', 'name': 'Ben', 'role': 'engineer', 'director': 'maya'},
            {'id': 'cora', 'name': 'Cora', 'role': 'engineer', 'director': 'maya'},
        ], 'agents': {
            'maya': {'id': 'maya', 'busy': True},
            'ada': {'id': 'ada', 'busy': False, 'offDuty': True, 'task': None},
            'ben': {'id': 'ben', 'busy': False, 'offDuty': True, 'task': None},
            'cora': {'id': 'cora', 'busy': False, 'offDuty': True, 'task': None},
        }, 'workQueue': [], 'tasks': {story['id']: story}, 'reports': []}
        sim._enter_peer_review(state, story, now_ms=1000)
        return state, story
