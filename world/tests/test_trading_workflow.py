import copy
import json
import os
import sys
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trading_workflow as flow
import blocker_control as blockers


class Workflow(unittest.TestCase):
    def test_scope_decision_removes_old_instructions_and_queues_measurable_repair(self):
        state = {'tasks': {'t': {'id': 't', 'title': 'Brief stakeholders', 'status': 'needs_review',
            'manualAttention': True, 'room': 'observatory', 'taskType': 'research', 'assignedTo': 'maya',
            'instructions': 'Focus on weatherstation', '_peerGate': {'escalated': True}}},
            'agentRoster': [{'id': 'maya'}], 'agents': {'maya': {'id': 'maya'}}, 'workQueue': []}
        fake = SimpleNamespace(_STATE_UPDATE_LOCK=threading.RLock(), get_state_from_db=lambda: state,
                               save_state_to_db=lambda s: None, log_action=lambda *a, **k: None)
        blockers.apply_action(fake, 't', {'action': 'clarify_scope', 'guidance': 'Document Coinbase spot scope.',
                            'acceptanceCriteria': 'Cite the agreed $50 limit and four role handoffs.'})
        repair = state['workQueue'][0]
        self.assertNotIn('weatherstation', repair['instructions'])
        self.assertEqual(repair['taskType'], 'research')
        self.assertIn('$50', repair['acceptanceCriteria'])

    def test_infrastructure_retry_refuses_to_queue_work_when_docker_is_down(self):
        fake = SimpleNamespace()
        with patch.object(blockers.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, 'Docker is still unavailable'):
                blockers.apply_action(fake, 't', {'action': 'retry_infrastructure'})

    def test_start_is_idempotent_and_pins_research_to_aria(self):
        state = {'tasks': {}, 'workQueue': []}
        self.assertTrue(flow.start(state))
        self.assertFalse(flow.start(state))
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'crypto-researcher')

    def test_incomplete_report_cannot_advance_even_if_marked_done(self):
        state = {'tasks': {}, 'workQueue': []}
        flow.start(state)
        state['workQueue'] = []
        state['tasks']['t'] = {'id': 't', 'tradingStage': 'research', 'status': 'done', 'libraryPath': 'incomplete.md'}
        flow.advance(state)
        self.assertEqual(state['tradingWorkflow']['phase'], 'blocked')
        self.assertEqual(state['tasks']['t']['status'], 'blocked')
        self.assertEqual(state['workQueue'], [])

    def test_accepted_research_queues_strategy_once_and_supplies_report(self):
        state = {'tasks': {}, 'workQueue': []}
        flow.start(state)
        state['workQueue'] = []
        state['tasks']['t'] = {'id': 't', 'tradingStage': 'research', 'status': 'done',
            'libraryPath': 'research.md', 'tradingAcceptance': {'accepted': True, 'manifest': {'pairs': ['BTC-USD']}}}
        flow.advance(state)
        flow.advance(state)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'strategy-analyst')
        self.assertIn('research.md', state['workQueue'][0]['instructions'])

    def test_research_requires_observed_quotes_and_candles(self):
        manifest = {'pairs': ['BTC-USD'], 'sources': ['https://api.coinbase.com'], 'liquidity': 'Measured', 'dataQuality': 'Checked'}
        report = '```json\n' + json.dumps(manifest) + '\n```'
        self.assertFalse(flow.validate_report('research', report, [])['accepted'])
        observed = [{'tool': 'coinbase_spot_market', 'productId': 'BTC-USD'},
                    {'tool': 'coinbase_spot_candles', 'productId': 'BTC-USD', 'count': 100}]
        self.assertTrue(flow.validate_report('research', report, observed)['accepted'])

    def test_backtest_metrics_must_match_actual_execution_and_held_out_windows(self):
        metrics = {'trainStart': 1, 'trainEnd': 2, 'testStart': 3, 'testEnd': 4,
                   'trades': 10, 'netPnlUsd': -2, 'maxDrawdownUsd': 4, 'feesBps': 60, 'slippageBps': 10}
        manifest = {'pairs': ['BTC-USD'], 'allocationUsd': 100, 'maxOrderUsd': 10,
                    'entryRules': ['Enter'], 'exitRules': ['Exit'], 'noTradeConditions': ['Stale data'], 'backtest': metrics}
        report = '```json\n' + json.dumps(manifest) + '\n```'
        prior = {'research': {'pairs': ['BTC-USD']}}
        self.assertFalse(flow.validate_report('strategy', report, [], prior)['accepted'])
        observed = flow.execution_observations(json.dumps({'backtest': metrics}))
        self.assertTrue(flow.validate_report('strategy', report, observed, prior)['accepted'])
        bad = copy.deepcopy(manifest)
        bad['backtest']['testStart'] = 1
        report = '```json\n' + json.dumps(bad) + '\n```'
        self.assertFalse(flow.validate_report('strategy', report, observed, prior)['accepted'])

    def test_cancelled_workflow_never_restarts_itself(self):
        state = {'tradingWorkflow': {'version': 1, 'phase': 'cancelled'}}
        flow.advance(state)
        self.assertNotIn('workQueue', state)

    def test_dismissed_task_resolves_notice_and_is_not_a_decision(self):
        state = {'tasks': {'t': {'id': 't', 'title': 'Old', 'status': 'failed', 'manualAttention': True,
                                'boardClearedAt': 1}},
                 'playerInbox': [{'kind': 'review_blocked', 'taskId': 't', 'status': 'awaiting_guidance'}]}
        blockers.reconcile_notices(state)
        self.assertEqual(blockers.cards(state), [])
        self.assertEqual(state['playerInbox'][0]['status'], 'resolved')

    def test_decision_contains_reason_evidence_owner_and_recommended_action(self):
        state = {'tasks': {'t': {'id': 't', 'title': 'Code', 'assignedTo': 'leo', 'status': 'failed',
                                'manualAttention': True, 'failNote': 'Docker daemon unavailable'}}}
        blockers.reconcile_notices(state)
        blockers.reconcile_notices(state)
        self.assertEqual(len(state['playerInbox']), 1)
        card = blockers.cards(state)[0]
        self.assertEqual(card['category'], 'infrastructure')
        self.assertEqual(card['recommendedAction'], 'retry_infrastructure')
        self.assertEqual(card['owner'], 'leo')


if __name__ == '__main__':
    unittest.main()
