"""CS329A takeaway #2 -- Weaver-style verifier ensemble for grading


The planner emits a per-requirement checklist ({id, question, section, type:
code|jev|human}) that travels with a project's subtasks, but the Python review
executor only produced a single holistic Jev actionable/clean verdict. These
tests cover the Python side of the ensemble:

1. sim.py threading: the checklist survives queue_work's whitelist and lands on
   the assigned task object (the orphan-requeue path already carried it).
2. content.py `_grade_code_requirement`: mechanical ground truth is the live
   quality-pipeline result; anything not pipeline-decidable degrades to
   insufficient_evidence, never a guess.
3. content.py `_grade_jev_requirement`: focused per-requirement Jev grade, gated
   by JEV_SAFETY_CONFIDENCE (low confidence or a failed call -> unsure).
4. content.py `_grade_review_checklist`: the ensemble combine -- a verified
   failure outvotes the holistic verdict, human/unsure/unknown requirements
   surface to the player, and the per-review Jev grade count is capped.
5. Integration in `_run_review_content`: a focused jev-requirement FAIL flips an
   otherwise-clean holistic verdict to actionable and queues a fix; a human
   requirement escalates instead of auto-deciding.
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402  (import first: serve lazily re-imports content at its
import content  # noqa: E402  # bottom, so content must not be mid-import when serve loads)
import sim  # noqa: E402

_HOLISTIC_CLEAN = ('clean', 0.95, 0.01)

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    # _run_review_content hits serve.get_or_create_agent_key (a DB write);
    # redirect the DB like every other serve-touching suite so fixtures never
    # land in a live think_tank.db.
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-checklist-test-')
    _PATCHER = mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        # Standby off in hermetic modules: single-slug chain keeps the
        # decision breaker unarmed in the shared test process.
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _answer(choice, confidence, cost=0.01):
    return {'answers': {'choice': {'choice': choice, 'confidence': confidence, 'probabilities': {}}},
            'usage': {'cost': cost}}


class ChecklistThreading(unittest.TestCase):
    def test_queue_work_preserves_checklist(self):
        state = {'agents': {}, 'workQueue': []}
        checklist = [{'id': 'r1', 'question': 'Does the opening name a concrete outcome?',
                      'section': 'opening', 'type': 'jev'}]
        sim.queue_work(state, [{'title': 'Review the landing page', 'room': 'pressoffice',
                                'checklist': checklist}])
        self.assertEqual(state['workQueue'][0]['checklist'], checklist)

    def test_assign_task_carries_checklist_onto_the_task_object(self):
        state = {'agents': {'ada': {'id': 'ada', 'x': 0, 'y': 0, 'busy': False,
                                    'task': None, 'offDuty': False}},
                 'tasks': {}}
        checklist = [{'id': 'r1', 'question': 'Q?', 'section': 's', 'type': 'code'}]
        with mock.patch.object(sim, 'find_path',
                               return_value=[{'x': 0, 'y': 0}, {'x': 40, 'y': 40}]):
            task = sim.assign_task(state, 'ada', 'Review', 'pressoffice', 'instr', None,
                                   {'checklist': checklist}, {}, {'pressoffice': {'x': 0, 'y': 0, 'w': 40, 'h': 40}},
                                   now_ms=1000, task_id_holder=[0])
        self.assertIsNotNone(task)
        self.assertEqual(task['checklist'], checklist)


class GradeCodeRequirement(unittest.TestCase):
    def test_pipeline_question_graded_from_pipeline_ground_truth(self):
        req = {'id': 'lint-clean', 'question': 'Does the code pass flake8 with no errors?',
               'section': 'repo', 'type': 'code'}
        self.assertEqual(content._grade_code_requirement(req, {'ok': True}), content.GRADE_MEETS)
        self.assertEqual(content._grade_code_requirement(req, {'ok': False}), content.GRADE_FAILS)

    def test_non_pipeline_question_degrades_to_unsure_never_guesses(self):
        # No mechanical evidence the executor holds can decide "the example uses
        # a table" -- degrading to unsure (surface to player) matches grading.js's
        # absent-predicate behavior instead of inventing a verdict.
        req = {'id': 'uses-table', 'question': 'Does example 3 use a table?',
               'section': 'example 3', 'type': 'code'}
        self.assertEqual(content._grade_code_requirement(req, {'ok': True}), content.GRADE_UNSURE)


class GradeJevRequirement(unittest.TestCase):
    def test_confident_meets_passes_through(self):
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.9)):
            verdict, conf = content._grade_jev_requirement({'question': 'Q?', 'section': 's'}, 'review text')
        self.assertEqual(verdict, content.GRADE_MEETS)
        self.assertAlmostEqual(conf, 0.9)

    def test_confident_fails_passes_through(self):
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_FAILS, 0.9)):
            verdict, _ = content._grade_jev_requirement({'question': 'Q?', 'section': 's'}, 'review text')
        self.assertEqual(verdict, content.GRADE_FAILS)

    def test_low_confidence_grade_degrades_to_unsure(self):
        # Jev contract: a meets/fails grade below the acting bar must not drive
        # an automatic revision -- surface it as insufficient instead.
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.4)):
            verdict, conf = content._grade_jev_requirement({'question': 'Q?', 'section': 's'}, 'review text')
        self.assertEqual(verdict, content.GRADE_UNSURE)
        self.assertAlmostEqual(conf, 0.4)

    def test_failed_call_degrades_to_unsure(self):
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('down')):
            verdict, conf = content._grade_jev_requirement({'question': 'Q?', 'section': 's'}, 'review text')
        self.assertEqual(verdict, content.GRADE_UNSURE)
        self.assertEqual(conf, 0.0)


class GradeReviewChecklist(unittest.TestCase):
    def test_human_requirement_is_escalated_never_graded(self):
        req = {'id': 'h1', 'question': 'Is the tone right for our audience?', 'section': 'tone', 'type': 'human'}
        out = content._grade_review_checklist([req], 'review', {'ok': True}, 'ada')
        self.assertEqual(out['grades'], [])
        self.assertEqual(out['escalate'], [(req, 'review decision for you')])

    def test_code_failure_and_jev_failure_both_grade_as_fails(self):
        checklist = [
            {'id': 'c1', 'question': 'Does the code pass flake8?', 'section': 'repo', 'type': 'code'},
            {'id': 'j1', 'question': 'Is the opening specific?', 'section': 'opening', 'type': 'jev'},
        ]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_FAILS, 0.9)):
            out = content._grade_review_checklist(checklist, 'review', {'ok': False}, 'ada')
        by_id = {g['id']: g for g in out['grades']}
        self.assertEqual(by_id['c1']['verdict'], content.GRADE_FAILS)
        self.assertEqual(by_id['j1']['verdict'], content.GRADE_FAILS)
        self.assertEqual(out['escalate'], [])

    def test_unsure_jev_and_unknown_type_escalate(self):
        checklist = [
            {'id': 'j1', 'question': 'Is the opening specific?', 'section': 'opening', 'type': 'jev'},
            {'id': 'x1', 'question': 'Mystery check?', 'section': 's', 'type': 'weird'},
        ]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.4)):
            out = content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        self.assertEqual(out['grades'][0]['verdict'], content.GRADE_UNSURE)
        self.assertEqual(len(out['escalate']), 2)

    def test_jev_grade_count_is_capped(self):
        checklist = [{'id': f'j{i}', 'question': f'Q{i}?', 'section': 's', 'type': 'jev'}
                     for i in range(content.MAX_CHECKLIST_JEV_GRADES + 2)]
        with mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer(content.GRADE_MEETS, 0.9)) as call:
            out = content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        self.assertEqual(call.call_count, content.MAX_CHECKLIST_JEV_GRADES)
        graded = [g for g in out['grades'] if not g.get('skipped')]
        skipped = [g for g in out['grades'] if g.get('skipped')]
        self.assertEqual(len(graded), content.MAX_CHECKLIST_JEV_GRADES)
        self.assertEqual(len(skipped), 2)
        self.assertEqual(len(out['escalate']), 2)

    def test_non_pipeline_code_requirement_is_unsure_grade(self):
        # A 'code' requirement whose question is NOT about code health has no
        # predicate the executor can apply -- it grades insufficient_evidence
        # (surfaced to the player), exactly like grading.js's absent-predicate
        # fallback. It also never contributes to a section's mechanical anchor.
        checklist = [{'id': 'c1', 'question': 'Is the design clean?', 'section': 'repo',
                      'type': 'code'}]
        out = content._grade_review_checklist(checklist, 'review', {'ok': True}, 'ada')
        self.assertEqual(out['grades'][0]['verdict'], content.GRADE_UNSURE)
        self.assertEqual(out['escalate'], [])


class RecordReviewProcessTrace(unittest.TestCase):
    def _run_writer(self, grades, calls=None, get_result=None):
        writes = []
        def fake_http(method, base, path, body=None, key=None):
            if method == 'GET' and 'library/file?path=' in path:
                return get_result if get_result is not None else {'error': 'no such file'}
            if method == 'POST' and path == '/api/library/file':
                writes.append(body)
                return {'allowed': True, 'ok': True}
            raise AssertionError(f'unexpected call {method} {path}')
        with mock.patch.object(content._serve, '_http_json', side_effect=fake_http):
            n = content._record_review_process_trace('http://x', 'key', 'ada', 'landing', 'Review landing',
                                                     'review', 'Ada', grades, 'the review text')
        return n, writes

    def test_writes_one_trace_per_verified_failure_bounded_and_deduped(self):
        grades = [{'id': 'c1', 'section': 'repo', 'question': 'Does it pass flake8?', 'type': 'code',
                   'verdict': content.GRADE_FAILS, 'confidence': 1.0},
                  {'id': 'j1', 'section': 'opening', 'question': 'Is the opening specific?', 'type': 'jev',
                   'verdict': content.GRADE_FAILS, 'confidence': 0.9},
                  {'id': 'j2', 'section': 'opening', 'question': 'Other question?', 'type': 'jev',
                   'verdict': content.GRADE_FAILS, 'confidence': 0.9},
                  {'id': 'j3', 'section': 'x', 'question': 'X?', 'type': 'jev',
                   'verdict': content.GRADE_FAILS, 'confidence': 0.9},
                  {'id': 'j4', 'section': 'y', 'question': 'Y?', 'type': 'jev',
                   'verdict': content.GRADE_FAILS, 'confidence': 0.9}]
        n, writes = self._run_writer(grades)
        self.assertEqual(n, content.MAX_CHECKLIST_TRACE_FILES)
        self.assertEqual(len(writes), content.MAX_CHECKLIST_TRACE_FILES)
        for w in writes:
            self.assertTrue(w['path'].startswith('pending_review/skills/review-trace/'))
            self.assertEqual(w['source'], 'external')
            self.assertLessEqual(len(w['content']), content._PROCESS_TRACE_MAX_CHARS)
            self.assertIn('verified FAILS', w['content'])

    def test_existing_lesson_dedupes(self):
        grades = [{'id': 'c1', 'section': 'repo', 'question': 'Q?', 'type': 'code',
                   'verdict': content.GRADE_FAILS, 'confidence': 1.0}]
        n, writes = self._run_writer(grades, get_result={'content': '# already queued'})
        self.assertEqual(n, 0)
        self.assertEqual(writes, [])

    def test_meets_and_unsure_never_write(self):
        grades = [{'id': 'c1', 'section': 'repo', 'question': 'Q?', 'type': 'code',
                   'verdict': content.GRADE_MEETS, 'confidence': 1.0},
                  {'id': 'j1', 'section': 's', 'question': 'Q2?', 'type': 'jev',
                   'verdict': content.GRADE_UNSURE, 'confidence': 0.4}]
        n, writes = self._run_writer(grades)
        self.assertEqual(n, 0)
        self.assertEqual(writes, [])


class RunReviewContentEnsemble(unittest.TestCase):
    # The ensemble integration: the checklist grades are computed and a focused
    # requirement failure OUTVOTES the holistic verdict, while human/unsure
    # requirements escalate to the player.

    def _run(self, checklist, call_side_effects, choice_side_effects):
        snapshot = {}
        task = {'id': 'task-r1', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing', 'checklist': checklist}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_mid_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_low_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               side_effect=call_side_effects), \
             mock.patch.object(content._serve, '_jev_choice', side_effect=choice_side_effects), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(content._serve, '_classify_failure', return_value='style'), \
             mock.patch.object(content._serve, '_record_failure', return_value='fail-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        return store.call_args[0][1]

    def test_focused_jev_failure_outvotes_clean_holistic_verdict(self):
        checklist = [{'id': 'j1', 'question': 'Is the opening specific?', 'section': 'opening', 'type': 'jev'}]
        result = self._run(
            checklist,
            [_answer('clean', 0.95), _answer(content.GRADE_FAILS, 0.9)],
            [_HOLISTIC_CLEAN, (content.GRADE_FAILS, 0.9, 0.01)],
        )
        self.assertEqual(result['checklistGrades'][0]['verdict'], content.GRADE_FAILS)
        self.assertTrue(result['queueFix'])  # ensemble outvoted the clean verdict
        self.assertEqual(result['note'], 'Filed a review on "Review landing" (text + visual), queued a fix')
        self.assertEqual(result['processTraceCount'], 1)  # SWiRL trace for the verified failure
        # The fix task is steered at the actual missed requirement, not just
        # pointed at the Library entry.
        self.assertIn('Is the opening specific?', result['queueFix']['instructions'])
        self.assertIn('Verified failing requirements', result['queueFix']['instructions'])

    def test_human_requirement_escalates_without_auto_deciding(self):
        checklist = [{'id': 'h1', 'question': 'Is the tone right?', 'section': 'tone', 'type': 'human'}]
        result = self._run(checklist, [_answer('clean', 0.95)], [_HOLISTIC_CLEAN])
        self.assertNotIn('queueFix', result)  # nothing auto-decided
        self.assertEqual(result['checklistEscalated'], ['h1'])

    def test_only_top_two_failed_requirements_named_in_the_fix(self):
        # Bounded steering: three verified fails, but the fix instruction names
        # the first MAX_QUEUE_FIX_REQUIREMENTS and no more.
        checklist = [{'id': f'j{i}', 'question': f'Question {i}?', 'section': 's', 'type': 'jev'}
                     for i in range(3)]
        result = self._run(
            checklist,
            [_answer('clean', 0.95)] + [_answer(content.GRADE_FAILS, 0.9)] * 3,
            [_HOLISTIC_CLEAN] + [(content.GRADE_FAILS, 0.9, 0.01)] * 3,
        )
        instructions = result['queueFix']['instructions']
        self.assertIn('Question 0?', instructions)
        self.assertIn('Question 1?', instructions)
        self.assertNotIn('Question 2?', instructions)  # capped

    def test_unsure_jev_requirement_escalates(self):
        checklist = [{'id': 'j1', 'question': 'Is the opening specific?', 'section': 'opening', 'type': 'jev'}]
        result = self._run(
            checklist,
            [_answer('clean', 0.95), _answer(content.GRADE_MEETS, 0.4)],
            [_HOLISTIC_CLEAN, (content.GRADE_UNSURE, 0.4, 0.01)],
        )
        self.assertNotIn('queueFix', result)
        self.assertEqual(result['checklistEscalated'], ['j1'])

    def test_no_model_tier_short_circuits_without_any_network_call(self):
        snapshot = {}
        task = {'id': 'task-r-n0', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing'}
        with mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value=None), \
             mock.patch.object(content._serve, '_mid_tier_slug', return_value=None), \
             mock.patch.object(content._serve, '_low_tier_slug', return_value=None), \
             mock.patch.object(content._serve, '_http_json') as http_mock, \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        result = store.call_args[0][1]
        self.assertFalse(result['ok'])
        self.assertIn('no model tier is configured', result['note'])
        http_mock.assert_not_called()  # never got to the critique/visual/library calls

    def test_qa_variant_uses_the_qa_lead_and_reports_QA(self):
        snapshot = {}
        task = {'id': 'task-qa1', 'title': 'QA landing', 'room': 'pressoffice',
                'taskType': 'qa', 'projectLabel': 'landing'}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer('clean', 0.95)), \
             mock.patch.object(content._serve, '_jev_choice', return_value=_HOLISTIC_CLEAN), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        result = store.call_args[0][1]
        self.assertEqual(result['note'], 'Filed a QA on "QA landing" (text + visual), nothing actionable found')

    def test_probe_round_feeds_real_probe_feedback_back_into_the_chat(self):
        snapshot = {}
        task = {'id': 'task-probe', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing'}
        calls = {'chat': 0, 'probe': 0}
        def http_side_effect(method, base, path, body=None, key=None):
            if path == '/api/chat':
                calls['chat'] += 1
                if calls['chat'] == 1:
                    return {'reply': '{"probeRequest": {"path": "index.html", "actions": [], '
                                    '"probes": ["document.body.className"]}}'}
                return {'reply': 'final assessment'}
            if path == '/api/page-probe':
                calls['probe'] += 1
                return {'actionLog': ['clicked'], 'customGlobals': [],
                        'results': {'document.body.className': 'dark'}}
            return {'reply': 'x'}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json', side_effect=http_side_effect), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer('clean', 0.95)), \
             mock.patch.object(content._serve, '_jev_choice', return_value=_HOLISTIC_CLEAN), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        self.assertEqual(calls['chat'], 2)  # first reply was a probe, second was the real assessment
        self.assertEqual(calls['probe'], 1)
        result = store.call_args[0][1]
        self.assertIn('Filed a review', result['note'])

    def test_probe_rounds_exhausted_then_final_assessment(self):
        # Both allowed probe rounds are used; on the LAST one the prompt
        # tells the reviewer to write the final assessment as plain text.
        snapshot = {}
        task = {'id': 'task-probe2', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing'}
        calls = {'chat': 0, 'probe': 0}
        def http_side_effect(method, base, path, body=None, key=None):
            if path == '/api/chat':
                calls['chat'] += 1
                if calls['chat'] <= 2:
                    return {'reply': '{"probeRequest": {"path": "index.html", "actions": [], '
                                    '"probes": ["document.title"]}}'}
                return {'reply': 'final assessment'}
            if path == '/api/page-probe':
                calls['probe'] += 1
                return {'actionLog': [], 'customGlobals': [],
                        'results': {'document.title': 'landing'}}
            return {'reply': 'x'}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json', side_effect=http_side_effect), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer('clean', 0.95)), \
             mock.patch.object(content._serve, '_jev_choice', return_value=_HOLISTIC_CLEAN), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        self.assertEqual(calls['probe'], 2)  # both rounds used
        self.assertEqual(calls['chat'], 3)
        result = store.call_args[0][1]
        self.assertIn('Filed a review', result['note'])

    def test_jev_verdict_raise_falls_back_to_clean(self):
        # The deterministic safe fallback: a Jev decision failure must NOT queue
        # a fix the review can't justify -- reads as clean.
        snapshot = {}
        task = {'id': 'task-jev-raise', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing'}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               side_effect=RuntimeError('judge down')), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        result = store.call_args[0][1]
        self.assertNotIn('queueFix', result)  # safe clean fallback
        self.assertTrue(result['ok'])

    def test_red_quality_pipeline_forces_actionable_even_when_holistic_says_clean(self):
        snapshot = {}
        task = {'id': 'task-red', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing'}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': False, 'failedStep': 'flake8',
                                             'results': [], 'note': 'quality pipeline FAILED at flake8'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer('clean', 0.95)), \
             mock.patch.object(content._serve, '_jev_choice', return_value=_HOLISTIC_CLEAN), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)
        result = store.call_args[0][1]
        self.assertFalse(result['pipelineOk'])
        self.assertIsNotNone(result['queueFix'])  # red pipeline can never read as approval
        self.assertIn('Quality pipeline is red', result['queueFix']['instructions'])
        self.assertEqual(result['queueFix']['productId'], task.get('productId'))

    def test_escalation_failure_is_swallowed(self):
        # A create_escalation raise must not break the review result -- the
        # grade/verdict still lands, just without the escalation side channel.
        checklist = [{'id': 'h1', 'question': 'Is the tone right?', 'section': 'tone', 'type': 'human'}]
        snapshot = {}
        task = {'id': 'task-esc-raise', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing', 'checklist': checklist}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               return_value=_answer('clean', 0.95)), \
             mock.patch.object(content._serve, '_jev_choice', return_value=_HOLISTIC_CLEAN), \
             mock.patch.object(content._serve, 'create_escalation',
                               side_effect=RuntimeError('escalation down')), \
             mock.patch.object(content._serve, '_classify_failure', return_value='style'), \
             mock.patch.object(content._serve, '_record_failure', return_value='fail-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)  # no raise
        result = store.call_args[0][1]
        self.assertNotIn('checklistEscalated', result)

    def test_process_trace_failure_is_swallowed(self):
        checklist = [{'id': 'j1', 'question': 'Is the opening specific?', 'section': 'opening', 'type': 'jev'}]
        snapshot = {}
        task = {'id': 'task-trace-raise', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing', 'checklist': checklist}
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content, '_record_review_process_trace',
                               side_effect=RuntimeError('library down')), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               side_effect=[_answer('clean', 0.95), _answer(content.GRADE_FAILS, 0.9)]), \
             mock.patch.object(content._serve, '_jev_choice',
                               side_effect=[_HOLISTIC_CLEAN, (content.GRADE_FAILS, 0.9, 0.01)]), \
             mock.patch.object(content._serve, 'create_escalation', return_value='esc-1'), \
             mock.patch.object(content._serve, '_classify_failure', return_value='style'), \
             mock.patch.object(content._serve, '_record_failure', return_value='fail-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None) as store:
            content._run_review_content(snapshot, 'ada', task)  # no raise
        result = store.call_args[0][1]
        self.assertNotIn('processTraceCount', result)  # swallowed -> 0, not surfaced


class WikiContextForTask(unittest.TestCase):
    def test_wiki_context_returns_the_injected_markdown(self):
        state = {'agents': {}}
        with mock.patch.object(sim, 'inject_wiki_context', return_value='# Room knowledge') as inject:
            out = content._wiki_context_for_task(state, {'room': 'pressoffice'})
        self.assertEqual(out, '# Room knowledge')
        inject.assert_called_once_with(state, {'room': 'pressoffice'})

    def test_wiki_context_exception_returns_empty_string(self):
        # The executor must still run with a blank knowledge context if the
        # injection path blows up -- never wedge the task on a wiki failure.
        with mock.patch.object(sim, 'inject_wiki_context', side_effect=RuntimeError('db down')):
            self.assertEqual(content._wiki_context_for_task({'agents': {}}, {'room': 'pressoffice'}), '')


class RecordClassifiedFailures(unittest.TestCase):
    # The 'sort' step: verified-failed and escalated requirements are
    # classified into the four buckets and written to the failure ledger, so
    # the weekly rule-mining pass can turn recurring patterns into operator
    # rule proposals. Best-effort -- a ledger failure never breaks the review.

    def test_verified_fails_are_classified_and_recorded(self):
        grades = [{'id': 'j1', 'question': 'quote the price', 'section': 'pricing',
                   'type': 'jev', 'verdict': content.GRADE_FAILS, 'confidence': 0.9},
                  {'id': 'j2', 'question': 'Is it specific?', 'section': 'opening',
                   'type': 'jev', 'verdict': content.GRADE_MEETS, 'confidence': 0.9},
                  {'id': 'j3', 'question': 'Any missing fields?', 'section': 'meta',
                   'type': 'jev', 'verdict': content.GRADE_FAILS, 'confidence': 0.4}]
        calls = []
        with mock.patch.object(content._serve, '_classify_failure',
                               side_effect=['factual_error', 'missing_information']), \
             mock.patch.object(content._serve, '_record_failure',
                               side_effect=lambda *a, **k: calls.append((a, k)) or 'fail-1'):
            count = content._record_classified_failures('ada', 'review', grades, [], 'the review text')
        self.assertEqual(count, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]['agent_id'], 'ada')
        self.assertEqual(calls[0][1]['section'], 'pricing')
        self.assertEqual(calls[0][0][0], 'factual_error')
        self.assertEqual(calls[0][1]['input_text'], 'the review text')
        self.assertEqual(calls[0][1]['rule_hint'], 'quote the price')
        self.assertEqual(calls[1][0][0], 'missing_information')  # meets grades never recorded

    def test_escalated_requirements_are_recorded(self):
        escalate = [({'id': 'h1', 'question': 'Is the tone right?', 'section': 'tone'}, 'review decision for you'),
                    ({'id': 'h2', 'question': 'Is the budget correct?', 'section': 'pricing'}, 'review decision for you')]
        calls = []
        with mock.patch.object(content._serve, '_classify_failure',
                               side_effect=['style', 'factual_error']), \
             mock.patch.object(content._serve, '_record_failure',
                               side_effect=lambda *a, **k: calls.append((a, k)) or 'fail-1'):
            content._record_classified_failures('ada', 'review', [], escalate, 'text')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][0], 'factual_error')  # money signal sorts deterministically

    def test_ledger_failure_never_breaks_the_review(self):
        grades = [{'id': 'j1', 'question': 'Q?', 'section': 's', 'type': 'jev',
                   'verdict': content.GRADE_FAILS, 'confidence': 0.9}]
        with mock.patch.object(content._serve, '_classify_failure',
                               side_effect=RuntimeError('db down')), \
             mock.patch.object(content._serve, '_record_failure'):
            self.assertEqual(content._record_classified_failures('ada', 'review', grades, [], 'text'), 0)

    def test_escalate_branch_ledger_failure_is_swallowed(self):
        # The same best-effort guarantee applies to the escalate loop: a ledger
        # failure while recording an escalated requirement never breaks review.
        escalate = [({'id': 'h1', 'question': 'Q?', 'section': 's'}, 'review decision for you')]
        with mock.patch.object(content._serve, '_classify_failure',
                               side_effect=RuntimeError('db down')), \
             mock.patch.object(content._serve, '_record_failure'):
            self.assertEqual(content._record_classified_failures('ada', 'review', [], escalate, 'text'), 0)


class ReviewTwoLineHandoff(unittest.TestCase):
    # The "write two lines for the person" rule: a checklist escalation carries
    # what the reviewer checked and what to look at first into create_escalation.

    def test_checklist_escalation_carries_two_line_handoff(self):
        checklist = [{'id': 'h1', 'question': 'Is the tone right?', 'section': 'tone', 'type': 'human'}]
        snapshot = {}
        task = {'id': 'task-2l', 'title': 'Review landing', 'room': 'pressoffice',
                'taskType': 'review', 'projectLabel': 'landing', 'checklist': checklist}
        calls = []
        with mock.patch.object(content, '_agent_name', return_value='Ada'), \
             mock.patch.object(content, '_gather_unified_context', return_value='context'), \
             mock.patch.object(content, '_run_quality_pipeline',
                               return_value={'ok': True, 'failedStep': None, 'results': [],
                                             'note': 'quality pipeline all green'}), \
             mock.patch.object(content, '_review_screenshot',
                               return_value={'ok': True, 'review': 'visual pass'}), \
             mock.patch.object(content._serve, 'SELF_BASE_URL', 'http://x'), \
             mock.patch.object(content._serve, '_coding_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_mid_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_low_tier_slug', return_value='gpt-4o-mini'), \
             mock.patch.object(content._serve, '_http_json',
                               side_effect=lambda method, base, path, body, key: {'reply': 'the review text'}), \
             mock.patch.object(content._serve, '_call_openrouter_decision_sync',
                               side_effect=[_answer('clean', 0.95)]), \
             mock.patch.object(content._serve, '_jev_choice', side_effect=[_HOLISTIC_CLEAN]), \
             mock.patch.object(content._serve, '_classify_failure', return_value='style'), \
             mock.patch.object(content._serve, '_record_failure', return_value='fail-1'), \
             mock.patch.object(content._serve, 'create_escalation',
                               side_effect=lambda *a, **k: calls.append((a, k)) or 'esc-1'), \
             mock.patch.object(sim, '_store_content_result', return_value=None):
            content._run_review_content(snapshot, 'ada', task)
        self.assertEqual(len(calls), 1)
        _args, kwargs = calls[0]
        self.assertEqual(kwargs['look_first'], 'Is the tone right?')
        self.assertIn('Reviewed 1 checklist requirements', kwargs['what_checked'])


if __name__ == '__main__':
    unittest.main()