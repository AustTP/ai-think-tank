"""Hermetic tests for the Question Registry (world/core/questions.py).

Same seam pattern as test_evidence.py: the registry's SQLite row and the
audit trail are swapped for in-memory structures, so no DB and no network is
ever touched. The lifecycle property under test throughout: a question is
OPEN until evidence closes it, a contradiction clears the answer, and a
raised conflict keeps the old->new chain auditable instead of overwriting.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.questions as questions  # noqa: E402


def _patch_registry(rows=None):
    store = {'questions': rows if rows is not None else {}}
    patchers = [
        unittest.mock.patch('core.questions._questions_read',
                            lambda: store['questions']),
        unittest.mock.patch('core.questions._questions_write',
                            lambda rows: store.__setitem__('questions', rows)),
        unittest.mock.patch('core.questions._audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return store


def _q(text, **over):
    rec = {'text': text}
    rec.update(over)
    return questions.record_question(**rec)


class RecordAndDedup(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_records_new_question(self):
        _patch_registry()
        result = questions.record_question(
            'What telemetry exposes prompt-injection abuse?',
            objective='monitoring',
            people_who_may_know=['Brian Barrett'],
        )
        self.assertTrue(result['recorded'])
        self.assertTrue(result['new'])
        self.assertEqual(result['id'], questions.get_question(result['id'])['id'])

    def test_same_text_is_one_question_with_two_askers(self):
        _patch_registry()
        first = questions.record_question('Who owns SIEM detections?', source='Austin')
        second = questions.record_question('Who owns SIEM detections?', source='Theo')
        self.assertFalse(second['new'])
        self.assertEqual(first['id'], second['id'])
        rec = questions.get_question(first['id'])
        self.assertEqual(rec['asked_by'], ['Austin', 'Theo'])

    def test_repeat_adds_people_without_duplicating(self):
        _patch_registry()
        first = questions.record_question('Where does telemetry land?',
                                          people_who_may_know=['Brian'])
        second = questions.record_question('Where does telemetry land?',
                                           people_who_may_know=['Elias'])
        rec = questions.get_question(first['id'])
        self.assertEqual(rec['people_who_may_know'], ['Brian', 'Elias'])
        self.assertFalse(second['new'])

    def test_blank_text_rejected(self):
        _patch_registry()
        result = questions.record_question('   ')
        self.assertFalse(result['recorded'])
        self.assertEqual(result['reason'], 'invalid')


class AnswerLifecycle(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_answer_closes_question(self):
        _patch_registry()
        result = _q('Is that telemetry available?')
        rec = questions.answer_question(
            result['id'], 'Raw findings land in the SIEM.', confidence=0.9)
        self.assertEqual(rec['status'], 'answered')
        self.assertFalse(rec['status'] in questions.OPEN_STATUSES)
        self.assertEqual(rec['answer'], 'Raw findings land in the SIEM.')

    def test_tentative_answer_stays_soft(self):
        _patch_registry()
        result = _q('Is that telemetry available?')
        rec = questions.answer_question(result['id'], 'Probably', status='tentatively_answered')
        self.assertEqual(rec['status'], 'tentatively_answered')

    def test_contradicted_is_not_an_answer(self):
        _patch_registry()
        result = _q('q?')
        with self.assertRaises(ValueError):
            questions.answer_question(result['id'], 'nope', status='contradicted')

    def test_contradiction_clears_answer_and_keeps_evidence(self):
        _patch_registry()
        result = _q('Ingestion is the primary gap?',
                    evidence_claim_ids=['ev-1'])
        questions.answer_question(result['id'], 'Yes')
        rec = questions.contradict_question(result['id'], evidence_claim_id='ev-2')
        self.assertEqual(rec['status'], 'contradicted')
        self.assertIsNone(rec['answer'])
        self.assertEqual(rec['evidence_claim_ids'], ['ev-1', 'ev-2'])

    def test_open_questions_filtered_by_objective(self):
        _patch_registry()
        _q('a?', objective='monitoring')
        _q('b?', objective='inventory')
        done = _q('c?', objective='monitoring')
        questions.answer_question(done['id'], 'yes')
        ids = [q['id'] for q in questions.open_questions('monitoring')]
        self.assertEqual(len(ids), 1)

    def test_confidence_bounds_enforced(self):
        _patch_registry()
        result = _q('q?')
        with self.assertRaises(ValueError):
            questions.answer_question(result['id'], 'x', confidence=1.5)
        with self.assertRaises(ValueError):
            questions.answer_question(result['id'], 'x', confidence='high')


class RaiseConflict(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_reframe_closes_old_and_links_new(self):
        _patch_registry()
        old = questions.record_question('How do we ingest the data?')
        new = questions.raise_conflict(
            old['id'], 'What content should we build from it?',
            people_who_may_know=['Brian Barrett'])
        self.assertEqual(questions.get_question(old['id'])['status'], 'contradicted')
        self.assertIn(old['id'], new['related_question_ids'])
        self.assertIn(new['id'], [q['id'] for q in questions.open_questions()])

    def test_conflict_with_unknown_old_question(self):
        _patch_registry()
        new = questions.raise_conflict('q-nonexistent', 'Reframed question?')
        self.assertTrue(new['id'])

    def test_conflict_requires_text(self):
        _patch_registry()
        with self.assertRaises(ValueError):
            questions.raise_conflict('q-x', '   ')


class ConversationPrep(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_caps_questions_at_max(self):
        store = _patch_registry()
        for n in range(8):
            questions.record_question(f'question {n} about SIEM?',
                                      people_who_may_know=['Brian Barrett'])
        with unittest.mock.patch('core.questions.record_question', questions.record_question):
            # attention_quote / stakeholders degrade gracefully without stubs
            brief = questions.prepare_conversation('Brian Barrett', max_questions=5)
        self.assertLessEqual(len(brief['asks']), 5)
        self.assertEqual(len(brief['unknown']), len(brief['asks']))
        del store

    def test_do_not_ask_lists_answered_questions(self):
        _patch_registry()
        done = questions.record_question('What does AIDR already provide?',
                                         people_who_may_know=['Brian Barrett'])
        questions.answer_question(done['id'], 'Searchable SIEM events.')
        questions.record_question('Which events become SOC alerts?',
                                  people_who_may_know=['Brian Barrett'])
        brief = questions.prepare_conversation('Brian Barrett')
        self.assertEqual([d['question_id'] for d in brief['do_not_ask']],
                         [done['id']])

    def test_requires_person(self):
        _patch_registry()
        with self.assertRaises(ValueError):
            questions.prepare_conversation('  ')


if __name__ == '__main__':
    unittest.main()
