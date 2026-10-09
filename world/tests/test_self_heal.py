"""Tests for the self-healing additions to world/sim.py.

1. _reclaim_orphaned_walking_tasks: a task left in 'walking'/'working' status
   whose assignee no longer holds it (agent.task != task_id) will NEVER resolve
   -- neither the completion loop (iterates agents) nor _repair_stalled_walkers
   (guards on the agent holding the task) can see it. Re-queues the orphan so
   assignment re-issues it. Seen live: a review-spawned fix task (task-4) stuck
   'walking' forever while its assignee was parked idle.

2. Skill-review sentinel guard: an absurdly far-future lastSkillReviewAt (a
   leaked TEST sentinel like 1e18, the SQLite-embedded "year 33658" value) must
   not permanently disable the standing skill-review sweep.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000  # 2026-09, matches _task_cycle(now=1_725_000_000.0)


def _agent(aid, **kw):
    base = {'id': aid, 'x': 0, 'y': 0, 'dir': 'down', 'offDuty': True,
            'visible': False, 'busy': False, 'task': None, 'path': None,
            'pathIndex': 0, 'pathTarget': None, 'pathActive': False,
            'pairWith': None, 'handoff': None, 'inRoom': None}
    base.update(kw)
    return base


def _state():
    return {
        'sim': {'owner': 'server'},
        'agentRoster': [{'id': 'ada'}, {'id': 'ben'}],
        'agents': {'ada': _agent('ada'), 'ben': _agent('ben')},
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
    }


def _orphan_task(task_id='task-1', assigned_to='ada', status='walking', **over):
    base = {
        'id': task_id, 'title': 'Fix the weather report', 'room': 'pressoffice',
        'instructions': 'Fix it.', 'assignedTo': assigned_to, 'status': status,
        'createdAt': _NOW_MS, 'taskType': 'code', 'reviewOf': None,
        'incident': False, 'research': None, 'checklist': None,
    }
    base.update(over)
    return base


class ReclaimOrphaned(unittest.TestCase):
    def test_requeues_a_walking_task_whose_holder_was_parked(self):
        # ada was assigned the fix, then her task field got cleared (e.g. by a
        # park sweep / save race) leaving a walking task with no holder.
        state = _state()
        state['agents']['ada']['task'] = None
        state['tasks']['task-1'] = _orphan_task()
        n = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(n, 1)
        self.assertNotIn('task-1', state['tasks'],
                         'orphan dict must be dropped (it was never worked)')
        self.assertEqual(len(state['workQueue']), 1,
                         're-queued so a fresh assignment picks it up')
        re = state['workQueue'][0]
        self.assertEqual(re['title'], 'Fix the weather report')
        self.assertEqual(re['assignedTo'], 'ada', 'keeps the review-fix pin')
        self.assertEqual(re['room'], 'pressoffice')

    def test_leaves_a_held_walking_task_alone(self):
        # ben genuinely walking a task he holds must NOT be reclaimed.
        state = _state()
        state['agents']['ben']['task'] = 'task-9'
        state['tasks']['task-9'] = _orphan_task('task-9', 'ben')
        n = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(n, 0)
        self.assertIn('task-9', state['tasks'])
        self.assertEqual(state['workQueue'], [])

    def test_reclaims_working_status_too(self):
        state = _state()
        state['tasks']['task-3'] = _orphan_task('task-3', 'ada', status='working')
        n = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(n, 1)
        self.assertEqual(state['workQueue'][0]['title'], 'Fix the weather report')

    def test_missing_assignee_is_reclaimed(self):
        # Assignee vanished entirely (fired) but task lingers.
        state = _state()
        state['tasks']['task-2'] = _orphan_task('task-2', assigned_to='ghost')
        n = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(n, 1)
        self.assertNotIn('task-2', state['tasks'])
        self.assertEqual(state['workQueue'][0]['title'], 'Fix the weather report',
                         're-queued so assignment re-picks a live agent')

    def test_preserves_review_fix_semantics(self):
        # A fix subtask (reviewOf == parent id) must carry that pin through.
        state = _state()
        state['tasks']['task-4'] = _orphan_task('task-4', 'ada', reviewOf='story-1')
        sim._reclaim_orphaned_walking_tasks(state)
        re = state['workQueue'][0]
        self.assertEqual(re['reviewOf'], 'story-1')

    def test_noop_when_no_tasks_or_no_orphans(self):
        self.assertEqual(sim._reclaim_orphaned_walking_tasks({'tasks': {}}), 0)
        self.assertEqual(sim._reclaim_orphaned_walking_tasks({}), 0)
        state = _state()  # no tasks at all
        self.assertEqual(sim._reclaim_orphaned_walking_tasks(state), 0)

    def test_task_held_via_agent_task_pointer_is_not_orphaned(self):
        # A working task may carry no assignedTo yet still be genuinely held by
        # an agent's .task pointer (legacy fixture / field not recorded). The
        # held-check must scan agents, not trust assignedTo alone.
        state = _state()
        state['agents']['cora'] = _agent('cora', task='task-5', busy=True)
        state['tasks']['task-5'] = {
            'id': 'task-5', 'status': 'working', 'room': 'pressoffice',
            'title': 'in flight', 'taskType': 'code',
            # NO assignedTo on purpose.
        }
        n = sim._reclaim_orphaned_walking_tasks(state)
        self.assertEqual(n, 0)
        self.assertIn('task-5', state['tasks'])
        self.assertEqual(state['workQueue'], [])


def _seed_pending_skill_file(library_dir, name='candidate.md', body='candidate reference material'):
    pending = os.path.join(library_dir, 'pending_review', 'skills')
    os.makedirs(pending, exist_ok=True)
    with open(os.path.join(pending, name), 'w') as f:
        f.write(body)
    return pending


class SkillReviewSentinel(unittest.TestCase):
    # The sweep fires only when content is actually waiting, so these cadence-
    # behavior tests patch LIBRARY_DIR to a temp tree with one pending file
    # (hermetic -- no dependence on the live library on disk).
    def setUp(self):
        import serve as serve_mod
        self._tmp = tempfile.mkdtemp()
        _seed_pending_skill_file(self._tmp)
        self._patcher = mock.patch.object(serve_mod, 'LIBRARY_DIR', self._tmp)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_leaked_test_sentinel_does_not_disable_standing_work(self):
        # A 1e18 (year ~33658) lastSkillReviewAt must be treated as unset so the
        # first skill review fires instead of never.
        state = _state()
        state['lastSkillReviewAt'] = 1_000_000_000_000_000_000
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        queued = [q for q in state['workQueue'] if q.get('skillReview')]
        self.assertEqual(len(queued), 1,
                         'leaked sentinel must not permanently mute skill review')

    def test_recent_skill_review_is_not_re_run(self):
        # A genuinely recent review stays quiet until the cadence elapses.
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [])

    def test_explicit_never_marker_silences_and_is_not_normalized(self):
        # The named CADENCE_NEVER marker intentionally mutes the ceremony AND is
        # NOT normalized by the far-future fallback (a real date can't be
        # confused with 'never' -- only the exact sentinel means it).
        state = _state()
        state['lastSkillReviewAt'] = sim.CADENCE_NEVER
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [],
                         'explicit never-marker stays silent')
        self.assertEqual(state['lastSkillReviewAt'], sim.CADENCE_NEVER,
                         'never-marker is preserved, not rewritten')

    def test_real_far_future_date_still_normalizes_like_legacy_sentinel(self):
        # A date far beyond 10 years out is still treated as unset (a leaked
        # 1e18-style macro), so a genuinely remote stamp can't permanently mute
        # the sweep -- independent of the explicit never-marker.
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS + 20 * 365 * 24 * 3600 * 1000
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual(len([q for q in state['workQueue'] if q.get('skillReview')]), 1,
                         'a far-future (non-never) stamp fires the first skill review')


class SkillReviewContentGate(unittest.TestCase):
    """The standing skill-review sweep is content-gated: when the
    cadence is due but nothing is waiting in pending_review/skills/, it must NOT
    queue a task (no agent pick, no Jev grade call) and must NOT advance the
    marker, so the sweep fires on the first later pass where content appears."""

    def setUp(self):
        import serve as serve_mod
        self._tmp = tempfile.mkdtemp()
        self._patcher = mock.patch.object(serve_mod, 'LIBRARY_DIR', self._tmp)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_due_with_no_pending_content_stays_quiet_and_unstamped(self):
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS - sim.SKILL_REVIEW_CADENCE_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [],
                         'an empty pending queue must not spawn a review task')
        self.assertEqual(state['lastSkillReviewAt'],
                         _NOW_MS - sim.SKILL_REVIEW_CADENCE_MS,
                         'marker must NOT advance when there is nothing to review')

    def test_due_with_pending_content_queues_and_stamps(self):
        _seed_pending_skill_file(self._tmp)
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS - sim.SKILL_REVIEW_CADENCE_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        queued = [q for q in state['workQueue'] if q.get('skillReview')]
        self.assertEqual(len(queued), 1)
        self.assertEqual(state['lastSkillReviewAt'], _NOW_MS,
                         'marker advances once real content is waiting')

    def test_pending_file_appearing_later_fires_the_very_next_pass(self):
        # No content on the first due pass -> quiet, unstamped. Content appears;
        # the next _check_schedules pass fires without waiting a full cadence.
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS - sim.SKILL_REVIEW_CADENCE_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [])
        _seed_pending_skill_file(self._tmp)
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual(len([q for q in state['workQueue'] if q.get('skillReview')]), 1,
                         'content arriving later must trigger the sweep promptly')

    def test_hidden_files_do_not_count_as_pending(self):
        # Dotfiles are excluded by the /api/library listing the executor reads,
        # so they must not trip the gate either.
        _seed_pending_skill_file(self._tmp, name='.hidden.md')
        state = _state()
        state['lastSkillReviewAt'] = _NOW_MS - sim.SKILL_REVIEW_CADENCE_MS
        sim._check_schedules(state, now=_NOW_MS / 1000, now_ms=_NOW_MS)
        self.assertEqual([q for q in state['workQueue'] if q.get('skillReview')], [])


class StaleWorkSweep(unittest.TestCase):
    """SM gap 1: a non-bug card wedged in 'walking'/'working' has no age alarm
    (bugs have RESTORE_TIMEOUT_MS). _stale_work_step re-queues a first-time
    offender fresh (holder released), then routes a repeat offender to the owning
    team's scrum master as a work-request -- bounded, never a spin loop."""

    def _run(self, state, now_ms, task_id, status, work_until_s=None, opened_at=None,
             **task_over):
        state['agents']['ada']['task'] = task_id
        state['agents']['ada']['busy'] = status == 'working'
        state['agents']['ada']['inRoom'] = 'pressoffice'
        opened_at = opened_at or (now_ms - sim.STALE_WORK_TIMEOUT_MS - 1)
        task = _orphan_task(task_id=task_id, assigned_to='ada', status=status,
                            createdAt=opened_at, openedAt=opened_at, **task_over)
        if status == 'working':
            task['workUntil'] = work_until_s
        state['tasks'][task_id] = task
        return sim._stale_work_step(state, now_ms / 1000, now_ms)

    def test_walking_task_older_than_ceiling_is_requeued_and_holder_released(self):
        state = _state()
        n = self._run(state, _NOW_MS, 'task-1', 'walking')
        self.assertEqual(n, 1)
        self.assertNotIn('task-1', state['tasks'])
        self.assertEqual(len(state['workQueue']), 1)
        item = state['workQueue'][0]
        self.assertEqual(item['title'], 'Fix the weather report')
        self.assertEqual(item['priority'], sim.WORK_PRIORITY['high'],
                         're-issued work is bumped to high so it is picked next')
        self.assertFalse(state['agents']['ada']['busy'])
        self.assertIsNone(state['agents']['ada']['task'],
                          'the stuck worker is released, never pinned by the wedge')

    def test_working_task_still_working_past_budget_grace_is_requeued(self):
        # workUntil elapsed long ago yet the task is STILL 'working' -- the
        # completion loop resolves every held working task within a pass, so this
        # is a genuine wedge, not a busy agent mid-budget.
        state = _state()
        n = self._run(state, _NOW_MS, 'task-1', 'working',
                      work_until_s=(_NOW_MS / 1000) - sim.STALE_WORK_BUDGET_GRACE_S - 60)
        self.assertEqual(n, 1)
        self.assertNotIn('task-1', state['tasks'])
        self.assertEqual(len(state['workQueue']), 1)

    def test_working_task_within_budget_is_left_alone(self):
        # Still inside workUntil (plus grace) -> legitimately in flight.
        state = _state()
        n = self._run(state, _NOW_MS, 'task-1', 'working',
                      work_until_s=(_NOW_MS / 1000) + 5)
        self.assertEqual(n, 0)
        self.assertIn('task-1', state['tasks'])
        self.assertEqual(state['workQueue'], [])

    def test_bugs_shadows_and_reviews_are_never_replanned(self):
        now_ms = _NOW_MS
        state = _state()
        self._run(state, now_ms, 'task-1', 'walking')  # sets ada holding task-1
        # A bug task older than the ceiling is the escalation step's domain.
        bug = _orphan_task(task_id='task-2', status='walking', taskType='bug',
                           createdAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1,
                           openedAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1)
        state['tasks']['task-2'] = bug
        # A shadow dry-run must never ship.
        shadow = _orphan_task(task_id='task-3', status='working', shadow=True,
                              workUntil=(now_ms / 1000) - sim.STALE_WORK_BUDGET_GRACE_S - 60,
                              createdAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1,
                              openedAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1)
        state['tasks']['task-3'] = shadow
        # A review/fix subtask is the stuck-gate watchdog's domain.
        review = _orphan_task(task_id='task-4', status='working', reviewOf='task-0',
                              workUntil=(now_ms / 1000) - sim.STALE_WORK_BUDGET_GRACE_S - 60,
                              createdAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1,
                              openedAt=now_ms - sim.STALE_WORK_TIMEOUT_MS - 1)
        state['tasks']['task-4'] = review
        state['agents']['ben']['task'] = None
        sim._stale_work_step(state, now_ms / 1000, now_ms)
        self.assertIn('task-2', state['tasks'], 'bugs are the escalation step\'s job')
        self.assertIn('task-3', state['tasks'], 'shadows never re-plan')
        self.assertIn('task-4', state['tasks'], 'review subtasks are the stuck-gate\'s job')

    def test_repeat_offender_routes_to_the_owning_scrum_master(self):
        state = _state()
        state['teams'] = [
            {'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'ada'},
        ]
        state['agentRoster'] = [{'id': 'ada'}, {'id': 'ben'}, {'id': 'dev'}]
        state['backlogRequests'] = []
        state['teamRefinementAt'] = {'dev': _NOW_MS}  # just refined -> kick should arm
        state['_staleWorkReplans'] = {repr(('Fix the weather report', 'pressoffice')): 2}
        n = self._run(state, _NOW_MS, 'task-1', 'walking', teamId='dev')
        self.assertEqual(n, 1)
        self.assertNotIn('task-1', state['tasks'])
        self.assertEqual(state['workQueue'], [], 'terminal re-plan does NOT re-queue')
        self.assertEqual(len(state['backlogRequests']), 1,
                          'the SM gets a work-request to re-plan the card')
        req = state['backlogRequests'][0]
        self.assertEqual(req['filedBy'], 'ada', 'attributed to the owning SM')
        self.assertEqual(req['room'], 'pressoffice')
        self.assertIn('stale in-flight work', req['reason'])
        # Refinement is kicked so the SM re-plans on the next pass, not next week.
        self.assertEqual(state['teamRefinementAt'].get('dev'), 0)

    def test_repeat_offender_with_no_team_is_dropped_not_looped(self):
        state = _state()
        state['backlogRequests'] = []
        state['_staleWorkReplans'] = {repr(('Fix the weather report', 'pressoffice')): 2}
        n = self._run(state, _NOW_MS, 'task-1', 'walking')
        self.assertEqual(n, 1)
        self.assertNotIn('task-1', state['tasks'], 'stale card is removed either way')
        self.assertEqual(state['workQueue'], [])
        self.assertEqual(state['backlogRequests'], [],
                          'no team to route to -> dropped, never a spin loop')


class WorkerStuckHelp(unittest.TestCase):
    """W4: worker stuck/help signal. A worker whose content execution keeps
    FAILING (a content-executor crash now stored ok=False, or a red pipeline) has
    no server-side signal today. _worker_stuck_help_signal tracks CONSECUTIVE
    content failures per worker and, once the streak crosses WORKER_HELP_AFTER_FAILS,
    routes a help work-request to the owning team's scrum master (file_work_request
    + kick_refinement_now, the same SM-routing as stale work) so the SM re-plans
    or helps instead of the worker silently churning. Dovetails with SM gap 1:
    stale work is a wedged CARD; W4 is a wedged WORKER."""

    def _signal(self, state, agent_id, task, result_ok, now_ms):
        return sim._worker_stuck_help_signal(state, agent_id, task, result_ok, now_ms)

    def test_clean_result_resets_the_streak(self):
        # A landed (ok) content result must reset any prior failure streak --
        # a worker who recovers is not stuck.
        state = _state()
        task = _orphan_task(task_id='task-1', status='working')
        self._signal(state, 'ada', task, False, _NOW_MS)
        self.assertEqual(state['agents']['ada']['_contentFailStreak'], 1)
        self._signal(state, 'ada', task, True, _NOW_MS)
        self.assertEqual(state['agents']['ada']['_contentFailStreak'], 0,
                         'a clean result resets the streak')

    def test_repeated_crashes_route_help_to_the_owning_scrum_master(self):
        # WORKER_HELP_AFTER_FAILS consecutive ok=False results -> the owning
        # team's scrum master gets a help work-request + refinement is kicked.
        state = _state()
        state['teams'] = [
            {'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'ada'},
        ]
        state['agentRoster'] = [{'id': 'ada'}, {'id': 'ben'}, {'id': 'dev'}]
        state['backlogRequests'] = []
        state['teamRefinementAt'] = {'dev': _NOW_MS}  # just refined -> kick should arm
        task = _orphan_task(task_id='task-1', status='working', teamId='dev')
        for _ in range(sim.WORKER_HELP_AFTER_FAILS):
            self._signal(state, 'ada', task, False, _NOW_MS)
        self.assertEqual(len(state['backlogRequests']), 1,
                          'the SM gets a help work-request after repeated failures')
        req = state['backlogRequests'][0]
        self.assertEqual(req['filedBy'], 'ada', 'attributed to the owning SM')
        self.assertEqual(req['room'], 'pressoffice')
        self.assertIn('stuck', req['reason'])
        self.assertEqual(state['teamRefinementAt'].get('dev'), 0,
                          'refinement is kicked so the SM handles it on the next pass')
        self.assertEqual(state['agents']['ada']['_contentFailStreak'], 0,
                          'the streak re-arms only after a fresh clean result')

    def test_fewer_than_threshold_failures_do_not_signal_yet(self):
        # Below the threshold the worker's streak just accumulates.
        state = _state()
        state['backlogRequests'] = []
        task = _orphan_task(task_id='task-1', status='working')
        self._signal(state, 'ada', task, False, _NOW_MS)
        self.assertEqual(state['backlogRequests'], [],
                          'no help signal below the threshold')
        self.assertEqual(state['agents']['ada']['_contentFailStreak'], 1)

    def test_failure_with_no_team_is_dropped_not_looped(self):
        # No owning team to route to -> no request filed, streak re-arms (the
        # worker is not left in a perpetual help-signal state).
        state = _state()
        state['backlogRequests'] = []
        task = _orphan_task(task_id='task-1', status='working')
        for _ in range(sim.WORKER_HELP_AFTER_FAILS + 1):
            self._signal(state, 'ada', task, False, _NOW_MS)
        self.assertEqual(state['backlogRequests'], [],
                          'no team to route to -> dropped, never a spin loop')
        self.assertEqual(state['agents']['ada']['_contentFailStreak'], 1,
                          'streak re-arms: the signal fired once, and the next '
                          'call starts a fresh run')

    def test_content_crash_is_stored_as_a_failure_not_silent_success(self):
        # A content-executor CRASH must be stored ok=False so the fail-closed
        # quality gate treats it like a red pipeline -- never a silent 'done'.
        # (Before W4 the crash handler stored only a note, which read as a
        # successful completion in _task_cycle.)
        seen = {}
        def boom(snapshot, agent_id, task, base_ctx):  # noqa: ARG001
            raise RuntimeError('executor blew up')

        real_thread = sim.threading.Thread

        class _InlineThread(real_thread):
            def start(self):
                self._target(*self._args, **self._kwargs)

        with mock.patch.object(sim, '_store_content_result',
                               lambda tid, r: seen.__setitem__(tid, r)), \
             mock.patch('sim.threading.Thread', _InlineThread):
            sim._dispatch_content_work(boom, _state(), 'ada',
                                       {'id': 'task-1', 'room': 'observatory',
                                        'research': {'topicId': 't1'}}, 1000.0)
        self.assertIn('task-1', seen, 'a crashed run still lands a result')
        self.assertIs(seen['task-1'].get('ok'), False,
                      'a crash must be marked ok=False, not a silent success')
        self.assertIn('Content execution failed', seen['task-1'].get('note', ''))


class CoachingLoopEscalation(unittest.TestCase):
    """W5: the growth-plan coaching loop. A single low grade coaches once; a
    worker who keeps closing below the floor must be RE-coached (each low close
    re-lands an escalating note, not a deduped silent no-op) up to
    COACHING_MAX_ROUNDS, then the problem escalates to the owning team's scrum
    master as a work-request -- bounded, never an infinite re-coach.
    _coaching_loop_step is the daily catch-up sweep: an agent who already has a
    coaching plan and a real trailing grade STILL below the floor gets
    re-coached (or escalated at the cap) even if no new deliverable landed to
    trigger the event path."""

    def _low_grade(self, state, agent_id, grade, title='Buggy headline', team_id=None):
        with mock.patch.object(sim, '_grading_decider', lambda *a, **k: float(grade)):
            sim._grade_completed_task(
                state, agent_id,
                {'id': f'd-{len(state.get("completedDeliverables") or [])}',
                 'room': 'pressoffice', 'title': title, 'taskType': 'code',
                 'reviewOf': None, 'teamId': team_id},
                _NOW_MS)

    def test_a_low_grade_coaches_once_and_lands_on_the_next_task(self):
        state = _state()
        self._low_grade(state, 'ada', 4.0)
        plans = state['growthPlans']['ada']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['kind'], 'low_grade')
        self.assertIn('round 1', plans[0]['note'])
        note = sim._coaching_note_for(state, 'ada')
        self.assertIn('Buggy headline', note)
        self.assertIsNone(sim._coaching_note_for(state, 'ada'),
                          'the note lands exactly once')

    def test_repeated_low_closes_re_coach_up_to_the_cap(self):
        # Each fresh low close re-lands an escalating note (repeat=True bypasses
        # the kind dedup) -- the worker is re-coached, not absorbed into one.
        state = _state()
        for i in range(sim.COACHING_MAX_ROUNDS):
            self._low_grade(state, 'ada', 4.0, title=f'Buggy headline {i}')
        plans = state['growthPlans']['ada']
        self.assertEqual(len(plans), sim.COACHING_MAX_ROUNDS)
        rounds = [p['note'] for p in plans]
        for r in range(1, sim.COACHING_MAX_ROUNDS + 1):
            self.assertTrue(any(f'round {r} ' in n for n in rounds),
                            f'coaching round {r} landed')

    def test_at_the_cap_a_low_close_escalates_not_re_coaches(self):
        # Past COACHING_MAX_ROUNDS, the loop is bounded: the owning SM gets a
        # work-request + governance entry, and NO further coaching note is
        # written (the worker is surfaced, not quietly re-coached forever).
        state = _state()
        state['teams'] = [
            {'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'ada', 'room': 'pressoffice'},
        ]
        state['agentRoster'] = [
            {'id': 'ada', 'director': 'dev'}, {'id': 'ben'}, {'id': 'dev'},
        ]
        state['backlogRequests'] = []
        for i in range(sim.COACHING_MAX_ROUNDS):
            self._low_grade(state, 'ada', 4.0, title=f'Buggy headline {i}', team_id='dev')
        with mock.patch('serve.log_action') as log_action:
            self._low_grade(state, 'ada', 4.0, title='Buggy headline cap', team_id='dev')
        plans = state['growthPlans']['ada']
        self.assertEqual(len(plans), sim.COACHING_MAX_ROUNDS,
                          'no coaching note beyond the cap')
        self.assertEqual(len(state['backlogRequests']), 1,
                          'the owning SM gets a work-request at the cap')
        req = state['backlogRequests'][0]
        self.assertEqual(req['filedBy'], 'ada', 'attributed to the owning SM')
        self.assertIn('below the', req['reason'])
        esc = [c for c in log_action.call_args_list if c.args[1] == 'coaching_loop_escalated']
        self.assertEqual(len(esc), 1, 'governance logs the escalation')
        self.assertEqual(esc[0].args[2]['agent'], 'ada')

    def test_a_grade_at_or_above_the_floor_never_coaches(self):
        state = _state()
        self._low_grade(state, 'ada', 6.0)
        self._low_grade(state, 'ada', sim.DELIVERABLE_GRADE_FLOOR)
        self.assertNotIn('ada', state.get('growthPlans', {}),
                          'only sub-floor grades coach')

    def test_daily_sweep_re_coaches_an_agent_still_below_the_floor(self):
        # The catch-up sweep: an agent with an applied plan and a trailing grade
        # STILL below the floor is re-coached even though no NEW deliverable
        # landed (the event path can't fire if the agent went quiet).
        state = _state()
        state['growthPlans'] = {'ada': [{'kind': 'low_grade', 'applied': True}]}
        state['completedDeliverables'] = [
            {'room': 'pressoffice', 'agentId': 'ada', 'grade': 4.0, 'gradeIsReal': True},
        ]
        n = sim._coaching_loop_step(state, _NOW_MS)
        self.assertEqual(n, 1)
        self.assertEqual(len(state['growthPlans']['ada']), 2,
                          'the sweep re-coached (round 2 note)')
        self.assertIn('round 2', state['growthPlans']['ada'][-1]['note'])

    def test_daily_sweep_escalates_once_at_the_cap(self):
        state = _state()
        state['teams'] = [
            {'id': 'dev', 'directorId': 'dev', 'scrumMasterId': 'ada', 'room': 'pressoffice'},
        ]
        state['agentRoster'] = [
            {'id': 'ada', 'director': 'dev'}, {'id': 'ben'}, {'id': 'dev'},
        ]
        state['backlogRequests'] = []
        state['governance'] = []
        plans = [{'kind': 'low_grade', 'applied': True}
                 for _ in range(sim.COACHING_MAX_ROUNDS)]
        state['growthPlans'] = {'ada': plans}
        state['completedDeliverables'] = [
            {'room': 'pressoffice', 'agentId': 'ada', 'grade': 3.0, 'gradeIsReal': True},
        ]
        n = sim._coaching_loop_step(state, _NOW_MS)
        self.assertEqual(n, 1)
        self.assertEqual(len(state['growthPlans']['ada']), sim.COACHING_MAX_ROUNDS,
                          'no note beyond the cap -- escalated instead')
        self.assertEqual(len(state['backlogRequests']), 1)

    def test_daily_sweep_skips_agents_who_recovered_or_were_never_coached(self):
        state = _state()
        state['growthPlans'] = {'ada': [{'kind': 'low_grade', 'applied': True}]}
        state['completedDeliverables'] = [
            {'room': 'pressoffice', 'agentId': 'ada', 'grade': 8.0, 'gradeIsReal': True},
        ]
        self.assertEqual(sim._coaching_loop_step(state, _NOW_MS), 0,
                          'recovered agent (grade above floor) is skipped')
        state2 = _state()
        state2['growthPlans'] = {}
        self.assertEqual(sim._coaching_loop_step(state2, _NOW_MS), 0,
                          'no plans -> nothing to catch up')


if __name__ == '__main__':
    unittest.main()