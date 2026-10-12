import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_director
import content
import sim


def fixture():
    state = {'agentRoster': [{'id': 'author'}, {'id': 'peer-a'}, {'id': 'peer-b'}],
             'agents': {a: {'id': a, 'offDuty': True} for a in ['author', 'peer-a', 'peer-b']},
             'tasks': {}, 'workQueue': []}
    code_director.create_director(state)
    task = {'id': 'parent', 'title': 'Implement API', 'assignedTo': 'author', 'room': 'pressoffice',
            'taskType': 'code', 'status': 'working', 'acceptanceCriteria': 'Tests pass', 'projectLabel': 'API'}
    state['tasks']['parent'] = task
    return state, task


class CodeDirector(unittest.TestCase):
    def test_director_is_created_once_and_reserved_from_delivery_and_review(self):
        state, _ = fixture()
        original = copy.deepcopy(state)
        self.assertEqual(code_director.create_director(state), 'code-director')
        self.assertEqual(state, original)
        self.assertNotIn('code-director', sim._eligible_candidates(state, True))
        self.assertNotIn('code-director', sim._pick_reviewer_ids(state, 'author'))

    def test_code_waits_for_director_before_any_peer_review(self):
        state, parent = fixture()
        sim._enter_peer_review(state, parent, 1000)
        self.assertEqual(parent['status'], 'blocked')
        self.assertEqual(parent['blockedReason'], 'code_director_check')
        self.assertEqual(len(state['workQueue']), 1)
        check = state['workQueue'][0]
        self.assertEqual(check['taskType'], 'code_preflight')
        self.assertEqual(check['assignedTo'], 'code-director')
        self.assertEqual(check['reviewAuthorId'], 'author')
        sim._enter_peer_review(state, parent, 1001)
        self.assertEqual(len(state['workQueue']), 1)

    def test_green_pipeline_opens_independent_reviews(self):
        state, parent = fixture()
        code_director.queue_check(state, parent)
        state['workQueue'] = []
        self.assertTrue(code_director.complete_check(state, {'id': 'check', 'reviewOf': 'parent'},
            {'ok': True, 'pipelineOk': True, 'evidence': 'pytest 10 passed, coverage 100%'}, 2000))
        self.assertEqual(parent['status'], 'needs_review')
        self.assertEqual(len(state['workQueue']), 2)
        self.assertNotIn('code-director', parent['_peerGate']['reviewerIds'])

    def test_missing_evidence_or_red_pipeline_cannot_open_review(self):
        for result in [{'ok': True, 'pipelineOk': True},
                       {'ok': True, 'pipelineOk': False, 'evidence': 'tests failed'},
                       {'ok': False, 'pipelineOk': True, 'evidence': 'output'}]:
            state, parent = fixture()
            code_director.queue_check(state, parent)
            self.assertFalse(code_director.complete_check(state, {'id': 'check', 'reviewOf': 'parent'}, result, 2000))
            self.assertEqual(parent['status'], 'blocked')

    def test_document_work_does_not_get_code_preflight(self):
        state, parent = fixture()
        parent.update(room='observatory', taskType='research')
        sim._enter_peer_review(state, parent, 1000)
        self.assertTrue(all(q['taskType'] == 'review' for q in state['workQueue']))

    def test_frozen_work_does_not_restart_automatically(self):
        state, parent = fixture()
        parent['_peerGate'] = {'escalated': True}
        self.assertIsNone(code_director.queue_check(state, parent))
        self.assertEqual(state['workQueue'], [])

    def test_green_existing_code_needs_no_model_repair(self):
        state, _ = fixture()
        with patch.object(content, '_run_quality_pipeline', return_value={'ok': True,
                    'results': [{'name': 'pytest-cov', 'stdout': '10 passed, coverage 100%'}]}), \
                patch.object(content, '_run_coding_content') as repair, \
                patch.object(content._serve, 'get_or_create_agent_key', return_value='test'), \
                patch.object(sim, '_store_content_result') as store:
            content._run_code_director_content(state, 'code-director', {'id': 'check'})
        repair.assert_not_called()
        self.assertTrue(store.call_args.args[1]['pipelineOk'])
        self.assertIn('10 passed', store.call_args.args[1]['evidence'])

    def test_failed_check_repairs_correct_product_sandbox_with_actual_errors(self):
        state, _ = fixture()
        state['products'] = {'product': {'sandboxId': 'product-work'}}
        with patch.object(content, '_run_quality_pipeline', return_value={'ok': False, 'failedStep': 'flake8', 'note': 'flake8 failed: module.py E999 SyntaxError',
                    'results': [{'name': 'flake8', 'exitCode': 1, 'stderr': 'module.py E999 SyntaxError'}]}), \
                patch.object(content, '_run_coding_content') as repair, \
                patch.object(content._serve, 'get_or_create_agent_key', return_value='test'):
            content._run_code_director_content(state, 'code-director', {'id': 'check', 'productId': 'product'})
        self.assertEqual(repair.call_args.args[2]['_sandboxId'], 'product-work')
        self.assertIn('SyntaxError', repair.call_args.kwargs['base_ctx'])


if __name__ == '__main__':
    unittest.main()
