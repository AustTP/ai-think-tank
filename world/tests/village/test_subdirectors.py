"""Subdirector layer + the 3+ rule (scale slice 4).

A subdirector is emergent middle management: an agent reporting to a
director who themselves has reports. The 3+ rule: at three or more
subdirectors, the director's remaining direct workers are distributed
across the layer and the director directs only subdirectors. Everything
that treats the roster `director` field as "the team" (spend attribution,
sprint staffing, status views) walks the chain to the root director so the
layer changes supervision, never team membership.
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

governance = sim.governance  # core.governance, under sim's shared namespace

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-subdirectors-')
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


def _team_state(workers=5, subs=3, workers_per_sub=None, admin=True):
    """dev team: director 'dev', `subs` subdirectors (sd1..sdN) each leading
    workers_per_sub (default 1) workers, `workers` direct workers (w1..wN)."""
    roster = []
    if admin:
        roster.append({'id': 'theo', 'isAdmin': True})
    roster.append({'id': 'dev', 'isDirector': True})
    for i in range(1, subs + 1):
        roster.append({'id': f'sd{i}', 'director': 'dev'})
        for j in range(1, (workers_per_sub if workers_per_sub is not None else 1) + 1):
            roster.append({'id': f's{i}w{j}', 'director': f'sd{i}'})
    for i in range(1, workers + 1):
        roster.append({'id': f'w{i}', 'director': 'dev'})
    return {
        'agentRoster': roster,
        'agents': {r['id']: {'id': r['id'], 'busy': False} for r in roster},
        'teams': [{'id': 'dev', 'directorId': 'dev'}],
        'tasks': {}, 'workQueue': [], 'playerInbox': [],
    }


class SubdirectorGovernance(unittest.TestCase):
    def test_subdirectors_are_emergent(self):
        state = _team_state(subs=2, workers_per_sub=1)
        self.assertEqual(governance.subdirectors_of(state, 'dev'),
                         ['sd1', 'sd2'])
        # A plain direct report with no reports of their own is not one.
        state['agentRoster'].append({'id': 'w9', 'director': 'dev'})
        self.assertEqual(governance.subdirectors_of(state, 'dev'),
                         ['sd1', 'sd2'])

    def test_layer_threshold_is_three(self):
        state = _team_state(subs=2, workers_per_sub=1)
        self.assertFalse(governance.has_subdirector_layer(state, 'dev'))
        state = _team_state(subs=3, workers_per_sub=1)
        self.assertTrue(governance.has_subdirector_layer(state, 'dev'))

    def test_rebalance_moves_direct_workers_across_the_layer(self):
        # Each subdirector already leads one worker (that is what makes her a
        # subdirector); 5 more direct workers ride on dev.
        state = _team_state(subs=3, workers_per_sub=1, workers=5)
        moved = governance.rebalance_under_subdirectors(state, 'dev')
        self.assertEqual(moved, 5)
        self.assertEqual(governance.direct_reports(state, 'dev'),
                         ['sd1', 'sd2', 'sd3'])
        loads = [len(governance.direct_reports(state, sid))
                 for sid in ('sd1', 'sd2', 'sd3')]
        self.assertEqual(loads, [3, 3, 2])  # balanced, roster-order tiebreak
        self.assertTrue(governance.has_subdirector_layer(state, 'dev'))

    def test_rebalance_no_op_below_threshold(self):
        state = _team_state(subs=2, workers_per_sub=0, workers=3)
        self.assertEqual(governance.rebalance_under_subdirectors(state, 'dev'), 0)
        self.assertEqual(len(governance.direct_reports(state, 'dev')), 5)

    def test_team_director_walks_up_through_the_layer(self):
        state = _team_state(subs=3, workers_per_sub=2)
        self.assertEqual(governance.team_director_of(state, 'w1'), 'dev')
        self.assertEqual(governance.team_director_of(state, 's1w1'), 'dev')
        self.assertEqual(governance.team_director_of(state, 'sd2'), 'dev')
        self.assertEqual(governance.team_director_of(state, 'dev'), 'dev')
        self.assertEqual(governance.team_director_of(state, 'theo'), 'theo')
        self.assertIsNone(governance.team_director_of(state, 'stranger'))

    def test_team_director_map_is_cycle_safe(self):
        state = _team_state(subs=1)
        state['agentRoster'].append({'id': 'x', 'director': 'y'})
        state['agentRoster'].append({'id': 'y', 'director': 'x'})
        roots = governance.team_director_map(state)
        self.assertIsNone(roots['x'])
        self.assertIsNone(roots['y'])

    def test_team_workers_exclude_the_layer(self):
        state = _team_state(subs=3, workers_per_sub=2, workers=1)
        workers = governance.team_workers(state, 'dev')
        # Everyone who works for the team is in: direct workers and
        # subdirectors' workers alike. Not in: the director, the subdirectors
        # (facilitators, not hands), the admin.
        self.assertNotIn('dev', workers)
        self.assertNotIn('sd1', workers)
        self.assertIn('s1w1', workers)
        self.assertIn('w1', workers)
        self.assertNotIn('theo', workers)

    def test_supervisor_for_new_hire_below_and_at_the_layer(self):
        state = _team_state(subs=2, workers_per_sub=1)
        self.assertEqual(
            governance.supervisor_for_new_hire(state, 'dev'), 'dev')
        state = _team_state(subs=3, workers_per_sub=1)
        # Layer active, all loads equal: least-loaded, roster-order tiebreak.
        self.assertEqual(
            governance.supervisor_for_new_hire(state, 'dev'), 'sd1')
        state['agentRoster'].append({'id': 'h1', 'director': 'sd1'})
        self.assertEqual(
            governance.supervisor_for_new_hire(state, 'dev'), 'sd2')


class LayerCeilingAndEnforcement(unittest.TestCase):
    def test_layer_max_is_six(self):
        self.assertEqual(governance.SUBDIRECTOR_LAYER_MAX, 6)

    def test_enforce_heals_an_organic_crossing(self):
        # Organic crossing: three agents under dev each quietly gained their
        # own reports, while dev still holds direct workers. One enforce pass
        # balances the layer and empties dev's direct-worker bench.
        state = _team_state(subs=3, workers_per_sub=1, workers=4)
        moved = governance.enforce_subdirector_layers(state)
        self.assertEqual(moved, 4)
        self.assertEqual(governance.direct_reports(state, 'dev'),
                         ['sd1', 'sd2', 'sd3'])
        # Idempotent: a second pass moves nothing.
        self.assertEqual(governance.enforce_subdirector_layers(state), 0)

    def test_enforce_leaves_below_threshold_directors_alone(self):
        state = _team_state(subs=2, workers_per_sub=1, workers=3)
        self.assertEqual(governance.enforce_subdirector_layers(state), 0)
        self.assertEqual(len(governance.direct_reports(state, 'dev')), 5)


class SprintPoolWithLayer(unittest.TestCase):
    def _sprint_state(self):
        state = _team_state(subs=3, workers_per_sub=2, workers=0)
        state['sprints'] = {
            'spr-1': {'id': 'spr-1', 'teamIds': ['dev'], 'workerCount': 6,
                      'ownerId': 'dev'},
        }
        state['teams'] = [{'id': 'dev', 'directorId': 'dev',
                           'scrumMasterId': None}]
        return state

    def test_rebalanced_workers_staff_the_sprint(self):
        state = self._sprint_state()
        pool = sim._sprint_worker_pool(state, 'spr-1')
        # Every worker staffs the sprint regardless of which subdirector they
        # report to -- s1w1 reports to subdirector sd1 and IS in the pool.
        # No subdirector and no director is in the pool.
        self.assertIn('s1w1', pool)
        self.assertEqual(len(pool), 6)
        self.assertNotIn('sd1', pool)
        self.assertNotIn('dev', pool)


if __name__ == '__main__':
    unittest.main()
