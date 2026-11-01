"""Product-ownership routing: the team that OWNS a product gets the work.

A large ask that names a product routes to the owning team's director --
never the shared backlog, never a fresh team, never a free-but-unrelated
team -- and it WAITS there if the owner is mid-sprint. The owning
director's review (the split ceremony when they lead subteams) partitions
the ask down their own line: pieces inherit the ownership stamps, an owned
'shared' piece is scoped to the reviewer's own subteams (sharedScope --
shared never means company-wide for owned work), and downstream cards pin
assignment to the owning team's members. Unowned asks keep the historical
availability-based routing exactly as it was.

The user's rule: if a team owns a product, work on that product must not
end up on another team.
"""

import asyncio
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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-ownership-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _agent(aid, role='engineer', **over):
    d = {'id': aid, 'name': aid.title(), 'role': role, 'color': '#888888',
         'model': 'small', 'x': 320, 'y': 300, 'busy': False, 'visible': True,
         'offDuty': False, 'inRoom': 'commandcenter', 'spawned': True}
    d.update(over)
    return d


def _org():
    """Two senior directors (dev, ops) under the admin; dev leads a
    subdirector layer (lead1, lead2 with w1/w2 and w3 below); ops leads one
    worker. Teams rows exist for dev and ops (root teams)."""
    roster = [
        _agent('theo', 'chief of staff', isAdmin=True, isDirector=True, director=None),
        _agent('dev', 'Supervisor', isDirector=True, director='theo'),
        _agent('ops', 'Supervisor', isDirector=True, director='theo'),
        _agent('lead1', 'engineer', director='dev'),
        _agent('lead2', 'engineer', director='dev'),
        _agent('w1', 'engineer', director='lead1'),
        _agent('w2', 'engineer', director='lead2'),
        _agent('w3', 'engineer', director='dev'),
        _agent('o1', 'engineer', director='ops'),
    ]
    agents = {d['id']: dict(d) for d in roster}
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'teams': [{'id': 'dev', 'name': 'Dev', 'directorId': 'dev',
                   'members': ['lead1', 'lead2', 'w3']},
                  {'id': 'ops', 'name': 'Ops', 'directorId': 'ops',
                   'members': ['o1']}],
        'products': {'flashlight': {'id': 'flashlight', 'name': 'Flashlight',
                                    'teamId': 'dev', 'status': 'draft'},
                     'lantern': {'id': 'lantern', 'name': 'Flashlight Pro',
                                 'teamId': 'ops', 'status': 'draft'}},
        'backlogRequests': [],
        'pendingDirectorSplits': {},
        'pendingBreakdowns': {},
        'pendingFoundations': [],
        'pendingFoundationReviews': {},
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    return state


class OwnerResolution(unittest.TestCase):
    def test_explicit_product_id_wins(self):
        state = _org()
        got = serve._owner_team_for_goal(state, 'something else entirely',
                                         product_id='flashlight')
        self.assertEqual(got, (state['products']['flashlight'], 'dev'))

    def test_name_match_case_insensitive(self):
        state = _org()
        got = serve._owner_team_for_goal(state, 'Add dark mode to flashlight please')
        self.assertEqual(got[1], 'dev')

    def test_longest_name_wins(self):
        state = _org()
        # 'Flashlight Pro' contains 'flashlight' -- the LONGER name must win.
        got = serve._owner_team_for_goal(state, 'Ship Flashlight Pro v2')
        self.assertEqual(got[0]['id'], 'lantern')
        self.assertEqual(got[1], 'ops')

    def test_no_match_is_unowned(self):
        state = _org()
        self.assertIsNone(serve._owner_team_for_goal(state, 'repave the parking lot'))

    def test_vanished_owner_team_is_unowned(self):
        state = _org()
        state['products']['ghost'] = {'id': 'ghost', 'name': 'Ghost', 'teamId': 'gone'}
        self.assertIsNone(serve._owner_team_for_goal(state, 'fix the ghost app'))


class OwnedIntakeRouting(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro) \
            if not hasattr(asyncio, 'run') else asyncio.run(coro)

    def test_owned_ask_routes_to_owner_even_when_all_teams_busy(self):
        state = _org()
        # Every team committed to an active sprint: the OLD behavior would
        # have filed this into the company-wide shared backlog.
        state['sprints'] = {'s-1': {'id': 's-1', 'status': 'active',
                                    'teamIds': ['dev', 'ops']}}
        outcome, data = self._run(serve._dispatch_big_task(
            state, 'Add dark mode to flashlight', 'player'))
        self.assertEqual(outcome, 'staffed')
        req = next(r for r in state['backlogRequests'])
        self.assertEqual(req['teamId'], 'dev')
        self.assertEqual(req['ownerTeamId'], 'dev')
        self.assertEqual(req['ownerProductId'], 'flashlight')
        self.assertNotIn('shared', req)
        self.assertEqual(data.get('ownerProductId'), 'flashlight')

    def test_owned_ask_waits_through_sprint_never_shared_or_new_team(self):
        state = _org()
        state['sprints'] = {'s-1': {'id': 's-1', 'status': 'active',
                                    'teamIds': ['dev', 'ops']}}
        with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog') as shared, \
                unittest.mock.patch.object(sim, 'spawn_new_team_for_request') as spawn:
            outcome, _ = self._run(serve._dispatch_big_task(
                state, 'flashlight needs a dimmer', 'player'))
        self.assertEqual(outcome, 'staffed')
        shared.assert_not_called()
        spawn.assert_not_called()

    def test_allow_shared_restores_the_shared_escape(self):
        state = _org()
        state['sprints'] = {'s-1': {'id': 's-1', 'status': 'active',
                                    'teamIds': ['dev', 'ops']}}
        canned = {'feature': {'id': 'f-1', 'name': 'F'},
                  'items': [{'id': 'b-1'}]}
        with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog',
                                        return_value=canned):
            outcome, _ = self._run(serve._dispatch_big_task(
                state, 'flashlight needs a dimmer', 'player', allow_shared=True))
        self.assertEqual(outcome, 'backlogged')

    def test_unowned_ask_keeps_the_historical_routing(self):
        state = _org()
        state['sprints'] = {'s-1': {'id': 's-1', 'status': 'active',
                                    'teamIds': ['dev', 'ops']}}
        canned = {'feature': {'id': 'f-1', 'name': 'F'},
                  'items': [{'id': 'b-1'}]}
        with unittest.mock.patch.object(serve, '_breakdown_into_shared_backlog',
                                        return_value=canned):
            outcome, _ = self._run(serve._dispatch_big_task(
                state, 'repave the parking lot', 'player'))
        self.assertEqual(outcome, 'backlogged')

    def test_owned_request_stamps_ride_file_large_request(self):
        state = _org()
        req = sim.file_large_request(state, 'theo', 'flashlight work', 'dev',
                                     now_ms=1_000, owner_product_id='flashlight')
        self.assertEqual(req['ownerTeamId'], 'dev')
        self.assertEqual(req['ownerProductId'], 'flashlight')
        # A product with no owning team stamps nothing.
        req2 = sim.file_large_request(state, 'theo', 'orphan work', 'dev',
                                      now_ms=2_000, owner_product_id='ghost')
        self.assertNotIn('ownerTeamId', req2)


class OwnedSplitCascade(unittest.TestCase):
    def _start(self, state, req):
        self.assertTrue(sim._start_split(state, 'dev', [req['id']], 1_000))

    def _resolve(self, state, pieces):
        decider = lambda *a, **k: {'pieces': pieces}  # noqa: E731
        sim._director_split_step(state, 1, 1000, decider=decider)   # convene
        sim._director_split_step(state, 2, 2000, decider=decider)   # resolve

    def test_owned_pieces_inherit_and_shared_is_scoped_to_own_subteams(self):
        state = _org()
        req = sim.file_large_request(state, 'theo', 'flashlight overhaul', 'dev',
                                     now_ms=1_000, owner_product_id='flashlight')
        # dev leads subdirectors lead1/lead2 -> the request awaits the split.
        self.assertTrue(req.get('awaitingSplit'))
        self._start(state, req)
        self._resolve(state, [
            {'team': 'own', 'ask': 'core dimmer logic'},
            {'team': 'shared', 'ask': 'docs for both subteams'},
        ])
        children = [r for r in state['backlogRequests'] if r.get('splitFrom') == req['id']]
        self.assertEqual(len(children), 2)
        own_piece = next(c for c in children if c['teamId'] == 'dev')
        shared_piece = next(c for c in children if not c.get('teamId'))
        for c in children:
            self.assertEqual(c['ownerTeamId'], 'dev')
            self.assertEqual(c['ownerProductId'], 'flashlight')
        self.assertEqual(shared_piece['sharedScope'], ['lead1', 'lead2'])

    def test_unowned_shared_piece_stays_company_wide(self):
        state = _org()
        req = sim.file_large_request(state, 'theo', 'repave the parking lot', 'dev',
                                     now_ms=1_000)
        self._start(state, req)
        self._resolve(state, [{'team': 'shared', 'ask': 'survey the lot'}])
        shared = next(r for r in state['backlogRequests']
                      if r.get('splitFrom') == req['id'])
        self.assertNotIn('sharedScope', shared)
        self.assertNotIn('ownerTeamId', shared)

    def test_founded_subteam_piece_keeps_the_owner(self):
        # The proposer is a SUBDIRECTOR (lead1): root directors found
        # immediately, so the propose -> parent-approval chain only exists
        # below the root. The piece files for the approved new subteam and
        # MUST still carry the owning team's stamps.
        state = _org()
        req = sim.file_large_request(state, 'theo', 'flashlight redesign', 'lead1',
                                     now_ms=1_000, owner_product_id='flashlight')
        self.assertTrue(req.get('awaitingSplit'))
        decider = lambda *a, **k: {  # noqa: E731
            'pieces': [{'team': 'new', 'ask': 'the whole redesign'}],
            'newSubdirectors': [{'seedWorker': 'w1', 'handOver': 1,
                                 'purpose': 'hardware line'}]}
        sim._director_split_step(state, 1, 1000, decider=decider)   # convene
        sim._director_split_step(state, 2, 2000, decider=decider)   # stages the proposal
        prop = next(p for p in state['pendingFoundations'] if p['status'] == 'pending')
        self.assertEqual(prop['ownerProductId'], 'flashlight')
        self.assertEqual(prop['parent'], 'dev')
        approve = lambda *a, **k: {'approvals': [  # noqa: E731
            {'id': prop['id'], 'approve': True, 'reason': 'needed'}]}
        sim._foundation_step(state, 3, 3000, decider=approve)   # dev convenes
        sim._foundation_step(state, 4, 4000, decider=approve)   # dev approves
        piece = next(r for r in state['backlogRequests']
                     if r.get('splitFrom') == req['id'])
        self.assertEqual(piece['ownerTeamId'], 'dev')
        self.assertEqual(piece['ownerProductId'], 'flashlight')


class ScopedSharedClaiming(unittest.TestCase):
    def test_scoped_shared_piece_visible_only_to_the_owning_org(self):
        state = _org()
        scoped = {'id': 'wrq-1', 'status': 'pending', 'breakdown': True,
                  'shared': True, 'sharedScope': ['lead1', 'lead2']}
        company = {'id': 'wrq-2', 'status': 'pending', 'breakdown': True,
                   'shared': True}
        state['backlogRequests'] = [scoped, company]
        got = sim._pending_breakdown_requests(state, 'lead1')
        self.assertIn(scoped, got)
        self.assertIn(company, got)
        got = sim._pending_breakdown_requests(state, 'ops')
        self.assertNotIn(scoped, got)
        self.assertIn(company, got)

    def test_unrelated_team_cannot_claim_a_scoped_piece(self):
        state = _org()
        scoped = {'id': 'wrq-1', 'status': 'pending', 'breakdown': True,
                  'shared': True, 'sharedScope': ['lead1', 'lead2']}
        state['backlogRequests'] = [scoped]
        started = sim._start_breakdown(state, 'ops', ['wrq-1'], 'o1', 2_000)
        self.assertFalse(started)
        self.assertIsNone(scoped.get('teamId'))
        started = sim._start_breakdown(state, 'lead1', ['wrq-1'], 'w1', 3_000)
        self.assertTrue(started)
        self.assertEqual(scoped.get('teamId'), 'lead1')


class OwnedCardAssignment(unittest.TestCase):
    def _state(self, **over):
        roster = [
            _agent('maya', 'chief of staff', isAdmin=True, isDirector=True, director=None),
            _agent('dev', 'Supervisor', isDirector=True, director='maya'),
            _agent('ops', 'Supervisor', isDirector=True, director='maya'),
            _agent('w1', 'engineer', director='dev'),
            _agent('o1', 'engineer', director='ops'),
        ]
        agents = {d['id']: {'id': d['id'], 'name': d['name'], 'x': 320, 'y': 300,
                            'busy': False, 'visible': True, 'offDuty': True,
                            'inRoom': None} for d in roster}
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': roster,
            'agents': agents,
            'teams': [{'id': 'dev', 'name': 'Dev', 'directorId': 'dev', 'members': ['w1']},
                      {'id': 'ops', 'name': 'Ops', 'directorId': 'ops', 'members': ['o1']}],
            'products': {},
            'sprints': {},
            'workQueue': [],
            'tasks': {},
            'reports': [],
        }
        state.update(over)
        return state

    def _assign(self, state, now_ms=10_000):
        grid, doors = sim._load_outdoor_geometry()
        holder = {0: 0}
        return sim._assign_due_item(state, state['workQueue'][0], True, grid,
                                    doors, now_ms, task_id_holder=holder)

    def _card(self, **over):
        card = {'title': 'dimmer logic', 'room': 'pressoffice', 'taskType': 'code',
                'priority': sim.WORK_PRIORITY['normal'], 'teamId': 'dev',
                'ownerTeamId': 'dev', 'ownerProductId': 'flashlight'}
        card.update(over)
        return card

    def test_owned_card_waits_when_the_owner_is_busy(self):
        state = self._state()
        state['agents']['w1']['busy'] = True
        state['workQueue'] = [self._card()]
        got = self._assign(state)
        self.assertIsNone(got)  # a non-issue owned card defers (stays queued)
        self.assertEqual(len(state['workQueue']), 1)
        self.assertEqual(state['tasks'], {})

    def test_owned_card_never_lands_on_an_idle_unrelated_team(self):
        state = self._state()
        state['agents']['w1']['busy'] = True
        state['agents']['o1']['offDuty'] = False  # ops is idle RIGHT THERE
        state['workQueue'] = [self._card()]
        got = self._assign(state)
        self.assertNotEqual(got, sim._ASSIGN_WAIT)
        self.assertTrue(got is None or got is None)

    def test_owned_card_assigns_to_a_free_owner_member(self):
        state = self._state()
        state['agents']['o1']['offDuty'] = True
        state['workQueue'] = [self._card()]
        got = self._assign(state)
        self.assertIsInstance(got, dict)
        self.assertEqual(got.get('assignedTo'), 'w1')

    def test_unowned_team_card_keeps_the_soft_preference(self):
        state = self._state()
        state['agents']['w1']['busy'] = True  # no free dev member
        state['workQueue'] = [self._card(ownerTeamId=None, ownerProductId=None)]
        got = self._assign(state)
        # The historical soft preference falls back to the full pool: the
        # idle ops worker CAN take an unowned card.
        self.assertIsInstance(got, dict)
        self.assertEqual(got.get('assignedTo'), 'o1')

    def test_queue_work_carries_the_ownership_stamps(self):
        state = self._state()
        card = self._card()
        sim.queue_work(state, [card])
        item = state['workQueue'][0]
        self.assertEqual(item['ownerTeamId'], 'dev')
        self.assertEqual(item['ownerProductId'], 'flashlight')


if __name__ == '__main__':
    unittest.main()
