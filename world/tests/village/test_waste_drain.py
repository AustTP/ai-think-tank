# Drain-the-backlog wave tests (operator, 2026-11-23): graduation sweep for
# blocked-with-deliverable cards (task-35817), filing-time report mapping
# (BEN-204 class), and actionable refusal reasons. Hermetic: no network.
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import sim  # noqa: E402
import serve  # noqa: E402


def _blocked_card():
    return {
        'id': 'task-b1',
        'title': 'Review the tank commits for coherence',
        'status': 'blocked',
        'taskType': 'spike',
        'deliverable': 'report',
        'room': 'pressoffice',
        'instructions': 'Read the packet and file a review.',
        'issueKey': 'BEN-207',
        'reportPath': 'archive/1795434284712-spike-task-35817.md',
        'blockedAt': 1,
        'blockedReason': 'repair_loop',
    }


def _state(task):
    return {
        'agents': {},
        'tasks': {task['id']: task},
        'workQueue': [],
        'agentRoster': [],
        'lastStaleWorkSweep': 0,
    }


class BlockedDeliverableGraduation(unittest.TestCase):
    """A card BLOCKED by the repair loop whose saved report exists must be
    re-issued once with instructions naming the saved file (task-35817:
    finished work rotted in 'blocked' because no pass re-entered the gate)."""

    def _run(self, state, now_ms):
        now_ms = max(now_ms, 1_000_000_000)
        state['lastStaleWorkSweep'] = 0
        with mock.patch.object(serve, '_safe_library_path',
                               side_effect=lambda rel: '/tmp/fake-lib/' + str(rel)), \
             mock.patch('os.path.isfile', return_value=True):
            return sim._stale_work_step(state, now_ms / 1000.0, now_ms)

    def test_blocked_card_with_saved_report_is_graduated(self):
        state = _state(_blocked_card())
        n = self._run(state, 2_000_000_000)
        self.assertGreaterEqual(n, 1)
        # the legacy task record is gone; a re-issue item is queued
        self.assertNotIn('task-b1', state['tasks'])
        item = state['workQueue'][-1]
        self.assertEqual(item.get('reissueOf'), 'task-b1')
        self.assertIn('1795434284712-spike-task-35817.md', item['instructions'])
        self.assertIn('Do not redo the work', item['instructions'])
        self.assertEqual(item.get('priority'), sim.WORK_PRIORITY['high'])

    def test_graduation_happens_only_once_per_title(self):
        state = _state(_blocked_card())
        n1 = self._run(state, 2_000_000_000)
        # re-issue lands a new task eventually; simulate re-blocking by
        # inserting another blocked card with the SAME title
        second = _blocked_card()
        second['id'] = 'task-b2'
        state['tasks']['task-b2'] = second
        n2 = self._run(state, 2_100_000_000)
        self.assertGreaterEqual(n1, 1)
        self.assertEqual(n2, 0)

    def test_blocked_card_without_artifact_is_untouched(self):
        card = _blocked_card()
        card['reportPath'] = None
        card['libraryPath'] = None
        state = _state(card)
        n = self._run(state, 2_000_000_000)
        self.assertEqual(n, 0)
        self.assertIn('task-b1', state['tasks'])

    def test_missing_file_on_disk_is_not_graduated(self):
        state = _state(_blocked_card())
        with mock.patch.object(serve, '_safe_library_path',
                               side_effect=lambda rel: '/tmp/fake-lib/' + str(rel)), \
             mock.patch('os.path.isfile', return_value=False):
            n = sim._stale_work_step(state, 2_000.0, 2_000_000_000)
        self.assertEqual(n, 0)
        self.assertIn('task-b1', state['tasks'])


class CompletionGapTexts(unittest.TestCase):
    """The refusal reason must name the SAVE PATH, not just the gap."""

    def test_report_gap_names_the_save_tools(self):
        task = {'taskType': 'spike', 'deliverable': 'report',
                'note': '', 'reportPath': None, 'libraryPath': None}
        gap = sim._completion_evidence_gap(task)
        self.assertIn('write_spike_report', gap)
        self.assertIn('library-file', gap)

    def test_report_with_artifact_but_no_note_gaps_on_note(self):
        task = {'taskType': 'spike', 'deliverable': 'report',
                'note': '', 'reportPath': 'archive/x.md', 'libraryPath': None}
        gap = sim._completion_evidence_gap(task)
        self.assertIn('note is empty', gap)

    def test_repair_stop_names_saved_artifact(self):
        state = {'agents': {}, '_repairLoopNotified': {}}
        task = {'id': 'task-z', 'title': 'T', 'status': 'blocked',
                'reportPath': 'archive/found.md'}
        with mock.patch.object(serve, '_safe_library_path',
                               side_effect=lambda rel: '/tmp/fake-lib/' + str(rel)), \
             mock.patch('os.path.isfile', return_value=True), \
             mock.patch.object(serve, 'get_state_from_db', return_value=state), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, 'log_action'):
            sim._stop_repair_loop(state, task,
                                  {'failures': 2, 'root': 'r', 'taskIds': []},
                                  'qa')
        self.assertIn('already saved at archive/found.md',
                      task['nextAction'])
        self.assertIn('Do NOT redo the work', task['nextAction'])
class FilingDeliverableMapping(unittest.TestCase):
    """A generic 'task' issue whose criteria are read-and-judge must map to
    a report deliverable at filing time (BEN-204 burned 8 review cycles on
    a code-graded note)."""

    def _read_only_body(self):
        return {'teamId': 'ben', 'type': 'task', 'summary': 's',
                'feature': 'quality', 'storyPoints': 1,
                'description': {
                    'userStory': 'As o, I want a review, so that I know.',
                    'acceptanceCriteria': [
                        'Given the packet, When the researcher reads it, '
                        'Then the report names what each commit does.'],
                    'never': ['NEVER: do not modify any code']},
                }

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(serve.app)

    def _state(self):
        return {'teams': [{'id': 'ben', 'name': 'Ben'}],
                'agents': {}, 'issues': [], 'workQueue': []}

    def _post_issues(self, body, state):
        import sim as sim_mod
        captured = {}

        def fake(state_, team_id, issue_type, summary, feature,
                 reporter_id, **kw):
            captured['deliverable'] = kw.get('deliverable')
            return {'key': 'BEN-X', 'type': issue_type, 'summary': summary}

        env = unittest.mock.patch.multiple(
            serve, get_state_from_db=lambda: state,
            verify_session=lambda cookie: True)
        with env, \
             mock.patch.object(sim_mod, 'file_issue', fake), \
             mock.patch.object(serve, 'save_state_to_db'), \
             mock.patch.object(serve, 'log_action'), \
             mock.patch.object(serve, '_append_passport_decision'):
            r = self._client().post('/api/intent/issues', json=body)
        return r, captured

    def test_read_only_task_files_report_deliverable(self):
        r, cap = self._post_issues(self._read_only_body(), self._state())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(cap.get('deliverable'), 'report')

    def test_explicit_deliverable_is_never_overridden(self):
        body = self._read_only_body()
        body['deliverable'] = 'code'
        r, cap = self._post_issues(body, self._state())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(cap.get('deliverable'), 'code')
