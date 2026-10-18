"""Theo's role policy (world/theo_policy.py): role resolution from
the roster, per-role capability gating, the owner-approval gate for
spend/external/repo/agent/container actions, the media content-class
rule, per-requester rate limiting and budgets, and the audit trail.

Pure logic over a roster dict -- no serve import, no DB, so every
test is hermetic. The clock is injectable for the rate-limit window.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theo_policy  # noqa: E402


def _state():
    # theo is the admin (Theo himself); nora is a director by flag;
    # dax becomes a director only because zed names dax as director;
    # ben/zed/cora are workers; ghost is unknown (least authority).
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'dax'},
            {'id': 'ben', 'director': 'nora'},
            {'id': 'zed', 'director': 'dax'},
            {'id': 'cora'},
        ],
    }


class _Clock(object):
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RoleResolution(unittest.TestCase):
    def test_player_session_is_owner(self):
        self.assertEqual(theo_policy.role_for_requester(_state(), None),
                         theo_policy.ROLE_OWNER)

    def test_admin_agent_is_director(self):
        self.assertEqual(theo_policy.role_for_requester(_state(), 'theo'),
                         theo_policy.ROLE_DIRECTOR)

    def test_director_flag_is_director(self):
        self.assertEqual(theo_policy.role_for_requester(_state(), 'nora'),
                         theo_policy.ROLE_DIRECTOR)

    def test_agent_with_reports_is_director(self):
        # dax has no isDirector flag, but zed names dax as director.
        self.assertEqual(theo_policy.role_for_requester(_state(), 'dax'),
                         theo_policy.ROLE_DIRECTOR)

    def test_plain_agent_is_worker(self):
        for aid in ('ben', 'zed', 'cora'):
            self.assertEqual(theo_policy.role_for_requester(_state(), aid),
                             theo_policy.ROLE_WORKER)

    def test_unknown_id_is_worker(self):
        self.assertEqual(theo_policy.role_for_requester(_state(), 'ghost'),
                         theo_policy.ROLE_WORKER)

    def test_non_dict_roster_entry_is_skipped(self):
        # A malformed roster entry must be skipped, not
        # crash role resolution: the real entry still
        # resolves after it.
        state = {'agentRoster': [
            'not-a-dict',
            {'id': 'theo', 'isAdmin': True},
        ]}
        self.assertEqual(
            theo_policy.role_for_requester(state, 'theo'),
            theo_policy.ROLE_DIRECTOR)


class CapabilityGating(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()

    def test_owner_may_do_everything(self):
        for action in ('converse', 'create_story', 'spend',
                       'change_policy', 'grant_key', 'provision_container'):
            d = self.policy.evaluate(self.state, None, action)
            self.assertTrue(d.allowed, action)
            self.assertEqual(d.role, theo_policy.ROLE_OWNER)

    def test_director_may_run_director_actions(self):
        d = self.policy.evaluate(self.state, 'nora', 'create_story')
        self.assertTrue(d.allowed)
        self.assertEqual(d.role, theo_policy.ROLE_DIRECTOR)

    def test_worker_may_run_worker_actions(self):
        d = self.policy.evaluate(self.state, 'cora', 'report_status')
        self.assertTrue(d.allowed)
        self.assertEqual(d.role, theo_policy.ROLE_WORKER)

    def test_worker_denied_director_action(self):
        d = self.policy.evaluate(self.state, 'cora', 'create_story')
        self.assertFalse(d.allowed)
        self.assertIn('role worker may not request create_story', d.reason)

    def test_worker_may_converse(self):
        d = self.policy.evaluate(self.state, 'cora', 'converse')
        self.assertTrue(d.allowed)

    def test_unknown_action_denied(self):
        d = self.policy.evaluate(self.state, 'cora', 'fly_to_moon')
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, 'unknown action')

    def test_media_requires_data_content_class(self):
        d = self.policy.evaluate(self.state, 'nora', 'process_image',
                                 {'content_class': 'instruction'})
        self.assertFalse(d.allowed)
        self.assertIn('classified as data', d.reason)
        d = self.policy.evaluate(self.state, 'nora', 'process_image',
                                 {'content_class': 'data'})
        self.assertTrue(d.allowed)


class ApprovalGates(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.policy = theo_policy.TheoPolicy()

    def test_worker_gated_action_needs_owner_approval(self):
        d = self.policy.evaluate(self.state, 'cora', 'spend',
                                 {'amountUsd': 5.0})
        self.assertFalse(d.allowed)
        self.assertTrue(d.requires_approval)
        self.assertIsNotNone(d.approval_id)
        self.assertEqual(d.reason, 'owner approval required')
        self.assertEqual(d.payload, {'amountUsd': 5.0})

    def test_director_gated_action_needs_owner_approval(self):
        d = self.policy.evaluate(self.state, 'nora', 'provision_container',
                                 {'image': 'python:3.11'})
        self.assertFalse(d.allowed)
        self.assertTrue(d.requires_approval)

    def test_owner_bypasses_gate(self):
        d = self.policy.evaluate(self.state, None, 'spend', {'amountUsd': 5.0})
        self.assertTrue(d.allowed)
        self.assertFalse(d.requires_approval)

    def test_owner_approval_then_requester_allowed(self):
        first = self.policy.evaluate(self.state, 'cora', 'spend',
                                     {'amountUsd': 5.0})
        self.assertTrue(first.requires_approval)
        self.assertTrue(self.policy.approve(self.state, None,
                                            first.approval_id))
        second = self.policy.evaluate(self.state, 'cora', 'spend',
                                      {'amountUsd': 5.0})
        self.assertTrue(second.allowed)
        self.assertEqual(second.reason, 'approved by owner')
        self.assertEqual(second.approval_id, first.approval_id)

    def test_owner_denial_keeps_gate_closed(self):
        first = self.policy.evaluate(self.state, 'cora', 'spend')
        self.assertTrue(self.policy.deny_approval(self.state, None,
                                                  first.approval_id))
        second = self.policy.evaluate(self.state, 'cora', 'spend')
        self.assertFalse(second.allowed)
        self.assertTrue(second.requires_approval)

    def test_only_owner_may_approve(self):
        first = self.policy.evaluate(self.state, 'cora', 'spend')
        self.assertFalse(self.policy.approve(self.state, 'nora',
                                             first.approval_id))
        self.assertFalse(self.policy.approve(self.state, 'cora',
                                             first.approval_id))
        # Still pending, so a real owner approval still works.
        self.assertTrue(self.policy.approve(self.state, None,
                                            first.approval_id))

    def test_approve_unknown_or_decided_id_fails(self):
        self.assertFalse(self.policy.approve(self.state, None, 'apr-nope'))
        first = self.policy.evaluate(self.state, 'cora', 'spend')
        self.assertTrue(self.policy.approve(self.state, None,
                                            first.approval_id))
        self.assertFalse(self.policy.approve(self.state, None,
                                             first.approval_id))

    def test_approval_is_per_requester_and_action(self):
        # cora's approved spend does not vouch for nora's spend.
        first = self.policy.evaluate(self.state, 'cora', 'spend')
        self.assertTrue(self.policy.approve(self.state, None,
                                            first.approval_id))
        other = self.policy.evaluate(self.state, 'nora', 'spend')
        self.assertTrue(other.requires_approval)


class AbusePrevention(unittest.TestCase):
    def test_rate_limit_denies_past_the_cap(self):
        clock = _Clock()
        policy = theo_policy.TheoPolicy(rate_limit=2, clock=clock)
        self.assertTrue(policy.evaluate(_state(), 'cora', 'converse').allowed)
        self.assertTrue(policy.evaluate(_state(), 'cora', 'converse').allowed)
        denied = policy.evaluate(_state(), 'cora', 'converse')
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, 'rate limit exceeded')

    def test_rate_limit_window_resets(self):
        clock = _Clock()
        policy = theo_policy.TheoPolicy(rate_limit=1, clock=clock)
        self.assertTrue(policy.evaluate(_state(), 'cora', 'converse').allowed)
        self.assertFalse(policy.evaluate(_state(), 'cora', 'converse').allowed)
        clock.advance(theo_policy.RATE_WINDOW_SECONDS)
        self.assertTrue(policy.evaluate(_state(), 'cora', 'converse').allowed)

    def test_rate_limit_is_per_requester(self):
        clock = _Clock()
        policy = theo_policy.TheoPolicy(rate_limit=1, clock=clock)
        self.assertTrue(policy.evaluate(_state(), 'cora', 'converse').allowed)
        self.assertFalse(policy.evaluate(_state(), 'cora', 'converse').allowed)
        self.assertTrue(policy.evaluate(_state(), 'ben', 'converse').allowed)

    def test_budget_allows_within_and_denies_over(self):
        policy = theo_policy.TheoPolicy()
        policy.usage.set_budget('cora', 10.0)
        self.assertTrue(policy.usage.charge('cora', 4.0))
        self.assertTrue(policy.usage.charge('cora', 6.0))
        self.assertFalse(policy.usage.charge('cora', 1.0))

    def test_zero_budget_blocks_all_spending(self):
        policy = theo_policy.TheoPolicy()
        policy.usage.set_budget('cora', 0.0)
        self.assertFalse(policy.usage.charge('cora', 0.01))

    def test_clearing_budget_restores_unlimited(self):
        policy = theo_policy.TheoPolicy()
        policy.usage.set_budget('cora', 10.0)
        self.assertTrue(policy.usage.charge('cora', 10.0))
        self.assertFalse(policy.usage.charge('cora', 1.0))
        policy.usage.set_budget('cora', None)
        self.assertTrue(policy.usage.charge('cora', 1000.0))

    def test_no_budget_means_unlimited(self):
        policy = theo_policy.TheoPolicy()
        for _ in range(50):
            self.assertTrue(policy.usage.charge('cora', 100.0))


class AuditTrail(unittest.TestCase):
    def test_every_decision_is_audited(self):
        policy = theo_policy.TheoPolicy()
        policy.evaluate(_state(), 'cora', 'create_story')
        policy.evaluate(_state(), None, 'spend')
        entries = policy.audit.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].requester_id, 'cora')
        self.assertEqual(entries[0].role, theo_policy.ROLE_WORKER)
        self.assertFalse(entries[0].allowed)
        self.assertEqual(entries[1].requester_id, None)
        self.assertEqual(entries[1].role, theo_policy.ROLE_OWNER)
        self.assertTrue(entries[1].allowed)

    def test_for_requester_filters(self):
        policy = theo_policy.TheoPolicy()
        policy.evaluate(_state(), 'cora', 'converse')
        policy.evaluate(_state(), 'ben', 'converse')
        self.assertEqual(len(policy.audit.for_requester('cora')), 1)
        self.assertEqual(len(policy.audit.for_requester('ben')), 1)

    def test_approval_decisions_are_audited(self):
        policy = theo_policy.TheoPolicy()
        first = policy.evaluate(_state(), 'cora', 'spend')
        policy.approve(_state(), None, first.approval_id)
        approvals = [e for e in policy.audit.entries()
                     if e.action.startswith('approve:')]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].requester_id, None)


class SandboxRule(unittest.TestCase):
    def test_request_tool_call_is_sandboxed(self):
        self.assertTrue(theo_policy.requires_sandbox('request_tool_call'))
        self.assertFalse(theo_policy.requires_sandbox('converse'))
        self.assertFalse(theo_policy.requires_sandbox('spend'))


if __name__ == '__main__':
    unittest.main()
