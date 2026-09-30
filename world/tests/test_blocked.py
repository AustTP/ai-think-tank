"""Tests for the SM-committed `blocked` bool field on issues.

The think tank gained a single player-facing `blocked` field (NOT a status), flipped
only by the owning team's scrum master via `_file_block_change` + `_block_step`.
It is set three ways -- requirements-met (supervisor Jev approves), stuck-on-the-
player (director approves an ask_player verdict), or a dependency (agent blocked
on another agent's task, no gate) -- and cleared three ways -- the player answered,
the agent self-resolved, or a dependency task landed (auto-clear).

This covers the hermetic sim.py helpers. The supervisor/director Jev deciders are
injected as plain callables so no network is needed.

1. `blocked` is a bool field, not in ISSUE_STATUSES; file_issue defaults it False.
2. set_issue_status rejects `blocked` (no longer a status); status transitions
   still release the worker and clear needsInput on done/closed.
3. Requirements-met: claim -> supervisor approve -> SM commits True; reject leaves
   it False; a duplicate claim is refused.
4. Stuck-on-player: request_player_input -> ask_player verdict -> inbox delivers
   AND a stuck_player block-change is filed -> SM commits True.
5. resolve_internally -> no inbox msg, SM commits False.
6. Player responds -> resolve_player_ask -> SM commits False.
7. Dependency: request_block_dependency (no gate) -> SM commits True, kind
   stuck_on_agent + dependsOnTask; auto-clear when the task lands -> SM commits
   False. A request with no designated SM is deferred, never dropped.
8. _block_step commits one per pass, only when the owning SM is free + on-duty,
   and respects the per-team cadence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402

_NOW_MS = 1_725_000_000_000


def _state(**over):
    base = {
        'sim': {'owner': 'server'},
        'teamBlockAt': {},
        'agents': {
            'sm': {'id': 'sm', 'name': 'SM', 'busy': False, 'offDuty': False},
            'dev': {'id': 'dev', 'name': 'Dev', 'busy': False, 'offDuty': False},
            'nadia': {'id': 'nadia', 'name': 'Nadia', 'busy': False, 'offDuty': False},
            'priya': {'id': 'priya', 'name': 'Priya', 'busy': False, 'offDuty': False},
        },
        'teams': [{'id': 'dev', 'name': "Dev's Crew", 'directorId': 'dev',
                   'scrumMasterId': 'sm', 'members': ['nadia', 'priya'],
                   'createdAt': 1}],
        'researchTopics': [],
        'tasks': {},
        'workQueue': [],
    }
    base.update(over)
    return base


def _file(state, summary='Ship the reports API', t='story'):
    return sim.file_issue(state, 'dev', t, summary, 'checkout', 'priya',
                          now_ms=_NOW_MS)


class BlockedIsAField(unittest.TestCase):
    def test_blocked_not_in_statuses(self):
        self.assertNotIn('blocked', sim.ISSUE_STATUSES)

    def test_file_issue_defaults_blocked_false(self):
        st = _state()
        issue = _file(st)
        self.assertIs(issue['blocked'], False)
        self.assertIsNone(issue.get('blockedAt'))

    def test_set_issue_status_rejects_blocked(self):
        st = _state()
        issue = _file(st)
        self.assertIsNone(sim.set_issue_status(st, issue['key'], 'blocked'))
        self.assertEqual(st['issues'][issue['key']]['status'], 'open')

    def test_done_status_releases_linked_request(self):
        st = _state()
        issue = _file(st)
        st.setdefault('backlogRequests', []).append(
            {'id': f"iss-{issue['key']}", 'issueKey': issue['key'],
             'status': 'pending'})
        sim.set_issue_status(st, issue['key'], 'done')
        req = st['backlogRequests'][0]
        self.assertEqual(req['status'], 'resolved')


class RequirementsMetPath(unittest.TestCase):
    def test_supervisor_approval_then_sm_commits(self):
        st = _state()
        issue = _file(st)
        self.assertTrue(sim.request_block_claim_met(st, issue['key'], 'nadia',
                                                    context='criteria pass',
                                                    now_ms=_NOW_MS))
        # Not yet due: before the gate window.
        self.assertIsNone(sim._supervisor_block_vote_sweep(st, _NOW_MS + 100))
        # Director approves -> filed -> SM commits True.
        self.assertTrue(sim._supervisor_block_vote_sweep(
            st, _NOW_MS + sim.BLOCK_CLAIM_GATE_MS + 10, decider=lambda *a: True))
        self.assertEqual(_pending_kinds(st, issue['key']), ['requirements_met'])
        sim._block_step(st, 0, _NOW_MS + 20_000)
        self.assertIs(st['issues'][issue['key']]['blocked'], True)
        self.assertEqual(st['issues'][issue['key']]['blockedKind'],
                         'requirements_met')

    def test_supervisor_rejection_leaves_unblocked(self):
        st = _state()
        issue = _file(st)
        sim.request_block_claim_met(st, issue['key'], 'nadia', now_ms=_NOW_MS)
        self.assertFalse(sim._supervisor_block_vote_sweep(
            st, _NOW_MS + sim.BLOCK_CLAIM_GATE_MS + 10, decider=lambda *a: False))
        self.assertEqual(_pending_kinds(st, issue['key']), [])
        self.assertIsNone(sim._block_step(st, 0, _NOW_MS + 20_000))
        self.assertIs(st['issues'][issue['key']]['blocked'], False)

    def test_duplicate_claim_refused(self):
        st = _state()
        issue = _file(st)
        sim.request_block_claim_met(st, issue['key'], 'nadia', now_ms=_NOW_MS)
        self.assertFalse(sim.request_block_claim_met(st, issue['key'], 'priya',
                                                     now_ms=_NOW_MS + 10))


class StuckOnPlayerPath(unittest.TestCase):
    def test_ask_player_delivers_inbox_and_sm_blocks(self):
        st = _state()
        issue = _file(st)
        sim.request_player_input(st, issue['key'], 'nadia', 'Which format?',
                                 now_ms=_NOW_MS)
        mid = sim.request_player_input_verdict(st, issue['key'],
                                               decider=lambda *a: 'ask_player')
        self.assertIsNotNone(mid)
        self.assertEqual(_pending_kinds(st, issue['key']), ['stuck_player'])
        # Agent is parked waiting on the player.
        self.assertIs(st['agents']['nadia'].get('_awaitingPlayer'), True)
        sim._block_step(st, 0, _NOW_MS + 10_000)
        self.assertIs(st['issues'][issue['key']]['blocked'], True)
        self.assertEqual(st['issues'][issue['key']]['blockedKind'], 'stuck_player')

    def test_resolve_internally_no_inbox_and_sm_unblocks(self):
        st = _state()
        issue = _file(st)
        sim.request_player_input(st, issue['key'], 'nadia', 'Which format?',
                                 now_ms=_NOW_MS)
        res = sim.request_player_input_verdict(st, issue['key'],
                                               decider=lambda *a: 'resolve_internally')
        self.assertEqual(res, 'internal')
        self.assertEqual(_pending_kinds(st, issue['key']), ['unblock_resolved'])
        self.assertFalse(st['agents']['nadia'].get('_awaitingPlayer'))
        sim._block_step(st, 0, _NOW_MS + 10_000)
        self.assertIs(st['issues'][issue['key']]['blocked'], False)

    def test_player_response_unblocks(self):
        st = _state()
        issue = _file(st)
        sim.request_player_input(st, issue['key'], 'nadia', 'Which format?',
                                 now_ms=_NOW_MS)
        mid = sim.request_player_input_verdict(st, issue['key'],
                                               decider=lambda *a: 'ask_player')
        sim._block_step(st, 0, _NOW_MS + 10_000)  # SM blocks
        self.assertIs(st['issues'][issue['key']]['blocked'], True)
        m = sim.resolve_player_ask(st, mid, 'Use JSON please')
        self.assertIsNotNone(m)
        self.assertEqual(m['status'], 'answered')
        self.assertEqual(_pending_kinds(st, issue['key']), ['unblock_response'])
        sim._block_step(st, 0, _NOW_MS + 15_000)
        self.assertIs(st['issues'][issue['key']]['blocked'], False)


class DependencyPath(unittest.TestCase):
    def test_dependency_blocks_then_auto_clears_on_landing(self):
        st = _state()
        issue = _file(st)
        rid = sim.request_block_dependency(st, issue['key'], 'nadia', 'task-5',
                                           'waiting on the pipeline rewrite')
        self.assertIsNotNone(rid)
        # No gate on this path: straight to the SM.
        self.assertEqual(_pending_kinds(st, issue['key']), ['stuck_on_agent'])
        sim._block_step(st, 0, _NOW_MS + 10_000)
        rec = st['issues'][issue['key']]
        self.assertIs(rec['blocked'], True)
        self.assertEqual(rec['blockedKind'], 'stuck_on_agent')
        self.assertEqual(rec['dependsOnTask'], 'task-5')
        # task-5 lands -> auto-unblock filed -> SM commits False.
        cleared = sim._auto_clear_dependency_blocks(st, 'task-5',
                                                    now_ms=_NOW_MS + 20_000)
        self.assertEqual(cleared, 1)
        self.assertEqual(_pending_kinds(st, issue['key']), ['unblock_landed'])
        sim._block_step(st, 0, _NOW_MS + 25_000)
        self.assertIs(st['issues'][issue['key']]['blocked'], False)
        self.assertIsNone(st['issues'][issue['key']].get('dependsOnTask'))

    def test_dependency_block_defers_without_sm(self):
        st = _state()
        st['teams'][0]['scrumMasterId'] = None
        issue = _file(st)
        sim.request_block_dependency(st, issue['key'], 'nadia', 'task-9')
        # No SM designated -> still pending, not dropped, not committed.
        self.assertIsNone(sim._block_step(st, 0, _NOW_MS + 10_000))
        self.assertEqual(len(sim._pending_block_changes(st, issue['key'])), 1)


class ScrumMasterCommitGate(unittest.TestCase):
    def test_one_commit_per_pass_and_cadence(self):
        st = _state()
        a = _file(st)
        b = _file(st, summary='A second card')
        # Two different cards's dependencies; both queued.
        sim.request_block_dependency(st, a['key'], 'nadia', 'task-1')
        sim.request_block_dependency(st, b['key'], 'nadia', 'task-2')
        self.assertEqual(len(sim._pending_block_changes(st, a['key'])), 1)
        self.assertEqual(len(sim._pending_block_changes(st, b['key'])), 1)
        # One commit for the first eligible card, then the second is deferred by
        # the per-team cadence (same team, so only the first goes through).
        c1 = sim._block_step(st, 0, _NOW_MS + 10_000)
        self.assertIsNotNone(c1)
        c2 = sim._block_step(st, 0, _NOW_MS + 10_000 + sim.BLOCK_CHANGE_CADENCE_MS - 1)
        self.assertIsNone(c2)  # cadence not elapsed for the same team yet
        c3 = sim._block_step(st, 0, _NOW_MS + 10_000 + sim.BLOCK_CHANGE_CADENCE_MS + 1)
        self.assertIsNotNone(c3)

    def test_busy_sm_defers_commit(self):
        st = _state()
        st['agents']['sm']['busy'] = True
        issue = _file(st)
        sim.request_block_dependency(st, issue['key'], 'nadia', 'task-3')
        self.assertIsNone(sim._block_step(st, 0, _NOW_MS + 10_000))
        self.assertIs(st['issues'][issue['key']]['blocked'], False)


def _pending_kinds(state, issue_key):
    return [c['kind'] for c in sim._pending_block_changes(state, issue_key)]


if __name__ == '__main__':
    unittest.main()