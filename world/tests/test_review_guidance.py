import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content  # noqa: E402
import review_guidance as guidance  # noqa: E402
import serve  # noqa: E402
import sim  # noqa: E402


def state():
    return {'tasks': {'t': {'id': 't', 'title': 'Build', 'status': 'failed', 'room': 'pressoffice',
                            'taskType': 'code', 'assignedTo': 'ben', 'teamId': 'team',
                            '_peerGate': {'escalated': True, 'cycleCount': 8}}},
            'agentRoster': [{'id': 'ben', 'director': 'team'}, {'id': 'ada', 'director': 'team'}],
            'agents': {'ben': {}, 'ada': {}, 'sm': {}}, 'workQueue': [],
            'teams': [{'id': 'team', 'directorId': 'team', 'scrumMasterId': 'sm'}]}


class Guidance(unittest.TestCase):
    def test_investigation_of_investigation_routes_to_original_once(self):
        s=state()
        s['tasks']['child']={'id':'child','status':'failed','investigationOf':'t'}
        guidance.apply_guidance(s,'child',{'action':'research'})
        self.assertEqual(s['workQueue'][0]['investigationOf'],'t')
        again=guidance.apply_guidance(s,'child',{'action':'research'})
        self.assertTrue(again['alreadyQueued'])
        self.assertEqual(len(s['workQueue']),1)

    def test_retry_queues_runnable_repair_and_clears_stale_blocker(self):
        s=state();t=s['tasks']['t']
        t.update(boardClearedAt=1,failNote='Old failure',blockedReason='code_director_check',
                 _codeDirectorPending=True,projectLabel='project')
        result=guidance.apply_guidance(s,'t',{'action':'retry','guidance':'Correct imports'})
        self.assertEqual(result['status'],'repairing')
        self.assertNotIn('failNote',t)
        self.assertNotIn('boardClearedAt',t)
        self.assertNotIn('_codeDirectorPending',t)
        q=s['workQueue'][0]
        self.assertTrue(q['directRoute'])
        self.assertTrue(q['taskMasterWork'])
        self.assertEqual(q['projectLabel'],'project')
        self.assertIn('Correct imports',q['instructions'])

    def test_stuck_review_communication_includes_cause_evidence_and_next_step(self):
        s = state()
        task = s['tasks']['t']
        task['_peerGate'] = {'cycleCount': sim.MAX_REVIEW_CYCLES - 1, 'approvals': 0}
        task['peerReviews'] = [{'reviewer': 'ada', 'verdict': 'actionable',
                                'checks': ['Missing source citations']}]
        with patch.object(serve, 'create_escalation') as escalate:
            self.assertTrue(sim._maybe_escalate_stuck_gate(task, task['_peerGate'],
                'quality pipeline keeps failing', failure_text='Docker daemon unavailable', state=s))
            sim._maybe_escalate_stuck_gate(task, task['_peerGate'], 'repeated', state=s)
        self.assertEqual(len(s['playerInbox']), 1)
        notice = s['playerInbox'][0]
        for text in ['Docker daemon unavailable', 'Missing source citations', 'Next step:', 'Guide review']:
            self.assertIn(text, notice['question'])
            self.assertIn(text, escalate.call_args.args[1])
        self.assertEqual(notice['status'], 'awaiting_guidance')
        self.assertEqual(notice['taskId'], 't')
        escalate.assert_called_once()

    def test_missing_review_verdict_reports_unknown_cause_without_inventing_one(self):
        task = state()['tasks']['t']
        message = sim._review_blocker_message(task, 'repeated failed review attempts')
        self.assertIn('No completed review verdict', message)
        self.assertIn('execution needs investigation', message)

    def test_applying_guidance_resolves_communication(self):
        s = state()
        sim._communicate_stuck_review(s, s['tasks']['t'], 'Review blocked')
        with patch.object(serve, 'get_state_from_db', return_value=s), \
                patch.object(serve, 'save_state_to_db'):
            serve._save_review_guidance('t', {'action': 'retry', 'guidance': 'Fix citations'})
        self.assertEqual(s['playerInbox'][0]['status'], 'resolved')
        self.assertIn('retry', s['playerInbox'][0]['answer'])

    def test_form_selects_recommended_action_instead_of_pause(self):
        for category, action in [('formatting', 'retry'), ('missing_inputs', 'provide_inputs'),
                                 ('code_defect', 'research')]:
            task = state()['tasks']['t']
            task['reviewDiagnosis'] = {'category': category}
            form = guidance.render_form(task, '/guide', {})
            self.assertIn(f'value="{action}" selected', form)
            self.assertNotIn('value="pause" selected', form)

    def test_guidance_waits_for_tick_and_reads_its_latest_state(self):
        current = state()
        started = threading.Event()
        finished = threading.Event()
        errors = []

        def submit():
            started.set()
            try:
                serve._save_review_guidance('t', {'action': 'retry', 'guidance': 'Fix the import'})
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.object(serve, 'get_state_from_db', side_effect=lambda: copy.deepcopy(current)), \
                patch.object(serve, 'save_state_to_db', side_effect=lambda s: current.update(s)):
            with serve._STATE_UPDATE_LOCK:
                worker = threading.Thread(target=submit)
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.05))
                current['tickMarker'] = 'latest tick'
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(current['tickMarker'], 'latest tick')
        self.assertEqual(len(current['workQueue']), 1)
        self.assertIn('Fix the import', current['tasks']['t']['instructions'])

    def test_all_guidance_actions_have_concrete_effects(self):
        for action in guidance.ACTIONS:
            with self.subTest(action=action):
                s = state()
                result = guidance.apply_guidance(s, 't', {
                    'action': action, 'guidance': 'Fix only the identified undefined import',
                    'acceptanceCriteria': 'A sourced document', 'taskType': 'research',
                    'agentId': 'ada', 'titles': 'Identify inputs\nValidate inputs'})
                self.assertEqual(result['action'], action)
                self.assertNotEqual(s['tasks']['t']['status'], 'done')
                if action in ('retry', 'provide_inputs', 'edit_scope', 'reassign'):
                    self.assertEqual(len(s['workQueue']), 1)
                    self.assertEqual(s['tasks']['t']['_peerGate']['approvals'], 0)
                if action == 'reassign':
                    self.assertEqual(s['workQueue'][0]['assignedTo'], 'ada')
                if action == 'edit_scope':
                    self.assertEqual(s['workQueue'][0]['room'], 'observatory')
                if action == 'split':
                    self.assertEqual(len(s['backlogRequests']), 2)
                if action == 'research':
                    self.assertEqual(s['workQueue'][0]['taskType'], 'spike')
                    self.assertEqual(s['workQueue'][0]['assignedTo'], 'sm')
                    self.assertEqual(s['tasks']['t']['status'], 'investigating')
                    self.assertFalse(s['tasks']['t']['manualAttention'])

    def test_research_without_guidance_routes_to_scrum_master(self):
        s = state()
        result = guidance.apply_guidance(s, 't', {'action':'research'})
        self.assertEqual(result['status'], 'investigating')
        self.assertEqual(s['tasks']['t']['assignedTo'], 'sm')
        self.assertEqual(s['workQueue'][0]['investigationOf'], 't')
        self.assertFalse(s['tasks']['t']['_peerGate']['escalated'])

    def test_invalid_retry_does_not_mutate_state(self):
        s = state()
        original = copy.deepcopy(s)
        with self.assertRaises(ValueError):
            guidance.apply_guidance(s, 't', {'action': 'edit_scope'})
        self.assertEqual(s, original)

    def test_repeated_error_stops_after_two_attempts(self):
        t = state()['tasks']['t']
        gate = t['_peerGate'] = {}
        with patch.object(serve, 'create_escalation') as esc:
            self.assertFalse(sim._maybe_escalate_stuck_gate(t, gate, 'lint', 'a.py:1:1: F821 undefined pd'))
            self.assertTrue(sim._maybe_escalate_stuck_gate(t, gate, 'lint', 'a.py:9:2: F821 undefined pd'))
        self.assertEqual(gate['cycleCount'], 2)
        self.assertEqual(t['reviewDiagnosis']['category'], 'code_defect')
        self.assertIn('taskId', esc.call_args.kwargs['on_approve_note'])

    def test_shared_workspace_assignment_waits(self):
        s = state()
        s['tasks']['active'] = {'id': 'active', 'room': 'pressoffice', 'status': 'working'}
        with patch.object(sim, 'assign_task') as assign:
            self.assertIsNone(sim._assign_due_item(s, {'title': 'Other code', 'room': 'pressoffice'}, True, {}, {}, 0))
        assign.assert_not_called()

    def test_email_form_checks_token_and_does_not_accept_approval(self):
        request = Mock(method='GET', query_params={'token': 'bad'})
        esc = {'kind': 'stuck review loop', 'status': 'pending', 'token': 'good', 'note': '{"taskId":"t"}'}
        with patch.object(serve, '_load_escalations', return_value={'esc': esc}):
            response = asyncio.run(serve.guide_escalated_review('esc', request))
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(ValueError):
            guidance.apply_guidance(state(), 't', {'action': 'approve'})


class WeatherAndDocumentReview(unittest.TestCase):
    def test_weather_failures_are_not_successes(self):
        with patch.object(serve, '_weather_geocode', return_value=None):
            self.assertTrue(serve._weather_fetch('No place').startswith('__TOOL_ERROR__'))
        with patch.object(serve, '_weather_fetch', return_value='__TOOL_ERROR__: missing data'), \
                patch.object(sim, '_store_content_result') as store:
            content._run_weather_content({}, 'ben', {'id': 'weather'})
        self.assertFalse(store.call_args.args[1]['ok'])
        self.assertTrue(store.call_args.args[1]['dataUnavailable'])

    def test_document_review_reads_document_and_skips_code_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'doc.md')
            with open(target, 'w') as f:
                f.write('Actual sourced document')
            snapshot = {'tasks': {'parent': {'id': 'parent', 'room': 'observatory', 'libraryPath': 'doc.md'}}}
            with patch.object(serve, '_safe_library_path', return_value=target), \
                    patch.object(serve, 'get_or_create_agent_key', return_value='test'), \
                    patch.object(serve, '_mid_tier_slug', return_value='model'), \
                    patch.object(serve, '_http_json', return_value={'reply': json.dumps({
                        'verdict': 'approve', 'summary': 'Verified', 'checks': [], 'risks': []})}), \
                    patch.object(content, '_run_quality_pipeline') as pipeline, \
                    patch.object(sim, '_store_content_result') as store:
                content._run_review_content(snapshot, 'ben', {'id': 'review', 'reviewOf': 'parent'})
            pipeline.assert_not_called()
            self.assertEqual(store.call_args.args[1]['peerVerdict'], 'clean')

    def test_formatter_precedes_author_checks(self):
        with patch.object(serve, '_http_json', return_value={'results': []}) as request:
            content._run_quality_pipeline('base', 'key', 'ben', 'sandbox', 'Quality gate for coding task: Fix')
        self.assertEqual(request.call_args.args[3]['steps'][0]['name'], 'format')
