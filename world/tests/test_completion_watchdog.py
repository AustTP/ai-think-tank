import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import completion_watchdog as watchdog
import sim
import task_master


def seed(status='recovery_planned'):
    return {'tasks': {'p': {'id': 'p', 'title': 'Deliver report', 'status': status,
                            'assignedTo': 'ada', 'originalAssignedTo': 'ada',
                            'taskType': 'research', 'room': 'observatory',
                            'acceptanceCriteria': 'Save sourced findings',
                            'investigationReport': 'archive/diagnosis.md'}},
            'agents': {'ada': {}, 'iris': {}},
            'agentRoster': [{'id': 'ada'}, {'id': 'iris'}],
            'teams': [{'id': 'team', 'scrumMasterId': 'iris', 'members': ['ada']}],
            'workQueue': []}


class CompletionWatchdog(unittest.TestCase):
    def test_completed_investigation_repair_closes_only_investigation(self):
        state = seed('needs_review')
        state['tasks'].update({
            'i': {'id':'i', 'status':'repairing', 'investigationOf':'p'},
            'r': {'id':'r', 'status':'done', 'reviewOf':'i', 'taskType':'spike', 'libraryPath':'archive/report.md'}})
        watchdog.step(state, 100000)
        self.assertEqual(state['tasks']['i']['status'], 'done')
        self.assertEqual(state['tasks']['p']['status'], 'needs_review')

    def test_later_saved_investigation_supersedes_stalled_old_one(self):
        state = seed('needs_review')
        state['tasks'].update({
            'i': {'id':'i', 'status':'investigating', 'investigationOf':'p', 'createdAt':1},
            'n': {'id':'n', 'status':'done', 'investigationOf':'p', 'createdAt':2, 'libraryPath':'archive/new.md'}})
        watchdog.step(state, 100000)
        self.assertEqual(state['tasks']['i']['status'], 'cancelled')
        self.assertEqual(state['tasks']['i']['supersededBy'], 'n')
        self.assertEqual(state['tasks']['p']['status'], 'needs_review')

    def test_plan_queues_original_repair_once_with_backoff(self):
        state = seed()
        watchdog.step(state, 100000)
        task = state['tasks']['p']
        self.assertEqual(task['status'], 'repairing')
        item = state['workQueue'][0]
        self.assertEqual(item['reviewOf'], 'p')
        self.assertEqual(item['notBefore'], 130000)
        self.assertIn('archive/diagnosis.md', item['instructions'])
        watchdog.step(state, 100001)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(task['completionRecovery']['attempts'], 1)

    def test_orphaned_repair_recovers_but_stops_after_limit(self):
        state = seed('repairing')
        for now in (100000, 200000, 400000):
            watchdog.step(state, now)
            self.assertEqual(len(state['workQueue']), 1)
            state['workQueue'] = []
        watchdog.step(state, 800000)
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertTrue(state['tasks']['p']['manualAttention'])
        self.assertEqual(state['workQueue'], [])

    def test_pause_budget_and_active_child_are_respected(self):
        for field in ('manualAttention', 'budgetExhausted'):
            state = seed()
            state['tasks']['p'][field] = True
            watchdog.step(state, 100000)
            self.assertEqual(state['workQueue'], [])
        state = seed('repairing')
        state['tasks']['c'] = {'id': 'c', 'reviewOf': 'p', 'status': 'working'}
        watchdog.step(state, 100000)
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['tasks']['p']['deliveryState'], 'actively_working')

    def test_failed_investigation_can_be_staffed_again(self):
        state = seed('investigating')
        state['tasks']['p'].update(recoveryOwner='iris', recoveryStaffed=True)
        state['tasks']['c'] = {'id': 'c', 'investigationOf': 'p', 'status': 'failed'}
        watchdog.step(state, 100000)
        self.assertEqual(state['workQueue'][0]['investigationOf'], 'p')
        self.assertEqual(state['workQueue'][0]['assignedTo'], 'ada')

    def test_completed_investigation_continues_to_repair(self):
        state = seed('investigating')
        state['tasks']['c'] = {'id': 'c', 'investigationOf': 'p', 'status': 'done',
                               'libraryPath': 'archive/actual.md'}
        watchdog.step(state, 100000)
        self.assertEqual(state['tasks']['p']['status'], 'repairing')
        self.assertIn('archive/actual.md', state['workQueue'][0]['instructions'])

    def test_bad_review_retries_review_without_repair(self):
        state = seed()
        state['tasks']['p']['reviewFailureKind'] = 'invalid_response'
        with patch.object(sim, '_enter_peer_review', side_effect=lambda s,t,n: sim.queue_work(s,[
                {'title':'Review', 'taskType':'review', 'reviewOf':t['id']}])) as enter, \
                patch.object(sim, '_queue_peer_repair') as repair:
            watchdog.step(state, 100000)
        enter.assert_called_once()
        repair.assert_not_called()
        self.assertEqual(state['tasks']['p']['status'], 'needs_review')

    def test_investigation_cannot_reset_exhausted_review_format_failures(self):
        state = seed()
        state['tasks']['p'].update(reviewFailureKind='invalid_response', _peerGate={'formatFailures':3})
        with patch.object(sim, '_enter_peer_review') as enter, patch.object(sim, '_queue_peer_repair') as repair:
            watchdog.step(state, 100000)
        enter.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        self.assertEqual(state['tasks']['p']['_peerGate']['formatFailures'], 3)

    def test_completion_priority_and_wip_cap(self):
        state = seed('working')
        state['completionControl'] = {'enabled': True, 'maxConcurrentStories': 1}
        items = [{'title':'New exploration', 'priority':3},
                 {'title':'Repair', 'reviewOf':'p', 'priority':1}]
        self.assertFalse(watchdog.can_start(state, items[0]))
        self.assertEqual(sim.pick_next_due_index(items, 100000, set(), state), 1)
        state['completionControl']['maxConcurrentStories'] = 5
        self.assertEqual(sim.pick_next_due_index(items, 100000, set(), state), 1)

    def test_failed_story_becomes_explicit_blocker_and_no_filler(self):
        state = seed('failed')
        watchdog.step(state, 100000)
        self.assertEqual(state['tasks']['p']['status'], 'blocked')
        state = seed('repairing')
        task_master.step(state, 100000)
        self.assertEqual(state['workQueue'], [])

    def test_spike_repair_finishes_only_with_saved_findings(self):
        parent = {'taskType':'spike', 'status':'repairing', '_peerRepairPending':True}
        self.assertFalse(watchdog.complete_spike_repair(parent, {'note':'Claimed success'}, 100000))
        result = {'ok':True, 'note':'Saved findings', 'libraryPath':'archive/result.md'}
        self.assertTrue(watchdog.complete_spike_repair(parent, result, 100000))
        self.assertEqual(parent['status'], 'done')
        self.assertNotIn('_peerRepairPending', parent)
        for extra in ({'taskType':'research'}, {'taskType':'spike','manualAttention':True},
                      {'taskType':'spike','_peerGate':{'escalated':True}}):
            self.assertFalse(watchdog.complete_spike_repair(extra, result, 100000))

    def test_recovery_preserves_parent_link_and_idle_is_unchanged(self):
        state = seed()
        watchdog.step(state, 100000)
        self.assertIs(sim._resolve_review_parent(state, state['workQueue'][0]), state['tasks']['p'])
        state = {'tasks':{},'workQueue':[]}
        watchdog.step(state, 100000)
        self.assertEqual(state, {'tasks':{},'workQueue':[]})
