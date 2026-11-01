"""Director split (multi-team large requests): a large ask landing on a
director with subdirectors is REVIEWED by the director first, then
partitioned across their subteams -- one child large-request per piece, each
bound for that subteam's normal breakdown ceremony. Pieces may target a
subdirector's subteam, the director's own pool, be SHARED (either team picks
them up, first ceremony claims), or found a NEW subdirector from an existing
worker. Priority threads through: intake ordering + per-piece urgency.
Villages: pieces record the receiving team's villageId.

DB-isolated (setUpModule, the test_state_split pattern). The decider is
injectable, so no model is ever called.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402
import core.governance as governance  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-director-split-')
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


def _org():
    """A director 'dev' with two subdirectors ('lead1', 'lead2'), each with
    workers, plus a second standalone team ('ops') for the either-team case."""
    state = serve.get_state_from_db() or {}
    state['agentRoster'] = [
        {'id': 'admin', 'name': 'Ada Admin', 'role': 'Chief of Staff', 'isAdmin': True},
        {'id': 'dev', 'name': 'Dev Director', 'role': 'Director', 'director': 'admin'},
        {'id': 'ops', 'name': 'Ops Director', 'role': 'Director', 'director': 'admin'},
        {'id': 'lead1', 'name': 'One Lead', 'role': 'Worker', 'director': 'dev'},
        {'id': 'lead2', 'name': 'Two Lead', 'role': 'Worker', 'director': 'dev'},
        {'id': 'w1', 'name': 'W One', 'role': 'Worker', 'director': 'lead1'},
        {'id': 'w2', 'name': 'W Two', 'role': 'Worker', 'director': 'lead1'},
        {'id': 'w3', 'name': 'W Three', 'role': 'Worker', 'director': 'dev'},
        {'id': 'o1', 'name': 'O One', 'role': 'Worker', 'director': 'ops'},
    ]
    state['agents'] = {r['id']: {'id': r['id'], 'name': r['name'], 'busy': False,
                                 'offDuty': False, 'approvedCount': 3}
                       for r in state['agentRoster']}
    state['teams'] = [
        {'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev', 'members': []},
        {'id': 'ops', 'name': "Ops Crew", 'directorId': 'ops', 'members': []},
    ]
    state['backlogRequests'] = []
    state['workQueue'] = []
    return state


def _save(state):
    serve.save_state_to_db(state)


def _reqs(state):
    return state.setdefault('backlogRequests', [])


def _pending(state):
    return [r for r in _reqs(state) if r.get('status') == 'pending'
            and r.get('breakdown')]


class SplitNotNeededTest(unittest.TestCase):
    def setUp(self):
        _save(_org())

    def test_no_subdirectors_files_straight_to_breakdown(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the shed', 'ops')
        _save(state)
        self.assertIsNotNone(req)
        self.assertNotIn('awaitingSplit', req)
        # ops has no subdirectors: the plain convene path sees it.
        self.assertEqual([r['id'] for r in sim._pending_breakdown_requests(state, 'ops')],
                         [req['id']])


class SharedBacklogTest(unittest.TestCase):
    def setUp(self):
        _save(_org())

    def test_shared_request_is_pickable_by_any_team(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Oil the machines', None)
        _save(state)
        self.assertIsNotNone(req)
        self.assertTrue(req.get('shared'))
        self.assertNotIn('teamId', req)
        for team in ('dev', 'ops'):
            got = sim._pending_breakdown_requests(state, team)
            self.assertEqual([r['id'] for r in got], [req['id']], team)

    def test_first_ceremony_claims_shared_request(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Oil the machines', None)
        _save(state)
        # ops convenes first and claims it.
        self.assertTrue(sim._start_breakdown(state, 'ops', [req['id']], 'o1', 1000))
        _save(state)
        self.assertEqual(req.get('teamId'), 'ops')
        self.assertTrue(req.get('claimedFromShared'))
        # dev's convene scan no longer sees it.
        self.assertEqual(sim._pending_breakdown_requests(state, 'dev'), [])

    def test_claim_race_drops_stale_gathered_ids(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Oil the machines', None)
        _save(state)
        # Both teams gather the same id, then ops starts first.
        stale_ids = [req['id']]
        self.assertTrue(sim._start_breakdown(state, 'ops', list(stale_ids), 'o1', 1000))
        # dev's ceremony start (same pass, later iteration) must refuse to
        # card a request another team claimed.
        self.assertFalse(sim._start_breakdown(state, 'dev', list(stale_ids), 'w1', 1001))
        self.assertEqual(req.get('teamId'), 'ops')


class PriorityTest(unittest.TestCase):
    def setUp(self):
        _save(_org())

    def test_priority_defaults_and_validation(self):
        state = serve.get_state_from_db()
        self.assertEqual(sim.file_large_request(state, 'admin', 'A', 'ops')['priority'], 'normal')
        self.assertEqual(sim.file_large_request(state, 'admin', 'B', 'ops', priority='urgent')['priority'], 'urgent')
        self.assertEqual(sim.file_large_request(state, 'admin', 'C', 'ops', priority='garbage')['priority'], 'normal')
        self.assertEqual(sim.file_large_request(state, 'admin', 'D', 'ops', priority=3)['priority'], 'urgent')
        _save(state)

    def test_urgent_requests_are_gathered_first(self):
        state = serve.get_state_from_db()
        slow = sim.file_large_request(state, 'admin', 'Slow burn', 'ops', priority='low')
        hot = sim.file_large_request(state, 'admin', 'Fire now', 'ops', priority='urgent')
        _save(state)
        reqs = sim._pending_breakdown_requests(state, 'ops')
        reqs.sort(key=lambda r: (-sim._request_priority_rank(r), r.get('filedAt') or 0))
        self.assertEqual([r['id'] for r in reqs], [hot['id'], slow['id']])


class SplitReviewTest(unittest.TestCase):
    def setUp(self):
        _save(_org())

    def test_request_for_layered_director_awaits_split(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)
        self.assertTrue(req.get('awaitingSplit'))
        # The plain breakdown convene must NOT see it; the split layer owns it.
        self.assertEqual(sim._pending_breakdown_requests(state, 'dev'), [])
        self.assertEqual([r['id'] for r in sim._pending_split_requests(state, 'dev')],
                         [req['id']])

    def test_split_partitions_across_subteams_and_shared(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, instructions, _goal):
            return {'pieces': [
                {'team': 'lead1', 'ask': 'Foundations', 'priority': 'urgent'},
                {'team': 'own', 'ask': 'Scaffold plans'},
                {'team': 'shared', 'ask': 'Material run', 'priority': 'low'},
            ]}

        sim._director_split_step(state, 1, 1000, decider=decider)   # convene
        sim._director_split_step(state, 2, 2000, decider=decider)   # resolve
        _save(state)
        self.assertEqual(req.get('status'), 'accepted')
        self.assertTrue(req.get('split'))
        self.assertEqual(len(req.get('splitPieces') or []), 3)
        children = {r['id']: r for r in _reqs(state) if r.get('splitFrom') == req['id']}
        self.assertEqual(len(children), 3)
        by_ask = {r['goal']: r for r in children.values()}
        self.assertEqual(by_ask['Foundations'].get('teamId'), 'lead1')
        self.assertEqual(by_ask['Foundations'].get('priority'), 'urgent')
        self.assertEqual(by_ask['Scaffold plans'].get('teamId'), 'dev')
        self.assertEqual(by_ask['Material run'].get('shared'), True)
        self.assertNotIn('teamId', by_ask['Material run'])
        # Provenance + village recording.
        for child in children.values():
            self.assertEqual(child.get('splitBy'), 'dev')
            self.assertEqual(child.get('villageId'), 'main')

    def test_subteam_breakdown_convene_sees_split_pieces(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'lead1', 'ask': 'Foundations'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)
        sim._director_split_step(state, 2, 2000, decider=decider)
        _save(state)
        # The subteam's emergent ceremony intake now sees the piece.
        got = sim._pending_breakdown_requests(state, 'lead1')
        self.assertEqual([r['id'] for r in got],
                         [r['id'] for r in _reqs(state) if r.get('splitFrom') == req['id']])
        # And the subteam can convene with the subdirector facilitating.
        child_id = got[0]['id']
        self.assertTrue(sim._start_breakdown(state, 'lead1', [child_id], 'lead1', 3000))
        _save(state)
        attendees = state['pendingBreakdowns']['lead1']
        self.assertIn('w1', attendees.get('people') or {})

    def test_outage_falls_back_to_own_team_breakdown(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)
        sim._director_split_step(state, 1, 1000, decider=lambda *a, **k: None)
        sim._director_split_step(state, 2, 2000, decider=lambda *a, **k: None)
        _save(state)
        self.assertFalse(req.get('awaitingSplit'))
        self.assertTrue(req.get('splitFallback'))
        self.assertEqual(req.get('status'), 'pending')
        # Back on the plain convene path for dev's own team.
        self.assertEqual([r['id'] for r in sim._pending_breakdown_requests(state, 'dev')],
                         [req['id']])

    def test_org_summary_flags_sprint_commitment(self):
        state = serve.get_state_from_db()
        state['sprints'] = {'sp-1': {'status': 'active', 'teamIds': ['dev']}}
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        by_id = {t['teamId']: t for t in org}
        self.assertEqual(by_id['dev']['sprint'], 'sp-1')
        # Subteams inherit the ROOT team's sprint commitment: sprint work is
        # the root team's committed pool and every subteam draws on it.
        self.assertEqual(by_id['lead1']['sprint'], 'sp-1')
        self.assertIsNone(by_id['lead1']['dedicatedTo'])

    def test_freed_team_is_contributable_again(self):
        """A team tied to sprint work is flagged committed while it lasts; the
        moment the work closes (or the permanent dedication clears) the same
        review sees a free subteam the director can ask to contribute."""
        state = serve.get_state_from_db()
        state['sprints'] = {'sp-1': {'status': 'active', 'teamIds': ['dev']}}
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        self.assertEqual({t['teamId']: t['sprint'] for t in org}['lead1'], 'sp-1')
        # Sprint closes.
        state['sprints']['sp-1']['status'] = 'closed'
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        self.assertIsNone({t['teamId']: t for t in org}['lead1']['sprint'])
        # Permanent dedication clears the same way.
        state['teams'][0]['dedicatedTo'] = 'the Q1 archive migration'
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        self.assertEqual({t['teamId']: t for t in org}['lead1']['dedicatedTo'],
                         'the Q1 archive migration')
        state['teams'][0].pop('dedicatedTo')
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        self.assertIsNone({t['teamId']: t for t in org}['lead1']['dedicatedTo'])

    def test_review_prompt_names_commitments(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        state['sprints'] = {'sp-1': {'status': 'active', 'teamIds': ['dev']}}
        _save(state)
        org = sim._split_org_summary(state, 'dev')
        text = sim._split_instructions(state, req, org, 'Dev Director')
        self.assertIn('IN ACTIVE SPRINT sp-1', text)
        self.assertIn('cannot START new pieces until that commitment ends', text)

    def test_bad_team_names_fail_open_to_own_team(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'nonexistent', 'ask': 'Mystery work'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)
        sim._director_split_step(state, 2, 2000, decider=decider)
        _save(state)
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].get('teamId'), 'dev')


class NewSubdirectorTest(unittest.TestCase):
    def setUp(self):
        _save(_org())

    def test_split_founds_subdirector_and_binds_piece(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'new', 'ask': 'New lane'}],
                    'newSubdirectors': [{'seedWorker': 'w3', 'handOver': 2,
                                         'purpose': ' owns the arch lane'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)
        sim._director_split_step(state, 2, 2000, decider=decider)
        _save(state)
        # w3 is now a subdirector by structure: they lead w1/w2 (handed over
        # from lead1's pool? No -- from DEV's plain workers; lead1's workers
        # are not dev's direct reports anymore, so the hand-over pool is
        # whoever still reports straight to dev).
        subs = governance.subdirectors_of(state, 'dev')
        self.assertIn('w3', subs)
        self.assertTrue(governance.direct_reports(state, 'w3'))
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].get('teamId'), 'w3')

    def test_founding_respects_ceiling_and_invalid_seeds(self):
        state = serve.get_state_from_db()
        # A seed that already leads people is not promotable.
        self.assertIsNone(sim._found_subdirector(state, 'dev', seed_worker='lead1'))
        # A seed from another org is not promotable.
        self.assertIsNone(sim._found_subdirector(state, 'dev', seed_worker='o1'))
        # Unknown seed.
        self.assertIsNone(sim._found_subdirector(state, 'dev', seed_worker='ghost'))
        # Valid seed with nobody to hand over fails (a leader with no reports
        # is not a subdirector by structure): dev's only remaining report IS
        # the seed, so the hand-over pool is empty.
        state2 = serve.get_state_from_db()
        state2['agentRoster'] = [r for r in state2['agentRoster']
                                 if r['id'] in ('admin', 'dev', 'lead2')]
        state2['agents'] = {r['id']: state2['agents'][r['id']]
                            for r in state2['agentRoster']}
        _save(state2)
        self.assertIsNone(sim._found_subdirector(state2, 'dev', seed_worker='lead2'))


if __name__ == '__main__':
    unittest.main()
