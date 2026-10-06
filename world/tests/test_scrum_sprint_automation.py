import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402
import sim  # noqa: E402


def state():
    return {
        'agentRoster': [{'id': 'director'}, {'id': 'sm', 'director': 'director'},
                        {'id': 'worker', 'director': 'director'}],
        'agents': {}, 'tasks': {},
        'teams': [{'id': 'team', 'directorId': 'director', 'scrumMasterId': 'sm',
                   'autoSprints': True}],
        'workQueue': [{'title': 'Investigate', 'goal': 'Understand evidence',
                       'teamId': 'team', 'taskType': 'spike'}],
    }


class ScrumSprintAutomation(unittest.TestCase):
    def test_groomed_work_gets_one_staffed_sprint(self):
        s = state()
        created = sim._plan_scrum_sprints(s, 1_000_000)
        self.assertEqual(len(created), 1)
        sid = created[0]
        self.assertEqual(s['sprints'][sid]['plannedBy'], 'sm')
        self.assertEqual(s['workQueue'][0]['sprintId'], sid)
        self.assertEqual(s['workQueue'][0]['room'], 'observatory')
        self.assertEqual(sim._sprint_worker_pool(s, sid), ['worker'])
        self.assertEqual(sim._plan_scrum_sprints(s, 1_000_001), [])

    def test_no_workers_does_not_create_a_stalled_sprint(self):
        s = state()
        s['agentRoster'].pop()
        self.assertEqual(sim._plan_scrum_sprints(s, 1_000_000), [])
        self.assertEqual(len(s['workQueue']), 1)

    def test_close_reports_actual_failure_and_retries_email(self):
        s = state()
        sid = sim._plan_scrum_sprints(s, 1_000_000)[0]
        s['workQueue'] = []
        s['tasks']['t'] = {'id': 't', 'sprintId': sid, 'title': 'Investigate',
                           'room': 'observatory', 'status': 'failed',
                           'note': 'Provider failed', 'libraryPath': 'reports/evidence.md'}
        sim.close_sprint(s, sid, 1_000_100)
        sim.close_sprint(s, sid, 1_000_101)
        self.assertEqual(len(s['emailOutbox']), 1)
        self.assertIn('failed', s['emailOutbox'][0]['body'])
        self.assertIn('reports/evidence.md', s['emailOutbox'][0]['body'])
        with patch.object(serve, 'send_player_email_sync', return_value=False), \
                patch.object(serve, 'send_player_telegram_sync') as telegram:
            sim._drain_email_outbox_sync(s)
        telegram.assert_not_called()
        self.assertEqual(s['sprints'][sid]['resultsEmail']['status'], 'retrying')
        s['emailOutbox'][0]['nextAttemptAt'] = 0
        with patch.object(serve, 'send_player_email_sync', return_value=True):
            sim._drain_email_outbox_sync(s)
        self.assertEqual(s['sprints'][sid]['resultsEmail']['status'], 'sent')
        self.assertEqual(s['emailOutbox'], [])

    def test_old_sprint_completion_does_not_close_new_sprint(self):
        s = state()
        sid = sim._plan_scrum_sprints(s, 1_000_000)[0]
        s['tasks']['old'] = {'title': 'Investigate', 'room': 'observatory',
                            'sprintId': 'old', 'status': 'done'}
        self.assertEqual(sim.sprint_progress(s, sid)['done'], 0)

    def test_terminal_failures_close_honestly_and_queue_results(self):
        s = state()
        sid = sim._plan_scrum_sprints(s, 1_000_000)[0]
        s['workQueue'] = []
        s['tasks']['t'] = {'id': 't', 'sprintId': sid, 'title': 'Investigate',
                           'room': 'observatory', 'status': 'failed'}
        with patch.object(sim, '_on_sprint_closed'):
            self.assertEqual(sim._auto_close_completed_sprints(s, 1_000_100), [sid])
        self.assertEqual(s['sprints'][sid]['outcome'], 'finished_with_failures')
        self.assertEqual(sim.sprint_progress(s, sid)['failed'], 1)
        self.assertEqual(sim.sprint_progress(s, sid)['done'], 0)
        self.assertEqual(len(s['emailOutbox']), 1)

    def test_breakdown_routes_document_story_to_research(self):
        s = state()
        s['workQueue'] = []
        s['backlogRequests'] = [{'id': 'req', 'status': 'pending', 'filedBy': 'director',
                                 'teamId': 'team', 'goal': 'Document the plan'}]
        pending = {'teamId': 'team', 'scrumMasterId': 'sm', 'reqIds': ['req'], 'people': {}}
        with patch.object(sim, '_log_governance'):
            sim._resolve_breakdown(s, pending, 1_000_000, decider=lambda *args: {
                'items': [{'title': 'Document strategy', 'type': 'story', 'taskType': 'research'}]})
        self.assertEqual(s['workQueue'][0]['taskType'], 'research')
        self.assertEqual(s['workQueue'][0]['room'], 'observatory')

    def test_slow_email_merges_receipt_without_reverting_tasks(self):
        import copy
        s = state()
        sid = sim._plan_scrum_sprints(s, 1_000_000)[0]
        sim.close_sprint(s, sid, 1_000_100)
        fresh = copy.deepcopy(s)
        fresh['tasks']['new'] = {'status': 'working'}
        fresh['emailOutbox'].append({'kind': 'new_notice', 'queuedAt': 1_000_200})
        with patch.object(serve, 'get_state_from_db', side_effect=[s, fresh]), \
                patch.object(serve, 'send_player_email_sync', return_value=True), \
                patch.object(serve, 'save_state_to_db') as save:
            sim._drain_emails_from_db()
        saved = save.call_args.args[0]
        self.assertEqual(saved['tasks']['new']['status'], 'working')
        self.assertEqual(saved['emailOutbox'][0]['kind'], 'new_notice')
        self.assertEqual(saved['sprints'][sid]['resultsEmail']['status'], 'sent')
