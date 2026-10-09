"""Tests for two capacity decisions:

1. Scheduled-item PREEMPTION: a due `queue_once` item (notBefore) must still
   fire at its specified time even when every agent is busy -- the case the
   player asked about ("if even everyone is busy that they work on scheduled
   items at the specified time"). At the active ceiling (MAX_ACTIVE_AGENTS),
   can_activate_another is False so no off-duty agent can be woken; instead we
   suspend one busy agent's current task (parked to resume after the scheduled
   item completes) and free her for the item.

2. HIRING GATE: stop spawning new agents at MAX_ACTIVE_AGENTS (25). A slot only
   reopens when workers go dormant AND those dormant workers have no scheduled
   task or work they will be re-awakened for (the dormant-wake refinement).

Hermetic: DB redirected to a throwaway temp dir; decider injected, no network.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve  # noqa: E402
import sim  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-preempt-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        THINK_TANK_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
        COLAB_STANDBY_ENABLED=False,
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _busy_at_cap_state():
    """A state at the active ceiling with every agent mid-'working' on a real
    task. 26 on-duty agents (25 workers + the admin) keeps active_agent_count
    >= MAX_ACTIVE_AGENTS so no off-duty wake is possible -- the exact preemption
    scenario. Callers queue the item under test themselves."""
    now = 100_000.0
    now_ms = int(now * 1000)
    roster = []
    agents = {}
    tasks = {}
    n = sim.MAX_ACTIVE_AGENTS + 1  # 25 workers + 1 admin, all on-duty
    for i in range(n):
        aid = f'a{i:02d}'
        is_admin = (i == 0)
        roster.append({'id': aid, 'name': f'Agent {i}',
                       'role': 'Admin' if is_admin else 'Worker',
                       'isAdmin': is_admin})
        agents[aid] = {'id': aid, 'x': 200 + (i % 8) * 80, 'y': 300 + (i // 8) * 80,
                       'dir': 'south', 'visible': False, 'busy': True,
                       'task': f't-{i}' if not is_admin else None,
                       'inRoom': 'house' if not is_admin else None,
                       'offDuty': False, 'stuckTimer': 0, 'replanCount': 0}
        if not is_admin:
            tasks[f't-{i}'] = {'id': f't-{i}', 'status': 'working', 'room': 'house',
                               'title': f'in-flight {i}', 'taskType': 'code',
                               'workUntil': now + 10_000.0}
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'tasks': tasks,
        'workQueue': [],
        'reports': [],
        'researchTopics': [],
        'lastHireAt': 0,
        'lastFiringReviewAt': 0,
        'lastStaleWorkSweep': 0,
        'lastSkillReviewAt': sim.CADENCE_NEVER,
        'lastDistillAt': sim.CADENCE_NEVER,
    }
    return state, now, now_ms


class ScheduledPreemption(unittest.TestCase):
    def _run_cycle(self, state, now, grid, doors, tid=[0]):
        sim._task_cycle(state, now=now, grid=grid, doors=doors,
                        task_id_holder=tid)
        return state

    def test_due_scheduled_item_fires_at_cap_via_preemption(self):
        state, now, now_ms = _busy_at_cap_state()
        sim.queue_once(state, 'Deploy the release', now_ms - 1000,
                       room='house', task_type='code')
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        # The scheduled item was assigned: someone holds a task with its title.
        holder = None
        scheduled_tasks = [t for t in (state.get('tasks') or {}).values()
                           if t.get('title') == 'Deploy the release']
        self.assertEqual(len(scheduled_tasks), 1, 'scheduled item became a task')
        sched = scheduled_tasks[0]
        self.assertEqual(sched['status'], 'walking')
        holder = sched.get('assignedTo')
        self.assertTrue(holder, 'scheduled item has an assignee')
        a = state['agents'][holder]
        self.assertTrue(a.get('_suspendedTask'), 'preempted agent parked her prior task')
        parked = state['tasks'][a['_suspendedTask']['task']]
        self.assertIsNotNone(parked.get('_suspendedForScheduled'),
                             'parked task flagged so sweeps skip it')
        self.assertEqual(a['task'], sched['id'], 'freed agent took the scheduled item')
        self.assertFalse(a.get('busy'), 'preempted agent detached from her old task')

    def test_parked_task_resumes_after_scheduled_item_completes(self):
        state, now, now_ms = _busy_at_cap_state()
        sim.queue_once(state, 'Deploy the release', now_ms - 1000,
                       room='house', task_type='code')
        grid, doors = sim._load_outdoor_geometry()
        tid = [0]
        self._run_cycle(state, now, grid, doors, tid)
        sched = next(t for t in (state.get('tasks') or {}).values()
                     if t.get('title') == 'Deploy the release')
        holder = sched['assignedTo']
        parked_id = state['agents'][holder]['_suspendedTask']['task']
        parked = state['tasks'][parked_id]
        self.assertEqual(parked['status'], 'working')
        # Simulate arrival + budget expiry for the scheduled item, then let the
        # completion loop finish it: the parked task must be reclaimed.
        sched['status'] = 'working'
        sched['workUntil'] = now - 1
        state['agents'][holder]['busy'] = True
        state['agents'][holder]['inRoom'] = sched['room']
        self._run_cycle(state, now + 2, grid, doors, tid)
        a = state['agents'][holder]
        self.assertEqual(a['task'], parked_id, 'parked task reclaimed after scheduled item')
        self.assertFalse(a.get('_suspendedTask'), 'suspension marker cleared')
        self.assertFalse(parked.get('_suspendedForScheduled'),
                         'parked flag cleared on resume')
        self.assertTrue(a.get('busy'), 'agent back to work on her own task')

    def test_parked_task_is_not_reclaimed_or_replanned_while_suspended(self):
        state, now, now_ms = _busy_at_cap_state()
        sim.queue_once(state, 'Deploy the release', now_ms - 1000,
                       room='house', task_type='code')
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        a0 = state['agents'][next(aid for aid, a in state['agents'].items()
                                  if a.get('_suspendedTask'))]
        parked_id = a0['_suspendedTask']['task']
        # Orphan reclaim must skip the parked task.
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)
        self.assertIn(parked_id, state['tasks'])
        # Stale-work sweep: advance well past the parked task's budget (it WOULD
        # be re-planned if not suspended), but keep the scheduled walking task
        # inside the walking timeout so it isn't the one re-planned. With the
        # parked flag set, the sweep must re-plan nothing.
        for t in (state.get('tasks') or {}).values():
            if t['id'] != parked_id:
                t['status'] = 'working'
                t['workUntil'] = now + 1_000_000.0  # not stale
        late = now + 200_000.0  # far past parked workUntil (110_000) + grace
        self.assertEqual(sim._stale_work_step(state, late, int(late * 1000)), 0)
        self.assertIn(parked_id, state['tasks'])


    def test_one_off_can_preempt_ordinary_or_sprint_work(self):
        # A ONE-OFF request may interrupt an agent on ordinary (non-scheduled)
        # work at the cap -- same preemption machinery, but the parked task must
        # be ordinary work, not a scheduled/standing card.
        state, now, now_ms = _busy_at_cap_state()
        # Queue a plain one-off (no notBefore).
        sim.queue_work(state, [{'title': 'One-off: fetch the ledger',
                           'room': 'house', 'instructions': 'ordinary request'}])
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        holder = next((aid for aid, a in state['agents'].items()
                       if a.get('task') and state['tasks'][a['task']]['title']
                       == 'One-off: fetch the ledger'), None)
        self.assertTrue(holder, 'one-off interrupted someone and got assigned')
        a = state['agents'][holder]
        self.assertTrue(a.get('_suspendedTask'), 'prior ordinary task parked')

    def test_one_off_does_not_preempt_scheduled_or_standing_work(self):
        # A one-off must NOT yank an agent off time-critical work. Give every
        # busy agent a scheduled (notBefore) task, then queue a one-off: it
        # cannot interrupt anyone, so it stays in the queue.
        state, now, now_ms = _busy_at_cap_state()
        for t in (state.get('tasks') or {}).values():
            t['notBefore'] = now_ms - 5000  # mark each in-flight card scheduled
        sim.queue_work(state, [{'title': 'One-off: fetch the ledger',
                           'room': 'house', 'instructions': 'ordinary request'}])
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        holders = [aid for aid, a in state['agents'].items()
                   if a.get('task') and state['tasks'][a['task']]['title']
                   == 'One-off: fetch the ledger']
        self.assertEqual(holders, [], 'one-off must not interrupt scheduled work')
        self.assertEqual([a.get('_suspendedTask') for a in state['agents'].values()
                          if isinstance(a, dict) and a.get('_suspendedTask')],
                         [], 'nobody was preempted')

    def test_scheduled_can_preempt_standing_work(self):
        # A due scheduled item outranks even standing cadence work: it preempts
        # a research (standing) card, because a scheduled item is the top
        # priority and must fire at its time.
        state, now, now_ms = _busy_at_cap_state()
        for t in (state.get('tasks') or {}).values():
            t['research'] = {'topicId': 't1', 'since': 0}
        sim.queue_once(state, 'Deploy the release', now_ms - 1000,
                       room='house', task_type='code')
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        holder = next((aid for aid, a in state['agents'].items()
                       if a.get('task') and state['tasks'][a['task']]['title']
                       == 'Deploy the release'), None)
        self.assertTrue(holder, 'scheduled item preempted a standing card')

    def test_one_off_does_not_preempt_standing_work(self):
        # Same as the scheduled case, but with a ONE-OFF: standing cadence work
        # is time-critical, so the one-off must wait.
        state, now, now_ms = _busy_at_cap_state()
        for t in (state.get('tasks') or {}).values():
            t['research'] = {'topicId': 't1', 'since': 0}
        sim.queue_work(state, [{'title': 'One-off: fetch the ledger',
                           'room': 'house', 'instructions': 'ordinary request'}])
        grid, doors = sim._load_outdoor_geometry()
        self._run_cycle(state, now, grid, doors)
        holders = [aid for aid, a in state['agents'].items()
                   if a.get('task') and state['tasks'][a['task']]['title']
                   == 'One-off: fetch the ledger']
        self.assertEqual(holders, [], 'one-off must not interrupt standing work')


class HireGate(unittest.TestCase):
    def decider(self, *a, **k):
        return None

    def test_hire_blocked_at_active_cap(self):
        state, now, now_ms = _busy_at_cap_state()
        before = len(state['agentRoster'])
        called = []
        def decider(*a, **k):
            called.append(1)
            return None
        self.assertFalse(sim._start_auto_hire(state, now_ms, {}, decider))
        self.assertEqual(state.get('_hireBlockedAtActiveCapNote'),
                         sim.MAX_ACTIVE_AGENTS + 1)
        self.assertEqual(called, [], 'decider never called at the active cap')
        self.assertEqual(len(state['agentRoster']), before, 'no roster growth at cap')

    def test_hire_blocked_when_dormant_worker_will_be_woken(self):
        # 4 on-duty agents, all busy (awake-idle == 0) + 1 dormant worker, and a
        # due scheduled item -- the dormant worker WILL be woken, so the slot is
        # not free and a new hire must be blocked.
        state, now, now_ms = _busy_at_cap_state()
        state['agentRoster'] = state['agentRoster'][:5]
        state['agents'] = {k: v for k, v in state['agents'].items()
                           if k in ('a00', 'a01', 'a02', 'a03', 'a04')}
        state['agents']['a04']['offDuty'] = True  # dormant worker
        state['agents']['a04']['visible'] = False
        # Rebuild workQueue with one due scheduled item (all busy -> awake-idle 0).
        state['workQueue'] = []
        sim.queue_once(state, 'Dormant wake probe', now_ms - 1000,
                       room='house', task_type='code')
        called = []
        def decider(*a, **k):
            called.append(1)
            return None
        self.assertTrue(sim._dormant_workers_will_be_woken(state, now_ms))
        self.assertFalse(sim._start_auto_hire(state, now_ms, {}, decider))
        self.assertTrue(state.get('_hireBlockedDormantWakeNote'))
        self.assertEqual(called, [], 'decider not called while a dormant slot is waking')

    def test_hire_proceeds_when_dormant_worker_has_no_wake_work(self):
        # A governance-shaped team (faye the admin-director, ada + ben her
        # reports, nora an idle director) PLUS one dormant worker who has NO
        # scheduled/pending work to be woken for -> the slot is genuinely free,
        # a hire may start (decider is consulted, no block note).
        state = {
            'sim': {'owner': 'server'},
            'agentRoster': [
                {'id': 'faye', 'name': 'Faye', 'role': 'Control Room', 'isAdmin': True},
                {'id': 'nora', 'name': 'Nora', 'role': 'Personnel', 'isDirector': True},
                {'id': 'ada', 'name': 'Ada', 'role': 'Research', 'model': 'small', 'director': 'faye'},
                {'id': 'ben', 'name': 'Ben', 'role': 'Banking', 'model': 'small', 'director': 'faye'},
                {'id': 'zoe', 'name': 'Zoe', 'role': 'Worker', 'director': 'faye'},
            ],
            'agents': {
                'faye': {'id': 'faye', 'x': 0, 'y': 0, 'busy': False, 'task': None,
                         'inRoom': None, 'offDuty': False, 'dir': 'south'},
                'nora': {'id': 'nora', 'x': 100, 'y': 100, 'busy': False, 'task': None,
                         'inRoom': None, 'offDuty': False, 'dir': 'south'},
                'ada': {'id': 'ada', 'x': 200, 'y': 200, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False, 'dir': 'south'},
                'ben': {'id': 'ben', 'x': 300, 'y': 300, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': False, 'dir': 'south'},
                'zoe': {'id': 'zoe', 'x': 400, 'y': 400, 'busy': False, 'task': None,
                        'inRoom': None, 'offDuty': True, 'visible': False, 'dir': 'south'},
            },
            'reports': [],
            'workQueue': [],
            'lastHireAt': 0,
            'researchTopics': [],
        }
        now_ms = int(time.time() * 1000)
        # A due unparked normal item -- an awake-idle agent (ada/ben/nora) can
        # take it, so the dormant worker (zoe) is NOT woken -> slot is free.
        state.setdefault('workQueue', []).append({
            'title': 'Scheduled research: weather data', 'room': 'observatory',
            'instructions': 'crawl', 'pair': False, 'notBefore': None,
            'priority': sim.WORK_PRIORITY['normal'], 'goal': 'weather-data',
            'research': {'topicId': 't1', 'since': 0}, 'taskType': 'research',
            'skillReview': False,
        })
        self.assertFalse(sim._dormant_workers_will_be_woken(state, now_ms))
        called = []
        def decider(*a, **k):
            called.append(1)
            return 'ben'
        sim._start_auto_hire(state, now_ms, {}, decider)
        self.assertFalse(state.get('_hireBlockedAtActiveCapNote'))
        self.assertFalse(state.get('_hireBlockedDormantWakeNote'))
        self.assertTrue(called, 'decider consulted for a genuinely free slot')


if __name__ == '__main__':
    unittest.main()
