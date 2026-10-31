"""Scale-tier workers (scale slice 5).

The 1,000-agent scenario runs plain workers on the cheap sublow band with no
JEV escalation, and skips the coaching loop for their closes: at scale,
coaching noise IS the cost. Code/review/qa is never pinned (grading
correctness is the whole job there), directors/subdirectors/scrum masters
are never scale-tier, and the switch defaults OFF (zero behavior change).
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402
import sim  # noqa: E402
from core import config as core_config  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-scale-tier-')
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


def _state():
    roster = [
        {'id': 'theo', 'isAdmin': True},
        {'id': 'dev', 'isDirector': True, 'director': 'theo'},
        {'id': 'sd1', 'director': 'dev'},
        {'id': 'sd1w1', 'director': 'sd1'},
        {'id': 'w1', 'director': 'dev'},
        {'id': 'w2', 'director': 'dev'},
        {'id': 'sm1', 'director': 'dev'},
    ]
    return {
        'agentRoster': roster,
        'agents': {r['id']: {'id': r['id']} for r in roster},
        'teams': [{'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'sm1'}],
        'tasks': {}, 'workQueue': [], 'playerInbox': [],
    }


def _policy(**over):
    saved = dict(core_config.OPERATOR_POLICY)
    core_config.OPERATOR_POLICY.update(
        {'scaleTierWorkers': True, 'scaleTierGradeFloor': 4.0, **over})
    return saved


class ScaleWorkerMembership(unittest.TestCase):
    def setUp(self):
        self._saved = _policy()

    def tearDown(self):
        core_config.OPERATOR_POLICY.clear()
        core_config.OPERATOR_POLICY.update(self._saved)

    def test_plain_workers_only(self):
        # sd1w1 is a plain worker under the subdirector layer: still a worker,
        # still scale-tier. Supervisors (dev/sd1), the admin, and the SM are
        # never scale-tier.
        ids = serve.governance.scale_worker_ids(_state())
        self.assertEqual(ids, {'w1', 'w2', 'sd1w1'})

    def test_row_none_for_supervisors_and_admin(self):
        s = _state()
        g = serve.governance
        self.assertIsNotNone(g.scale_worker_row(s, 'w1'))
        self.assertIsNone(g.scale_worker_row(s, 'theo'))
        self.assertIsNone(g.scale_worker_row(s, 'dev'))
        self.assertIsNone(g.scale_worker_row(s, 'sd1'))
        self.assertIsNone(g.scale_worker_row(s, 'sm1'))

    def test_policy_default_is_off(self):
        self.assertFalse(
            core_config.OPERATOR_POLICY_DEFAULT['scaleTierWorkers'])
        self.assertEqual(
            core_config.OPERATOR_POLICY_DEFAULT['scaleTierGradeFloor'], 4.0)
        # Shipped JSON carries the same values.
        shipped = core_config.get_operator_policy()
        self.assertFalse(shipped['scaleTierWorkers'])
        self.assertEqual(shipped['maxTotalAgents'], 1000)


class ScaleTierTierPin(unittest.TestCase):
    def setUp(self):
        self._saved = _policy()
        # Band-selective mock: only the sublow band resolves to the sentinel.
        # A blanket mock would poison every band's lookup and make the
        # "never pinned" assertions meaningless.
        self._slug_patcher = unittest.mock.patch.object(
            serve, '_raw_model_tier_slug',
            side_effect=lambda band: 'sublow-slug' if band == 'sublow'
            else f'{band}-slug')
        self._slug_patcher.start()
        # Deterministic roster membership without touching the DB.
        self._cache_patcher = unittest.mock.patch.object(
            serve, '_SCALE_ACTOR_CACHE',
            {'at': time.time() + 9999.0, 'ids': frozenset({'w1'})})
        self._cache_patcher.start()

    def tearDown(self):
        self._cache_patcher.stop()
        self._slug_patcher.stop()
        core_config.OPERATOR_POLICY.clear()
        core_config.OPERATOR_POLICY.update(self._saved)
        serve._FREE_POOL_ACTOR_AGENT.set(None)

    def _stamp(self, agent_id):
        serve._FREE_POOL_ACTOR_TEAM.set(None)
        serve._FREE_POOL_ACTOR_AGENT.set(agent_id)

    def test_scale_worker_pinned_to_sublow(self):
        self._stamp('w1')
        self.assertEqual(
            serve._resolve_model_tier('summarize a research note',
                                      task_type='research'),
            'sublow-slug')

    def test_code_never_pinned(self):
        self._stamp('w1')
        self.assertNotEqual(
            serve._resolve_model_tier('implement the card', task_type='code'),
            'sublow-slug')

    def test_latency_never_pinned(self):
        self._stamp('w1')
        self.assertNotEqual(
            serve._resolve_model_tier('pulse triage check',
                                      task_type='pulse'),
            'sublow-slug')

    def test_policy_off_or_no_actor_changes_nothing(self):
        core_config.OPERATOR_POLICY['scaleTierWorkers'] = False
        self._stamp('w1')
        self.assertNotEqual(
            serve._resolve_model_tier('anything', task_type='research'),
            'sublow-slug')
        core_config.OPERATOR_POLICY['scaleTierWorkers'] = True
        self._stamp(None)
        self.assertNotEqual(
            serve._resolve_model_tier('anything', task_type='research'),
            'sublow-slug')


class ScaleTierCoachingSkip(unittest.TestCase):
    def setUp(self):
        self._saved = _policy()
        self._state = _state()
        self._logged = []
        self._log_patcher = unittest.mock.patch.object(
            serve, 'log_action',
            side_effect=lambda agent, action, details, authorized=False:
            self._logged.append(action))
        self._log_patcher.start()

    def tearDown(self):
        self._log_patcher.stop()
        core_config.OPERATOR_POLICY.clear()
        core_config.OPERATOR_POLICY.update(self._saved)

    def test_low_close_skips_coaching_for_scale_worker(self):
        s = self._state
        sim._coach_low_grade(s, 'w1', 'pressoffice', 3.0,
                             int(time.time() * 1000), 'A card')
        self.assertNotIn('w1', s.get('growthPlans') or {})
        self.assertIn('scale_tier_grade_accepted', self._logged)

    def test_low_close_still_coaches_a_normal_worker(self):
        core_config.OPERATOR_POLICY['scaleTierWorkers'] = False
        s = self._state
        sim._coach_low_grade(s, 'w1', 'pressoffice', 3.0,
                             int(time.time() * 1000), 'A card')
        plans = (s.get('growthPlans') or {}).get('w1') or []
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].get('kind'), 'low_grade')
        self.assertNotIn('scale_tier_grade_accepted', self._logged)


class ScaleCapacityConfig(unittest.TestCase):
    def test_total_agent_cap_raised_for_scale(self):
        self.assertEqual(sim.MAX_TOTAL_AGENTS, 1000)


if __name__ == '__main__':
    unittest.main()
