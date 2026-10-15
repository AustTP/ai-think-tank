"""Teamwork agreements: config validation, the team -> operator -> code-default
fallback chain, ceiling clamping at READ time, and the invariant that an
empty/missing agreement behaves exactly like today (no agreement at all).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import sim  # noqa: E402


def _state(agreement=..., extra_team=None):
    """A two-agent state: d1 directs the team, w1 reports to d1. Pass
    agreement=None to omit the key entirely, a dict to store it."""
    team = {'id': 'd1', 'directorId': 'd1', 'name': 'Alpha'}
    if agreement is not ...:
        team['agreement'] = agreement
    if extra_team:
        team.update(extra_team)
    return {
        'agentRoster': [
            {'id': 'd1', 'name': 'Nora', 'director': None},
            {'id': 'w1', 'name': 'Bo', 'director': 'd1'},
        ],
        'teams': [team],
    }


def _operator(**overrides):
    """Patch config.OPERATOR_POLICY to the defaults plus overrides."""
    base = dict(config.OPERATOR_POLICY_DEFAULT)
    base.update(overrides)
    return unittest.mock.patch.dict(config.OPERATOR_POLICY, base, clear=True)


class ValidateTeamAgreement(unittest.TestCase):
    def test_valid_keys_kept_and_coerced(self):
        out = config.validate_team_agreement(
            {'scrumMasterMinTeamSize': 6, 'taskWorkDurationS': '8.5'}
            | {'refinementCadenceMs': '604800000'})
        self.assertEqual(out['scrumMasterMinTeamSize'], 6)
        self.assertEqual(out['refinementCadenceMs'], 604800000)
        self.assertNotIn('taskWorkDurationS', out)  # not an agreeable key

    def test_unknown_and_invalid_dropped_fail_closed_per_key(self):
        out = config.validate_team_agreement({
            'bogusKey': 1,                     # unknown
            'deliverableGradeFloor': 'lots',   # wrong type
            'maxTeamMembers': 999,             # out of schema range
            'coachingMaxRounds': 2,            # valid -- survives the rest
        })
        self.assertEqual(out, {'coachingMaxRounds': 2})

    def test_non_dict_yields_empty(self):
        self.assertEqual(config.validate_team_agreement(None), {})
        self.assertEqual(config.validate_team_agreement([1]), {})
        self.assertEqual(config.validate_team_agreement('x'), {})

    def test_no_operator_clamp_at_write(self):
        # A team may STORE a value above the operator's cap (the clamp is a
        # read-time min), so validation never applies the ceiling.
        with _operator(maxTeamMembers=6):
            out = config.validate_team_agreement({'maxTeamMembers': 9})
        self.assertEqual(out['maxTeamMembers'], 9)

    def test_ceiling_keys_are_agreeable(self):
        self.assertTrue(config.TEAM_CEILING_KEYS <= config.TEAM_AGREEABLE_KEYS)


class TeamPolicyFallback(unittest.TestCase):
    def test_no_agreement_falls_through_to_operator(self):
        with _operator(scrumMasterMinTeamSize=5):
            self.assertEqual(sim.team_policy(_state(), 'd1', 'scrumMasterMinTeamSize'), 5)

    def test_agreement_wins_for_plain_keys(self):
        with _operator(scrumMasterMinTeamSize=5):
            st = _state({'scrumMasterMinTeamSize': 7})
            self.assertEqual(sim.team_policy(st, 'd1', 'scrumMasterMinTeamSize'), 7)

    def test_non_agreeable_key_ignores_agreement(self):
        with _operator(taskCycleS=6.0):
            st = _state({'taskCycleS': 99})
            self.assertEqual(sim.team_policy(st, 'd1', 'taskCycleS'), 6.0)

    def test_invalid_stored_value_falls_back_to_operator(self):
        # Direct state writes bypass validate; read-time re-coerce fails and
        # the operator value wins.
        with _operator(scrumMasterMinTeamSize=5):
            st = _state({'scrumMasterMinTeamSize': 'lots'})
            self.assertEqual(sim.team_policy(st, 'd1', 'scrumMasterMinTeamSize'), 5)

    def test_ref_by_team_id_and_by_director_id(self):
        st = _state({'scrumMasterMinTeamSize': 7})
        self.assertEqual(sim.team_policy(st, 'd1', 'scrumMasterMinTeamSize'), 7)
        self.assertEqual(sim.team_policy(st, None, 'scrumMasterMinTeamSize'),
                         config.OPERATOR_POLICY_DEFAULT['scrumMasterMinTeamSize'])

    def test_agent_team_ref_resolves_through_roster(self):
        self.assertEqual(sim.agent_team_ref(_state(), 'w1'), 'd1')
        self.assertIsNone(sim.agent_team_ref(_state(), 'ghost'))


class CeilingClamp(unittest.TestCase):
    def test_team_value_above_operator_is_clamped_at_read(self):
        with _operator(maxTeamMembers=6):
            st = _state({'maxTeamMembers': 9})
            self.assertEqual(sim.team_policy(st, 'd1', 'maxTeamMembers'), 6)

    def test_operator_edit_is_live_without_state_rewrite(self):
        st = _state({'maxTeamMembers': 9})
        with _operator(maxTeamMembers=6):
            self.assertEqual(sim.team_policy(st, 'd1', 'maxTeamMembers'), 6)
        with _operator(maxTeamMembers=10):
            self.assertEqual(sim.team_policy(st, 'd1', 'maxTeamMembers'), 9)

    def test_team_value_at_or_below_operator_wins(self):
        with _operator(maxTeamMembers=6):
            st = _state({'maxTeamMembers': 3})
            self.assertEqual(sim.team_policy(st, 'd1', 'maxTeamMembers'), 3)


class EmptyAgreementIsToday(unittest.TestCase):
    def test_empty_and_missing_agreement_resolve_identically(self):
        with _operator():
            missing = sim.team_policy(_state(), 'd1')          # no key
            empty = sim.team_policy(_state({}), 'd1')          # {}
            cleared = sim.team_policy(_state(None), 'd1')      # cleared
        self.assertEqual(missing, empty)
        self.assertEqual(empty, cleared)

    def test_full_map_covers_every_policy_key(self):
        resolved = sim.team_policy(_state({}), 'd1')
        self.assertEqual(set(resolved), set(config.OPERATOR_POLICY_DEFAULT))
        self.assertEqual(len(resolved), 82)

    def test_non_dict_stored_agreement_ignored(self):
        with _operator(scrumMasterMinTeamSize=5):
            st = _state({'scrumMasterMinTeamSize': 7})
            st['teams'][0]['agreement'] = 'corrupt'
            self.assertEqual(sim.team_policy(st, 'd1', 'scrumMasterMinTeamSize'), 5)


if __name__ == '__main__':
    unittest.main()
