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

import sim

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


if __name__ == '__main__':
    unittest.main()