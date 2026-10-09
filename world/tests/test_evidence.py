"""Hermetic tests for the Research Desk evidence + coverage ledgers.

world/evidence.py keeps its own two ledger rows (never the kv_state blob),
and every public function reads/writes them through _evidence_read/_write and
_coverage_read/_write. These tests replace those four seams with in-memory
dicts, so no real DB (and no network) is ever touched -- same pattern as
test_bank.py.

The safety property under test throughout: a SHORT URL's raw string is never
a source identity. Two short links that look the same but resolve to
different landing sites must produce DIFFERENT identities, and an unresolved
short link is never treated as a verified source.
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evidence  # noqa: E402


def _patch_ledgers(evidence_rows=None, coverage_rows=None):
    """Swap the four ledger seams for in-memory dicts. Returns the backing
    dicts so a test can inspect what was written. The append-only audit
    (which owns its own SQLite row) is no-op'd so unit tests stay DB-free --
    the audit's SQL layer is exercised against a real temp DB in
    test_evidence_api.py."""
    store = {'evidence': evidence_rows if evidence_rows is not None else {},
             'coverage': coverage_rows if coverage_rows is not None else {}}
    patchers = [
        unittest.mock.patch('evidence._evidence_read',
                            lambda: store['evidence']),
        unittest.mock.patch('evidence._evidence_write',
                            lambda rows: store.__setitem__('evidence', rows)),
        unittest.mock.patch('evidence._coverage_read',
                            lambda: store['coverage']),
        unittest.mock.patch('evidence._coverage_write',
                            lambda rows: store.__setitem__('coverage', rows)),
        unittest.mock.patch('evidence.append_audit', return_value=None),
    ]
    for p in patchers:
        p.start()
    return store


def _claim(**over):
    rec = {
        'source_url': 'https://example.com/post/1',
        'resolved_url': 'https://example.com/post/1',
        'claim': 'The product now supports X search.',
    }
    rec.update(over)
    return rec


class SourceIdentity(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_resolved_url_wins_over_short_source_url(self):
        # t.co/abc and bit.ly/abc LOOK the same but land on different sites:
        # the resolved landing URL is the identity, never the short string.
        identity, meta = evidence.source_identity(
            'https://t.co/abc123', 'https://realcompany.com/blog/launch')
        self.assertEqual(identity, 'https://realcompany.com/blog/launch')
        self.assertTrue(meta['resolved_ok'])
        self.assertTrue(meta['is_short_link'])
        self.assertEqual(meta['resolved_host'], 'realcompany.com')

    def test_two_short_links_with_same_token_different_landings_differ(self):
        a, _ = evidence.source_identity('https://bit.ly/zzz', 'https://site-a.com/x')
        b, _ = evidence.source_identity('https://bit.ly/zzz', 'https://site-b.com/x')
        self.assertNotEqual(a, b)

    def test_short_link_with_no_resolved_url_is_unresolved_not_trusted(self):
        identity, meta = evidence.source_identity('https://t.co/abc123', None)
        self.assertFalse(meta['resolved_ok'])
        self.assertTrue(meta['is_short_link'])
        self.assertTrue(identity.startswith(evidence.UNRESOLVED_PREFIX))
        self.assertNotIn('t.co', identity)  # opaque -- cannot be conflated with a real site

    def test_unresolved_short_link_never_collides_with_resolved_identity(self):
        unresolved, _ = evidence.source_identity('https://t.co/abc123', None)
        resolved, _ = evidence.source_identity('https://t.co/abc123', 'https://real.com/x')
        self.assertNotEqual(unresolved, resolved)

    def test_x_status_post_reduces_to_status_identity(self):
        identity, meta = evidence.source_identity(
            'https://x.com/someone/status/2107949161878606089?utm=track',
            'https://x.com/someone/status/2107949161878606089?utm=track')
        self.assertEqual(identity, 'https://x.com/someone/status/2107949161878606089')
        self.assertTrue(meta['resolved_ok'])

    def test_plain_url_without_resolved_url_is_own_identity(self):
        identity, meta = evidence.source_identity('https://Docs.Python.org/3/')
        self.assertEqual(identity, 'https://docs.python.org/3/')
        self.assertTrue(meta['resolved_ok'])
        self.assertFalse(meta['is_short_link'])


class RecordSourceClaim(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_records_an_independent_claim(self):
        store = _patch_ledgers()
        r = evidence.record_source_claim(_claim())
        self.assertTrue(r['recorded'])
        self.assertTrue(r['independent'])
        rec = store['evidence'][r['id']]
        self.assertEqual(rec['status'], 'needs_review')
        self.assertTrue(rec['resolved_ok'])

    def test_same_resolved_identity_is_a_repost_never_independent(self):
        store = _patch_ledgers()
        first = evidence.record_source_claim(_claim())
        # Same landing URL via a DIFFERENT short link: still the same source,
        # so the second record is a repost, not independent corroboration.
        second = evidence.record_source_claim(_claim(
            source_url='https://t.co/short1', resolved_url='https://example.com/post/1'))
        self.assertTrue(second['recorded'])
        self.assertFalse(second['independent'])
        self.assertEqual(second['repost_of'], first['id'])
        self.assertFalse(store['evidence'][second['id']]['independent'])

    def test_covered_identity_is_skipped_without_material_update(self):
        _patch_ledgers(
            evidence_rows={},
            coverage_rows={'https://example.com/post/1': {
                'status': 'covered', 'reviewed': True, 'covered_date': 2000}})
        r = evidence.record_source_claim(_claim(lastModified=1000))
        self.assertFalse(r['recorded'])
        self.assertEqual(r['reason'], 'covered')

    def test_covered_identity_reopens_on_material_update(self):
        _patch_ledgers(
            evidence_rows={},
            coverage_rows={'https://example.com/post/1': {
                'status': 'covered', 'reviewed': True, 'covered_date': 2000}})
        r = evidence.record_source_claim(_claim(lastModified=3000))
        self.assertTrue(r['recorded'])  # material change after the covered date

    def test_rejects_record_without_source_or_claim(self):
        _patch_ledgers()
        r = evidence.record_source_claim({'source_url': 'https://x.com/a'})
        self.assertFalse(r['recorded'])
        self.assertEqual(r['reason'], 'invalid')
        r = evidence.record_source_claim({'claim': 'no source here'})
        self.assertFalse(r['recorded'])
        self.assertEqual(r['reason'], 'invalid')

    def test_unresolved_short_link_gets_open_question(self):
        store = _patch_ledgers()
        r = evidence.record_source_claim(_claim(
            source_url='https://t.co/abc123', resolved_url=None))
        self.assertTrue(r['recorded'])
        self.assertFalse(r['resolved_ok'])
        rec = store['evidence'][r['id']]
        self.assertIn('short link not resolved', ' '.join(rec['open_questions']))


class ObservationChange(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_appends_a_before_after_observation_to_a_claim(self):
        store = _patch_ledgers()
        cid = evidence.record_source_claim(_claim())['id']
        r = evidence.record_observation_change(
            cid, 'price', '$100', '$95', source_url='https://example.com/price')
        self.assertTrue(r['recorded'])
        self.assertTrue(r['appended'])
        obs = store['evidence'][cid]['observations']
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]['field'], 'price')
        self.assertEqual(obs[0]['before'], '$100')
        self.assertEqual(obs[0]['after'], '$95')
        self.assertEqual(obs[0]['source_url'], 'https://example.com/price')

    def test_observations_accumulate_a_history(self):
        store = _patch_ledgers()
        cid = evidence.record_source_claim(_claim())['id']
        evidence.record_observation_change(cid, 'price', '$100', '$95')
        evidence.record_observation_change(cid, 'price', '$95', '$90')
        obs = store['evidence'][cid]['observations']
        self.assertEqual([(o['before'], o['after']) for o in obs],
                         [('$100', '$95'), ('$95', '$90')])

    def test_unknown_claim_id_creates_a_claim_from_the_observation(self):
        store = _patch_ledgers()
        r = evidence.record_observation_change(
            None, 'positioning', 'no LLM mentions', 'pitches itself as agent-native',
            source_url='https://example.com/about')
        self.assertTrue(r['recorded'])
        rec = store['evidence'][r['id']]
        self.assertIn('positioning changed from', rec['claim'])
        self.assertEqual(rec['observations'][0]['after'], 'pitches itself as agent-native')

    def test_requires_field_and_after(self):
        _patch_ledgers()
        r = evidence.record_observation_change('ev-x', 'price', '$100', None,
                                               source_url='https://x.com')
        self.assertFalse(r['recorded'])
        r = evidence.record_observation_change('ev-x', '', '$100', '$95',
                                               source_url='https://x.com')
        self.assertFalse(r['recorded'])

    def test_unknown_claim_without_source_is_rejected(self):
        _patch_ledgers()
        r = evidence.record_observation_change('ev-nope', 'price', '$1', '$2')
        self.assertFalse(r['recorded'])
        self.assertEqual(r['reason'], 'invalid')


class ReviewAndCoverage(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_mark_reviewed_clears_and_covers_an_independent_claim(self):
        store = _patch_ledgers()
        r = evidence.record_source_claim(_claim())
        rec = evidence.mark_reviewed(r['id'], 'usable_as_written', artifact='skills/x.md')
        self.assertEqual(rec['status'], 'cleared')
        cov = store['coverage']['https://example.com/post/1']
        self.assertEqual(cov['status'], 'covered')
        self.assertTrue(cov['reviewed'])
        self.assertEqual(cov['artifact'], 'skills/x.md')

    def test_blocked_decision_never_marks_coverage(self):
        store = _patch_ledgers()
        r = evidence.record_source_claim(_claim())
        rec = evidence.mark_reviewed(r['id'], 'blocked_until_checked')
        self.assertEqual(rec['status'], 'blocked')
        self.assertNotIn('https://example.com/post/1', store['coverage'])

    def test_pending_review_is_not_covered(self):
        _patch_ledgers()
        evidence.mark_covered('https://example.com/post/1', artifact='skills/x.md',
                              reviewed=False)
        self.assertFalse(evidence.is_source_covered('https://example.com/post/1'))

    def test_covered_requires_reviewed_true(self):
        _patch_ledgers()
        evidence.mark_covered('https://example.com/post/1', artifact='skills/x.md',
                              reviewed=True)
        self.assertTrue(evidence.is_source_covered('https://example.com/post/1'))

    def test_covered_plus_material_update_not_covered_anymore(self):
        import time
        _patch_ledgers()
        evidence.mark_covered('https://example.com/post/1', artifact='skills/x.md',
                              reviewed=True)
        now_ms = int(time.time() * 1000)
        # A source lastModified AFTER the covered date reopens the item.
        self.assertFalse(evidence.is_source_covered('https://example.com/post/1',
                                                    material_update_ms=now_ms + 100000))
        # An older lastModified means no new material: still covered.
        self.assertTrue(evidence.is_source_covered('https://example.com/post/1',
                                                   material_update_ms=now_ms - 100000))


class RunFailures(unittest.TestCase):
    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_failed_run_never_suppresses_the_next_attempt(self):
        _patch_ledgers()
        evidence.record_run_failure('https://example.com/post/1', 'synthesis failed (no model tier)')
        self.assertFalse(evidence.is_source_covered('https://example.com/post/1'))
        # A later successful review can still cover it.
        evidence.mark_covered('https://example.com/post/1', artifact='skills/x.md', reviewed=True)
        self.assertTrue(evidence.is_source_covered('https://example.com/post/1'))

    def test_failure_does_not_demote_a_covered_item(self):
        store = _patch_ledgers()
        evidence.mark_covered('https://example.com/post/1', artifact='skills/x.md', reviewed=True)
        evidence.record_run_failure('https://example.com/post/1', 'later run failed')
        self.assertEqual(store['coverage']['https://example.com/post/1']['status'], 'covered')


class SearchWindow(unittest.TestCase):
    def test_first_run_searches_the_whole_window(self):
        self.assertEqual(evidence.search_since_ms(0), 0)
        self.assertEqual(evidence.search_since_ms(None), 0)

    def test_overlap_subtracts_from_last_run(self):
        last = 5 * 24 * 3600 * 1000
        self.assertEqual(evidence.search_since_ms(last, evidence.DEFAULT_OVERLAP_MS),
                         last - 24 * 3600 * 1000)

    def test_overlap_is_never_negative(self):
        self.assertEqual(evidence.search_since_ms(1000, overlap_ms=5000), 0)


class AuditWiring(unittest.TestCase):
    """The mutation functions must record WHO did WHAT in the append-only
    audit. These tests capture the audit calls (the SQL layer itself is
    exercised against a real temp DB in test_evidence_api.py)."""

    def tearDown(self):
        unittest.mock.patch.stopall()

    def test_recording_a_claim_appends_claim_recorded(self):
        _patch_ledgers()
        with unittest.mock.patch('evidence.append_audit') as aud:
            evidence.record_source_claim(_claim(), actor='cora')
        aud.assert_called()
        action = aud.call_args[0][1]
        self.assertEqual(action, 'claim_recorded')

    def test_review_appends_claim_reviewed_and_coverage_covered(self):
        store = _patch_ledgers()
        res = evidence.record_source_claim(_claim(), actor='cora')
        with unittest.mock.patch('evidence.append_audit') as aud:
            evidence.mark_reviewed(res['id'], 'usable_as_written', actor='lex')
        actions = [c.args[1] for c in aud.call_args_list]
        self.assertIn('claim_reviewed', actions)
        self.assertIn('coverage_covered', actions)
        self.assertEqual(store['coverage'][res['identity']]['status'], 'covered')

    def test_review_by_player_writes_player_as_actor(self):
        _patch_ledgers()
        res = evidence.record_source_claim(_claim(), actor='cora')
        with unittest.mock.patch('evidence.append_audit') as aud:
            evidence.mark_reviewed(res['id'], 'usable_as_written', actor='player')
        for c in aud.call_args_list:
            self.assertEqual(c.args[0], 'player')

    def test_failure_appends_coverage_failed(self):
        _patch_ledgers()
        with unittest.mock.patch('evidence.append_audit') as aud:
            evidence.record_run_failure('https://example.com/post/1', 'boom', actor='cora')
        self.assertEqual(aud.call_args[0][1], 'coverage_failed')


if __name__ == '__main__':
    unittest.main()
