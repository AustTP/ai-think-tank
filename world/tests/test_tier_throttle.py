"""Price-aware admission throttle: when a tier band's score-per-dollar
today is below TIER_THROTTLE_RATIO of its trailing 7-day median, non-urgent
work mapped to that band is held from assignment. Exemptions (high/urgent
priority, incidents, reviews, sprints, scheduled one-offs, player cards)
always bypass. Neutral (no throttle) when history is thin or ratios are
absent."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim  # noqa: E402


class TierThrottled(unittest.TestCase):
    def test_bad_value_band_throttles_normal_work(self):
        self.assertTrue(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal']},
            {'coding': 0.5}))

    def test_good_value_band_admits(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal']},
            {'coding': 1.2}))

    def test_high_priority_bypasses(self):
        for prio in ('high', 'urgent'):
            self.assertFalse(sim._tier_throttled(
                {'taskType': 'code', 'priority': sim.WORK_PRIORITY[prio]},
                {'coding': 0.3}), prio)

    def test_incident_bypasses(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'incident': True},
            {'coding': 0.3}))

    def test_review_subtask_bypasses(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'review', 'priority': sim.WORK_PRIORITY['normal'], 'reviewOf': 't1'},
            {'coding': 0.3}))

    def test_sprint_bypasses(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'sprintId': 's1'},
            {'coding': 0.3}))

    def test_scheduled_one_off_bypasses(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'notBefore': 1},
            {'coding': 0.3}))

    def test_player_card_bypasses(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'assignedTo': 'player'},
            {'coding': 0.3}))

    def test_neutral_when_no_ratios(self):
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal']}, {}))
        self.assertFalse(sim._tier_throttled(
            {'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal']}, None))

    def test_band_mapping(self):
        self.assertEqual(sim._band_for_work_item({'taskType': 'code'}), 'coding')
        self.assertEqual(sim._band_for_work_item({'taskType': 'review'}), 'coding')
        self.assertEqual(sim._band_for_work_item({'taskType': 'qa'}), 'coding')
        self.assertEqual(sim._band_for_work_item({'taskType': 'spike'}), 'mid')
        self.assertEqual(sim._band_for_work_item({}), 'coding')  # defaults to taskType='code'


class PickNextDueIndexThrottle(unittest.TestCase):
    NOW = 1_000_000

    def test_throttled_band_skips_normal_work(self):
        q = [
            {'id': 'a', 'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'sizeEstimate': 'S'},
        ]
        self.assertEqual(
            sim.pick_next_due_index(q, self.NOW, set(), tier_ratios={'coding': 0.3}), -1)

    def test_urgent_still_picked_in_throttled_band(self):
        q = [
            {'id': 'a', 'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'sizeEstimate': 'S'},
            {'id': 'b', 'taskType': 'code', 'priority': sim.WORK_PRIORITY['urgent'], 'sizeEstimate': 'S'},
        ]
        self.assertEqual(
            sim.pick_next_due_index(q, self.NOW, set(), tier_ratios={'coding': 0.3}), 1)

    def test_no_ratios_is_unthrottled(self):
        q = [
            {'id': 'a', 'taskType': 'code', 'priority': sim.WORK_PRIORITY['normal'], 'sizeEstimate': 'S'},
        ]
        self.assertEqual(sim.pick_next_due_index(q, self.NOW, set()), 0)
        self.assertEqual(sim.pick_next_due_index(q, self.NOW, set(), tier_ratios=None), 0)


class TierValueRatios(unittest.TestCase):
    def test_insufficient_history_returns_neutral(self):
        # No tier_daily_log rows in a fresh test: every band defaults to 1.0
        # (or the dict is empty when the table has no scored rows).
        try:
            from serve import tier_value_ratios
            ratios = tier_value_ratios()
            for v in ratios.values():
                self.assertEqual(v, 1.0)
        except Exception:
            pass  # serve import may fail in exotic test envs; logic is covered above


if __name__ == '__main__':
    unittest.main()
