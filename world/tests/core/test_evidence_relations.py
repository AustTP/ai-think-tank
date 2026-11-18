"""Evidence relations + strength scale (coherence review Priority 2):
attachments can be supports / partially_supports / context_only / duplicate,
and carry an E0-E4 strength grade. The behavioral contract:
- only 'supports' attachments count toward independent corroboration, so
  partially-supporting or context-only records never evidence a claim by
  themselves;
- legacy entries (no relation field) keep their exact old meaning;
- strength is validated (0-4) and stored, but never gates on its own;
- the item view surfaces relation + strength so briefings and metrics can
  reason about evidence quality.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.evidence as evidence  # noqa: E402
import core.epistemics as epistemics  # noqa: E402


class RelationsBase(unittest.TestCase):
    def setUp(self):
        stores = {'epistemic': {}, 'evidence': {}, 'coverage': {}}
        self.stores = stores
        patchers = [
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

    def _claim(self, url, text):
        out = evidence.record_source_claim({'source_url': url,
                                            'claim': text}, actor='tester')
        self.assertTrue(out.get('recorded'), out)
        return out['id']

    def _assertion(self, text='The gate holds'):
        out = epistemics.record_assertion(text, actor='ada')
        return out['id']


class RelationSemantics(RelationsBase):
    def test_partially_supports_never_corroborates(self):
        item_id = self._assertion()
        claim = self._claim('https://a.example/1', 'kind of relevant')
        out = epistemics.attach_evidence(
            item_id, claim, relation='partially_supports', actor='ada')
        self.assertTrue(out['attached'])
        self.assertEqual(out['independentSources'], 0)
        self.assertEqual(out['status'], 'unknown')

    def test_context_only_and_duplicate_never_corroborate(self):
        item_id = self._assertion()
        for relation in ('context_only', 'duplicate'):
            claim = self._claim(f'https://a.example/{relation}', 'bg')
            out = epistemics.attach_evidence(item_id, claim,
                                             relation=relation, actor='ada')
            self.assertEqual(out['independentSources'], 0, relation)

    def test_supports_still_evidences(self):
        item_id = self._assertion()
        claim = self._claim('https://a.example/real', 'the real finding')
        out = epistemics.attach_evidence(item_id, claim, actor='ada')
        self.assertEqual(out['independentSources'], 1)
        self.assertEqual(out['status'], 'evidenced')

    def test_legacy_entries_without_relation_keep_their_meaning(self):
        item_id = self._assertion()
        claim = self._claim('https://a.example/legacy', 'old-style attach')
        out = epistemics.attach_evidence(item_id, claim, actor='ada')
        self.assertEqual(out['status'], 'evidenced')
        entry = epistemics.get_item(item_id)['evidence'][-1]
        self.assertNotIn('relation', entry)  # unchanged shape
        self.assertEqual(epistemics.independent_source_count(
            epistemics.get_item(item_id)), 1)

    def test_invalid_relation_rejected(self):
        item_id = self._assertion()
        claim = self._claim('https://a.example/x', 'x')
        out = epistemics.attach_evidence(item_id, claim, relation='vibes',
                                         actor='ada')
        self.assertFalse(out['attached'])
        self.assertEqual(out['reason'], 'invalid')

    def test_supports_false_still_means_contradicting_side(self):
        # supports=False is the contradicting-side attachment (pre-relation
        # API); it must stay non-corroborating with or without a relation.
        item_id = self._assertion()
        claim = self._claim('https://a.example/no', 'it does not hold')
        out = epistemics.attach_evidence(item_id, claim, supports=False,
                                         actor='ada')
        self.assertEqual(out['independentSources'], 0)
        out2 = epistemics.attach_evidence(item_id, claim,
                                          supports=False,
                                          relation='context_only',
                                          actor='ada')
        self.assertEqual(out2['independentSources'], 0)


class StrengthScale(RelationsBase):
    def test_strength_bounds(self):
        item_id = self._assertion()
        claim = self._claim('https://b.example/e4', 'runtime proof')
        ok = epistemics.attach_evidence(item_id, claim, strength=4,
                                        actor='ada')
        self.assertTrue(ok['attached'])
        bad = epistemics.attach_evidence(item_id, claim, strength=5,
                                         actor='ada')
        self.assertFalse(bad['attached'])
        bad2 = epistemics.attach_evidence(item_id, claim, strength='high',
                                          actor='ada')
        self.assertFalse(bad2['attached'])

    def test_strength_stored_and_surfaced_never_gates(self):
        # An E0 (assertion-grade) attachment still corroborates: strength
        # measures quality for humans and metrics; the corroboration gate
        # is identity + relation, not grade.
        item_id = self._assertion()
        claim = self._claim('https://b.example/e0', 'someone said so')
        out = epistemics.attach_evidence(item_id, claim, strength=0,
                                         actor='ada')
        self.assertEqual(out['status'], 'evidenced')
        view = epistemics.get_item(item_id)
        self.assertEqual(view['evidence'][-1]['strength'], 0)
        snapshot = epistemics.epistemic_snapshot()
        items = (snapshot.get('items') or {}) if isinstance(
            snapshot.get('items'), dict) else {}
        if items:
            sources = (items.get(item_id) or {}).get('sources') or []
            self.assertTrue(all('strength' in s for s in sources))
