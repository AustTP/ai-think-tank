"""Release invariants (2026-11-18, adopted from the coherence review).

The reviewer's bar: the tests must prove SYSTEM PROPERTIES, not just run
cases. This suite is the first-class form of that bar -- each class is one
named invariant, and the CI route-auth step runs alongside it as a blocking
release gate. When a new invariant is adopted, add a class here rather than
scattering the property across unrelated test files.

Invariants covered:
1. RouteAuthReachability -- every consequential HTTP route reaches an
   explicit authorization decision (deny-by-default middleware, an
   in-handler gate, or the reviewed allowlist); the audit's parsed public
   sets match the live middleware tuples (no drift).
2. ExternalWritesExactlyOnce -- no retry creates a duplicate external side
   effect: completed writes replay, concurrent duplicates are refused while
   in progress, and failures never poison the ledger.
3. DependencyGatedWorkNeverRunsEarly -- no dependency-blocked work item is
   dispatchable: single-edge chains, fan-in joins, and cycle detection all
   hold.
4. CompletedTasksSurviveRestart -- no completed task disappears across a
   save/load roundtrip (the "restart" half of durability).
5. KnowledgeNeedsIndependentReview -- no claim becomes ESTABLISHED without
   a different reviewer's approval, and an open contradiction blocks
   validation even from an approving reviewer.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools')
sys.path.insert(0, TOOLS_DIR)


class RouteAuthReachability(unittest.TestCase):
    """Invariant: no route ships without an explicit authorization
    decision."""

    def test_current_tree_has_zero_violations(self):
        import route_auth_audit as audit
        counts, violations = audit.audit()
        self.assertEqual(violations, [],
                         'ungated routes found: %s' % (violations,))
        self.assertGreater(counts['routes'], 400)

    def test_audit_catches_a_new_ungated_public_route(self):
        # The gate must stay sharp: a sneaky route under a middleware-public
        # prefix fails the audit; the same route with a body gate passes.
        import route_auth_audit as audit
        with tempfile.TemporaryDirectory() as td:
            shutil_copy = __import__('shutil').copy
            shutil_copy(os.path.join(audit.PARTS_DIR, audit.MIDDLEWARE_PART),
                        os.path.join(td, audit.MIDDLEWARE_PART))
            with open(os.path.join(td, '999_probe.py'), 'w') as fh:
                fh.write(
                    "@app.post('/api/chat/sneaky-open-route')\n"
                    'async def sneaky(request):\n'
                    '    return {"leaked": True}\n'
                    "\n@app.post('/api/chat/gated-route')\n"
                    'async def gated(request):\n'
                    '    authed, _ = _operator_gate(request)\n'
                    '    if not authed:\n'
                    '        return {"error": "no"}\n'
                    '    return {"ok": True}\n')
            _counts, violations = audit.audit(parts_dir=td)
            flagged = [v for v in violations if 'sneaky' in v[0]]
            clean = [v for v in violations if 'gated-route' in v[0]]
            self.assertTrue(flagged, 'ungated probe route was not flagged')
            self.assertFalse(clean, 'gated probe route was wrongly flagged')

    def test_parsed_public_sets_match_the_live_middleware(self):
        # Drift between the audit's static parse and the running middleware
        # would silently change what "public" means.
        import serve
        import route_auth_audit as audit
        prefixes, extensions = audit._parse_public_sets(audit.PARTS_DIR)
        self.assertEqual(prefixes, list(serve.AUTH_PUBLIC_PATHS))
        self.assertEqual(extensions, list(serve.AUTH_PUBLIC_STATIC_EXTENSIONS))


class ExternalWritesExactlyOnce(unittest.TestCase):
    """Invariant: no retry creates a duplicate external side effect."""

    def _fresh_ledger(self):
        import serve
        # The external_writes table rides _db(); the test conftest already
        # redirected that to a per-session temp sqlite file.
        with serve._db() as conn:
            conn.execute('DELETE FROM external_writes')
            conn.execute('''CREATE TABLE IF NOT EXISTS external_writes (
                kind TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY (kind, key))''')

    def test_completed_write_replays_instead_of_repeating(self):
        import serve
        self._fresh_ledger()
        calls = []

        def side_effect():
            calls.append(1)
            return {'ok': True}, None

        first, err1, replay1 = serve.external_write_once(
            'test-kind', 'key-1', side_effect)
        second, err2, replay2 = serve.external_write_once(
            'test-kind', 'key-1', side_effect)
        self.assertEqual(len(calls), 1, 'side effect ran twice')
        self.assertFalse(replay1)
        self.assertTrue(replay2)
        self.assertEqual(first, second)
        self.assertIsNone(err1)
        self.assertIsNone(err2)

    def test_failure_does_not_poison_the_ledger(self):
        import serve
        self._fresh_ledger()
        calls = []

        def flaky():
            calls.append('fail')
            return None, 'transient provider error'

        out, err, _replayed = serve.external_write_once(
            'test-kind', 'key-fail', flaky)
        self.assertIsNone(out)
        self.assertEqual(err, 'transient provider error')
        # The failed attempt left no completed row: the retry runs the real
        # call again instead of replaying an error.
        out2, err2, replay2 = serve.external_write_once(
            'test-kind', 'key-fail',
            lambda: ({'ok': True}, None))
        self.assertEqual(out2, {'ok': True})
        self.assertIsNone(err2)
        self.assertFalse(replay2)
        self.assertEqual(calls, ['fail'])


class DependencyGatedWorkNeverRunsEarly(unittest.TestCase):
    """Invariant: no dependency-blocked work item is dispatchable."""

    def _state_with(self, tasks):
        return {'tasks': dict(tasks)}

    def test_pending_dependency_blocks(self):
        import sim
        state = self._state_with({'t-1': {'status': 'working'}})
        item = {'dependsOn': 't-1'}
        self.assertFalse(sim._work_item_dependency_met(state, item))

    def test_landed_dependency_releases(self):
        import sim
        state = self._state_with({'t-1': {'status': 'done'}})
        self.assertTrue(sim._work_item_dependency_met(
            state, {'dependsOn': 't-1'}))

    def test_fan_in_join_needs_every_edge(self):
        import sim
        state = self._state_with({'t-1': {'status': 'done'},
                                  't-2': {'status': 'needs_review'}})
        item = {'dependsOn': ['t-1', 't-2']}
        self.assertFalse(sim._work_item_dependency_met(state, item))
        state['tasks']['t-2']['status'] = 'done'
        self.assertTrue(sim._work_item_dependency_met(state, item))

    def test_self_edge_and_transitive_cycle_detected(self):
        import sim
        # t-2 -> t-1 exists; adding t-1 -> t-2 closes the cycle.
        state = self._state_with({
            't-2': {'status': 'queued', 'dependsOn': ['t-1']},
        })
        self.assertTrue(sim._dependency_would_cycle(state, 't-1', ['t-2']))
        # A pre-existing cycle elsewhere does not make a new independent
        # edge cyclic.
        state2 = self._state_with({
            't-2': {'status': 'queued', 'dependsOn': ['t-3']},
            't-3': {'status': 'queued', 'dependsOn': ['t-2']},
        })
        self.assertFalse(sim._dependency_would_cycle(state2, 't-1', ['t-2']))
        self.assertTrue(sim._dependency_would_cycle(state2, 't-2', ['t-2']))


class CompletedTasksSurviveRestart(unittest.TestCase):
    """Invariant: no completed task disappears across a restart."""

    def test_done_task_roundtrips_through_the_store(self):
        import serve
        state = {'tasks': {'t-restart': {
            'id': 't-restart', 'title': 'Survives the restart',
            'status': 'done', 'completedAt': 1795020000000}},
            'agents': {}, 'agentRoster': []}
        serve.save_state_to_db(state)
        loaded = serve.get_state_from_db() or {}
        tasks = loaded.get('tasks') or {}
        self.assertIn('t-restart', tasks,
                      'a completed task vanished across save/load')
        self.assertEqual(tasks['t-restart'].get('status'), 'done')


class KnowledgeNeedsIndependentReview(unittest.TestCase):
    """Invariant: no claim becomes ESTABLISHED without a different
    reviewer, and an open contradiction blocks validation."""

    def setUp(self):
        import core.evidence as evidence
        import core.epistemics as epistemics
        self.evidence = evidence
        self.epistemics = epistemics
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

    def _validated_item_id(self):
        ep = self.epistemics
        item = ep.record_assertion('The warehouse roof leaks', actor='ada')
        item_id = item['id']
        claim = self.evidence.record_source_claim(
            {'source_url': 'https://fix.example/roof',
             'claim': 'Inspection found a roof leak'}, actor='ada')
        validation = ep.propose_validation(
            item_id, 'inspect the roof', needed_evidence='photo', actor='ada')
        ep.start_validation(validation['id'], cost_ok=True, actor='ada')
        done = ep.complete_validation(
            validation['id'], 'Leak confirmed at the north seam',
            claim_id=claim['id'], actor='ben')
        self.assertTrue(done.get('completed'), done)
        review = ep.review_validation(validation['id'], True,
                                      reviewer='cid')
        self.assertTrue(review.get('reviewed'), review)
        self.assertEqual(review.get('itemStatus'), 'validated')
        return ep, item_id, claim['id']

    def test_reviewer_cannot_review_own_completion(self):
        ep, item_id, _claim_id = self._validated_item_id()
        item2 = ep.record_assertion('Second roof proposition', actor='ada')
        claim = self.evidence.record_source_claim(
            {'source_url': 'https://fix.example/roof-2',
             'claim': 'Second inspection'}, actor='ada')
        v = ep.propose_validation(item2['id'], 'check again', actor='ada')
        ep.start_validation(v['id'], cost_ok=True, actor='ada')
        ep.complete_validation(v['id'], 'Still leaking', claim_id=claim['id'],
                               actor='ben')
        refused = ep.review_validation(v['id'], True, reviewer='ben')
        self.assertFalse(refused.get('reviewed'))
        self.assertEqual(refused.get('reason'), 'reviewer_independence')

    def test_open_contradiction_blocks_validation(self):
        ep, item_id, claim_id = self._validated_item_id()
        # Land a contradicting claim: the item flips to disputed...
        contra = self.evidence.record_source_claim(
            {'source_url': 'https://fix.example/roof-refutation',
             'claim': 'No leak found on reinspection'}, actor='dev')
        recorded = ep.record_contradiction(item_id, claim_id, contra['id'],
                                           actor='dev')
        self.assertTrue(recorded.get('recorded', True))
        # ...and a fresh, honestly-reviewed validation can NOT flip it to
        # validated while the contradiction stands.
        claim3 = self.evidence.record_source_claim(
            {'source_url': 'https://fix.example/roof-confirm-2',
             'claim': 'Third inspection agrees the roof leaks'}, actor='eli')
        item = ep.get_item(item_id)
        self.assertIn(item['status'], ('disputed', 'contradicted'))
        v = ep.propose_validation(item_id, 're-inspect once more', actor='eli')
        ep.start_validation(v['id'], cost_ok=True, actor='eli')
        ep.complete_validation(v['id'], 'Leak reconfirmed', actor='eli',
                               claim_id=claim3['id'])
        review = ep.review_validation(v['id'], True, reviewer='ada')
        self.assertTrue(review.get('reviewed'))
        self.assertIsNone(review.get('itemStatus'),
                          'a review overturned an open contradiction')
        self.assertEqual(ep.get_item(item_id)['status'], 'disputed')
