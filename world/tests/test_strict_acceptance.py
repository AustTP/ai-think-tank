import copy
import json
from unittest.mock import patch

import pytest

import review_quality
import sim


REFERENCES = {'criteria': {'C1': 'Integrate the trading wrapper.', 'C2': 'Integration tests pass.'},
    'evidence': {'E1': 'agent.py imports and calls the trading wrapper.',
                 'E2': 'pytest: 12 integration tests passed.'}}


def approval():
    return {'verdict': 'approve', 'summary': 'Required integration and tests are verified.',
        'checks': [], 'risks': [], 'repairStories': [], 'acceptanceChecks': [
            {'criterionId': 'C1', 'status': 'met', 'evidenceIds': ['E1'],
             'explanation': 'The agent invokes the wrapper.'},
            {'criterionId': 'C2', 'status': 'met', 'evidenceIds': ['E2'],
             'explanation': 'The recorded integration tests pass.'}]}


def test_approval_requires_every_criterion_and_resolves_actual_evidence():
    assessment = review_quality.approval_assessment(json.dumps(approval()), REFERENCES)
    assert assessment['verified']
    assert assessment['checks'][0]['evidence'] == [REFERENCES['evidence']['E1']]
    assert assessment['checks'][1]['criterion'] == REFERENCES['criteria']['C2']


def test_reviewer_partial_percentage_cannot_approve_unmet_work():
    data = approval()
    data['acceptanceChecks'][1].update(status='unmet', completionPercent=65)
    result = review_quality.approval_assessment(json.dumps(data), REFERENCES)
    assert not result['verified']
    assert result['checks'][0]['completionPercent'] == 100
    assert result['checks'][1]['completionPercent'] == 65
    data['acceptanceChecks'][1].update(status='unverified', completionPercent=90)
    assert review_quality.approval_assessment(json.dumps(data), REFERENCES)['checks'][1]['completionPercent'] == 0
    data['acceptanceChecks'][1].update(status='unmet', evidenceIds=['FAKE'], completionPercent=90)
    assert review_quality.approval_assessment(json.dumps(data), REFERENCES)['checks'][1]['completionPercent'] == 0


@pytest.mark.parametrize('change', ['missing', 'unmet', 'unverified', 'unknown_evidence',
    'no_evidence', 'duplicate', 'outstanding_repair', 'known_failure'])
def test_model_approve_cannot_override_missing_criteria_or_known_failures(change):
    data = approval()
    if change == 'missing':
        data['acceptanceChecks'].pop()
    elif change in ('unmet', 'unverified'):
        data['acceptanceChecks'][1]['status'] = change
    elif change == 'unknown_evidence':
        data['acceptanceChecks'][1]['evidenceIds'] = ['E999']
    elif change == 'no_evidence':
        data['acceptanceChecks'][1]['evidenceIds'] = []
    elif change == 'duplicate':
        data['acceptanceChecks'].append(copy.deepcopy(data['acceptanceChecks'][0]))
    elif change == 'outstanding_repair':
        data['repairStories'] = [{'title': 'Implement missing integration'}]
    else:
        data['risks'] = ['[ACCEPTANCE_GAP] Required integration is still missing.']
    assert not review_quality.approval_assessment(json.dumps(data), REFERENCES)['verified']


@pytest.mark.parametrize('failure', ['missing_proof', 'red_pipeline', 'artifact_failure',
    'checklist_failure', 'unverified_checklist', 'unresolved_repair', 'blocking_finding'])
def test_completion_gate_refuses_a_clean_vote_with_known_or_unverified_failures(failure):
    parent = {'id': 'p', 'status': 'needs_review', 'taskType': 'research', 'room': 'observatory',
        '_peerGate': {'approvals': 1, 'approvers': ['reviewer-a'], 'reviewerIds': ['reviewer-a', 'reviewer-b'],
                      'enteredMs': 100, 'reviewedTaskIds': [], 'cycleCount': 0}}
    review = {'id': 'r', 'reviewOf': 'p', 'assignedTo': 'reviewer-b', 'taskType': 'review', 'createdAt': 500}
    state = {'tasks': {'p': parent, 'r': review}, 'agents': {}, 'workQueue': []}
    result = {'ok': True, 'pipelineOk': True, 'peerVerdict': 'clean',
        'acceptanceAssessment': review_quality.approval_assessment(json.dumps(approval()), REFERENCES)}
    if failure == 'missing_proof':
        result.pop('acceptanceAssessment')
    elif failure == 'red_pipeline':
        result['pipelineOk'] = False
    elif failure == 'artifact_failure':
        result['reviewDiagnostics'] = {'artifactChecks': {'passed': False, 'errors': ['Required CSV is missing.']}}
    elif failure in ('checklist_failure', 'unverified_checklist'):
        result['checklistGrades'] = [{'verdict': 'fails_requirement' if failure == 'checklist_failure' else 'insufficient_evidence'}]
    elif failure == 'unresolved_repair':
        result['peerRepairStories'] = [{'title': 'Fix failing tests'}]
    else:
        result['peerChecks'] = ['[ACCEPTANCE_GAP] Required integration is missing.']
    with patch.object(sim, '_queue_player_email'), patch.object(sim, '_enter_peer_review'), patch.object(sim, 'queue_work'):
        sim._apply_content_result(state, review, result, 1000)
    assert parent['_peerGate']['approvals'] == 0
    assert parent['_peerGate']['approvers'] == []
    assert parent['status'] != 'done'


def test_fully_verified_clean_vote_can_still_count():
    parent = {'id': 'p', 'status': 'needs_review', 'taskType': 'research', 'room': 'observatory',
        '_peerGate': {'approvals': 0, 'approvers': [], 'reviewerIds': ['reviewer-a'],
                      'enteredMs': 100, 'reviewedTaskIds': [], 'cycleCount': 0}}
    review = {'id': 'r', 'reviewOf': 'p', 'assignedTo': 'reviewer-a', 'taskType': 'review', 'createdAt': 500}
    state = {'tasks': {'p': parent, 'r': review}, 'agents': {}, 'workQueue': []}
    sim._apply_content_result(state, review, {'ok': True, 'pipelineOk': True, 'peerVerdict': 'clean',
        'acceptanceAssessment': review_quality.approval_assessment(json.dumps(approval()), REFERENCES)}, 1000)
    assert parent['_peerGate']['approvals'] == 1


def test_old_approvals_without_acceptance_evidence_are_resubmitted_for_review():
    parent = {'id': 'p', 'status': 'needs_review', '_peerGate': {
        'approvals': 2, 'approvers': ['a', 'b'], 'reviewerIds': ['a', 'b'], 'enteredMs': 100}}
    state = {'tasks': {'p': parent}, 'agents': {}, 'workQueue': []}
    with patch.object(sim, '_reenter_gate_review') as review:
        assert not sim._parent_close_from_vote(state, parent, 1000)
    review.assert_called_once()
    assert parent['_peerGate']['approvals'] == 0
    assert parent['status'] != 'done'


def test_direct_completion_cannot_bypass_missing_approval_evidence():
    parent = {'id': 'p', 'status': 'needs_review', '_peerGate': {'approvals': 2, 'approvers': ['a', 'b']}}
    assert not sim._close_gated_story({'tasks': {'p': parent}}, parent)
    assert parent['status'] == 'needs_review'


def test_stale_unverified_review_cannot_erase_current_verified_votes():
    parent = {'id': 'p', 'status': 'needs_review', '_peerGate': {'approvals': 1,
        'approvers': ['a'], 'reviewerIds': ['a', 'b'], 'enteredMs': 100, 'acceptanceVerified': True}}
    review = {'id': 'old', 'reviewOf': 'p', 'taskType': 'review', 'assignedTo': 'b', 'createdAt': 50}
    state = {'tasks': {'p': parent, 'old': review}, 'agents': {}, 'workQueue': []}
    sim._apply_content_result(state, review, {'peerVerdict': 'clean', 'pipelineOk': True}, 1000)
    assert parent['_peerGate']['approvals'] == 1
    assert parent['_peerGate']['approvers'] == ['a']
