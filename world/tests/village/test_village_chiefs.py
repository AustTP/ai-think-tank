"""Village chiefs of staff (operator direction, 2026-11-01).

The org's authority chain is: chief of staff (theo, the one global admin)
-> per-village chiefs of staff -> senior directors (12 TOTAL across all
villages, not 12 per village) -> subdirector layers -> workers.

A chief is an APPOINTED authority tier -- an explicit roster flag
(`villageChief: True` + `chiefOfVillage: <villageId>`), never emergent.
The tests pin the invariants that keep the tiers straight:

- a chief's team-leading children are SENIOR directors (they hold teams
  rows), never the chief's subdirectors;
- the senior-director cap counts org-wide, including teams under chiefs;
- a chief (and a senior director under a chief) founds subdirectors
  IMMEDIATELY -- the chief tier must not demote the senior layer into
  proposers;
- a chief reviews large asks by ROUTING pieces to their senior teams;
  founding subdirectors is not the chief's tool;
- the layer-enforcement sweep ignores chiefs (their children are senior,
  not subdirectors, so no 3+ rebalance applies at that tier).
"""

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
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-chiefs-test-')
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
    """theo (admin) -> skye (village chief, west) -> two senior directors
    (dev, ops, each holding a teams row) -> workers. A second village
    (east) has one senior team (ivy) reporting straight to theo -- the
    pre-chief shape a village keeps until a chief is appointed."""
    roster = [
        _agent('theo', 'chief of staff', isAdmin=True, isDirector=True, director=None),
        _agent('skye', 'village chief of staff', isDirector=True, director='theo',
               villageChief=True, chiefOfVillage='west', villageId='west'),
        _agent('dev', 'Supervisor', isDirector=True, director='skye', villageId='west'),
        _agent('ops', 'Supervisor', isDirector=True, director='skye', villageId='west'),
        _agent('ivy', 'Supervisor', isDirector=True, director='theo', villageId='east'),
        _agent('w1', 'engineer', director='dev', villageId='west'),
        _agent('w2', 'engineer', director='ops', villageId='west'),
        _agent('w3', 'engineer', director='ivy', villageId='east'),
    ]
    agents = {d['id']: dict(d) for d in roster}
    return {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'teams': [{'id': 'dev', 'name': 'Dev', 'directorId': 'dev', 'members': ['w1']},
                  {'id': 'ops', 'name': 'Ops', 'directorId': 'ops', 'members': ['w2']},
                  {'id': 'ivy', 'name': 'Ivy', 'directorId': 'ivy', 'members': ['w3']}],
        'products': {},
        'sprints': {},
        'backlogRequests': [],
        'pendingDirectorSplits': {},
        'pendingBreakdowns': {},
        'pendingFoundations': [],
        'pendingFoundationReviews': {},
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }


class ChiefRecognition(unittest.TestCase):
    def test_chief_helpers(self):
        state = _org()
        self.assertEqual(governance.village_chief_ids(state), ['skye'])
        self.assertTrue(governance.is_village_chief(state, 'skye'))
        self.assertFalse(governance.is_village_chief(state, 'dev'))
        self.assertEqual(governance.chief_of_village(state, 'west'), 'skye')
        self.assertIsNone(governance.chief_of_village(state, 'east'))

    def test_chiefs_children_are_senior_not_subdirectors(self):
        state = _org()
        self.assertEqual(governance.subdirectors_of(state, 'skye'), [])
        # And the admin's team-leading children are senior too.
        self.assertEqual(governance.subdirectors_of(state, 'theo'), [])
        self.assertEqual(sorted(governance.direct_reports(state, 'skye')),
                         ['dev', 'ops'])

    def test_team_director_ids_span_every_tier(self):
        state = _org()
        self.assertEqual(governance.team_director_ids(state),
                         {'dev', 'ops', 'ivy'})

    def test_layer_enforcement_ignores_chiefs(self):
        state = _org()
        self.assertEqual(governance.enforce_subdirector_layers(state), 0)
        self.assertEqual(sorted(governance.direct_reports(state, 'skye')),
                         ['dev', 'ops'])


class SeniorCapIsGlobal(unittest.TestCase):
    def test_cap_counts_every_village(self):
        state = _org()
        # 12 is the operator's total: three senior teams across TWO villages
        # count together, not per village.
        self.assertEqual(governance.village_team_count(state), 3)

    def test_teams_under_a_chief_count_toward_the_cap(self):
        state = _org()
        teams = state['teams']
        teams.append({'id': 'x1', 'name': 'X1', 'directorId': 'x1',
                      'members': []})
        state['agentRoster'].append(_agent('x1', 'Supervisor', isDirector=True,
                                           director='skye', villageId='west'))
        self.assertEqual(governance.village_team_count(state), 4)

    def test_spawning_refused_at_twelve_senior_total(self):
        state = _org()
        # Fill the tier to 12 org-wide (dev, ops, ivy + 9 more across
        # villages, mixed reporting: some under skye, some under theo).
        teams = state['teams']
        roster = state['agentRoster']
        for i in range(9):
            parent = 'skye' if i % 2 else 'theo'
            teams.append({'id': f't{i}', 'name': f'T{i}', 'directorId': f't{i}',
                          'members': []})
            roster.append(_agent(f't{i}', 'Supervisor', isDirector=True,
                                 director=parent))
        self.assertEqual(governance.village_team_count(state), 12)
        spawned = sim.spawn_new_team_for_request(state, 'one more team',
                                                 admin_id='theo')
        self.assertIsNone(spawned)
        self.assertEqual(state['_teamSpawnBlockedAtVillageCap']['teams'], 12)


class ChiefReviewFlow(unittest.TestCase):
    def _save(self, state):
        serve.save_state_to_db(state)

    def test_chief_and_senior_under_chief_found_immediately(self):
        state = _org()
        self.assertEqual(sim._founding_mode(state, 'skye'), 'immediate')
        self.assertEqual(sim._founding_mode(state, 'dev'), 'immediate')
        # A subdirector under a SENIOR director still proposes.
        state['agentRoster'].append(_agent('w9', 'engineer', director='dev'))
        state['agentRoster'].append(_agent('w10', 'engineer', director='w9'))
        self.assertEqual(sim._founding_mode(state, 'w9'), 'propose')

    def test_chief_bound_request_awaits_the_chiefs_split(self):
        state = _org()
        req = sim.file_large_request(state, 'theo', 'redo the west village grid',
                                     'skye', now_ms=1000)
        self.assertTrue(req.get('awaitingSplit'))
        self._save(state)
        # The chief reviews by ROUTING: their org summary lists the west
        # village's senior teams, and the prompt names their role.
        org = sim._split_org_summary(state, 'skye')
        team_ids = {t['teamId'] for t in org}
        self.assertIn('dev', team_ids)
        self.assertIn('ops', team_ids)
        self.assertNotIn('ivy', team_ids)  # east village is not skye's org
        text = sim._split_instructions(
            state, req, org, 'Skye',
            founding_mode=sim._founding_mode(state, 'skye'),
            director_id='skye')
        self.assertIn('regional VP', text)
        self.assertIn('Founding subdirectors is not your tool', text)

    def test_chief_routes_pieces_to_senior_teams(self):
        state = _org()
        req = sim.file_large_request(state, 'theo', 'redo the west village grid',
                                     'skye', now_ms=1000)
        self._save(state)

        def decider(_state, _instructions, _goal):
            return {'pieces': [{'team': 'dev', 'ask': 'street grid'},
                               {'team': 'ops', 'ask': 'utilities'}]}

        sim._director_split_step(state, 1, 1000, decider=decider)   # convene
        sim._director_split_step(state, 2, 2000, decider=decider)   # resolve
        self._save(state)
        pieces = [r for r in state['backlogRequests']
                  if r.get('splitFrom') == req['id']]
        self.assertEqual(sorted(p['teamId'] for p in pieces), ['dev', 'ops'])
        for p in pieces:
            self.assertEqual(p.get('splitBy'), 'skye')

    def test_senior_director_under_chief_splits_their_own_layer(self):
        # dev (senior, under the chief) keeps the full root-director flow:
        # their request awaits THEIR split, their org lists their
        # subdirectors, and immediate founding still works below them.
        state = _org()
        state['agentRoster'].extend([
            _agent('lead1', 'engineer', director='dev', villageId='west'),
            _agent('w4', 'engineer', director='lead1', villageId='west'),
        ])
        req = sim.file_large_request(state, 'theo', 'flashlight rework', 'dev',
                                     now_ms=1000)
        self.assertTrue(req.get('awaitingSplit'))
        self.assertEqual(sim._founding_mode(state, 'dev'), 'immediate')
        org = sim._split_org_summary(state, 'dev')
        self.assertIn('lead1', {t['teamId'] for t in org})


if __name__ == '__main__':
    unittest.main()
