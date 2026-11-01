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
    state['pendingFoundations'] = []
    state['pendingFoundationReviews'] = {}
    state['pendingDirectorSplits'] = {}
    state['pendingBreakdowns'] = {}
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

    def test_subteam_piece_routes_through_the_subdirectors_review(self):
        """A piece routed to a subdirector awaits THEIR split review (not the
        plain breakdown); the review's 'own' routing lands it in their
        subteam's breakdown intake without re-awaiting another split."""
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'lead1', 'ask': 'Foundations'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)   # dev convenes
        sim._director_split_step(state, 2, 2000, decider=decider)   # dev resolves
        _save(state)
        piece = next(r for r in _reqs(state) if r.get('splitFrom') == req['id'])
        self.assertEqual(piece.get('teamId'), 'lead1')
        # Not in the breakdown intake yet: lead1 reviews first.
        self.assertEqual(sim._pending_breakdown_requests(state, 'lead1'), [])
        # lead1's split review routes the piece to their own subteam.
        own_decider = (lambda _s, _i, _g:
                       {'pieces': [{'team': 'own', 'ask': piece['goal']}]})
        sim._director_split_step(state, 3, 3000, decider=own_decider)
        sim._director_split_step(state, 4, 4000, decider=own_decider)
        _save(state)
        leaf = next(r for r in _reqs(state) if r.get('splitFrom') == piece['id'])
        self.assertEqual(leaf.get('teamId'), 'lead1')
        self.assertNotIn('awaitingSplit', leaf)  # no split ping-pong
        got = sim._pending_breakdown_requests(state, 'lead1')
        self.assertEqual([r['id'] for r in got], [leaf['id']])
        # And the subteam can convene with the subdirector facilitating.
        self.assertTrue(sim._start_breakdown(state, 'lead1', [leaf['id']], 'lead1', 5000))
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

        sim._director_split_step(state, 1, 1000, decider=decider)   # convene
        sim._director_split_step(state, 2, 2000, decider=decider)   # resolve
        _save(state)
        # Root director: founding is immediate, no approval ceremony needed.
        self.assertEqual(len([p for p in (state.get('pendingFoundations') or [])
                              if p.get('status') == 'pending']), 0)
        # w3 is now a subdirector by structure: they lead two workers handed
        # over from dev's own pool.
        subs = governance.subdirectors_of(state, 'dev')
        self.assertIn('w3', subs)
        self.assertTrue(governance.direct_reports(state, 'w3'))
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].get('teamId'), 'w3')

    def test_subdirector_proposal_requires_parent_approval(self):
        """The approval chain: a subdirector may PROPOSE a founding, but the
        founder's PARENT director judges necessity in an approval review.
        Unilateral founding below the root is refused; only the parent's
        approval executes it (parent -> subdirector -> new sub: one managed
        level deeper, every level consented)."""
        state = serve.get_state_from_db()
        self.assertTrue(governance.subdirectors_of(state, 'dev'))
        # lead1 (a subdirector) receives a piece: it awaits lead1's review.
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'lead1')
        _save(state)
        self.assertTrue(req.get('awaitingSplit'))
        # lead1's review context offers PROPOSALS, not direct founding.
        org = sim._split_org_summary(state, 'lead1')
        text = sim._split_instructions(
            state, req, org, 'One Lead',
            founding_mode=sim._founding_mode(state, 'lead1'))
        self.assertIn('PROPOSALS ONLY', text)
        self.assertIn('PARENT DIRECTOR judges', text)
        self.assertEqual(sim._founding_mode(state, 'lead1'), 'propose')
        # Unilateral mechanic: no approval -> refused; a NON-parent approver
        # -> refused too. Only the founder's own parent may sign.
        self.assertIsNone(sim._found_subdirector(state, 'lead1', seed_worker='w1'))
        self.assertIsNone(sim._found_subdirector(state, 'lead1', seed_worker='w1',
                                                 approved_by='ops'))

    def test_subteam_piece_routes_through_the_subdirectors_review(self):
        """A piece routed to a subdirector awaits THEIR split review (not the
        plain breakdown); the review's 'own' routing lands it in their
        subteam's breakdown intake without re-awaiting another split."""
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'dev')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'lead1', 'ask': 'Foundations'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)   # dev convenes
        sim._director_split_step(state, 2, 2000, decider=decider)   # dev resolves
        _save(state)
        piece = next(r for r in _reqs(state) if r.get('splitFrom') == req['id'])
        self.assertEqual(piece.get('teamId'), 'lead1')
        # Not in the breakdown intake yet: lead1 reviews first.
        self.assertEqual(sim._pending_breakdown_requests(state, 'lead1'), [])
        # lead1's split review routes the piece to their own subteam.
        sim._director_split_step(state, 3, 3000, decider=lambda _s, _i, _g:
                                 {'pieces': [{'team': 'own', 'ask': piece['goal']}]})
        sim._director_split_step(state, 4, 4000, decider=lambda _s, _i, _g:
                                 {'pieces': [{'team': 'own', 'ask': piece['goal']}]})
        _save(state)
        leaf = next(r for r in _reqs(state) if r.get('splitFrom') == piece['id'])
        self.assertEqual(leaf.get('teamId'), 'lead1')
        self.assertNotIn('awaitingSplit', leaf)  # no split ping-pong
        got = sim._pending_breakdown_requests(state, 'lead1')
        self.assertEqual([r['id'] for r in got], [leaf['id']])
        # And the subteam can convene with the subdirector facilitating.
        self.assertTrue(sim._start_breakdown(state, 'lead1', [leaf['id']], 'lead1', 5000))
        _save(state)
        attendees = state['pendingBreakdowns']['lead1']
        self.assertIn('w1', attendees.get('people') or {})

    def test_subdirector_proposal_holds_piece_until_parent_decides(self):
        """A subdirector's 'new' piece is HELD on a proposal; the parent's
        approval review decides; approved -> the piece files for the new
        subteam, denied -> it files inside the proposer's own subteam. The
        work is never dropped either way, and nothing is created without the
        parent's judgment."""
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'lead1')
        _save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'new', 'ask': 'New lane', 'priority': 'high'}],
                    'newSubdirectors': [{'seedWorker': 'w2', 'handOver': 2,
                                         'purpose': 'owns the arch lane'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)   # lead1 convenes
        sim._director_split_step(state, 2, 2000, decider=decider)   # lead1 resolves
        _save(state)
        # The ask is accepted with the piece held; NOTHING was founded yet.
        self.assertEqual(req.get('status'), 'accepted')
        self.assertEqual(req['splitPieces'][0]['team'], 'pending-approval')
        props = [p for p in (state.get('pendingFoundations') or [])
                 if p.get('status') == 'pending']
        self.assertEqual(len(props), 1)
        prop = props[0]
        self.assertEqual(prop['founder'], 'lead1')
        self.assertEqual(prop['parent'], 'dev')
        self.assertEqual(prop['piece']['ask'], 'New lane')
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']
                    and r.get('status') == 'pending']
        self.assertEqual(children, [])  # held, not filed
        # The parent's approval review convenes (parent + proposer) and DENIES.
        sim._foundation_step(state, 3, 3000,
                             decider=lambda _s, _i, _g: {'approvals': [
                                 {'id': prop['id'], 'approve': False,
                                  'reason': 'lead1 has capacity'}]})
        sim._foundation_step(state, 4, 4000,
                             decider=lambda _s, _i, _g: {'approvals': [
                                 {'id': prop['id'], 'approve': False,
                                  'reason': 'lead1 has capacity'}]})
        _save(state)
        self.assertEqual(prop.get('status'), 'denied')
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        # Denied -> the piece stays with the proposer's own subteam.
        self.assertEqual(children[0].get('teamId'), 'lead1')
        self.assertIn('judged the founding unnecessary', children[0].get('splitNotes') or '')
        self.assertNotIn('w2', governance.subdirectors_of(state, 'lead1'))

    def test_subdirector_proposal_approved_founds_deeper(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'lead1')
        _save(state)

        def split_decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'new', 'ask': 'New lane'}],
                    'newSubdirectors': [{'seedWorker': 'w2', 'handOver': 2,
                                         'purpose': 'owns the arch lane'}]}

        sim._director_split_step(state, 1, 1000, decider=split_decider)
        sim._director_split_step(state, 2, 2000, decider=split_decider)
        _save(state)
        prop = next(p for p in state['pendingFoundations'] if p['status'] == 'pending')

        def approve_decider(_state, _instructions, _goal):
            return {'approvals': [{'id': prop['id'], 'approve': True,
                                   'reason': 'the arch needs its own lane'}]}

        sim._foundation_step(state, 3, 3000, decider=approve_decider)
        sim._foundation_step(state, 4, 4000, decider=approve_decider)
        _save(state)
        self.assertEqual(prop.get('status'), 'approved')
        self.assertEqual(prop.get('subdirector'), 'w2')
        # The new sub leads w1 (handed over from lead1's pool).
        self.assertIn('w1', governance.direct_reports(state, 'w2'))
        self.assertIn('w2', governance.subdirectors_of(state, 'lead1'))
        # The held piece now files bound for the new subteam.
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].get('teamId'), 'w2')
        self.assertIn('Approved by', children[0].get('splitNotes') or '')

    def test_foundation_outage_denies_conservatively(self):
        state = serve.get_state_from_db()
        req = sim.file_large_request(state, 'admin', 'Build the arch', 'lead1')
        _save(state)

        def split_decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'new', 'ask': 'New lane'}],
                    'newSubdirectors': [{'purpose': 'owns the arch lane'}]}

        sim._director_split_step(state, 1, 1000, decider=split_decider)
        sim._director_split_step(state, 2, 2000, decider=split_decider)
        _save(state)
        self.assertEqual(len([p for p in state['pendingFoundations']
                              if p['status'] == 'pending']), 1)
        # Outage: decider returns None -> deny is the conservative default.
        sim._foundation_step(state, 3, 3000, decider=lambda _s, _i, _g: None)
        sim._foundation_step(state, 4, 4000, decider=lambda _s, _i, _g: None)
        _save(state)
        self.assertFalse(any(p['status'] == 'pending'
                             for p in state['pendingFoundations']))
        children = [r for r in _reqs(state) if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].get('teamId'), 'lead1')

    def test_founding_converts_workers_never_spawns(self):
        """Founding reorganizes existing people: roster size is unchanged,
        one worker became a manager, and the hand-over workers still work --
        the pool never fills with managers."""
        state = serve.get_state_from_db()
        before = len(state['agentRoster'])
        worker_count = sum(1 for r in state['agentRoster']
                           if not governance.direct_reports(state, r['id'])
                           and not r.get('isAdmin'))
        sid = sim._found_subdirector(state, 'dev', seed_worker='w3', hand_over=2,
                                     approved_by=None)
        self.assertEqual(sid, 'w3')  # root director: unilateral founding stands
        self.assertEqual(len(state['agentRoster']), before)
        after_workers = sum(1 for r in state['agentRoster']
                            if not governance.direct_reports(state, r['id'])
                            and not r.get('isAdmin'))
        self.assertEqual(after_workers, worker_count - 1)  # exactly one manager minted

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
