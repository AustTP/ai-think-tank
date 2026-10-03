"""Fault-aware routing memory, ported from a real 2026 paper
(StigmergyRouter, UC Berkeley/ACM CAIS): a lightweight pheromone-memory layer
that steers _assign_due_item's round-robin away from an agent whose work was
just orphaned/reclaimed, using only cheap local counters (no re-classification,
no LLM call). Deliberately short-lived (a cooldown, not a lasting judgment --
that's the grading system's job) and a SOFT preference, never a hard lock: if
every eligible candidate is currently cooling down, assignment falls back to
the plain round-robin pick rather than ever blocking real work.

Isolated the same way tests/test_oncall.py is (a real think_tank.db side effect
lives inside _assign_due_item via serve.log_action) -- DB_PATH/THINK_TANK_DIR/etc
redirected to a temp dir for the whole module.
"""

import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import sim

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think tank-fault-routing-test-')
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


def _director(rid, is_admin=False, **over):
    d = {'id': rid, 'name': rid.title(), 'role': 'director' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': rid}
    d.update(over)
    return d


def _state(**over):
    # Non-admin roster order (round-robin base order): ben, cora, dax, zia.
    roster = [
        _director('maya', is_admin=True),
        _director('ben', director='maya'),
        _director('cora', director='maya'),
        _director('dax', director='maya'),
        _director('zia', director='maya'),
    ]
    # offDuty=True: x=0,y=0 is not a real walkable spot, so the CHOSEN agent
    # must go through appear_from_outskirts (real reachable placement) before
    # assign_task's find_path call, same as test_oncall.py's PinWakeBug does.
    agents = {d['id']: {'id': d['id'], 'name': d['name'], 'x': 0, 'y': 0, 'busy': False,
                        'visible': True, 'offDuty': True, 'inRoom': None} for d in roster}
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'teams': [],
        'products': {},
        'sprints': {},
        'workQueue': [{'title': 'Build the thing', 'room': 'pressoffice', 'taskType': 'code',
                       'priority': sim.WORK_PRIORITY['normal']}],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _assign(state, now_ms=10_000):
    grid, doors = sim._load_outdoor_geometry()
    holder = {0: 0}
    item = state['workQueue'][0]
    return sim._assign_due_item(state, item, True, grid, doors, now_ms, task_id_holder=holder)


class FailureScoreMath(unittest.TestCase):
    def test_no_recorded_failure_scores_zero(self):
        state = {}
        self.assertEqual(sim._agent_failure_score(state, 'ben', 10_000), 0.0)

    def test_fresh_failure_scores_near_full_count(self):
        state = {}
        sim.record_agent_failure(state, 'ben', now_ms=10_000)
        # Checked immediately (zero elapsed age) -- no decay yet.
        self.assertAlmostEqual(sim._agent_failure_score(state, 'ben', 10_000), 1.0)

    def test_score_decays_by_half_after_one_half_life(self):
        state = {}
        sim.record_agent_failure(state, 'ben', now_ms=0)
        half_life_ms = sim.FAILURE_COOLDOWN_HALF_LIFE_S * 1000
        self.assertAlmostEqual(sim._agent_failure_score(state, 'ben', half_life_ms), 0.5, places=6)

    def test_repeated_failures_accumulate_count(self):
        state = {}
        sim.record_agent_failure(state, 'ben', now_ms=10_000)
        sim.record_agent_failure(state, 'ben', now_ms=10_000)
        self.assertAlmostEqual(sim._agent_failure_score(state, 'ben', 10_000), 2.0)

    def test_falsy_agent_id_is_a_noop(self):
        state = {}
        sim.record_agent_failure(state, None, now_ms=10_000)
        self.assertEqual(state.get('agentFailureMemory', {}), {})


class RoundRobinFaultAvoidance(unittest.TestCase):
    def test_skips_the_pointed_at_candidate_when_it_has_a_fresh_failure(self):
        state = _state()
        state['sim']['rr'] = {'task': 0}  # pointer at index 0 -> 'ben'
        sim.record_agent_failure(state, 'ben', now_ms=9_900)  # 100ms before "now"
        task = _assign(state, now_ms=10_000)
        self.assertIsNotNone(task)
        # ben is cooling down -> the next eligible candidate (cora) gets it.
        self.assertEqual(task.get('assignedTo'), 'cora')

    def test_stale_failure_no_longer_deprioritizes(self):
        state = _state()
        state['sim']['rr'] = {'task': 0}  # pointer at index 0 -> 'ben'
        very_old = 10_000 - int(sim.FAILURE_COOLDOWN_HALF_LIFE_S * 1000 * 20)  # ~20 half-lives ago
        sim.record_agent_failure(state, 'ben', now_ms=very_old)
        task = _assign(state, now_ms=10_000)
        # The signal has fully decayed -- plain round-robin picks ben as usual.
        self.assertEqual(task.get('assignedTo'), 'ben')

    def test_never_blocks_assignment_when_everyone_is_cooling_down(self):
        state = _state()
        state['sim']['rr'] = {'task': 0}
        for aid in ('ben', 'cora', 'dax', 'zia'):
            sim.record_agent_failure(state, aid, now_ms=9_900)
        task = _assign(state, now_ms=10_000)
        # A soft preference, never a hard lock -- work still gets assigned.
        self.assertIsNotNone(task)
        self.assertIn(task.get('assignedTo'), {'ben', 'cora', 'dax', 'zia'})


class RolloverReassignPin(unittest.TestCase):
    """Refinement's sprint-rollover re-plan pins a carried-over card to a fresh
    explicit owner via `_reassignedTo`; _assign_due_item honors it like any other
    pin, and falls back to the generic pool when that member is unavailable."""

    def test_reassigned_pin_is_honored_when_target_free(self):
        state = _state()
        state['workQueue'] = [{'title': 'Rolled over card', 'room': 'pressoffice',
                               'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'],
                               '_reassignedTo': 'cora'}]
        task = _assign(state, now_ms=10_000)
        self.assertIsNotNone(task)
        self.assertEqual(task.get('assignedTo'), 'cora')

    def test_reassigned_pin_falls_back_when_target_busy(self):
        state = _state()
        state['agents']['cora']['busy'] = True
        state['agents']['cora']['task'] = 't-x'
        state['tasks'] = {'t-x': {'id': 't-x', 'status': 'working',
                                  'assignedTo': 'cora'}}
        state['workQueue'] = [{'title': 'Rolled over card', 'room': 'pressoffice',
                               'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'],
                               '_reassignedTo': 'cora'}]
        task = _assign(state, now_ms=10_000)
        # A soft pin, never a hard lock: the card still gets assigned.
        self.assertIsNotNone(task)
        self.assertNotEqual(task.get('assignedTo'), 'cora')


class FeatureAffinity(unittest.TestCase):
    """Feature affinity: a soft pull toward the agent who has completed similar
    work before (same room / feature / product), layered on top of the team +
    fault-aware round-robin -- never a hard lock, ties preserve roster order."""

    def _done(self, agent_id, room='pressoffice', **over):
        task = {'id': f'd-{agent_id}-{room}', 'title': 'Prior', 'room': room,
                'status': 'done', 'assignedTo': agent_id, 'projectLabel': None,
                'goal': None, 'productId': None}
        task.update(over)
        return task

    def _pick(self, **over):
        item = {'title': 'Backlog card', 'room': 'pressoffice', 'taskType': 'code',
                'priority': sim.WORK_PRIORITY['normal']}
        item.update(over)
        return item

    def test_pulls_toward_agent_with_prior_similar_work(self):
        state = _state()
        state['tasks'] = {'d-1': self._done('cora')}
        state['workQueue'] = [self._pick()]
        task = _assign(state, now_ms=10_000)
        self.assertEqual(task.get('assignedTo'), 'cora',
                         'affinity beats the round-robin pointer at ben')

    def test_affinity_is_soft_no_lock(self):
        state = _state()
        state['tasks'] = {'d-1': self._done('cora')}
        state['agents']['cora']['busy'] = True
        state['agents']['cora']['task'] = 't-x'
        state['tasks']['t-x'] = {'id': 't-x', 'status': 'working',
                                 'assignedTo': 'cora'}
        state['workQueue'] = [self._pick()]
        task = _assign(state, now_ms=10_000)
        # cora is ineligible; the card still gets assigned elsewhere.
        self.assertIsNotNone(task)
        self.assertNotEqual(task.get('assignedTo'), 'cora')

    def test_feature_match_preferred_over_plain_room_match(self):
        state = _state()
        state['tasks'] = {
            'd-1': self._done('cora', room='observatory'),
            'd-2': self._done('dax', room='pressoffice', projectLabel='bigfeature'),
        }
        state['workQueue'] = [self._pick(projectLabel='bigfeature')]
        task = _assign(state, now_ms=10_000)
        # dax matches room AND feature (2); cora matches neither (0).
        self.assertEqual(task.get('assignedTo'), 'dax')

    def test_ties_preserve_roster_order(self):
        state = _state()
        state['tasks'] = {
            'd-1': self._done('cora'),
            'd-2': self._done('dax'),
        }
        state['workQueue'] = [self._pick()]
        task = _assign(state, now_ms=10_000)
        self.assertEqual(task.get('assignedTo'), 'cora',
                         'equal affinity falls back to roster order')

    def test_no_history_means_plain_round_robin(self):
        state = _state()
        state['workQueue'] = [self._pick()]
        task = _assign(state, now_ms=10_000)
        self.assertEqual(task.get('assignedTo'), 'ben',
                         'no affinity signal -> unchanged round-robin')


class OnboardingAffinity(unittest.TestCase):
    """A brand-new team member has NO completed-work history, so feature affinity
    can never route work to them (they'd fall to pure round-robin). Description
    affinity fills that gap: when nobody has matching prior work, the candidate
    whose role/mission DESCRIPTION fits the task is soft-pulled toward it."""

    def _hire(self, rid, role='engineer', mission='', **over):
        d = _director(rid, director='maya')
        d['role'] = role
        d['profile'] = {'mission': mission, 'instructions': []}
        d.update(over)
        return d

    def _weather_item(self):
        return {'title': 'Crawl weather data and file a digest', 'room': 'observatory',
                'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'],
                'instructions': 'Crawl the weather data source and write up findings.'}

    def test_fresh_hire_gets_work_matching_its_description(self):
        state = _state()
        # Everyone is a fresh hire (no done tasks); zia's description names the
        # exact work domain. Feature affinity is 0 for all, so description
        # affinity must route the weather work to zia over the round-robin head.
        state['agentRoster'] = [
            _director('maya', is_admin=True),
            self._hire('ben', mission='Support the team with general backlog work.'),
            self._hire('cora', mission='Support the team with general backlog work.'),
            self._hire('dax', mission='Support the team with general backlog work.'),
            self._hire('zia', role='Assistant weather researcher',
                       mission='Support the research team by picking up weather data research overflow.'),
        ]
        state['agents']['zia'] = {**state['agents']['zia'], 'offDuty': True, 'x': 0, 'y': 0}
        state['workQueue'] = [self._weather_item()]
        task = _assign(state, now_ms=10_000)
        self.assertIsNotNone(task)
        self.assertEqual(task.get('assignedTo'), 'zia',
                         'the fresh hire whose description fits the work must be chosen')

    def test_description_affinity_is_soft_not_a_lock(self):
        state = _state()
        # Nobody's description matches the work domain at all -> unchanged
        # round-robin (ben at the pointer), no fabricated match.
        state['agentRoster'] = [
            _director('maya', is_admin=True),
            self._hire('ben', mission='Support the team with general backlog work.'),
            self._hire('cora', mission='Support the team with general backlog work.'),
            self._hire('dax', mission='Support the team with general backlog work.'),
            self._hire('zia', mission='Support the team with general backlog work.'),
        ]
        state['workQueue'] = [self._weather_item()]
        task = _assign(state, now_ms=10_000)
        self.assertEqual(task.get('assignedTo'), 'ben')

    def test_feature_affinity_wins_over_description(self):
        # Once an agent HAS matching history, feature affinity outranks a fresh
        # hire's description -- prior proof beats a written promise.
        state = _state()
        state['agentRoster'] = [
            _director('maya', is_admin=True),
            self._hire('ben', mission='Support the team with general backlog work.'),
            self._hire('cora', mission='Support the team with general backlog work.'),
            self._hire('dax', mission='Support the team with general backlog work.'),
            self._hire('zia', role='Assistant weather researcher',
                       mission='Support the research team by picking up weather data research overflow.'),
        ]
        state['tasks'] = {'d-1': {'id': 'd-1', 'title': 'Prior', 'room': 'observatory',
                                  'status': 'done', 'assignedTo': 'ben',
                                  'projectLabel': None, 'goal': None, 'productId': None}}
        state['workQueue'] = [self._weather_item()]
        task = _assign(state, now_ms=10_000)
        self.assertEqual(task.get('assignedTo'), 'ben',
                         'prior matching work must beat a description-only match')


class OrphanReclaimRecordsFailure(unittest.TestCase):
    def test_reclaiming_an_orphaned_task_records_its_assignee_as_a_failure(self):
        state = {
            'agents': {'ben': {'id': 'ben', 'task': None}},
            'tasks': {'task-9': {'id': 'task-9', 'status': 'walking', 'assignedTo': 'ben',
                                 'title': 'Stuck thing', 'room': 'pressoffice'}},
            'workQueue': [],
        }
        reclaimed = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(reclaimed, 1)
        self.assertIn('ben', state.get('agentFailureMemory', {}))
        self.assertEqual(state['agentFailureMemory']['ben']['count'], 1)

    def test_orphan_with_no_assignee_records_nothing(self):
        state = {
            'agents': {},
            'tasks': {'task-9': {'id': 'task-9', 'status': 'walking', 'assignedTo': None,
                                 'title': 'Stuck thing', 'room': 'pressoffice'}},
            'workQueue': [],
        }
        reclaimed = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(reclaimed, 1)
        self.assertEqual(state.get('agentFailureMemory', {}), {})


if __name__ == '__main__':
    unittest.main()
