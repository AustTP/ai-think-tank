"""Hermetic tests for the investigations composite (coherence review,
Priority 1: the case-level index over the ledger/tasks/wiki). The store
seam is swapped for in-memory dicts, same pattern as test_epistemics.py.

Properties under test:
- a case derives its validation state LIVE from referenced ledger items
  (established / contradicted / stale / partial / unverified), never a
  stored stale copy;
- a conclusion requires text, explicit confidence, and at least one cited
  ledger item (the conclusion rests on the ledger, not the case object);
- open disposition lifecycle: conclude once, then it is closed;
- reference add/drop is idempotent and recorded in history;
- overdue and reassessment flags derive from the clock, not storage.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.epistemics as epistemics  # noqa: E402
import core.evidence as evidence  # noqa: E402
import core.investigations as investigations  # noqa: E402


class InvestigationsBase(unittest.TestCase):
    def setUp(self):
        stores = {'investigations': {}, 'epistemic': {}, 'evidence': {},
                  'coverage': {}}
        self.stores = stores
        patchers = [
            unittest.mock.patch('core.investigations._store_read',
                                lambda: stores['investigations']),
            unittest.mock.patch('core.investigations._store_write',
                                lambda blob: stores.__setitem__(
                                    'investigations', blob)),
            unittest.mock.patch('core.epistemics._ledger_read',
                                lambda: stores['epistemic']),
            unittest.mock.patch('core.epistemics._ledger_write',
                                lambda blob: stores.__setitem__(
                                    'epistemic', blob)),
            unittest.mock.patch('core.evidence._evidence_read',
                                lambda: stores['evidence']),
            unittest.mock.patch('core.evidence._evidence_write',
                                lambda rows: stores.__setitem__(
                                    'evidence', rows)),
            unittest.mock.patch('core.evidence._coverage_read',
                                lambda: stores['coverage']),
            unittest.mock.patch('core.evidence._coverage_write',
                                lambda rows: stores.__setitem__(
                                    'coverage', rows)),
            unittest.mock.patch('core.evidence.append_audit',
                                return_value=None),
        ]
        for p in patchers:
            p.start()
        self.patchers = patchers

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _validated_item(self, text='The pipeline is audited'):
        ep = epistemics
        item = ep.record_assertion(text, actor='ada')
        claim = evidence.record_source_claim(
            {'source_url': f'https://x.example/{item["id"]}',
             'claim': f'Evidence for {text}'}, actor='ada')
        v = ep.propose_validation(item['id'], 'check it', actor='ada')
        ep.start_validation(v['id'], cost_ok=True, actor='ada')
        ep.complete_validation(v['id'], 'Confirmed', claim_id=claim['id'],
                               actor='ben')
        ep.review_validation(v['id'], True, reviewer='cid')
        return item['id'], claim['id']

    def _open_case(self, **kw):
        out = investigations.create_investigation(
            kw.pop('question', 'Is the warehouse roof leaking?'), actor='ada',
            **kw)
        self.assertTrue(out.get('created'), out)
        return out['id']


class CreationAndDerivation(InvestigationsBase):
    def test_create_requires_a_question(self):
        out = investigations.create_investigation('   ', actor='ada')
        self.assertFalse(out.get('created'))
        self.assertEqual(out.get('reason'), 'invalid')

    def test_unverified_then_established_then_contradicted(self):
        inv = self._open_case()
        self.assertEqual(investigations.get_investigation(inv)
                         ['validationState'], 'unverified')
        item_id, claim_id = self._validated_item()
        investigations.update_investigation(
            inv, actor='ada', add_ledger_item_ids=[item_id])
        self.assertEqual(investigations.get_investigation(inv)
                         ['validationState'], 'established')
        # A contradicting claim flips the DERIVED state of the case.
        claim2 = evidence.record_source_claim(
            {'source_url': 'https://x.example/refute',
             'claim': 'No audit happened'}, actor='dev')
        recorded = epistemics.record_contradiction(item_id, claim_id,
                                                   claim2['id'],
                                                   note='audit log '
                                                        'contradicted',
                                                   actor='dev')
        self.assertTrue(recorded.get('recorded'), recorded)
        self.assertEqual(investigations.get_investigation(inv)
                         ['validationState'], 'contradicted')

    def test_mixed_statuses_are_partial(self):
        inv = self._open_case()
        good, _c = self._validated_item('good pipe')
        open_item = epistemics.record_unknown(
            'does the other pipeline audit?', actor='ada')
        investigations.update_investigation(
            inv, actor='ada', add_ledger_item_ids=[good, open_item['id']])
        self.assertEqual(investigations.get_investigation(inv)
                         ['validationState'], 'partial')

    def test_overdue_and_reassessment_are_derived_from_the_clock(self):
        far = 4102444800000    # 2100: always future
        past = 1000            # always past
        inv = investigations.create_investigation(
            'deadline case', actor='ada', deadline_ms=far,
            reassessment_ms=far)
        case = investigations.get_investigation(inv['id'])
        self.assertFalse(case['overdue'])
        self.assertFalse(case['reassessmentDue'])
        past_case = investigations.get_investigation(
            inv['id'], now_ms=far + 1)
        self.assertTrue(past_case['overdue'])
        self.assertTrue(past_case['reassessmentDue'])


class ConclusionDiscipline(InvestigationsBase):
    def test_conclusion_requires_ledger_items(self):
        inv = self._open_case()
        out = investigations.conclude_investigation(
            inv, 'It leaks', confidence='high', actor='ada')
        self.assertFalse(out.get('concluded'))
        self.assertEqual(out.get('reason'), 'no_ledger_items')

    def test_conclusion_requires_confidence(self):
        inv = self._open_case(ledger_item_ids=[self._validated_item()[0]])
        out = investigations.conclude_investigation(
            inv, 'It leaks', actor='ada')
        self.assertFalse(out.get('concluded'))

    def test_conclude_then_closed(self):
        inv = self._open_case(ledger_item_ids=[self._validated_item()[0]])
        out = investigations.conclude_investigation(
            inv, 'It leaks at the north seam', confidence='high',
            actor='ada')
        self.assertTrue(out.get('concluded'))
        again = investigations.conclude_investigation(
            inv, 'Reopen attempt', confidence='low', actor='ada')
        self.assertFalse(again.get('concluded'))
        self.assertEqual(again.get('reason'), 'wrong_disposition')

    def test_abandoned_needs_no_evidence(self):
        inv = self._open_case()
        out = investigations.conclude_investigation(
            inv, 'Operator withdrew the question', disposition='abandoned',
            actor='ada')
        self.assertTrue(out.get('concluded'))
        self.assertEqual(out.get('case', {}).get('confidence'), None)


class ReferenceBookkeeping(InvestigationsBase):
    def test_refs_are_deduped_and_history_records_changes(self):
        inv = self._open_case()
        item_id, _claim = self._validated_item()
        first = investigations.update_investigation(
            inv, actor='ada', add_ledger_item_ids=[item_id])
        self.assertTrue(first.get('updated'))
        # Re-adding the same ref is a deliberate no-op: dedupe, no history.
        second = investigations.update_investigation(
            inv, actor='ben', add_ledger_item_ids=[item_id])
        self.assertFalse(second.get('updated'))
        self.assertEqual(second.get('reason'), 'no_changes')
        # A real change (owner reassignment) IS recorded.
        third = investigations.update_investigation(inv, actor='ben',
                                                    owner='ben')
        self.assertTrue(third.get('updated'))
        case = investigations.get_investigation(inv)
        self.assertEqual(case['ledgerItemIds'], [item_id])
        updates = [h for h in case.get('history', [])
                   if h.get('event') == 'updated']
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]['actor'], 'ada')
        self.assertEqual(updates[1]['actor'], 'ben')

    def test_drop_refs(self):
        inv = self._open_case(ledger_item_ids=[self._validated_item()[0]])
        item_id = investigations.get_investigation(inv)['ledgerItemIds'][0]
        out = investigations.update_investigation(inv, actor='ada',
                                                  drop_ref_ids=[item_id])
        self.assertTrue(out.get('updated'))
        self.assertEqual(
            investigations.get_investigation(inv)['ledgerItemIds'], [])
        self.assertEqual(investigations.get_investigation(inv)
                         ['validationState'], 'unverified')
