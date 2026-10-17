"""Theo's granted-key lane (world/theo_policy.py): the
owner's KeyRegistry grants, AgentKey records, save/load
round trips, and evaluate_key -- the decision path an
ask takes when it arrives with a granted key instead of
a roster identity.

Pure logic over a roster dict -- no serve import, no DB.
The clock is injectable so expiry and the per-key rate
limit are testable.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theo_policy  # noqa: E402


def _state():
    return {
        'agentRoster': [
            {'id': 'theo', 'isAdmin': True},
            {'id': 'nora', 'isDirector': True},
            {'id': 'ben', 'director': 'nora'},
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


class GrantKey(unittest.TestCase):
    def setUp(self):
        self.registry = theo_policy.KeyRegistry(clock=_Clock())

    def test_owner_grant_returns_raw_and_record(self):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        self.assertIsInstance(raw, str)
        self.assertGreater(len(raw), 16)
        self.assertEqual(key.agent_id, 'cora')
        self.assertEqual(key.role, theo_policy.ROLE_WORKER)
        self.assertTrue(key.active)
        self.assertIsNone(key.expires_at)
        self.assertIsNone(key.budget)

    def test_grant_records_only_the_hash(self):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        record = self.registry.list_keys()[0]
        self.assertNotIn(raw, str(record))
        self.assertNotIn('key_hash', record)
        self.assertNotIn('keyHash', record)

    def test_non_owner_grant_refused(self):
        for role in (theo_policy.ROLE_DIRECTOR,
                     theo_policy.ROLE_WORKER):
            self.assertIsNone(
                self.registry.grant_key(role, 'cora',
                                        theo_policy.ROLE_WORKER))
        self.assertEqual(self.registry.list_keys(), [])

    def test_unknown_role_refused(self):
        self.assertIsNone(
            self.registry.grant_key(
                theo_policy.ROLE_OWNER, 'cora', 'superuser'))
        self.assertEqual(self.registry.list_keys(), [])

    def test_grant_carries_limits_and_ttl(self):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER,
            rate_limit=3, budget=1.5, ttl=60)
        self.assertEqual(key.rate_limit, 3)
        self.assertEqual(key.budget, 1.5)
        self.assertEqual(key.expires_at, 1060.0)


class VerifyKey(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.registry = theo_policy.KeyRegistry(clock=self.clock)

    def test_verify_round_trips(self):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        resolved = self.registry.verify(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.key_id, key.key_id)
        self.assertEqual(resolved.agent_id, 'cora')

    def test_wrong_key_is_none(self):
        raw, _ = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        self.assertIsNone(self.registry.verify(raw + 'x'))
        self.assertIsNone(self.registry.verify(''))
        self.assertIsNone(self.registry.verify(None))

    def test_revoked_key_is_none(self):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        self.assertTrue(self.registry.revoke(
            theo_policy.ROLE_OWNER, key.key_id))
        self.assertIsNone(self.registry.verify(raw))

    def test_expired_key_is_none(self):
        raw, _ = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER, ttl=60)
        self.clock.advance(61)
        self.assertIsNone(self.registry.verify(raw))

    def test_revoke_is_owner_only(self):
        _, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER)
        self.assertFalse(self.registry.revoke(
            theo_policy.ROLE_WORKER, key.key_id))
        self.assertFalse(self.registry.revoke(
            theo_policy.ROLE_OWNER, 'key-nope'))


class PersistKeys(unittest.TestCase):
    def test_save_load_round_trip(self):
        clock = _Clock()
        registry = theo_policy.KeyRegistry(clock=clock)
        raw, key = registry.grant_key(
            theo_policy.ROLE_OWNER, 'cora',
            theo_policy.ROLE_WORKER,
            rate_limit=5, budget=2.0, ttl=90)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'keys.json')
            registry.save(path)
            loaded = theo_policy.KeyRegistry.load(
                path, clock=clock)
        resolved = loaded.verify(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.key_id, key.key_id)
        self.assertEqual(resolved.rate_limit, 5)
        self.assertEqual(resolved.budget, 2.0)
        self.assertEqual(resolved.expires_at,
                         key.expires_at)

    def test_load_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                theo_policy.KeyRegistry.load(
                    os.path.join(tmp, 'absent.json'))


class EvaluateKey(unittest.TestCase):
    def setUp(self):
        self.state = _state()
        self.clock = _Clock()
        self.registry = theo_policy.KeyRegistry(clock=self.clock)

    def _grant(self, agent_id='cora',
               role=theo_policy.ROLE_WORKER, **kwargs):
        raw, key = self.registry.grant_key(
            theo_policy.ROLE_OWNER, agent_id, role, **kwargs)
        return raw, key

    def test_no_registry_is_configured_denial(self):
        policy = theo_policy.TheoPolicy()
        decision = policy.evaluate_key(
            theo_policy.ActionRequest('converse'), 'any')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason,
                         'key registry not configured')

    def test_invalid_key_denial(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant()
        decision = policy.evaluate_key(
            theo_policy.ActionRequest('converse'),
            raw + 'tampered')
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason, 'invalid, revoked, or expired key')

    def test_worker_key_may_send_message(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='cora',
                             role=theo_policy.ROLE_WORKER)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest(
                'send_message', {'recipient': 'nora',
                                 'text': 'hi'}), raw)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.agent_id, 'cora')
        self.assertEqual(decision.role,
                         theo_policy.ROLE_WORKER)

    def test_worker_key_denied_director_action(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='cora',
                             role=theo_policy.ROLE_WORKER)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest('create_story'), raw)
        self.assertFalse(decision.allowed)
        self.assertIn('role worker may not request '
                      'create_story', decision.reason)
        self.assertEqual(decision.agent_id, 'cora')

    def test_director_key_may_director_action(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='nora',
                             role=theo_policy.ROLE_DIRECTOR)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest('create_story'), raw)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.role,
                         theo_policy.ROLE_DIRECTOR)

    def test_key_rate_limit_is_per_key(self):
        policy = theo_policy.TheoPolicy(
            rate_limit=100, registry=self.registry,
            clock=self.clock)
        raw, _ = self._grant(agent_id='cora',
                             role=theo_policy.ROLE_WORKER,
                             rate_limit=1)
        first = policy.evaluate_key(
            theo_policy.ActionRequest('converse'), raw)
        second = policy.evaluate_key(
            theo_policy.ActionRequest('converse'), raw)
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason, 'rate limit exceeded')
        # The roster lane is untouched: cora's own
        # allowance is the policy-wide default.
        roster = policy.evaluate(self.state, 'cora',
                                 'converse')
        self.assertTrue(roster.allowed)

    def test_gated_action_needs_approval_through_key(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='cora',
                             role=theo_policy.ROLE_WORKER)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest(
                'spend', {'amountUsd': 5.0}), raw)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertEqual(decision.reason,
                         'owner approval required')

    def test_owner_key_bypasses_gate(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='theo',
                             role=theo_policy.ROLE_OWNER)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest(
                'spend', {'amountUsd': 5.0}), raw)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)

    def test_media_content_rule_applies_to_keys(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='nora',
                             role=theo_policy.ROLE_DIRECTOR)
        decision = policy.evaluate_key(
            theo_policy.ActionRequest(
                'process_image', {'content_class':
                                  'instruction'}), raw)
        self.assertFalse(decision.allowed)
        self.assertIn('classified as data', decision.reason)

    def test_unknown_action_through_key(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant()
        decision = policy.evaluate_key(
            theo_policy.ActionRequest('fly_to_moon'), raw)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, 'unknown action')

    def test_key_decisions_are_audited_with_the_agent(self):
        policy = theo_policy.TheoPolicy(registry=self.registry)
        raw, _ = self._grant(agent_id='cora')
        policy.evaluate_key(
            theo_policy.ActionRequest('converse'), raw)
        entry = policy.audit.entries()[0]
        self.assertEqual(entry.requester_id, 'cora')
        self.assertEqual(entry.role, theo_policy.ROLE_WORKER)
        self.assertTrue(entry.allowed)


if __name__ == '__main__':
    unittest.main()
