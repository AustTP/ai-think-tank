"""Content coding branches, review checklist, and plain completion helpers."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402
import content  # noqa: E402


class CodingContentBranches(unittest.TestCase):
    def setUp(self):
        # Staging must copy an EMPTY sandbox, not the live workroom tree.
        self.sandboxes = tempfile.mkdtemp(prefix='think tank-content-')
        self.addCleanup(shutil.rmtree, self.sandboxes, True)
        for target, value in (
                (serve, self.sandboxes),
                (content._serve, self.sandboxes),
                ('SELF_BASE_URL', 'http://x')):
            if isinstance(target, str):
                continue
            patcher = mock.patch.object(target, 'SANDBOXES_DIR', value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch('sim._peer_coding_class', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _snapshot(self):
        return {'agentRoster': [{'id': 'cora', 'name': 'Cora'}],
                'agents': {'cora': {'name': 'Cora'}}}

    def _run(self, chat_replies, exec_response=None, pipeline_response=None,
             coding_tier='tier', colab_available=False, task=None):
        seen = {}
        pipeline_calls = []
        patcher = mock.patch('sim._store_content_result',
                             lambda task_id, result: seen.__setitem__(task_id, result))
        patcher.start()
        self.addCleanup(patcher.stop)
        patchers = [
            mock.patch.object(serve, 'SELF_BASE_URL', 'http://x'),
            mock.patch.object(serve, 'get_or_create_agent_key', return_value='key'),
            mock.patch.object(serve, '_coding_tier_slug', return_value=coding_tier),
            mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', colab_available),
            mock.patch.object(serve, 'COLAB_ENABLED', colab_available),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return chat_replies.pop(0)
            if path == '/api/execute':
                return exec_response if exec_response is not None else {'allowed': True,
                                                                        'exitCode': 0,
                                                                        'timedOut': False}
            if path == '/api/pipeline':
                pipeline_calls.append(body)
                return pipeline_response if pipeline_response is not None else {
                    'ok': True, 'failedStep': None, 'results': [],
                    'note': 'quality pipeline all green'}
            return {'ok': True}
        mock.patch.object(serve, '_http_json', side_effect=side_effect).start()
        self.addCleanup(mock.patch.stopall)
        if task is None:
            task = {'id': 'c1', 'title': 'Build the thing', 'projectLabel': 'Proj'}
        result = content._run_coding_content(self._snapshot(), 'cora', task)
        return result, seen.get(task.get('id', 'c1'), {}), pipeline_calls

    def test_no_coding_tier(self):
        result, note, _ = self._run([], coding_tier=None)
        self.assertFalse(result)
        self.assertIn('no coding model tier', note['note'])

    def test_model_call_failed(self):
        # Two failed replies: the first triggers an infra retry, the second
        # is the final failure (tagged infra=True so sim.py skips coaching).
        result, note, _ = self._run([{'reply': ''}, {'reply': ''}])
        self.assertFalse(result)
        self.assertIn('model call failed', note['note'])
        self.assertTrue(note.get('infra'), 'final model-call failure must be tagged infra')

    def test_model_call_budget_exhausted(self):
        result, note, _ = self._run(
            [{'budgetExhausted': True, 'taskSpendUsd': 1.0,
              'taskSpendAttempts': 3}])
        self.assertFalse(result)
        self.assertTrue(note.get('budgetExhausted'))
        self.assertEqual(note.get('tier'), 'tier')

    def test_model_call_retry_succeeds(self):
        # First call fails (infra retry), second succeeds -- the task proceeds.
        result, note, _ = self._run([{'reply': ''},
                                  {'reply': 'cat > a.js << \'EOF\'\nok\nEOF'}])
        self.assertTrue(result, 'a transient model-call failure must be retried, not fatal')

    def test_empty_command(self):
        result, note, _ = self._run([{'reply': '   ```bash```  '}])
        self.assertFalse(result)
        self.assertIn('empty command', note['note'])

    def test_unbalanced_heredoc_exhausts_continuations(self):
        result, note, _ = self._run([{'reply': 'cat > a.js << EOF\nopen'}] * 3)
        self.assertFalse(result)
        self.assertIn('truncated after', note['note'])

    def test_continuation_completes_balanced_heredoc(self):
        result, note, _ = self._run([
            {'reply': 'cat > a.js << EOF\npart one'},
            {'reply': 'part two\nEOF'},
        ])
        self.assertTrue(result)
        self.assertIn('a.js', note['command'])

    def test_probe_rounds_then_final_command(self):
        # A probeRequest reply is fed page-probe feedback (not a command), and
        # after the round cap the model is told to write the final command.
        result, note, _ = self._run([
            {'reply': '{"probeRequest": {"path": "index.html", "actions": [{"type": "click", "selector": "text=Play"}], "probes": ["typeof window.G"]}}'},
            {'reply': '{"probeRequest": {"path": "index.html", "actions": [], "probes": []}}'},
            {'reply': '{"probeRequest": {"path": "index.html", "actions": [], "probes": ["document.title"]}}'},
            {'reply': 'cat > a.js << EOF\nx\nEOF'},
        ])
        self.assertTrue(result)
        self.assertIn('quality pipeline all green', note['note'])

    def test_colab_rounds_then_final_command(self):
        # A colabRun reply offloads to _colab_compute_run (not a command), and
        # after the round cap the model is told to write the final command.
        seen = {}
        mock.patch('sim._store_content_result',
                   lambda task_id, result: seen.__setitem__(task_id, result)).start()
        for target, value in [('SELF_BASE_URL', 'http://x'),
                              ('get_or_create_agent_key', 'key'),
                              ('_coding_tier_slug', 't')]:
            mock.patch.object(serve, target, return_value=value).start()
        mock.patch.object(serve, 'COLAB_CLI_AVAILABLE', True).start()
        mock.patch.object(serve, 'COLAB_ENABLED', True).start()
        mock.patch.object(serve, '_colab_compute_run',
                          return_value={'session': 's', 'elapsed_s': 1, 'units': 1,
                                        'stdout': 'computed'}).start()
        replies = iter([
            {'reply': '{"colabRun": {"code": "print(1)", "purpose": "p", "runtime": "gpu"}}'},
            {'reply': '{"colabRun": {"code": "print(2)", "purpose": "p", "runtime": "cpu"}}'},
            {'reply': '{"colabRun": {"code": "print(3)", "purpose": "p", "runtime": "cpu"}}'},
            {'reply': 'cat > a.js << EOF\nx\nEOF'},
        ])
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return next(replies)
            if path == '/api/execute':
                return {'allowed': True, 'exitCode': 0, 'timedOut': False, 'stdout': ''}
            if path == '/api/pipeline':
                return {'ok': True, 'failedStep': None, 'results': [], 'note': 'green'}
            return {'ok': True}
        mock.patch.object(serve, '_http_json', side_effect=side_effect).start()
        self.addCleanup(mock.patch.stopall)
        task = {'id': 'c4', 'title': 'T', 'projectLabel': 'P'}
        result = content._run_coding_content(self._snapshot(), 'cora', task)
        self.assertTrue(result)
        self.assertIn('quality pipeline all green', seen['c4']['note'])

    def test_auto_link_failure_warns_but_still_ok(self):
        # A written-but-unlinked .js whose auto-link follow-up is blocked ends
        # up as a WARNING note, not a failed task.
        seen = {}
        mock.patch('sim._store_content_result',
                   lambda task_id, result: seen.__setitem__(task_id, result)).start()
        for target, value in [('SELF_BASE_URL', 'http://x'),
                              ('get_or_create_agent_key', 'key'),
                              ('_coding_tier_slug', 't')]:
            mock.patch.object(serve, target, return_value=value).start()
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return {'reply': 'cat > a.js << EOF\nx\nEOF'}
            if path == '/api/execute':
                purpose = (body or {}).get('purpose') or ''
                command = (body or {}).get('command') or ''
                if 'Coding task' in purpose:
                    return {'allowed': True, 'exitCode': 0, 'timedOut': False, 'stdout': ''}
                if 'LINKED' in command:
                    return {'stdout': 'UNLINKED:a.js\n'}
                if 'Linking previously-unlinked' in purpose:
                    return {'allowed': False, 'reason': 'denied'}
                return {'allowed': True, 'exitCode': 0, 'timedOut': False, 'stdout': ''}
            if path == '/api/pipeline':
                return {'ok': True, 'failedStep': None, 'results': [], 'note': 'green'}
            return {'ok': True}
        mock.patch.object(serve, '_http_json', side_effect=side_effect).start()
        self.addCleanup(mock.patch.stopall)
        task = {'id': 'c5', 'title': 'T', 'projectLabel': 'P'}
        result = content._run_coding_content(self._snapshot(), 'cora', task)
        self.assertTrue(result)
        self.assertIn('not referenced', seen['c5']['note'])
        self.assertIn('failed', seen['c5']['note'])

    def test_execute_blocked(self):
        result, note, _ = self._run([{'reply': 'cat > a.js << EOF\nx\nEOF'}],
                                 exec_response={'allowed': False, 'reason': 'denied'})
        self.assertFalse(result)
        self.assertIn('blocked', note['note'])

    def test_execute_nonzero_exit(self):
        result, note, _ = self._run([{'reply': 'cat > a.js << EOF\nx\nEOF'}],
                                 exec_response={'allowed': True, 'exitCode': 2, 'timedOut': False})
        self.assertFalse(result)
        self.assertIn('non-zero', note['note'])

    def test_full_success_with_unlinked_and_phantom_cleanup(self):
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return {'reply': 'cat > a.js << EOF\nx\nEOF'}
            if path == '/api/execute':
                if 'LINKED' in str(body.get('command') or ''):
                    return {'stdout': 'UNLINKED:a.js\n'}
                if 'PHANTOM' in str(body.get('command') or '') or 'src="' in str(body.get('command') or ''):
                    return {'stdout': 'PHANTOM:index.html:ghost.js\n'}
                if 'dangling' in (body.get('purpose') or '').lower() or \
                   'selectors' in (body.get('purpose') or '').lower():
                    return {'allowed': True, 'exitCode': 0, 'timedOut': False,
                            'stdout': '--- index.html ---\n<div></div>\n--- app.js ---\ndocument.querySelector(".g")\n'}
                return {'allowed': True, 'exitCode': 0, 'timedOut': False, 'stdout': ''}
            if path == '/api/pipeline':
                return {'ok': True, 'failedStep': None, 'results': [], 'note': 'green'}
            return {'ok': True}
        seen = {}
        mock.patch('sim._store_content_result',
                   lambda task_id, result: seen.__setitem__(task_id, result)).start()
        mock.patch.object(serve, 'SELF_BASE_URL', 'http://x').start()
        mock.patch.object(serve, 'get_or_create_agent_key', return_value='key').start()
        mock.patch.object(serve, '_coding_tier_slug', return_value='t').start()
        mock.patch.object(serve, '_http_json', side_effect=side_effect).start()
        self.addCleanup(mock.patch.stopall)
        task = {'id': 'c2', 'title': 'T', 'projectLabel': 'P'}
        result = content._run_coding_content(self._snapshot(), 'cora', task)
        self.assertTrue(result)
        note = seen['c2']['note']
        self.assertIn('auto-linked', note)
        self.assertIn("don't actually exist", note)
        self.assertIn('queries selector', note)
        self.assertIn('quality pipeline all green', note)

    def test_quality_pipeline_red_fails_task(self):
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path == '/api/chat':
                return {'reply': 'cat > a.js << EOF\nx\nEOF'}
            if path == '/api/execute':
                return {'allowed': True, 'exitCode': 0, 'timedOut': False, 'stdout': ''}
            if path == '/api/pipeline':
                return {'ok': False, 'failedStep': 'flake8', 'results': [], 'note': 'flake8 FAILED'}
            return {'ok': True}
        seen = {}
        mock.patch('sim._store_content_result',
                   lambda task_id, result: seen.__setitem__(task_id, result)).start()
        mock.patch.object(serve, 'SELF_BASE_URL', 'http://x').start()
        mock.patch.object(serve, 'get_or_create_agent_key', return_value='key').start()
        mock.patch.object(serve, '_coding_tier_slug', return_value='t').start()
        mock.patch.object(serve, '_http_json', side_effect=side_effect).start()
        self.addCleanup(mock.patch.stopall)
        task = {'id': 'c3', 'title': 'T', 'projectLabel': 'P'}
        result = content._run_coding_content(self._snapshot(), 'cora', task)
        self.assertFalse(result)
        self.assertIn('FAILED at flake8', seen['c3']['note'])

    def test_non_code_lane_skips_quality_pipeline(self):
        # Fix 3: a research/story card that reaches the coding executor must
        # skip the real pipeline (flake8 would fail on prose) and still finish
        # ok; the note says so instead of claiming the pipeline is green.
        result, note, pipeline_calls = self._run(
            [{'reply': 'cat > a.js << \'EOF\'\nok\nEOF'}],
            task={'id': 'r1', 'title': 'Summarize findings',
                  'taskType': 'research', 'projectLabel': 'Proj'})
        self.assertTrue(result)
        self.assertEqual(pipeline_calls, [])
        self.assertIn('quality pipeline skipped (non-code lane)', note['note'])

class ReviewChecklistBranches(unittest.TestCase):
    def test_grade_code_requirement_no_hint_is_unsure(self):
        self.assertEqual(content._grade_code_requirement(
            {'question': 'Is the design clean?'}, {'ok': True}), content.GRADE_UNSURE)

    def test_grade_jev_requirement_low_confidence_and_exception(self):
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value={'d': 1}), \
             mock.patch.object(serve, '_jev_choice',
                               return_value=(content.GRADE_FAILS, 0.3, 0.01)), \
             mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.5):
            verdict, conf = content._grade_jev_requirement(
                {'question': 'q', 'section': 's'}, 'review')
        self.assertEqual(verdict, content.GRADE_UNSURE)
        self.assertEqual(conf, 0.3)
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('down')):
            verdict, conf = content._grade_jev_requirement({'question': 'q'}, 'review')
        self.assertEqual(verdict, content.GRADE_UNSURE)
        self.assertEqual(conf, 0.0)

    def test_grade_review_checklist_anchors_and_caps(self):
        checklist = []
        for i in range(6):
            checklist.append({'id': f'c{i}', 'section': 'code', 'question': f'pipeline green? {i}',
                              'type': 'code'})
            checklist.append({'id': f'j{i}', 'section': 'code', 'question': f'jev q {i}',
                              'type': 'jev'})
        checklist.append({'id': 'h', 'section': 'code', 'question': 'human q', 'type': 'human'})
        checklist.append({'id': 'x', 'section': 'code', 'question': 'weird q', 'type': 'mystery'})
        qp = {'ok': True}
        def jev_choice(decision):
            return (content.GRADE_MEETS, 0.9, 0.01)
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               return_value={'d': 1}), \
             mock.patch.object(serve, '_jev_choice', side_effect=jev_choice), \
             mock.patch.object(serve, '_effective_review_grade_confidence', return_value=0.5), \
             mock.patch.object(serve, '_insert_review_calibration_sample') as cal:
            out = content._grade_review_checklist(checklist, 'review', qp, 'ada')
        # The 6th jev requirement is skipped by the spend cap.
        skipped = [g for g in out['grades'] if g.get('skipped')]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(len([g for g in out['grades'] if g['type'] == 'jev' and not g.get('skipped')]),
                         content.MAX_CHECKLIST_JEV_GRADES)
        # human and unknown escalate.
        reasons = [reason for _, reason in out['escalate']]
        self.assertIn('review decision for you', reasons)
        self.assertTrue(any('unrecognized type' in r for r in reasons))
        # Unanimous code verdicts anchor the section -> calibration samples recorded.
        cal.assert_called()

    def test_grade_review_checklist_unsure_escalates_jev_failure(self):
        checklist = [{'id': 'j1', 'section': 's', 'question': 'q', 'type': 'jev'}]
        with mock.patch.object(serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('down')):
            out = content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        reasons = [r for _, r in out['escalate']]
        self.assertTrue(any('Jev call failed' in r for r in reasons))

    def test_record_review_process_trace(self):
        grades = [{'id': 'g1', 'verdict': content.GRADE_FAILS, 'section': 's',
                   'question': 'q', 'type': 'code', 'confidence': 0.9}]
        calls = {'get': 0, 'post': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                calls['get'] += 1
                return {'content': None}
            if path == '/api/library/file':
                calls['post'] += 1
                return {'ok': True}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            n = content._record_review_process_trace('b', 'k', 'ada', 'P', 'T', 'review', 'Ada',
                                                     grades, 'full review')
        self.assertEqual(n, 1)
        self.assertEqual(calls['post'], 1)

    def test_record_review_process_trace_dedups_existing(self):
        grades = [{'id': 'g1', 'verdict': content.GRADE_FAILS, 'section': 's',
                   'question': 'q', 'type': 'code', 'confidence': None}]
        with mock.patch.object(serve, '_http_json',
                               return_value={'content': 'already there'}):
            n = content._record_review_process_trace('b', 'k', 'ada', 'P', 'T', 'review', 'Ada',
                                                     grades, 'full review')
        self.assertEqual(n, 0)

    def test_record_review_process_trace_post_failure_swallowed(self):
        grades = [{'id': 'g1', 'verdict': content.GRADE_FAILS, 'section': 's',
                   'question': 'q', 'type': 'code', 'confidence': 0.9},
                  {'id': 'g2', 'verdict': content.GRADE_FAILS, 'section': 's',
                   'question': 'q2', 'type': 'code', 'confidence': 0.9}]
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                return {'content': None}
            if path == '/api/library/file':
                raise RuntimeError('write failed')
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            n = content._record_review_process_trace('b', 'k', 'ada', 'P', 'T', 'review', 'Ada',
                                                     grades, 'full review')
        self.assertEqual(n, 0)

    def test_record_review_process_trace_get_failure_swallowed(self):
        grades = [{'id': 'g1', 'verdict': content.GRADE_FAILS, 'section': 's',
                   'question': 'q', 'type': 'code', 'confidence': 0.9}]
        calls = {'post': 0}
        def side_effect(method, base, path, body=None, header=None, timeout=30):
            if path.startswith('/api/library/file?'):
                raise RuntimeError('read failed')
            if path == '/api/library/file':
                calls['post'] += 1
                return {'ok': True}
            return {'ok': True}
        with mock.patch.object(serve, '_http_json', side_effect=side_effect):
            n = content._record_review_process_trace('b', 'k', 'ada', 'P', 'T', 'review', 'Ada',
                                                     grades, 'full review')
        self.assertEqual(n, 1)
        self.assertEqual(calls['post'], 1)

class PlainCompletionAndReflection(unittest.TestCase):
    def test_plain_completion_success_accrues_cost(self):
        with mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {'content': '  answer  '}}],
                                             'usage': {'cost': 0.5}}), \
             mock.patch.object(serve, '_accrue_spend') as accrue:
            self.assertEqual(content._plain_completion('m', [], 100), 'answer')
            accrue.assert_called_once_with('spike', 0.5, village_id=None)

    def test_plain_completion_no_cost(self):
        with mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {'content': 'x'}}],
                                             'usage': {}}), \
             mock.patch.object(serve, '_accrue_spend') as accrue:
            self.assertEqual(content._plain_completion('m', [], 100), 'x')
            accrue.assert_not_called()

    def test_plain_completion_exception_and_bad_data(self):
        with mock.patch.object(serve, '_call_openrouter_sync', side_effect=RuntimeError('x')):
            self.assertEqual(content._plain_completion('m', [], 100), '')
        with mock.patch.object(serve, '_call_openrouter_sync', return_value={}):
            self.assertEqual(content._plain_completion('m', [], 100), '')
        with mock.patch.object(serve, '_call_openrouter_sync', return_value=None):
            self.assertEqual(content._plain_completion('m', [], 100), '')

    def test_plain_completion_task_budget_gate(self):
        with mock.patch.object(serve, '_task_budget_exhausted', return_value=True):
            self.assertEqual(content._plain_completion('m', [], 100, task_id='t9'), '')

    def test_plain_completion_task_id_accrues_task_spend(self):
        with mock.patch.object(serve, '_call_openrouter_sync',
                               return_value={'choices': [{'message': {'content': 'a'}}],
                                             'usage': {'cost': 0.25}}), \
             mock.patch.object(serve, '_task_budget_exhausted', return_value=False), \
             mock.patch.object(serve, '_accrue_spend') as accrue, \
             mock.patch.object(serve, '_accrue_task_spend') as task_accrue:
            self.assertEqual(content._plain_completion('m', [], 100, task_id='t9'), 'a')
            accrue.assert_called_once()
            task_accrue.assert_called_once_with('t9', 0.25)

    def test_parse_reflection_variants(self):
        self.assertEqual(content._parse_reflection('{"confidence": 0.8, "note": "ok"}'), (0.8, 'ok'))
        self.assertEqual(content._parse_reflection('prose {"confidence": 0.7, "note": "n"} prose'),
                         (0.7, 'n'))
        self.assertEqual(content._parse_reflection('not json'), (1.0, ''))
        self.assertEqual(content._parse_reflection('[]'), (1.0, ''))
        self.assertEqual(content._parse_reflection(None), (1.0, ''))
        self.assertEqual(content._parse_reflection('{"confidence": 5, "note": 3}'), (1.0, ''))
        self.assertEqual(content._parse_reflection('{"confidence": -1, "note": "x"}'), (1.0, 'x'))
