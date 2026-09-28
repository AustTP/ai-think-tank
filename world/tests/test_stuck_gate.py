"""Phase E3.1: stuck-at-review escalation watchdog.

A peer-gated story locks its reviewer pair at entry; if that pair becomes
unreachable/inert (both reviewers gone or perpetually unavailable), nothing
today widens it, so the story can sit in needs_review with no path to a vote.
_sweep_stuck_gates fixes that: it re-picks a fresh REACHABLE pair and re-pins
the pending reviewOf subtasks to them, bounded by a grace window and a rescue
cooldown. It never auto-closes -- a stuck gate is made reachable, and done stays
governed by _peer_gate_should_close.

This suite validates the watchdog hermetically. Not actually DB-free:
_sweep_stuck_gates' rescue path has inline `from serve import log_action`
calls (task_peer_widened / task_review_requeued), a real side effect that
writes into whatever real village.db sits at serve.py's default path unless
DB_PATH is redirected below. Found 2026-09-25 via a live production
village.db that picked up these exact fixture rows after a routine test run.
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
    _TMP_DIR = tempfile.mkdtemp(prefix='village-stuck-gate-test-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
        VILLAGE_DIR=_TMP_DIR,
        AGENTS_DIR=os.path.join(_TMP_DIR, 'agents'),
        LIBRARY_DIR=os.path.join(_TMP_DIR, 'library'),
        PASSPORT_PATH=os.path.join(_TMP_DIR, 'library', '.passport.json'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _roster_agent(id, director='maya', is_admin=False, **over):
    r = {'id': id, 'name': id.capitalize(), 'role': 'director' if is_admin else 'engineer',
         'isAdmin': is_admin, 'director': director}
    r.update(over)
    return r


def _state(**over):
    roster = [
        # The admin/director has NO own director (director=None) -- mirrors the
        # production roster convention (serve.py:1416); otherwise she'd appear in
        # her own reports and leak into the reviewer pool when a director self-
        # references.
        _roster_agent('maya', director=None, is_admin=True),
        _roster_agent('ben', director='maya'),
        _roster_agent('cora', director='maya'),
        _roster_agent('dax', director='maya'),
        _roster_agent('zia', director='maya'),
        _roster_agent('ava', director='maya'),
    ]
    agents = {d['id']: {'id': d['id'], 'name': d['name'], 'x': 0, 'y': 0,
                        'busy': False, 'visible': True, 'offDuty': False,
                        'inRoom': None, 'completedRooms': [], 'mailbox': []}
              for d in roster}
    state = {
        'sim': {'owner': 'server'},
        'agentRoster': roster,
        'agents': agents,
        'workQueue': [],
        'tasks': {},
        'reports': [],
    }
    state.update(over)
    return state


def _gated_task(state, author='ava', reviewers=('cora', 'dax'), entered_ms=0,
                reviewed_task_ids=None, **over):
    task = {'id': 'task-1', 'title': 'Build the thing', 'room': 'pressoffice',
            'assignedTo': author, 'status': 'needs_review',
            '_peerGate': {'approvals': 0, 'approvers': [], 'reviewerIds': list(reviewers),
                          'enteredMs': entered_ms,
                          'reviewedTaskIds': list(reviewed_task_ids or [])},
            'createdAt': 0}
    task.update(over)
    state.setdefault('tasks', {})['task-1'] = task
    return task


def _review_subtask(state, parent_id='task-1', assigned='cora', title='Review: x'):
    item = {'title': title, 'room': 'pressoffice', 'taskType': 'review',
            'projectLabel': 'x', 'assignedTo': assigned, 'reviewOf': parent_id,
            'instructions': 'review'}
    state.setdefault('workQueue', []).append(item)
    return item


class StuckGateSweep(unittest.TestCase):
    def test_unreachable_pair_is_widened_to_reachable(self):
        # Both locked reviewers are GONE (fired -- no live agent). The watchdog
        # must re-pick fresh reachable reviewers and re-pin the subtasks.
        state = _state()
        del state['agents']['cora']
        del state['agents']['dax']
        _gated_task(state, entered_ms=0)
        s1 = _review_subtask(state, assigned='cora')
        s2 = _review_subtask(state, assigned='dax', title='Review: x2')
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)  # well past grace
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['ben', 'zia'])
        # Re-pinned: each subtask now routed to a fresh reviewer.
        self.assertEqual({s1['assignedTo'], s2['assignedTo']}, {'ben', 'zia'})
        self.assertNotIn('cora', gate['reviewerIds'])
        self.assertNotIn('dax', gate['reviewerIds'])

    def test_reachable_locked_reviewer_is_left_alone(self):
        # A locked reviewer who is on-duty-and-idle is reachable; the watchdog
        # must NOT churn (no re-pick, no re-pin) while she could still vote.
        state = _state()
        _gated_task(state, entered_ms=0)
        s1 = _review_subtask(state, assigned='cora')
        # cora + dax both idle and present -> reachable.
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['cora', 'dax'])  # untouched
        self.assertEqual(s1['assignedTo'], 'cora')

    def test_grace_period_respected(self):
        # A gate younger than STUCK_GATE_GRACE_MS is never widened, even with an
        # unreachable pair -- reviewer 1 may still be about to act.
        state = _state()
        del state['agents']['cora']
        del state['agents']['dax']
        _gated_task(state, entered_ms=0)
        sim._sweep_stuck_gates(state, now_ms=sim.STUCK_GATE_GRACE_MS - 1)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['cora', 'dax'])

    def test_bounded_rescue_cooldown(self):
        # After a rescue, the same gate isn't re-picked again within the cooldown
        # window -- no re-pick storm on a pathological story.
        state = _state()
        del state['agents']['cora']
        del state['agents']['dax']
        _gated_task(state, entered_ms=0)
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['reviewerIds'], ['ben', 'zia'])  # rescued once
        # Immediately re-sweep: fresh pair is reachable AND cooldown active.
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000 + 1)
        self.assertEqual(state['tasks']['task-1']['_peerGate']['reviewerIds'], ['ben', 'zia'])

    def test_healthy_gate_never_widened(self):
        # A healthy gate (reachable reviewer, <2 approvals) is left untouched;
        # the watchdog never overrides _peer_gate_should_close as the done rule.
        state = _state()
        _gated_task(state, entered_ms=0)
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['approvals'], 0)
        self.assertEqual(gate['reviewerIds'], ['cora', 'dax'])

    def test_no_review_subtask_matrix(self):
        # A gated story whose reviews were all consumed (done) with NO pending
        # subtask left is stuck even when its locked reviewers are reachable --
        # there is nothing for them to vote on, so the gate deadlocks at 0
        # approvals. The watchdog must RE-ENQUEUE a fresh review to the reachable
        # pair (not churn the pair, not no-op), so the gate regains a vote path.
        state = _state()
        _gated_task(state, entered_ms=0, reviewed_task_ids=['r1', 'r2'])
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)
        gate = state['tasks']['task-1']['_peerGate']
        # Pair untouched -- both reviewers are present and reachable.
        self.assertEqual(gate['reviewerIds'], ['cora', 'dax'])
        # Recovery: a fresh review is now queued for them.
        fresh = [q for q in state['workQueue']
                 if q.get('reviewOf') == 'task-1' and q.get('taskType') == 'review']
        self.assertEqual(len(fresh), 2, 'a consumed-no-vote gate must re-enqueue reviews')
        self.assertEqual({q['assignedTo'] for q in fresh}, {'cora', 'dax'})
        # Bounded: a follow-up sweep within the cooldown adds no further reviews.
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000 + 1)
        again = [q for q in state['workQueue']
                 if q.get('reviewOf') == 'task-1' and q.get('taskType') == 'review']
        self.assertEqual(len(again), 2, 'rescue cooldown must bound re-enqueues')

    def test_widened_restarts_timeout_window(self):
        # A rescue resets enteredMs so the 1-clean+timeout fair-window restarts
        # for the fresh reviewer (she shouldn't inherit an already-elapsed wait).
        state = _state()
        del state['agents']['cora']
        del state['agents']['dax']
        _gated_task(state, entered_ms=1000)
        sim._sweep_stuck_gates(state, now_ms=40 * 60 * 1000)
        gate = state['tasks']['task-1']['_peerGate']
        self.assertEqual(gate['enteredMs'], 40 * 60 * 1000)

    def test_repeated_no_verdict_rescues_freeze_after_the_bound(self):
        # Bounded review-cycle escalation (2026-09-26): real gap caught live --
        # this exact rescue path (a review consumed with no verdict, reachable
        # pair, re-enqueue a fresh one) used to be able to fire once per
        # cooldown window FOREVER, with no overall cap. Sweep past the 30-min
        # cooldown MAX_REVIEW_CYCLES+1 times; it must stop re-enqueuing and
        # escalate once, not keep going indefinitely.
        state = _state()
        _gated_task(state, entered_ms=0, reviewed_task_ids=['r1', 'r2'])
        total_enqueued = 0
        with unittest.mock.patch.object(serve, 'create_escalation') as esc:
            for i in range(sim.MAX_REVIEW_CYCLES + 2):
                now_ms = (i + 1) * (sim.STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS + 1000)
                sim._sweep_stuck_gates(state, now_ms=now_ms)
                pending = [q for q in state['workQueue'] if q.get('reviewOf') == 'task-1']
                total_enqueued += len(pending)
                # Simulate each rescued review being picked up and consumed
                # with no verdict again (the real failure this bug protects
                # against) -- once assigned, a real work item leaves
                # workQueue, so the next sweep sees "no pending review" again.
                state['workQueue'] = [q for q in state['workQueue'] if q.get('reviewOf') != 'task-1']
        gate = state['tasks']['task-1']['_peerGate']
        self.assertTrue(gate['escalated'])
        esc.assert_called_once()
        # MAX_REVIEW_CYCLES-1 real re-enqueues (2 reviewers each) happened
        # before the Nth check itself froze the gate instead of rescuing --
        # not one per sweep call (there were MAX_REVIEW_CYCLES+2 sweeps total).
        self.assertEqual(total_enqueued, (sim.MAX_REVIEW_CYCLES - 1) * 2)


if __name__ == '__main__':
    unittest.main()