"""Hermetic tests for the epistemic ledger (core/epistemics.py).

The ledger keeps its own SQLite row (never the kv_state blob) and every
public function reads/writes through _ledger_read/_ledger_write. These tests
replace that seam -- and the evidence ledger's four seams it attaches
through -- with in-memory dicts, so no DB and no network is touched (same
pattern as test_evidence.py).

The safety properties under test throughout:
- two agents repeating the same source never count as independent
  corroboration (identity-level dedup on attachments);
- a validation result never validates its item without a DIFFERENT
  reviewer's approval;
- contradictory evidence stays visible until a written resolution, and a
  resolution cannot invent a winner outside the recorded sides;
- stale records stay readable but lose trusted-current status;
- decisions cite only records that exist, and replay shows what changed.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import core.evidence as evidence  # noqa: E402
import core.epistemics as epistemics  # noqa: E402


def _patch_all():
    """Swap every storage seam for in-memory dicts. Returns the backing
    stores so tests can seed/inspect evidence claims."""
    stores = {
        'epistemic': {},
        'evidence': {},
        'coverage': {},
    }
    patchers = [
        unittest.mock.patch('core.epistemics._ledger_read',
                            lambda: stores['epistemic']),
        unittest.mock.patch('core.epistemics._ledger_write',
                            lambda blob: stores.__setitem__('epistemic', blob)),
        unittest.mock.patch('core.evidence._evidence_read',
                            lambda: stores['evidence']),
        unittest.mock.patch('core.evidence._evidence_write',
                            lambda rows: stores.__setitem__('evidence', rows)),
        unittest.mock.patch('core.evidence._coverage_read',
                            lambda: stores['coverage']),
        unittest.mock.patch('core.evidence._coverage_write',
                            lambda rows: stores.__setitem__('coverage', rows)),
        unittest.mock.patch('core.evidence.append_audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return stores


def _claim(stores, url, text, *, resolved=None):
    """Seed one evidence claim through the real evidence API."""
    out = evidence.record_source_claim(
        {'source_url': url, 'claim': text, 'resolved_url': resolved},
        actor='tester')
    assert out.get('recorded'), out
    return out['id']


FRESH = 4102444800000  # 2100-01-01, always future at grading time
STALE = 1              # epoch-ms 1, always past


class EpistemicLifecycle(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_unknown_recorded_and_deduped_by_text(self):
        a = epistemics.record_unknown('Is the pipeline audited?', actor='mira')
        b = epistemics.record_unknown('is  the  pipeline AUDITED?', actor='june')
        self.assertTrue(a['recorded'] and a['new'])
        self.assertTrue(b['recorded'] and not b['new'])
        self.assertEqual(a['id'], b['id'])
        item = epistemics.get_item(a['id'])
        self.assertEqual(sorted(item['asked_by']), ['june', 'mira'])

    def test_hypothesis_starts_hypothesized_not_established(self):
        out = epistemics.record_hypothesis('Gaps live in the exec hop',
                                           actor='sam')
        self.assertEqual(out['status'], 'hypothesized')
        self.assertEqual(epistemics.classify_item(epistemics.get_item(out['id'])),
                         'Hypothesis')

    def test_assertion_stays_unknown_and_grades_unsupported(self):
        out = epistemics.record_assertion('The pager auto-escalates',
                                          actor='mira')
        self.assertEqual(out['status'], 'unknown')
        self.assertEqual(epistemics.classify_item(epistemics.get_item(out['id'])),
                         'Unsupported Assumption')

    def test_evidence_advances_unknown_to_evidenced(self):
        item = epistemics.record_unknown('Is the pipeline audited?',
                                         actor='mira')['id']
        cid = _claim(self.stores, 'https://docs.example.com/a',
                     'The pipeline is audited')
        out = epistemics.attach_evidence(item, cid, actor='mira')
        self.assertTrue(out['attached'])
        self.assertEqual(out['status'], 'evidenced')
        self.assertEqual(out['independentSources'], 1)

    def test_same_source_twice_is_corroboration_never_independent(self):
        item = epistemics.record_unknown('Is the pipeline audited?',
                                         actor='mira')['id']
        c1 = _claim(self.stores, 'https://docs.example.com/a',
                    'The pipeline is audited')
        c2 = _claim(self.stores, 'https://docs.example.com/a',
                    'Mirror: the pipeline is audited')
        first = epistemics.attach_evidence(item, c1, actor='mira')
        self.assertFalse(first['corroborated'])
        second = epistemics.attach_evidence(item, c2, actor='june')
        self.assertTrue(second['corroborated'])
        self.assertEqual(second['independentSources'], 1)
        self.assertEqual(second['status'], 'evidenced')

    def test_distinct_identities_are_independent_sources(self):
        item = epistemics.record_unknown('Is the pipeline audited?',
                                         actor='mira')['id']
        c1 = _claim(self.stores, 'https://docs.example.com/a', 'Audited')
        c2 = _claim(self.stores, 'https://telemetry.example.com/b', 'Seen live')
        epistemics.attach_evidence(item, c1, actor='mira')
        out = epistemics.attach_evidence(item, c2, actor='june')
        self.assertEqual(out['independentSources'], 2)

    def test_attachment_requires_recorded_claim(self):
        item = epistemics.record_unknown('Is the pipeline audited?',
                                         actor='mira')['id']
        out = epistemics.attach_evidence(item, 'ev-nonexistent', actor='mira')
        self.assertEqual(out['reason'], 'unknown_claim')

    def test_attachment_refused_on_superseded_item(self):
        item = epistemics.record_unknown('Old claim', actor='mira')['id']
        sup = epistemics.supersede_item(item, 'New claim', actor='theo')
        self.assertTrue(sup['superseded'])
        cid = _claim(self.stores, 'https://docs.example.com/a', 'x')
        out = epistemics.attach_evidence(item, cid, actor='mira')
        self.assertEqual(out['reason'], 'closed')
        self.assertEqual(epistemics.get_item(item)['status'], 'superseded')


class ValidationGate(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()
        self.item = epistemics.record_unknown('Does the alert reach the rota?',
                                              actor='mira')['id']

    def tearDown(self):
        unittest.mock.patch.stopall()

    def _validated_path(self):
        val = epistemics.propose_validation(
            self.item, 'Trace one blocked attempt end to end',
            estimated_cost_usd=0.5, worth_it=True, actor='mira')['id']
        self.assertEqual(epistemics.start_validation(val, cost_ok=True,
                                                     actor='mira')['status'],
                         'running')
        done = epistemics.complete_validation(
            val, 'Event id visible in the rota channel',
            source_url='https://siem.example.com/q', claim_text='Rota sees it',
            actor='mira')
        self.assertTrue(done['completed'])
        self.assertTrue(done['awaitingReview'])
        return val, done['resultClaimId']

    def test_propose_start_complete_awaiting_review(self):
        val, claim_id = self._validated_path()
        item = epistemics.get_item(self.item)
        self.assertEqual(item['status'], 'unknown',
                         'completion alone must NOT validate the item')
        self.assertEqual(epistemics.get_validation(val)['status'],
                         'completed')

    def test_bank_exhaustion_parks_the_validation_blocked(self):
        val = epistemics.propose_validation(
            self.item, 'Trace one blocked attempt', actor='mira')['id']
        out = epistemics.start_validation(val, cost_ok=False, actor='mira')
        self.assertEqual(out['reason'], 'bank_budget_exhausted')
        v = epistemics.get_validation(val)
        self.assertFalse(v['status'] == 'running')
        self.assertTrue(v['blocked'])
        self.assertIn('bank budget', v['blockedReason'])

    def test_blocked_validation_never_starts(self):
        val = epistemics.propose_validation(
            self.item, 'Pull the ticket export', blocked=True,
            blocked_reason='no authority to read tickets', actor='mira')['id']
        out = epistemics.start_validation(val, cost_ok=True, actor='mira')
        self.assertEqual(out['reason'], 'blocked')

    def test_result_requires_a_cited_claim(self):
        val = epistemics.propose_validation(self.item, 'Trace it',
                                            actor='mira')['id']
        epistemics.start_validation(val, cost_ok=True, actor='mira')
        out = epistemics.complete_validation(val, 'Looks fine', actor='mira')
        self.assertEqual(out['reason'], 'invalid')

    def test_reviewer_independence_enforced(self):
        val, _claim_id = self._validated_path()
        out = epistemics.review_validation(val, True, reviewer='mira')
        self.assertEqual(out['reason'], 'reviewer_independence')
        self.assertEqual(epistemics.get_item(self.item)['status'], 'unknown')

    def test_other_reviewer_approval_validates(self):
        val, _claim_id = self._validated_path()
        out = epistemics.review_validation(val, True, reviewer='verifier',
                                           authoritative_owner='platform')
        self.assertEqual(out['itemStatus'], 'validated')
        item = epistemics.get_item(self.item)
        self.assertEqual(item['status'], 'validated')
        self.assertEqual(item['authoritativeOwner'], 'platform')
        self.assertEqual(epistemics.classify_item(item), 'Known')

    def test_rejection_leaves_item_unvalidated(self):
        val, _claim_id = self._validated_path()
        out = epistemics.review_validation(val, False, reviewer='verifier',
                                           note='evidence does not show the rota')
        self.assertEqual(out['status'], 'rejected')
        self.assertEqual(epistemics.get_item(self.item)['status'], 'unknown')


class Contradictions(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()
        self.item = epistemics.record_unknown('Is filter v2 deployed?',
                                              actor='mira')['id']
        self.a = _claim(self.stores, 'https://docs.example.com/f',
                        'v2 is deployed')
        self.b = _claim(self.stores, 'https://telemetry.example.com/c',
                        'v2 signature absent in prod')
        epistemics.attach_evidence(self.item, self.a, actor='mira')
        epistemics.attach_evidence(self.item, self.b, actor='june')

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_contradiction_disputes_and_stays_open(self):
        out = epistemics.record_contradiction(self.item, self.a, self.b,
                                              actor='verifier')
        self.assertTrue(out['recorded'])
        self.assertEqual(out['itemStatus'], 'disputed')
        item = epistemics.get_item(self.item)
        self.assertEqual(item['status'], 'disputed')
        self.assertEqual(item['preDisputeStatus'], 'evidenced')
        snap = epistemics.epistemic_snapshot()
        self.assertEqual(len(snap['openContradictions']), 1)
        self.assertEqual(snap['openContradictions'][0]['claimIdA'], self.a)
        self.assertEqual(snap['openContradictions'][0]['claimIdB'], self.b)

    def test_resolve_requires_text_and_a_recorded_side(self):
        cx = epistemics.record_contradiction(self.item, self.a, self.b,
                                             actor='verifier')['id']
        self.assertEqual(
            epistemics.resolve_contradiction(cx, '', actor='theo')['reason'],
            'invalid')
        self.assertEqual(
            epistemics.resolve_contradiction(cx, 'I prefer telemetry',
                                             winner_claim_id='ev-nope',
                                             actor='theo')['reason'],
            'invalid')
        self.assertEqual(epistemics.get_contradiction(cx)['status'], 'open')

    def test_resolve_with_winner_restores_prior_status(self):
        cx = epistemics.record_contradiction(self.item, self.a, self.b,
                                             actor='verifier')['id']
        out = epistemics.resolve_contradiction(
            cx, 'config snapshot verified on the host; docs stale',
            winner_claim_id=self.b, actor='verifier')
        self.assertEqual(out['itemStatus'], 'evidenced')
        self.assertEqual(epistemics.get_contradiction(cx)['status'], 'resolved')

    def test_resolve_without_winner_drops_to_unresolved(self):
        cx = epistemics.record_contradiction(self.item, self.a, self.b,
                                             actor='verifier')['id']
        out = epistemics.resolve_contradiction(
            cx, 'neither side is trustworthy; needs a host check',
            winner_claim_id=None, actor='verifier')
        self.assertEqual(out['itemStatus'], 'unresolved')
        snap = epistemics.epistemic_snapshot()
        self.assertTrue(any(u['id'] == self.item for u in snap['unknowns']))

    def test_history_preserves_both_sides_after_resolution(self):
        cx = epistemics.record_contradiction(self.item, self.a, self.b,
                                             actor='verifier')['id']
        epistemics.resolve_contradiction(cx, 'telemetry verified',
                                         winner_claim_id=self.b,
                                         actor='verifier')
        item = epistemics.get_item(self.item)
        notes = [h['note'] for h in item['history']]
        self.assertTrue(any('contradiction' in n and 'resolved' in n
                            for n in notes))
        self.assertEqual(len(item['evidence']), 2,
                         'both sides must stay attached and visible')


class Staleness(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_expired_review_window_is_stale_but_readable(self):
        item = epistemics.record_unknown('Rate limit is 100 rpm',
                                         actor='mira',
                                         review_after_ms=STALE)['id']
        cid = _claim(self.stores, 'https://docs.example.com/rl', '100 rpm')
        epistemics.attach_evidence(item, cid, actor='mira',
                                   review_after_ms=STALE)
        self.assertEqual(epistemics.effective_status(
            epistemics.get_item(item)), 'stale')
        snap = epistemics.epistemic_snapshot()
        self.assertTrue(any(v['id'] == item for v in snap['stale']))
        self.assertEqual(epistemics.classify_item(
            epistemics.get_item(item)), 'Stale Evidence')

    def test_refresh_restores_trusted_status_with_history(self):
        item = epistemics.record_unknown('Rate limit is 100 rpm',
                                         actor='mira',
                                         review_after_ms=STALE)['id']
        cid = _claim(self.stores, 'https://docs.example.com/rl', '100 rpm')
        epistemics.attach_evidence(item, cid, actor='mira',
                                   review_after_ms=STALE)
        out = epistemics.refresh_stale(item, review_after_ms=FRESH,
                                       actor='mira')
        self.assertEqual(out['status'], 'evidenced')
        notes = [h['note'] for h in epistemics.get_item(item)['history']]
        self.assertTrue(any('stale review window reset' in n for n in notes))

    def test_fresh_validated_item_is_not_stale(self):
        item = epistemics.record_unknown('x', actor='mira',
                                         review_after_ms=FRESH)['id']
        cid = _claim(self.stores, 'https://docs.example.com/x', 'x is so')
        epistemics.attach_evidence(item, cid, actor='mira')
        self.assertEqual(epistemics.effective_status(
            epistemics.get_item(item)), 'evidenced')


class Supersession(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_supersede_keeps_history_and_starts_fresh(self):
        item = epistemics.record_unknown('Old rate is 100 rpm',
                                         actor='mira')['id']
        cid = _claim(self.stores, 'https://docs.example.com/rl', '100 rpm')
        epistemics.attach_evidence(item, cid, actor='mira')
        out = epistemics.supersede_item(item, 'Rate is now 500 rpm',
                                        actor='theo')
        self.assertTrue(out['superseded'])
        old = epistemics.get_item(item)
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(old['supersededBy'], out['newId'])
        self.assertTrue(old['history'])
        fresh = epistemics.get_item(out['newId'])
        self.assertEqual(fresh['supersedes'], item)
        self.assertEqual(fresh['status'], 'unknown',
                         'the replacement must earn its own status')


class Decisions(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def _seed_item(self):
        item = epistemics.record_unknown('Gateway audit is live',
                                         actor='mira')['id']
        cid = _claim(self.stores, 'https://docs.example.com/g', 'audit live')
        epistemics.attach_evidence(item, cid, actor='mira')
        val = epistemics.propose_validation(item, 'Cross-check dashboards',
                                            actor='mira')['id']
        epistemics.start_validation(val, cost_ok=True, actor='mira')
        epistemics.complete_validation(val, 'Dashboard matches',
                                       claim_id=cid, actor='mira')
        epistemics.review_validation(val, True, reviewer='verifier')
        return item, cid

    def test_decision_citations_are_validated(self):
        out = epistemics.record_decision(
            'Adopt the SIEM alert', item_ids=['ek-nonexistent'],
            actor='theo')
        self.assertEqual(out['reason'], 'unknown_citation')

    def test_decision_artifact_actions_and_replay(self):
        item, cid = self._seed_item()
        out = epistemics.record_decision(
            'Treat the SIEM alert as interim monitoring',
            rationale='audit pipeline is established',
            item_ids=[item], claim_ids=[cid],
            reconsider_when='the rota validation completes',
            actions=['Draft the detection-rule review'], actor='theo')
        self.assertTrue(out['recorded'])
        self.assertEqual(len(out['actionIds']), 1)
        replay = epistemics.replay_decision(out['id'])
        self.assertEqual(replay['citedNow'][item]['statusNow'], 'validated')
        aid = out['actionIds'][0]
        done = epistemics.complete_action(out['id'], aid, actor='mira')
        self.assertTrue(done['completed'])
        snap = epistemics.epistemic_snapshot()
        self.assertEqual(snap['openActions'], [])
        self.assertTrue(any(d['id'] == out['id'] for d in snap['decisions']))

    def test_snapshot_separates_asserted_from_established(self):
        item, _cid = self._seed_item()
        hyp = epistemics.record_hypothesis('Gaps live in the exec hop',
                                           actor='sam')['id']
        assertion = epistemics.record_assertion('Pager auto-escalates',
                                                actor='june')['id']
        snap = epistemics.epistemic_snapshot()
        self.assertTrue(any(v['id'] == item for v in snap['established']))
        self.assertTrue(any(v['id'] == hyp for v in snap['asserted']))
        self.assertTrue(any(v['id'] == assertion
                            for v in snap['asserted']))
        self.assertEqual(
            epistemics.classify_item(epistemics.get_item(item)), 'Known')

    def test_brief_distinguishes_asserted_vs_established(self):
        cid = _claim(self.stores, 'https://docs.example.com/g', 'audit live')
        item = epistemics.record_unknown('Gateway audit is live',
                                         actor='mira')['id']
        epistemics.attach_evidence(item, cid, actor='mira')
        hyp = epistemics.record_hypothesis('Gaps live in the exec hop',
                                           actor='sam')['id']
        brief = epistemics.epistemic_brief()
        self.assertIn('EPISTEMIC STATE', brief)
        self.assertIn('Asserted, NOT established', brief)
        self.assertIn(hyp, brief)


class ValidationViews(unittest.TestCase):
    def setUp(self):
        self.stores = _patch_all()

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_required_and_blocked_validations_surface(self):
        rota = epistemics.record_unknown('Rota sees blocked attempts?',
                                         actor='mira')['id']
        req = epistemics.propose_validation(
            rota, 'Trace one blocked attempt', estimated_cost_usd=0.5,
            worth_it=True, actor='mira')['id']
        red = epistemics.record_unknown('Red-team attempt count',
                                        actor='mira')['id']
        blocked = epistemics.propose_validation(
            red, 'Pull the ticket export', blocked=True,
            blocked_reason='no authority to read tickets', actor='mira')['id']
        snap = epistemics.epistemic_snapshot()
        self.assertTrue(any(v['id'] == req
                            for v in snap['validationsRequired']))
        self.assertTrue(any(v['id'] == blocked
                            for v in snap['validationsBlocked']))
        self.assertIn('no authority', snap['validationsBlocked'][0]
                      ['blockedReason'])


if __name__ == '__main__':
    unittest.main()
