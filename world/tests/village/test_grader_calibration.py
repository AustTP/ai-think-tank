"""Grader calibration audit (scale slice 6).

A devastating grade (<= 1.0) on a task record that carries real artifact
evidence is the false-negative signature: the grader judged the title, not
the work (the lydia distill case -- a wiki page written, graded 0.0). Such
grades are annotated `gradeSuspect`, logged as grade_artifact_mismatch,
excluded from the trailing-grade averages, and never trigger coaching. An
honest empty record (no artifact evidence) stays a legitimate low grade.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-grade-cal-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        BROWSE_TRAIL_PATH=os.path.join(_TMP_DIR, 'browse_trail.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _task(**over):
    task = {'id': 't1', 'title': 'Distill the observatory notes',
            'room': 'pressoffice', 'taskType': 'report',
            'instructions': 'Write the wiki page.',
            'wikiPage': 'state-of-knowledge'}
    task.update(over)
    return task


def _state():
    return {
        'agentRoster': [{'id': 'theo', 'isAdmin': True},
                        {'id': 'w1', 'director': 'theo'}],
        'agents': {'theo': {'id': 'theo'}, 'w1': {'id': 'w1'}},
        'teams': [], 'tasks': {}, 'workQueue': [], 'playerInbox': [],
    }


class ArtifactEvidence(unittest.TestCase):
    def test_evidence_signals(self):
        for field, value in (('deliverableText', 'The page contents.'),
                             ('note', 'Wrote the page.'),
                             ('writtenPy', ['a.py']),
                             ('libraryPath', 'library/x'),
                             ('reportPath', 'archive/2026-report-task-1.md'),
                             ('wikiPage', 'state-of-knowledge'),
                             ('sourceUrls', ['https://example.test'])):
            self.assertTrue(sim._artifact_evidence_present(_task(**{field: value})),
                            field)

    def test_work_order_alone_is_not_artifact_evidence(self):
        self.assertFalse(sim._artifact_evidence_present(
            _task(wikiPage='', note='')))
        self.assertFalse(sim._artifact_evidence_present(None))

    def test_grade_evidence_includes_report_path_and_body(self):
        # The RVP Telegram-scan report landed via reportPath; the grader used
        # to judge from the work order alone and graded a good triage 0.
        report = '# Triage report\n\nClass 1 is a real systemic problem.'
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/report.md'), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data=report)):
            lines = sim._deliverable_evidence_lines(
                _task(reportPath='archive/2026-report-task-1.md'))
        joined = '\n'.join(lines)
        self.assertIn('Report artifact: archive/2026-report-task-1.md', joined)
        self.assertIn('Class 1 is a real systemic problem', joined)

    def test_grade_evidence_survives_a_missing_report_file(self):
        with mock.patch.object(serve, '_safe_library_path',
                               return_value='/tmp/gone.md'):
            lines = sim._deliverable_evidence_lines(
                _task(reportPath='archive/gone.md'))
        self.assertIn('Report artifact: archive/gone.md', '\n'.join(lines))


class CalibrationAudit(unittest.TestCase):
    def _grade(self, grade_value, **task_over):
        s = _state()
        now_ms = int(time.time() * 1000)
        with unittest.mock.patch.object(sim, '_grading_decider',
                                        return_value=grade_value):
            sim._grade_completed_task(s, 'w1', _task(**task_over), now_ms)
            # Two-phase contract: the real judgment is computed off the tick
            # thread; the test seam runs the same worker batch + merge
            # synchronously (see tests/village/conftest.py).
            sim._grade_pending_sync(s)
        return s

    def test_zero_grade_on_real_artifact_is_suspect(self):
        s = self._grade(0.0)
        entry = s['completedDeliverables'][0]
        self.assertTrue(entry['gradeSuspect'])
        self.assertEqual(entry['grade'], 0.0)
        self.assertTrue(entry['gradeIsReal'])

    def test_suspect_grade_never_coaches(self):
        s = self._grade(0.0)
        self.assertNotIn('w1', s.get('growthPlans') or {})

    def test_below_floor_above_ceiling_still_coaches(self):
        s = self._grade(3.0)
        entry = s['completedDeliverables'][0]
        self.assertFalse(entry['gradeSuspect'])
        plans = (s.get('growthPlans') or {}).get('w1') or []
        self.assertEqual(len(plans), 1)

    def test_zero_on_empty_record_is_a_legitimate_grade(self):
        s = self._grade(0.0, wikiPage='', note='', deliverableText='')
        entry = s['completedDeliverables'][0]
        self.assertFalse(entry['gradeSuspect'])
        plans = (s.get('growthPlans') or {}).get('w1') or []
        self.assertEqual(len(plans), 1)

    def test_fabricated_fallback_is_never_suspect(self):
        # An outage fallback (gradeIsReal False) already carries its own
        # marker; the audit must not double-flag it.
        with unittest.mock.patch.object(sim, '_grading_decider',
                                        return_value=None):
            s = _state()
            sim._grade_completed_task(s, 'w1', _task(),
                                      int(time.time() * 1000))
        entry = s['completedDeliverables'][0]
        self.assertFalse(entry['gradeIsReal'])
        self.assertFalse(entry['gradeSuspect'])


class SuspectGradesStayOutOfAverages(unittest.TestCase):
    def test_agent_trailing_grade_excludes_suspect(self):
        s = _state()
        s['completedDeliverables'] = [
            {'agentId': 'w1', 'grade': 7.0, 'gradeIsReal': True},
            {'agentId': 'w1', 'grade': 0.0, 'gradeIsReal': True,
             'gradeSuspect': True},
        ]
        self.assertEqual(sim._agent_trailing_grade(s, 'w1'), 7.0)

    def test_room_trailing_grade_excludes_suspect(self):
        s = _state()
        s['completedDeliverables'] = [
            {'room': 'pressoffice', 'grade': 7.0, 'gradeIsReal': True},
            {'room': 'pressoffice', 'grade': 0.0, 'gradeIsReal': True,
             'gradeSuspect': True},
        ]
        self.assertEqual(sim._room_trailing_grade(s, 'pressoffice'), 7.0)


if __name__ == '__main__':
    unittest.main()


class GradePromptEvidence(unittest.TestCase):
    """Regression (2026-11-01): the honest-absence line lived in a try/else
    that fired whenever the anchors call merely did not RAISE -- so a prompt
    that carried the completion note ALSO claimed the record had none, and
    the judge (honestly) graded 0. Evidence and the absence line are now
    mutually exclusive by construction."""

    def _captured_prompt(self, task_over, anchors='none'):
        s = _state()
        captured = {}

        def fake_decider(state, instructions, *args, **kwargs):
            captured['prompt'] = instructions
            return 8.0

        with unittest.mock.patch.object(sim, '_grading_decider',
                                        side_effect=fake_decider):
            with unittest.mock.patch.object(
                    serve, '_rag_room_anchors',
                    return_value=([] if anchors == 'none'
                                  else ['Comparable prior deliverable in this '
                                        'room, "x" (task-9), was graded 8/10.'])):
                sim._grade_completed_task(s, 'w1', _task(**task_over),
                                          int(time.time() * 1000))
                sim._grade_pending_sync(s)
        return captured.get('prompt', '')

    def test_evidence_present_never_claims_absence(self):
        prompt = self._captured_prompt({'note': 'Wrote the page.'})
        self.assertIn('Wrote the page.', prompt)
        self.assertNotIn('no deliverable content', prompt)

    def test_absence_line_only_on_an_empty_record(self):
        prompt = self._captured_prompt({'note': '', 'wikiPage': '', 'instructions': ''})
        self.assertIn('no deliverable content', prompt)

    def test_evidence_and_anchors_coexist_without_absence(self):
        prompt = self._captured_prompt({'note': 'Wrote the page.'},
                                       anchors='some')
        self.assertIn('Wrote the page.', prompt)
        self.assertIn('Comparable prior deliverable', prompt)
        self.assertNotIn('no deliverable content', prompt)

    def test_anchor_outage_keeps_evidence_prompt(self):
        def boom(*args, **kwargs):
            raise RuntimeError('rag down')
        s = _state()
        captured = {}

        def fake_decider(state, instructions, *args, **kwargs):
            captured['prompt'] = instructions
            return 8.0

        with unittest.mock.patch.object(sim, '_grading_decider',
                                        side_effect=fake_decider):
            with unittest.mock.patch.object(serve, '_rag_room_anchors',
                                            side_effect=boom):
                sim._grade_completed_task(
                    s, 'w1', _task(note='Wrote the page.'),
                    int(time.time() * 1000))
                sim._grade_pending_sync(s)
        self.assertIn('Wrote the page.', captured.get('prompt', ''))
        self.assertNotIn('no deliverable content', captured.get('prompt', ''))
